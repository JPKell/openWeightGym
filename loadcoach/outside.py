"""Outside services: models that are answered somewhere else than on this machine's cards.

A service is an address that speaks OpenAI's format, such as OpenRouter, a rented server or
another LoadCoach. It is registered once, with a name. Its key is not registered: that is a
file on the front machine, `NAME.key` in the keys folder. Authorized administrators can
replace it from Cloud APIs; the stored value is never returned or kept in the database.

A service offers models. Each has a name of ours, which callers ask for, the service's own
name for it, and optionally a price, so that what an answer cost can be worked out when the
service does not say.

Services are kept in the same file as the users. This file only keeps them. Sending a request
out, and deciding whether it may leave, is in app.py.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from db import DatabaseSource, database, migrate
from db.schemas import OUTSIDE_SERVICES

from loadcoach.models import NAME as MODEL_NAME

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # a service's name. It is also the name of its key file, so it is kept plain
PER = 1_000_000  # a price is in dollars for this many tokens, which is how these services state theirs


def check_service(name: str, settings: dict, taken: set[str]) -> None:
    """Raise ValueError, saying why, if these could not be a service's settings. `taken` are the names models have already, here or at other services."""
    if not NAME.fullmatch(name):
        raise ValueError(f"{name!r}: a service's name is lowercase letters, digits and dashes.")
    if not isinstance(settings, dict) or settings.keys() - {"url", "ceiling", "models", "key_env"}:
        raise ValueError(f"service {name!r} may set url, ceiling and models.")
    url, ceiling, models = settings.get("url"), settings.get("ceiling"), settings.get("models", {})
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"service {name!r} needs url: its address, as far as and including /v1.")
    if ceiling is not None and (type(ceiling) not in (int, float) or ceiling < 0):
        raise ValueError(f"service {name!r}: ceiling is the most it may cost in 24 hours, in dollars. Leave it out for none.")
    price = lambda one: all(type(one.get(way, 0)) in (int, float) and one.get(way, 0) >= 0 for way in ("in", "out"))  # noqa: E731
    good = lambda ours, one: isinstance(ours, str) and MODEL_NAME.fullmatch(ours) and isinstance(one, dict) and one.keys() <= {"theirs", "in", "out", "vision", "json_schema"} and isinstance(one.get("theirs"), str) and one["theirs"] and price(one) and all(type(one.get(key, False)) is bool for key in ('vision', 'json_schema'))  # noqa: E731
    if not isinstance(models, dict) or not all(good(ours, one) for ours, one in models.items()):
        raise ValueError(f"service {name!r}: models is a list of OUR NAME = {{theirs, in, out}}. theirs is the service's own name for the model. in and out are prices in dollars for a million tokens, and may be left out.")
    if both := sorted(models.keys() & taken):
        raise ValueError(f"There is already a model named {', '.join(both)}.")


def cost_of(counts: dict, price: dict) -> float | None:
    """What an answer cost in dollars, worked out from its token counts and the model's price. None if the model has no price."""
    if "in" not in price and "out" not in price:
        return None
    return round((counts.get("prompt_tokens", 0) * price.get("in", 0) + counts.get("answer_tokens", 0) * price.get("out", 0)) / PER, 6)


class Services:
    """The outside services, kept in the database. A change is written at once and counts from the next request."""

    def __init__(self, path: DatabaseSource, keys: str, first: dict[str, dict] | None = None, handed: dict[str, str] | None = None) -> None:
        """`keys` is the folder the key files are in. `first` are the services to start with, used once, when the table is empty:
        that is how the one service that used to be written in the settings file is taken over. `handed` are keys given
        by the settings themselves, each for the service of that name, used where a service has no key file."""
        self.keys, self.handed = Path(keys), handed or {}
        source = database(path)
        self.db = source.connect(check_same_thread=False)
        migrate(source, self.db, "loadcoach.outside", OUTSIDE_SERVICES)
        self.taken_over = 0
        if first and not self.names():
            for name, settings in first.items():
                self.put(name, settings)
            self.taken_over = len(first)

    def names(self) -> list[str]:
        """List configured hosted services in their operator-defined order."""
        return [name for (name,) in self.db.execute("SELECT name FROM services ORDER BY position, name")]

    def get(self, name: str) -> dict | None:
        """Read one hosted service's retained configuration, or None when absent."""
        row = self.db.execute("SELECT settings FROM services WHERE name = ?", (name,)).fetchone()
        return row and json.loads(row[0])

    def put(self, name: str, settings: dict) -> None:
        """Add a service, or replace the settings of one that exists. Check them first with check_service."""
        with self.db:
            self.db.execute("INSERT INTO services (name, settings, position) VALUES (?, ?, COALESCE((SELECT MAX(position)+1 FROM services),1)) ON CONFLICT (name) DO UPDATE SET settings = excluded.settings", (name, json.dumps(settings)))

    def remove(self, name: str) -> bool:
        """Delete a hosted service configuration and report whether it existed."""
        with self.db:
            return self.db.execute("DELETE FROM services WHERE name = ?", (name,)).rowcount > 0

    def models(self) -> dict[str, dict]:
        """Every model the services offer, by our name for it: its `service`, the service's name for it as `theirs`, and its price if it has one."""
        rows = self.db.execute("SELECT name, settings FROM services ORDER BY position, name").fetchall()
        return {ours: {"service": name, **one}
                for name, settings in rows for ours, one in json.loads(settings).get("models", {}).items()}

    def key_file(self, name: str) -> Path:
        """Return the private key-file path for the named hosted service."""
        return self.keys / f"{name}.key"

    def set_key(self, name: str, value: str) -> None:
        """Atomically replace one configured service's owner-only credential file."""
        if not NAME.fullmatch(name) or self.get(name) is None:
            raise ValueError("Choose an existing cloud API.")
        key = value.strip() if isinstance(value, str) else ""
        if not 1 <= len(key) <= 8192 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("Enter an API key of 1 to 8192 printable characters, without spaces.")
        self.keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".cloud-key-", dir=self.keys)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(key + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.key_file(name))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def key(self, name: str) -> str:
        """A service's key, read when it is needed, so that a new one needs no restart. Empty if there is none.

        It is the file `NAME.key` in the keys folder. A service taken over from the settings
        file may have its key in an environment variable instead, as it had there.
        """
        try:
            return self.key_file(name).read_text().strip()
        except OSError:
            return os.environ.get((self.get(name) or {}).get("key_env", ""), "") or self.handed.get(name, "")
