"""A stand-in for Hugging Face, for the checks: the three addresses LoadCoach asks, answered from memory.

It answers as the real one was seen to on 2026-10-09: a list of repositories, a list of a
repository's files with the SHA-256 of each under `lfs.oid`, and for a file the address of
another machine that holds it. That machine sends the part of a file that `range` asks for.
"""

import asyncio
import hashlib

import httpx


class FakeHub:
    """One repository, `acme/tiny-GGUF`, holding `files`. What was asked of it is kept in `seen`."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.repository, self.files = "acme/tiny-GGUF", files
        self.closed = False  # True: its files are only for an account it was opened to, whose token is `good`
        self.slow = 0.0  # seconds between the pieces of a file, to give a check time to look
        self.lies: set[str] = set()  # files it describes with a SHA-256 that is not theirs
        self.claims: dict[str, int] = {}  # files it says are another size than they are
        self.seen: list[tuple[str, str, dict]] = []  # the machine asked, the path, and the headers that came

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), follow_redirects=True)

    def asked_of(self, host: str) -> list[dict]:
        """The headers of every request for a file that reached one machine."""
        return [headers for where, path, headers in self.seen if where == host and path.endswith(".gguf")]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.seen.append((host, path, dict(request.headers)))
        if host == "cdn.test":
            return self.sent(self.files[path.lstrip("/")], request.headers.get("range"))
        if path == "/api/models":
            found = self.repository if request.url.params["search"].lower() in self.repository.lower() else None
            return httpx.Response(200, json=[{"id": found, "downloads": 5, "likes": 2, "createdAt": "2026-01-02T03:04:05.000Z", "tags": ["gguf"]}] if found else [])
        if path == f"/api/models/{self.repository}/tree/main":
            listed = [{"type": "directory", "path": "parts", "size": 0}, {"type": "file", "path": "README.md", "size": 10}]
            for name, content in self.files.items():
                digest = "0" * 64 if name in self.lies else hashlib.sha256(content).hexdigest()
                listed.append({"type": "file", "path": name, "size": self.claims.get(name, len(content)), "lfs": {"oid": digest}})
            return httpx.Response(200, json=listed)
        name = path.removeprefix(f"/{self.repository}/resolve/main/")
        if name not in self.files or (self.closed and request.headers.get("authorization") != "Bearer good"):
            return httpx.Response(401, json={"error": "Repository not found"})  # the same answer for both, as the real one gives
        return httpx.Response(302, headers={"location": f"https://cdn.test/{name}"})

    def sent(self, content: bytes, wanted: str | None) -> httpx.Response:
        start = int(wanted.removeprefix("bytes=").rstrip("-")) if wanted else 0

        async def pieces():
            for at in range(start, len(content), 64):
                await asyncio.sleep(self.slow)
                yield content[at : at + 64]

        return httpx.Response(206 if wanted else 200, content=pieces())
