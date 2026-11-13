"""Bounded diagnostics for refused requests with no route or explicit node.

There is no default-node dispatch. Keep record IDs out of route diagnostics and
print each route once per process so missing caller routes can be investigated.
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
    """Count refused unmapped requests by route."""

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
        self.say(f"turnstile: unrouted request {method} {key[1]} on port {port}", flush=True)

    def routes(self) -> set[str]:
        """Every route seen so far, as `METHOD pattern port`."""
        return {f"{method} {route} {port}" for method, route, port in self.seen}
