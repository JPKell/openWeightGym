"""Front's accounts, as compute asks for them: for work done later in someone's name.

Compute keeps no accounts. A request carries its caller in a statement signed by turnstile (see
statement.py). Some work runs later, when nobody is there with a statement: a batch answered hours
after it was handed in, a benchmark task, a recurring schedule. It is done with what the account
may do at that moment, so compute asks turnstile for every account, at most every ten seconds,
with the machine key. The reports that name accounts other than the caller's ask the same.

Nothing is written down. If front has not answered for a minute, no account is known, and work in
anyone's name waits or is refused. History the front keeps and worker-side execution boundaries are
documented in docs/database-storage.md.

The event log is front's too (milestone 91). Compute writes its entries as before, and
`send_log` hands them to turnstile every few seconds. Those not yet sent stay in compute's log,
however long front is away. How fast each model answers and when it was last used are read
from front's log, which reaches further back than the day compute keeps.

A batch's request may name a role. The roles are on front too, so compute asks turnstile which
model to send it to, as it would pick for that account asking now.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from loadcoach.statement import HEADER, PROOF, make, proof, read_key, user_of
from loadcoach.users import User
from loadcoach.log import LEGACY

FRESH_FOR = 10  # seconds a list of the accounts is used before front is asked again
SEND_EVERY = 2  # seconds between sending front the log entries it does not have
TRUSTED_FOR = 60  # seconds without an answer from front after which no account is known


class Accounts:
    """The accounts as front last said them. Ask `fresh()` first; the rest only reads what it fetched."""

    def __init__(self, front: str, key_file: str, client: httpx.AsyncClient | None = None, machine_name: str = LEGACY) -> None:
        """Configure the signed front-account client and its current account cache."""
        self.front, self.key_file, self.machine_name = front.rstrip("/"), key_file, machine_name
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2))
        self.known: dict[str, User] = {}
        self.asked = self.answered = 0.0
        self.sending = asyncio.Lock()
        self.observation_reported = 0.

    async def fresh(self) -> None:
        """Ask front for the accounts, unless it was asked a moment ago."""
        if not self.front or not self.key_file or time.monotonic() - self.asked < FRESH_FOR:
            return
        self.asked = time.monotonic()
        try:
            self.known = {one["name"]: user_of(one) for one in (await self.ask("/turnstile/accounts"))["accounts"]}
            self.answered = time.monotonic()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if time.monotonic() - self.answered > TRUSTED_FOR:
                self.known = {}

    async def pick(self, model: str, user: str) -> dict | None:
        """For a batch's request: the model turnstile picks for the role `model`, for `user`, with why. `picked` is None for a name that is not one of their roles. None if front did not answer."""
        if not self.front or not self.key_file:
            return None
        try:
            return await self.ask("/turnstile/pick?" + urlencode({"model": model, "for": user}))
        except (httpx.HTTPError, ValueError):
            return None

    async def send_log(self, log, keep_text: int, prices) -> None:
        """Send front the log's entries it does not have, every few seconds, for as long as LoadCoach runs.

        `keep_text` is how many requests front keeps in full. `prices()` are the hosted models with what each costs, which
        front prices a hosted answer recorded without its cost by.
        """
        while True:
            try:
                await self.send_now(log, keep_text, prices)
            except (httpx.HTTPError, ValueError):
                pass  # front is away: they are sent when it is back
            await asyncio.sleep(SEND_EVERY)

    async def send_now(self, log, keep_text: int, prices, always: bool = False) -> None:
        """Send front every entry it does not have yet. Also when turnstile asks, before it shows the log, so that it shows what has just happened: then `always`, with the prices, even with no entries."""
        async with self.sending:  # one at a time, so that what front has only ever goes forward
            while True:
                entries, texts = log.unsent()
                observations = [json.loads(row[0]) for row in log.db.execute(
                    'SELECT data FROM observation_spans WHERE sent=0 ORDER BY at LIMIT 500').fetchall()]
                if not entries and not observations and not always and time.monotonic()-self.observation_reported < 10:
                    return
                health = log.observations.health() if hasattr(log, 'observations') else {'recording':False}
                count, oldest = log.db.execute('SELECT COUNT(*),MIN(at) FROM observation_spans WHERE sent=0').fetchone()
                health.update(backlog_count=count, backlog_age_seconds=max(0, time.time()-oldest) if oldest else 0.)
                await self.ask("/turnstile/log", {"entries": entries, "texts": texts, "keep_text": keep_text, "prices": prices(),
                                                "observations": observations,
                                                "observation_health": health})
                self.observation_reported = time.monotonic()
                acknowledged = True
                if observations:
                    from loadcoach.observation_store import acknowledge
                    acknowledged = await asyncio.to_thread(acknowledge, log.db.database,
                        [{'id':row['trace_id']+row['id'], 'finished':row.get('finished', True)} for row in observations])
                if entries:
                    log.sent(entries[-1]["id"])
                if not acknowledged or len(entries) < 500 and len(observations) < 500:
                    return

    async def log_facts(self) -> dict | None:
        """From front's log: how fast each model answers (`speed`), how long it last took to load (`load`), and when it was last used (`last_used`). None if front did not answer."""
        if not self.front or not self.key_file:
            return None
        try:
            return await self.ask("/turnstile/log-facts")
        except (httpx.HTTPError, ValueError):
            return None

    async def ask(self, where: str, sent: dict | None = None) -> dict:
        """Ask turnstile, as this machine, with the machine key: a GET, or a POST of `sent`. ValueError if its answer is not proven to come from front."""
        key, body = read_key(self.key_file), json.dumps(sent).encode() if sent is not None else b""
        said, nonce = make(key, "POST" if sent is not None else "GET", where, body, machine=self.machine_name)
        answer = await self.client.request("POST" if sent is not None else "GET", self.front + where, content=body,
                                           headers={HEADER: said, **({"content-type": "application/json"} if body else {})})
        if answer.status_code != 200 or not secrets.compare_digest(answer.headers.get(PROOF, ""), proof(key, nonce)):
            raise ValueError("front did not answer, or not with the machine key")
        return answer.json()

    def by_name(self, name: str) -> User | None:
        """Return the currently cached signed account with this name."""
        return self.known.get(name)

    def by_identity(self, identity: str) -> User | None:
        """Resolve a stable account identity in the current signed account cache."""
        return next((user for user in self.known.values() if user.identity == identity), None)

    def identity(self, name: str) -> str | None:
        """Return a cached account's stable identity, or None when unknown."""
        user = self.known.get(name)
        return user.identity if user else None

    def memberships(self, only: str | None = None) -> dict[str, str]:
        """Each account's group, or just the one named `only`."""
        return {name: user.group_name for name, user in self.known.items() if only is None or name == only}
