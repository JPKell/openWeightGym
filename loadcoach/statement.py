"""Signed statements: how compute knows who is asking without holding any accounts.

Accounts and keys are kept on the control node. Turnstile checks each request's key there,
then sends the request on to compute with a statement in one header: who the caller is, what
they may do, when the statement stops counting, a random nonce, and a hash of the method, the
address and the body. The statement is signed with a secret that control node and that compute machine
share, the machine key. The key itself never crosses the network.

Compute checks the signature, the time and the hash, and refuses a nonce it has seen before, so
a request copied off the network cannot be sent again or changed. Its answer carries a proof:
the nonce signed with the same secret. Turnstile refuses an answer without it, which says the
answer came from the machine that holds that secret. Neither protects what is said from being
read on the way: only TLS would.

The same statement, with `machine` in place of a caller, is how compute asks control node for something.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from loadcoach.groups import Group
from loadcoach.users import User

HEADER = "x-suite-statement"
PROOF = "x-suite-proof"
LASTS = 60  # seconds a statement counts for after it is made
DRIFT = 5  # seconds the two machines' clocks may differ by
OVERLAP = 600  # seconds the key before a replaced one still counts, so that what was on its way is not refused


def read_key(path: str) -> str:
    """The machine key, read afresh each time, so that a replaced key counts at once."""
    return Path(path).read_text().strip()


def keys(path: str) -> list[str]:
    """The keys that count: the machine key, and for OVERLAP seconds after it was replaced, the one before."""
    before = Path(path + ".previous")
    if before.exists() and time.time() - before.stat().st_mtime < OVERLAP:
        return [read_key(path), read_key(str(before))]
    return [read_key(path)]


def replace_key(path: str, new: str) -> None:
    """Make `new` the machine key, keeping the one it replaces as PATH.previous. Each file is readable by its owner only."""
    if len(new) < 32 or not new.isprintable() or " " in new:
        raise ValueError("A machine key is at least 32 characters, with no spaces: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    for name, text in ((path + ".previous", read_key(path) if Path(path).exists() else new), (path, new)):
        # Create owner-only bytes from the outset; never follow a predictable .new link.
        with tempfile.NamedTemporaryFile(mode="w", dir=Path(name).parent, prefix=Path(name).name + ".", delete=False) as file:
            written = Path(file.name)
            try:
                file.write(text + "\n")
                file.flush()
                os.fsync(file.fileno())
                os.replace(written, name)
            finally:
                written.unlink(missing_ok=True)


def seal(key: str, salt: str, new: str) -> str:
    """A new key hidden with the old one, so that it can be sent over the network: only a holder of `key` can open it.

    It is mixed with a pad made from the old key and a random `salt`, used once. 64 bytes of pad
    cover any key `replace_key` takes from secrets.token_urlsafe(32), which is 43 characters.
    """
    pad = hashlib.sha256(f"{key}:seal:{salt}".encode()).digest() * 2
    if len(new) > len(pad):
        raise ValueError("That key is too long to seal.")
    return bytes(a ^ b for a, b in zip(new.encode().ljust(len(pad), b"\0"), pad)).hex()


def unseal(key: str, salt: str, sealed: str) -> str:
    """Recover a rotated machine key using the previous key and one-time salt."""
    pad = hashlib.sha256(f"{key}:seal:{salt}".encode()).digest() * 2
    return bytes(a ^ b for a, b in zip(bytes.fromhex(sealed), pad)).rstrip(b"\0").decode()


def sign(key: str, text: str) -> str:
    """Return the SHA-256 HMAC of a statement with the shared machine key."""
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


def address(path: str, query: str | bytes = "") -> str:
    """The address a statement is made for: the path, and the query if there is one."""
    query = query.decode() if isinstance(query, bytes) else query
    return path + ("?" + query if query else "")


def make(key: str, method: str, where: str, body: bytes, caller: dict | None = None, machine: str = "", origin: str = "", purpose: str = "") -> tuple[str, str]:
    """A statement for one request, and its nonce. `where` is the path with its query, and `origin` the caller's own address."""
    nonce = secrets.token_hex(16)
    said = {"caller": caller, "machine": machine, "origin": origin, "method": method.upper(), "where": where,
            "body": hashlib.sha256(body).hexdigest(), "until": time.time() + LASTS, "nonce": nonce}
    if purpose:
        said["purpose"] = purpose
    from loadcoach.observability import wire
    if context := wire():
        said['observation'] = context
    text = base64.urlsafe_b64encode(json.dumps(said).encode()).decode()
    return f"{text}.{sign(key, text)}", nonce


class Nonces:
    """The nonces accepted lately. One seen twice before it has run out is a copy, and refused."""

    def __init__(self) -> None:
        """Initialize the accepted nonce cache for replay detection."""
        self.seen: dict[str, float] = {}

    def first_time(self, nonce: str, until: float) -> bool:
        """Reject a replayed nonce and retain accepted nonces through their drift window."""
        now = time.time()
        if len(self.seen) > 10_000 or any(ends < now for ends in list(self.seen.values())[:50]):
            self.seen = {one: ends for one, ends in self.seen.items() if ends >= now}
        if nonce in self.seen:
            return False
        self.seen[nonce] = until + DRIFT
        return True


def check(key: str, statement: str, method: str, where: str, body: bytes, nonces: Nonces) -> dict:
    """What a statement says, if it is sound. Raises ValueError saying what is wrong with it."""
    text, _, signature = statement.partition(".")
    if not hmac.compare_digest(sign(key, text), signature):
        raise ValueError("The statement is not signed with this machine's key.")
    said = json.loads(base64.urlsafe_b64decode(text))
    if said["until"] + DRIFT < time.time():
        raise ValueError("The statement has run out. Are the two machines' clocks a minute or more apart?")
    if (said["method"], said["where"], said["body"]) != (method.upper(), where, hashlib.sha256(body).hexdigest()):
        raise ValueError("The statement was made for another request.")
    if not nonces.first_time(said["nonce"], said["until"]):
        raise ValueError("This request has been seen before.")
    return said


def proof(key: str, nonce: str) -> str:
    """What an answer carries to show it comes from the machine that holds `key`."""
    return sign(key, "proof:" + nonce)


def caller_of(user: User) -> dict:
    """A user, as a statement carries them: their name, their account's id and everything they may do."""
    # A set of abilities is written sorted; a list, such as the tools shown, keeps its order.
    group = {name: sorted(value) if isinstance(value, frozenset) else list(value) if isinstance(value, tuple) else value
             for name, value in asdict(user.group).items()}
    return {"name": user.name, "identity": user.identity, "group_name": user.group_name, "group": group, "until": user.until, "via": user.via,
            "key_identity": user.key_identity}


def user_of(caller: dict) -> User:
    """The user a statement names, back as LoadCoach works with them."""
    group = dict(caller["group"])
    group["can"] = frozenset(group["can"])
    for name in ("models", "tools"):
        group[name] = None if group[name] is None else tuple(group[name])
    group["hidden_tools"] = tuple(group["hidden_tools"])
    return User(caller["name"], caller["group_name"], Group(**group), caller["until"], caller["via"], caller["identity"], caller.get("key_identity"))


class Statements:
    """Checks the statement on each request to a program on compute, and signs each answer.

    A request with a sound statement goes on with what it said in `request.state.said`, and with
    the caller's own address, as turnstile saw it, in place of turnstile's. One
    with a statement that is not sound is refused here. One with none goes on as it is, and
    is the program's to refuse: the `loadcoach` command on this machine signs no statement.
    Without a key file, nothing is signed and nothing is checked.
    """

    def __init__(self, app, key_file: str = "") -> None:
        """Configure signed request verification and its process-local nonce cache."""
        self.app, self.key_file, self.nonces = app, key_file, Nonces()

    async def __call__(self, scope, receive, send):
        """Verify signed request identity and bind the response to the accepted nonce."""
        statement = dict(scope.get("headers", [])).get(HEADER.encode())
        if scope["type"] != "http" or not statement or not self.key_file:
            return await self.app(scope, receive, send)
        parts, more = [], True
        while more:  # the whole body, since the statement covers it. Turnstile has read it whole already
            message = await receive()
            parts.append(message.get("body", b""))
            more = message.get("more_body", False)
        body, said = b"".join(parts), None
        # The path as it was sent, not as it reads once decoded: "a%2Fb" is not "a/b".
        sent = address((scope.get("raw_path") or scope["path"].encode()).decode(), scope["query_string"])
        for key in keys(self.key_file):  # the key, or for a while after it was replaced, the one before
            try:
                said = check(key, statement.decode(), scope["method"], sent, body, self.nonces)
                break
            except (ValueError, KeyError, TypeError):
                continue
        if said is None:
            refusal = json.dumps({"error": {"message": "This request's statement is not sound. Send it through turnstile.", "type": "statement_error"}}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": refusal})
        scope.setdefault("state", {})["said"] = said
        from loadcoach.observability import adopt, identify
        adopt(said.get('observation'))
        if said.get('purpose') == 'front-probe':
            from loadcoach.observability import annotate
            annotate(benchmark=True)
        if said.get('caller'):
            identify(user_of(said['caller']))
        scope["state"]["key"] = key  # the key it was signed with, which the answer proves and a new key is sealed with
        if said.get("origin"):
            scope = {**scope, "client": (said["origin"], 0)}  # who is asking is the caller, not turnstile, also in the log
        given = False

        async def again():
            """Replay the verified request body to the downstream ASGI application."""
            nonlocal given
            if given:
                return await receive()  # whatever comes next, such as the caller hanging up
            given = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def signed(message):
            """Attach the accepted nonce's machine proof to response headers."""
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (PROOF.encode(), proof(key, said["nonce"]).encode())]}
            await send(message)

        await self.app(scope, again, signed)
