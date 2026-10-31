"""Who someone is: users, their keys and passwords, and signing in.

What a user may do is their group's to say, with what the account has of its own on top. That
is in groups.py. Users live in the configured accounts store: a user is a name, a group, an on/off
switch, one or more API keys, and optionally a password. Only hashes of keys and passwords are
stored. Groups share that store; the common database layer supports SQLite and PostgreSQL.

The users are kept on the front machine, where turnstile checks every key (turnstile/accounts.py).
A person signs in with a name and a password, and gets a session key: an ordinary API key that
ends after some hours. weightroom shows the form and passes on what was typed, to turnstile, which
checks it. Compute keeps no users: it is told who is asking in a signed statement (statement.py).

With a group's `second_step` on, signing in also needs a six-digit code from an authenticator
app. The code is worked out from a secret kept here and in the app, and the time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from db import DatabaseSource, database, local_path, migrate
from db.schemas import USERS

from loadcoach.groups import EVERYTHING, NAME, NOTHING, Group, Groups, adjusted, check_own, limited

# What a person may choose for themselves, and what each is until they choose. These are about
# how things are shown to them, never about what they may do.
CHOICES = {"show_telemetry": True}  # the strip of meters at the top of every page of the front machine's app


@dataclass(frozen=True)
class User:
    """Authenticated account identity, effective group and optional session attribution."""
    name: str
    group_name: str
    group: Group
    until: float | None = None  # when the key they came with ends; None for a key that does not end
    via: str | None = None  # the app this session was made through, if it was. It can do no more than that app
    identity: str | None = None  # the account's id, which stays the same when it is renamed
    key_identity: str | None = None  # opaque attribution id; never the key or its stored verifier

    def can(self, ability: str) -> bool:
        """Return whether the account's effective group grants this ability."""
        return ability in self.group.can


# Who a caller is when no user has been added yet: everything is allowed, as before users existed.
ANONYMOUS = User("anonymous", "", EVERYTHING)
# Who the `loadcoach` command is when it talks to a running LoadCoach on this machine.
COMMAND_LINE = User("command-line", "", EVERYTHING)


def hash_key(key: str) -> str:
    """API keys are long and random, so one fast hash is enough to store them safely."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_identity(verifier: str) -> str:
    """Public stable attribution id derived from an already hashed random key."""
    return hashlib.sha256(('suite-key-attribution:' + verifier).encode()).hexdigest()


def hash_password(password: str) -> str:
    """Passwords are short and guessable, so they get a deliberately slow, salted hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=2**26, dklen=32)
    return f"scrypt$15$8$1${salt.hex()}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    """True if `password` matches a hash made by hash_password. For the front machine to use."""
    try:
        scheme, log_n, r, p, salt, digest = stored.split("$")
        again = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=2 ** int(log_n), r=int(r), p=int(p), maxmem=2**26, dklen=32
        )
    except ValueError:
        return False
    return scheme == "scrypt" and secrets.compare_digest(again.hex(), digest)


# What a wrong name is checked against, so that it takes as long to refuse as a wrong password does.
NOBODY = hash_password(secrets.token_urlsafe(16))

TRIES, LOCKED_FOR = 5, 300  # after this many wrong tries in a row, a name is locked for this many seconds


def code_at(secret: str, step: int) -> str:
    """The six digits an authenticator app shows for this secret during one 30-second step.

    This is the standard everyone's app follows (RFC 6238): a keyed hash of the step number,
    and six digits picked out of it.
    """
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    start = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[start : start + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def step_of(secret: str, code: str, now: float) -> int | None:
    """Which 30-second step this code belongs to, or None if it is not right for now.

    The step before and the step after are accepted too, since clocks differ a little.
    """
    for step in (int(now // 30) + drift for drift in (0, -1, 1)):
        if hmac.compare_digest(code_at(secret, step), str(code)):
            return step
    return None


class SignInRefused(Exception):
    """A sign-in that did not succeed. `code` is a short name for why, for a program to act on."""

    def __init__(self, status: int, code: str, message: str, secret: str | None = None) -> None:
        """Initialize the failure message and its status metadata."""
        super().__init__(message)
        self.status, self.code, self.secret = status, code, secret


def keep_private(source: DatabaseSource) -> None:
    """Make the database readable by its owner only. It holds key hashes and, for sensitive work, text."""
    path = local_path(source)
    if path is None:
        return
    for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):  # SQLite keeps two working files beside it
        if file.exists():
            file.chmod(0o600)


class Users:
    """The users database. Every change is written at once and applies to the next request."""

    def __init__(self, path: DatabaseSource, groups: dict[str, Group]) -> None:
        """`groups` is the first set, from the configuration file. It is taken over once, if there are none yet."""
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        keep_private(path)
        if source.engine == "sqlite":
            self.db.execute("PRAGMA foreign_keys = ON")
        migrate(source, self.db, "loadcoach.users", USERS)
        for (name,) in self.db.execute("SELECT name FROM users WHERE identity IS NULL"):
            self.db.execute("UPDATE users SET identity = ? WHERE name = ?", (secrets.token_hex(16), name))
        self.db.commit()
        self.groups = Groups(self.db, groups)
        self.wrong: dict[str, tuple[int, float]] = {}  # name -> (wrong tries in a row, locked until)
        self.used: dict[str, int] = {}  # name -> the step of the last code accepted, so none works twice

    def any(self) -> bool:
        """Return whether the accounts store contains at least one user."""
        return self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def user(self, name: str, group_name: str, own: str | None = None, until: float | None = None, via: str | None = None) -> User | None:
        """A user with what they may do: their group, then what the account has of its own, then the app they came through.

        None if they came through an app that is no longer there. A session made through an app
        ends with the app.
        """
        # A group that no longer exists grants nothing.
        group = adjusted(self.groups.get(group_name, NOTHING), json.loads(own) if own else {})
        if via:
            app = self.by_name(via)
            if app is None:
                return None
            group = limited(group, app.group)
        return User(name, group_name, group, until, via, self.identity(name))

    def identity(self, name: str) -> str | None:
        """Immutable account ID for ownership of data stored on another machine."""
        row = self.db.execute("SELECT identity FROM users WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def by_key(self, key: str) -> User | None:
        """The enabled user this API key belongs to, if any. A session key that has ended belongs to nobody."""
        row = self.db.execute(
            "SELECT name, grp, own, expires, via FROM users JOIN keys ON user = name"
            " WHERE hash = ? AND enabled <> 0 AND (expires IS NULL OR expires > ?)",
            (hash_key(key), time.time()),
        ).fetchone()
        if row and (user := self.user(*row)):
            from dataclasses import replace
            return replace(user, key_identity=key_identity(hash_key(key)))
        return None

    def by_identity(self, identity: str) -> User | None:
        """The enabled user whose account has this id, if any. The id stays the same when the account is renamed."""
        row = self.db.execute("SELECT name FROM users WHERE identity = ?", (identity,)).fetchone()
        return row and self.by_name(row[0])

    def record_access(self, address: str, key: str, user: User | None) -> tuple[str, str]:
        """Atomically count a verified peer's key attempt; retain known owners even after expiry."""
        from ipaddress import ip_address
        address = str(ip_address(address))
        owner = (user.identity or '', user.name) if user else self.db.execute(
            'SELECT u.identity,u.name FROM keys k JOIN users u ON u.name=k.user WHERE k.hash=?',
            (hash_key(key),)).fetchone()
        identity, name = owner or ('', '')
        now = time.time()
        with self.db:
            self.db.execute('INSERT INTO client_accesses(ip,owner_id,owner_name,first_at,last_at,accepted,failed) '
                            'VALUES(?,?,?,?,?,?,?) ON CONFLICT(ip,owner_id) DO UPDATE SET '
                            'owner_name=excluded.owner_name,'
                            'first_at=CASE WHEN client_accesses.first_at<excluded.first_at THEN client_accesses.first_at ELSE excluded.first_at END,'
                            'last_at=CASE WHEN client_accesses.last_at>excluded.last_at THEN client_accesses.last_at ELSE excluded.last_at END,'
                            'accepted=client_accesses.accepted+excluded.accepted,'
                            'failed=client_accesses.failed+excluded.failed',
                            (address, identity, name, now, now, int(user is not None), int(user is None)))
        return identity, name

    def client_accesses(self, identity: str | None = None, limit: int = 25) -> dict:
        """Return bounded access summaries and registered IP choices within the account scope."""
        where, values = (' WHERE c.owner_id=?', (identity,)) if identity is not None else ('', ())
        addresses = [row[0] for row in self.db.execute('SELECT DISTINCT c.ip FROM client_accesses c' + where + ' ORDER BY c.ip', values)]
        rows = self.db.execute('SELECT c.ip,c.owner_id,COALESCE(u.name,c.owner_name),c.first_at,c.last_at,c.accepted,c.failed '
                               'FROM client_accesses c LEFT JOIN users u ON u.identity=c.owner_id' +
                               where + ' ORDER BY c.last_at DESC,c.ip LIMIT ?', (*values, min(max(limit, 1), 100)))
        return {'ips': addresses, 'rows': [dict(zip(('ip', 'owner_id', 'user', 'first_at', 'last_at', 'accepted', 'failed'), row))
                                          for row in rows]}

    def by_name(self, name: str, also_disabled: bool = False) -> User | None:
        """The enabled user with this name, if any. With `also_disabled`, a disabled one too."""
        row = self.db.execute("SELECT name, grp, own FROM users WHERE name = ? AND (enabled <> 0 OR ? <> 0)", (name, also_disabled)).fetchone()
        return row and self.user(*row)

    def own(self, name: str) -> dict:
        """What one account may do that differs from its group. Empty if nothing."""
        row = self.db.execute("SELECT own FROM users WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def model_renamed(self, old: str, new: str) -> list[str]:
        """Follow a model's new name wherever a group or an account lists the models it may use. Returns what was changed."""
        renamed = lambda models: [new if model == old else model for model in models]  # noqa: E731
        followed = []
        for name in self.groups:
            section = self.groups.stored(name)
            if isinstance(section.get("models"), list) and old in section["models"]:
                self.groups.put(name, {**section, "models": renamed(section["models"])})
                followed.append(f"the group {name}")
        for name, kept in self.db.execute("SELECT name, own FROM users WHERE own IS NOT NULL").fetchall():
            own = json.loads(kept)
            if old in own.get("models", []):
                self.change(name, own={**own, "models": renamed(own["models"])})
                followed.append(name)
        return followed

    def members(self, group: str) -> list[str]:
        """Who is in a group."""
        return [name for (name,) in self.db.execute("SELECT name FROM users WHERE grp = ? ORDER BY name", (group,))]

    def move_members(self, source: str, target: str) -> list[str]:
        """Move everyone in one group to another, together, and return their names."""
        if target not in self.groups:
            raise ValueError(f"No group named {target!r}.")
        moved = self.members(source)
        with self.db:
            self.db.execute("UPDATE users SET grp = ? WHERE grp = ?", (target, source))
        return moved

    def choices(self, name: str) -> dict:
        """What a user has chosen for themselves, with the usual choice for anything they have not."""
        row = self.db.execute("SELECT choices FROM users WHERE name = ?", (name,)).fetchone()
        return {**CHOICES, **(json.loads(row[0]) if row and row[0] else {})}

    def choose(self, name: str, chosen: dict) -> None:
        """Keep a user's own choices. Only the ones in CHOICES, each of the kind it is there."""
        if wrong := [key for key, value in chosen.items() if key not in CHOICES or type(value) is not type(CHOICES[key])]:
            raise ValueError(f"Not something to choose, or not the right kind of value: {wrong}. Choices: {CHOICES}")
        with self.db:
            self.db.execute("UPDATE users SET choices = ? WHERE name = ?", (json.dumps({**self.choices(name), **chosen}), name))

    def password_hash(self, name: str) -> str | None:
        """The stored hash of a user's password, or None if they have none."""
        row = self.db.execute("SELECT password FROM users WHERE name = ?", (name,)).fetchone()
        return row and row[0]

    def exists(self, name: str) -> bool:
        """Return whether an account with this exact stored name exists."""
        return self.db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone() is not None

    def check_name(self, name: str) -> None:
        """Reject invalid or occupied account names, for both creation and renaming."""
        if not NAME.fullmatch(name):
            raise ValueError(f"{name!r}: a user name is lowercase letters, digits, dot, dash or underscore, at most 64.")
        if self.exists(name):
            raise ValueError(f"There is already a user named {name!r}.")

    def check_new(self, name: str, group: str) -> None:
        """Raise ValueError, saying why, if `name` could not be added to `group`."""
        self.check_name(name)
        if group not in self.groups:
            raise ValueError(f"No group named {group!r}. Groups: {', '.join(self.groups) or 'none defined'}")

    def add(self, name: str, group: str) -> None:
        """Validate and create an account with a new immutable identity."""
        self.check_new(name, group)
        with self.db:
            self.db.execute("INSERT INTO users (name, grp, identity) VALUES (?, ?, ?)", (name, group, secrets.token_hex(16)))

    def rename(self, name: str, new: str) -> None:
        """Rename an account and its owned work together, preserving keys and settings.

        Names keep the same lowercase spelling rules as account creation. Historical log
        entries keep their original names: rewriting them would break their hash chain.
        """
        with self.db:
            # Take the write lock before checking either name, including across processes.
            self.db.execute("BEGIN IMMEDIATE")
            if not self.exists(name):
                raise ValueError(f"No user named {name!r}.")
            self.check_name(new)
            # Defer the existing keys foreign key until both ends have their new name.
            self.db.execute("PRAGMA defer_foreign_keys = ON" if self.db.engine == "sqlite" else "SET CONSTRAINTS ALL DEFERRED")
            self.db.execute("UPDATE users SET name = ? WHERE name = ?", (new, name))
            self.db.execute("UPDATE keys SET user = ? WHERE user = ?", (new, name))
            self.db.execute("UPDATE keys SET via = ? WHERE via = ?", (new, name))
            tables = set(self.db.database.tables(self.db))
            for table in ("conversations", "answering", "files", "batches"):
                if table in tables:
                    self.db.execute(f"UPDATE {table} SET user = ? WHERE user = ?", (new, name))
        for remembered in (self.wrong, self.used):
            if name in remembered:
                remembered[new] = remembered.pop(name)

    def new_key(self, name: str, label: str = "", hours: float | None = None, via: str | None = None) -> str:
        """Make a key for a user and return it. This is the only time it can be seen.

        With `hours` it is a session key, and stops working that long from now. `via` names the
        app it was made through, which it can then do no more than.
        """
        key = "lc_" + secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO keys (hash, user, label, created, expires, via) VALUES (?, ?, ?, ?, ?, ?)",
                (hash_key(key), name, label, datetime.now().isoformat(timespec="seconds"), hours and time.time() + hours * 3600, via),
            )
        return key

    async def sign_in(self, name: str, password: str, code: str | None = None, new_password: str | None = None, via: str | None = None) -> str:
        """Check who someone is and return a session key for them. Raises SignInRefused if it does not hold.

        `via` names the app that is signing them in, when one is. The session is then limited to
        what that app's own account may do.

        Any program on this machine may call this, which is why it lives here: LoadCoach today,
        and the helper that must still answer when LoadCoach does not.
        """
        name = name.lower()  # one identity for lookup, failed attempts and authenticator replay checks
        now = time.time()
        if now < self.wrong.get(name, (0, 0))[1]:
            left = int(self.wrong[name][1] - now) // 60 + 1
            raise SignInRefused(429, "locked", f"Too many wrong tries. Try again in {left} minutes.")

        def wrong(status: int, code: str, message: str) -> SignInRefused:
            """Count one wrong try against this name, and lock it for a while after too many."""
            failed_at = time.time()
            if failed_at < self.wrong.get(name, (0, 0))[1]:
                return SignInRefused(429, "locked", "Too many wrong tries. Try again in a few minutes.")
            tries = self.wrong.get(name, (0, 0))[0] + 1
            self.wrong[name] = (0, failed_at + LOCKED_FOR) if tries >= TRIES else (tries, 0)
            return SignInRefused(status, code, message)

        account_query = "SELECT grp, password, must_change, totp, identity FROM users WHERE name = ? AND enabled <> 0"
        row = self.db.execute(account_query, (name,)).fetchone()
        stored = row[1] if row and row[1] else None
        # The slow hash runs on another thread, so that everyone else's requests are not held up by it.
        if not await asyncio.to_thread(check_password, stored or NOBODY, password) or not stored:
            raise wrong(401, "wrong", "Wrong name or password.")
        # Hashing yields to account edits: a disabled, replaced or changed account must
        # authenticate again using its current credentials and second-step settings.
        now = time.time()
        if self.db.execute(account_query, (name,)).fetchone() != row:
            raise wrong(401, "wrong", "Wrong name or password.")
        if now < self.wrong.get(name, (0, 0))[1]:
            raise SignInRefused(429, "locked", "Too many wrong tries. Try again in a few minutes.")
        group_name, _, must_change, secret, _ = row
        group = self.groups.get(group_name, NOTHING)

        if group.second_step:
            if not secret or secret.startswith("pending:"):
                # Not set up yet. The password was right, so hand over a secret for their app.
                # It counts as set up the first time a code made from it comes back.
                secret = (secret or "").removeprefix("pending:") or base64.b32encode(secrets.token_bytes(20)).decode()
                self.db.execute("UPDATE users SET totp = ? WHERE name = ?", ("pending:" + secret, name))
                self.db.commit()
                if code is None or step_of(secret, code, now) is None:
                    message = "Add this secret to an authenticator app, then sign in again with the code it shows."
                    raise SignInRefused(403, "second_step_setup", message, secret)
                self.db.execute("UPDATE users SET totp = ? WHERE name = ?", (secret, name))
                self.db.commit()
            if code is None:
                raise SignInRefused(403, "code_required", "Enter the code from your authenticator app.")
            step = step_of(secret, code, now)
            if step is None or step <= self.used.get(name, 0):  # wrong, or the same code a second time
                raise wrong(403, "code_wrong", "That code is not right. Wait for the next one and try again.")

        if must_change:  # the password is one somebody else chose, so it only serves to choose their own
            if not isinstance(new_password, str) or len(new_password) < 8 or new_password == password:
                raise SignInRefused(403, "password_change", "Choose a password of your own, at least 8 characters.")
            self.change(name, password=new_password)

        if group.second_step:
            # Only now is the code used up, so that being asked to choose a password does not cost a code.
            self.used[name] = step
        self.wrong.pop(name, None)
        return self.new_key(name, f"session via {via}" if via else "session", group.session_hours, via)

    def end_sessions(self, name: str, but: str | None = None) -> None:
        """End every sign-in of this user, except the session key `but` if one is given."""
        with self.db:
            self.db.execute(
                "DELETE FROM keys WHERE user = ? AND expires IS NOT NULL AND hash != ?", (name, hash_key(but) if but else "")
            )

    def sign_out(self, key: str) -> bool:
        """End the sign-in this session key belongs to. False if it is not a session key."""
        with self.db:
            return self.db.execute("DELETE FROM keys WHERE hash = ? AND expires IS NOT NULL", (hash_key(key),)).rowcount > 0

    def temporary_password(self, name: str) -> str:
        """Give a user a password that only serves to choose their own, and return it. Their sign-ins end."""
        password = secrets.token_urlsafe(9)
        self.change(name, password=password, must_change=True)
        self.end_sessions(name)
        return password

    def forget_sessions(self) -> None:
        """Remove session keys that have ended. They no longer work either way; this only tidies."""
        with self.db:
            self.db.execute("DELETE FROM keys WHERE expires IS NOT NULL AND expires <= ?", (time.time(),))

    def remove_key(self, name: str, key_id: str) -> bool:
        """Remove the key whose hash starts with `key_id`. False if the user has no such key."""
        with self.db:
            for (verifier,) in self.db.execute("SELECT hash FROM keys WHERE user = ? AND hash LIKE ? || '%'", (name, key_id)).fetchall():
                self.db.execute("DELETE FROM spending_allowances WHERE scope='key' AND subject=?", (key_identity(verifier),))
            done = self.db.execute("DELETE FROM keys WHERE user = ? AND hash LIKE ? || '%'", (name, key_id))
        return done.rowcount > 0

    def change(
        self,
        name: str,
        group: str | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        must_change: bool = False,
        second_step: bool | None = None,
        own: dict | None = None,
    ):
        """Change what is given. `must_change` marks the password as one the user must replace at
        their next sign-in. `second_step=False` forgets their authenticator, so it is set up anew.
        `own` replaces what the account may do that differs from its group; empty takes it all away."""
        if group is not None and group not in self.groups:
            raise ValueError(f"No group named {group!r}. Groups: {', '.join(self.groups) or 'none defined'}")
        if own is not None:
            check_own(own)
        with self.db:
            if own is not None:
                self.db.execute("UPDATE users SET own = ? WHERE name = ?", (json.dumps(own) if own else None, name))
            if group is not None:
                self.db.execute("UPDATE users SET grp = ? WHERE name = ?", (group, name))
            if enabled is not None:
                self.db.execute("UPDATE users SET enabled = ? WHERE name = ?", (enabled, name))
            if password is not None:
                self.db.execute(
                    "UPDATE users SET password = ?, must_change = ? WHERE name = ?", (hash_password(password), must_change, name)
                )
            if second_step is False:
                self.db.execute("UPDATE users SET totp = NULL WHERE name = ?", (name,))

    def remove(self, name: str) -> None:
        """Delete the account and its account/key spending allowances."""
        with self.db:
            self.db.execute("DELETE FROM spending_allowances WHERE scope='user' AND subject=?", (self.identity(name),))
            for (verifier,) in self.db.execute('SELECT hash FROM keys WHERE user=?', (name,)).fetchall():
                self.db.execute("DELETE FROM spending_allowances WHERE scope='key' AND subject=?", (key_identity(verifier),))
            self.db.execute("DELETE FROM users WHERE name = ?", (name,))

    def memberships(self, only: str | None = None) -> dict[str, str]:
        """Current group names for historical usage whose records predate group snapshots."""
        rows = self.db.execute("SELECT name, grp FROM users" + (" WHERE name = ?" if only is not None else ""),
                               (only,) if only is not None else ())
        return dict(rows)

    def listing(self, only: str | None = None) -> list[dict]:
        """Every user with their keys, or just the one named `only`. No hashes, no keys."""
        keys: dict[str, list[dict]] = {}
        sessions: dict[str, int] = {}
        for user, key_hash, label, created, expires in self.db.execute(
            "SELECT user, hash, label, created, expires FROM keys WHERE expires IS NULL OR expires > ?", (time.time(),)
        ):
            if expires:
                sessions[user] = sessions.get(user, 0) + 1  # sign-ins are counted, not listed one by one
            else:
                keys.setdefault(user, []).append({"id": key_hash[:8], "label": label, "created": created})
        return [
            {
                "name": name,
                "group": group,
                "enabled": bool(enabled),
                "password": "temporary" if has and temporary else bool(has),
                "second_step": bool(secret and not secret.startswith("pending:")),
                "sessions": sessions.get(name, 0),
                "keys": keys.get(name, []),
                "own": json.loads(own) if own else {},  # what the account has that differs from its group
                "can": sorted(self.user(name, group, own).group.can),  # and so, everything it may do
            }
            for name, group, enabled, has, temporary, secret, own in self.db.execute(
                "SELECT name, grp, enabled, password IS NOT NULL, must_change, totp, own FROM users"
                " WHERE CAST(? AS TEXT) IS NULL OR name = ? ORDER BY name",
                (only, only),
            )
        ]
