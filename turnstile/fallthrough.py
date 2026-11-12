"""Requests turnstile sends to the default node only because nothing else routed them.

Phase 0 of docs/peer-nodes-plan.md. Turnstile's last resort sends a request to the default
compute node whatever node it was meant for. Each distinct method, path pattern and port that
reaches that resort is printed once per process, so the journal lists what still depends on a
default node. Later phases route those paths and the list shrinks to nothing.
"""

import re

LIMIT = 500  # distinct patterns remembered; beyond this nothing more is printed, so the journal stays bounded

# Path segments that name one record rather than a route: numbers, prefixed random IDs
# (batch_…, model_…), long hexadecimal digests and UUIDs.
_RECORD = re.compile(r"(?<=/)(?:\d+|[a-z]+_[0-9a-f]{12,}|[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|$)")


def pattern(path: str) -> str:
    """The route a path belongs to, with record IDs replaced by `{id}`."""
    return _RECORD.sub("{id}", path)


class Fallthrough:
    """Counts the requests that reached the default node by fallthrough, by route."""

    def __init__(self, limit: int = LIMIT, say=print) -> None:
        """Remember at most `limit` routes; `say` prints the first of each (the journal, by default)."""
        self.limit, self.say = limit, say
        self.seen: dict[tuple[str, str, int], int] = {}

    def note(self, method: str, path: str, port: int) -> None:
        """Count one request. The first of each route is printed; later ones are only counted."""
        key = (method, pattern(path), port)
        if key in self.seen:
            self.seen[key] += 1
            return
        if len(self.seen) >= self.limit:
            return
        self.seen[key] = 1
        self.say(f"turnstile: default-node fallthrough {method} {key[1]} on port {port}", flush=True)

    def routes(self) -> set[str]:
        """Every route seen so far, as `METHOD pattern port`."""
        return {f"{method} {route} {port}" for method, route, port in self.seen}
