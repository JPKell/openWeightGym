"""Derived, automatically refreshed Python source graph and optional docstring vectors.

Only Git-tracked regular Python files in the configured repository are read. Parsing never
imports repository code. SQLite is a disposable cache, not an authority for source or access.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
import sqlite3
import stat
import subprocess
import threading
import time
import tokenize
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

MAX_FILE = 1024 * 1024
MAX_TOTAL = 64 * MAX_FILE
MAX_FILES = 5000
MAX_SYMBOLS = 50000
SCHEMA = 1


def bounded(value: int, low: int, high: int, name: str) -> int:
    """Reject invalid model-supplied bounds instead of allowing unbounded work."""
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def normalized(values: list) -> list[float]:
    """Validate one bounded nonzero vector before storing or comparing it."""
    if not isinstance(values, list) or not 1 <= len(values) <= 4096:
        raise ValueError("Embedding dimensions must be between 1 and 4096")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
        raise ValueError("Embedding values must be finite numbers")
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or not norm:
        raise ValueError("Embedding must have finite nonzero magnitude")
    return [v / norm for v in values]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a configured embedding credential to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Embedder:
    """Opt-in OpenAI-compatible embeddings; use the existing OWG inference service."""

    def __init__(self, settings: dict):
        self.url = settings.get("embedding_url", "")
        self.model = settings.get("embedding_model", "")
        self.key_file = settings.get("embedding_key_file", "")
        self.revision = settings.get("embedding_revision", "")
        if self.url:
            parsed = urllib.parse.urlsplit(self.url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or not self.model or not self.revision):
                raise ValueError("Embeddings need an HTTP(S) URL without credentials/query/fragment, model and revision")
        self.identity = hashlib.sha256(json.dumps([self.url, self.model, self.revision]).encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json", "X-Turnstile-No-Wake": "1"}
        if self.key_file:
            headers["Authorization"] = "Bearer " + Path(self.key_file).expanduser().read_text().strip()
        request = urllib.request.Request(self.url, data=json.dumps({"model": self.model, "input": texts}).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(2 * MAX_FILE + 1)
            if len(raw) > 2 * MAX_FILE:
                raise ValueError("Embedding response exceeds 2 MiB")
            body = json.loads(raw)
            rows = sorted(body["data"], key=lambda row: row["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise ValueError("Embedding response indices do not match the batch")
            vectors = [normalized(row["embedding"]) for row in rows]
            if len({len(v) for v in vectors}) != 1:
                raise ValueError("Embedding dimensions differ within the batch")
            return vectors
        except (OSError, ValueError, KeyError, TypeError) as problem:
            # Provider bodies, URLs and credentials do not belong in tool responses or logs.
            raise ValueError(f"Embedding service unavailable or invalid ({type(problem).__name__}); lexical search remains available") from None


def parse(path: str, content: bytes, digest: str) -> tuple[list[tuple], list[tuple]]:
    """Extract definitions and syntactic relationships, without executing imports or code."""
    tree = ast.parse(content, filename=path)
    symbols, edges = [], []
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def definition(self, node, name, kind):
            qualname = ".".join([s[1] for s in self.stack if s[1]] + [name])
            ident = f"{path}::{qualname}@{getattr(node, 'lineno', 1)}"
            symbols.append((ident, path, name, qualname, kind, getattr(node, "lineno", 1),
                            getattr(node, "end_lineno", len(content.splitlines())) or 1,
                            (ast.get_docstring(node, clean=True) or "")[:8000], digest,
                            module if kind == "module" else module + "." + qualname))
            if self.stack:
                edges.append((self.stack[-1][0], ident, "contains", getattr(node, "lineno", 1)))
            self.stack.append((ident, "" if kind == "module" else name))
            for child in node.body:
                self.visit(child)
            self.stack.pop()

        def visit_Module(self, node):
            self.definition(node, "<module>", "module")

        def visit_ClassDef(self, node):
            # Bases are expressions owned by the class definition, captured after containment.
            self.definition(node, node.name, "class")
            ident = next(s[0] for s in reversed(symbols) if s[1] == path and s[5] == node.lineno and s[4] == "class")
            for base in node.bases:
                target = self.target(base)
                if target:
                    edges.append((ident, target, "inherits", node.lineno))

        def visit_FunctionDef(self, node):
            self.definition(node, node.name, "function")

        visit_AsyncFunctionDef = visit_FunctionDef

        def target(self, node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                prefix = self.target(node.value)
                return prefix + "." + node.attr if prefix else ""
            return ""

        def visit_Call(self, node):
            if target := self.target(node.func):
                edges.append((self.stack[-1][0], target, "calls", node.lineno))
            self.generic_visit(node)

        def visit_Import(self, node):
            for alias in node.names:
                edges.append((self.stack[-1][0], alias.name, "imports", node.lineno))

        def visit_ImportFrom(self, node):
            for alias in node.names:
                edges.append((self.stack[-1][0], "." * node.level + (node.module or "") + "." + alias.name, "imports", node.lineno))

    Visitor().visit(tree)
    return symbols, edges


class CodeIndex:
    """One allowlisted repository's transactional SQLite cache and bounded refresh worker."""

    def __init__(self, root: str, settings: dict):
        self.root = Path(root).resolve()
        self.embedder = Embedder(settings)
        cache = Path(settings.get("index_directory", "~/.cache/openweightgym/code")).expanduser()
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = str(self.root) + "\n" + self.embedder.identity
        self.path = cache / (hashlib.sha256(identity.encode()).hexdigest()[:24] + ".sqlite3")
        if self.path.is_symlink():
            raise ValueError("Code index database cannot be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.interval = bounded(settings.get("index_interval", 30), 1, 3600, "index_interval")
        self.embedding_error = ""
        self.refresh_error = ""
        with self.connect() as db:
            if db.execute("pragma user_version").fetchone()[0] not in (0, SCHEMA):
                raise ValueError("Unsupported code index schema; stop the host and remove its derived database to rebuild")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS symbols(id TEXT PRIMARY KEY, path TEXT, name TEXT, qualname TEXT,
                    kind TEXT, line INTEGER, end_line INTEGER, doc TEXT, hash TEXT, dotted TEXT);
                CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path);
                CREATE TABLE IF NOT EXISTS edges(source TEXT, target TEXT, kind TEXT, line INTEGER);
                CREATE INDEX IF NOT EXISTS edges_source ON edges(source);
                CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
                CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(id UNINDEXED, name, doc);
                CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, hash TEXT, identity TEXT, vector TEXT);
                CREATE TABLE IF NOT EXISTS embedding_failures(id TEXT PRIMARY KEY, attempted REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            """)
            db.execute(f"pragma user_version={SCHEMA}")
            db.execute("DELETE FROM vectors WHERE identity != ?", (self.embedder.identity,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        """Run bounded refresh/retry cycles until the owning host exits or closes this index."""
        def work():
            while not self.stop.is_set():
                try:
                    self.refresh()
                    self.embed_pending()
                except Exception as problem:
                    self.refresh_error = f"Refresh failed ({type(problem).__name__}); next cycle retries"
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=work, name="code-index", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)

    def tracked(self) -> list[str]:
        result = subprocess.run(["git", "-C", str(self.root), "ls-files", "-z", "--cached", "--", "*.py"],
                                capture_output=True, timeout=30, check=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return sorted(set(os.fsdecode(p) for p in result.stdout.split(b"\0") if p))

    def read(self, path: str) -> bytes:
        """Use no-follow directory descriptors to refuse symlinks, including raced parents."""
        parts = Path(path).parts
        if not parts or Path(path).is_absolute() or any(p in ("..", ".git") for p in parts):
            raise ValueError("Invalid source path")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                    raise ValueError("Source must be a regular Python file of at most 1 MiB")
                content = stream.read(MAX_FILE + 1)
                if len(content) > MAX_FILE:
                    raise ValueError("Source exceeds 1 MiB")
                return content
        finally:
            os.close(fd)

    def refresh(self):
        """Hash tracked working-tree sources; atomically replace changed graph rows and remove stale ones."""
        with self.lock, self.connect() as db:
            paths = set(self.tracked())
            if len(paths) > MAX_FILES:
                raise ValueError(f"Repository exceeds {MAX_FILES} tracked Python files; configure a smaller repository")
            old = {r["path"]: r["hash"] for r in db.execute("SELECT path, hash FROM files")}
            total, count = 0, db.execute("SELECT count(*) FROM symbols").fetchone()[0]
            for path in sorted(set(old) | set(paths)):
                content, digest, error, symbols, edges = b"", "", "", [], []
                if path in paths:
                    try:
                        content = self.read(path)
                        total += len(content)
                        if total > MAX_TOTAL:
                            raise RuntimeError("Repository exceeds 64 MiB of Python source")
                        digest = hashlib.sha256(content).hexdigest()
                        if old.get(path) == digest:
                            continue
                        symbols, edges = parse(path, content, digest)
                    except (OSError, ValueError, SyntaxError, RecursionError) as problem:
                        # Exclude unreadable/invalid sources immediately, rather than serving old definitions.
                        error = type(problem).__name__
                count -= db.execute("SELECT count(*) FROM symbols WHERE path=?", (path,)).fetchone()[0]
                count += len(symbols)
                if count > MAX_SYMBOLS:
                    raise ValueError(f"Repository exceeds {MAX_SYMBOLS} symbols")
                for table, key in (("edges", "source"), ("docs", "id"), ("vectors", "id"), ("embedding_failures", "id")):
                    db.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT id FROM symbols WHERE path=?)", (path,))
                db.execute("DELETE FROM symbols WHERE path=?", (path,))
                db.execute("DELETE FROM files WHERE path=?", (path,))
                if path in paths:
                    db.execute("INSERT INTO files VALUES(?,?,?)", (path, digest, error))
                db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?)", symbols)
                db.executemany("INSERT INTO edges VALUES(?,?,?,?)", edges)
                db.executemany("INSERT INTO docs VALUES(?,?,?)", [(s[0], s[3], s[7]) for s in symbols])
            db.execute("INSERT OR REPLACE INTO metadata VALUES('updated', ?)", (str(time.time()),))
            self.refresh_error = ""

    def embed_pending(self):
        """Embed at most 16 pending docstrings per cycle; publish only still-current source hashes."""
        if not self.embedder.url:
            return
        with self.lock, self.connect() as db:
            rows = db.execute("""SELECT s.* FROM symbols s LEFT JOIN vectors v ON v.id=s.id
                LEFT JOIN embedding_failures f ON f.id=s.id WHERE s.doc!='' AND v.id IS NULL
                ORDER BY coalesce(f.attempted,0),s.id LIMIT 16""").fetchall()
        if not rows:
            return
        try:
            vectors = self.embedder.embed([r["qualname"] + "\n" + r["doc"] for r in rows])
            with self.lock, self.connect() as db:
                existing = db.execute("SELECT vector FROM vectors LIMIT 1").fetchone()
                if existing and len(json.loads(existing[0])) != len(vectors[0]):
                    db.execute("DELETE FROM vectors")
                    self.embedding_error = "Embedding dimensions changed; vectors cleared and next cycle rebuilds"
                    return
                for row, vector in zip(rows, vectors, strict=True):
                    db.execute("INSERT OR REPLACE INTO vectors SELECT id, hash, ?, ? FROM symbols WHERE id=? AND hash=?",
                               (self.embedder.identity, json.dumps(vector), row["id"], row["hash"]))
                    db.execute("DELETE FROM embedding_failures WHERE id=?", (row["id"],))
            self.embedding_error = ""
        except ValueError as problem:
            self.embedding_error = str(problem)
            with self.lock, self.connect() as db:
                for row in rows:
                    db.execute("INSERT OR REPLACE INTO embedding_failures SELECT id,?,? FROM symbols WHERE id=? AND hash=?",
                               (time.time(), self.embedding_error, row["id"], row["hash"]))

    def status(self):
        with self.lock, self.connect() as db:
            updated = db.execute("SELECT value FROM metadata WHERE key='updated'").fetchone()
            return {"repository": str(self.root), "database": str(self.path), "updated": float(updated[0]) if updated else None,
                    "refresh_interval_seconds": self.interval, "refresh_error": self.refresh_error,
                    "files": db.execute("SELECT count(*) FROM files").fetchone()[0],
                    "symbols": db.execute("SELECT count(*) FROM symbols").fetchone()[0],
                    "edges": db.execute("SELECT count(*) FROM edges").fetchone()[0],
                    "excluded_count": db.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0],
                    "excluded": [dict(r) for r in db.execute("SELECT path,error FROM files WHERE error!='' ORDER BY path LIMIT 50")],
                    "embedding_enabled": bool(self.embedder.url), "embedding_error": self.embedding_error,
                    "failed_docstrings": db.execute("SELECT count(*) FROM embedding_failures").fetchone()[0],
                    "vectors": db.execute("SELECT count(*) FROM vectors").fetchone()[0],
                    "pending_docstrings": db.execute("SELECT count(*) FROM symbols WHERE doc!='' AND id NOT IN (SELECT id FROM vectors)").fetchone()[0]}

    @staticmethod
    def record(row):
        return {k: row[k] for k in ("id", "path", "qualname", "kind", "line", "end_line", "hash")}

    def symbols(self, query: str, limit: int, offset: int):
        bounded(limit, 1, 50, "limit")
        bounded(offset, 0, MAX_SYMBOLS, "offset")
        if len(query) > 200:
            raise ValueError("query must be at most 200 characters")
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT * FROM symbols WHERE instr(lower(qualname),lower(?)) OR instr(lower(path),lower(?)) ORDER BY path,line,id LIMIT ? OFFSET ?",
                              (query, query, limit + 1, offset)).fetchall()
            return {"symbols": [self.record(r) for r in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None}

    def symbol(self, ident: str, offset: int, lines: int):
        bounded(offset, 0, 1000000, "offset")
        bounded(lines, 1, 100, "lines")
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM symbols WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("Unknown symbol; use symbols to get a current id")
            content = self.read(row["path"])
            if hashlib.sha256(content).hexdigest() != row["hash"]:
                raise ValueError("Source changed during the read; retry symbols")
            encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
            source = content.decode(encoding).splitlines()[row["line"] - 1:row["end_line"]]
            selected = source[offset:offset + lines]
            text = "\n".join(selected)
            return {**self.record(row), "docstring": row["doc"], "source": text[:16000], "source_truncated": len(text) > 16000,
                    "next_offset": offset + lines if offset + lines < len(source) else None}

    def related(self, ident: str, direction: str, limit: int, offset: int):
        bounded(limit, 1, 50, "limit")
        bounded(offset, 0, 1000000, "offset")
        if direction not in ("outgoing", "incoming"):
            raise ValueError("direction must be outgoing or incoming")
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM symbols WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("Unknown symbol; use symbols to get a current id")
            # Containment is exact. Other targets are source spellings, not a Python call resolver.
            if direction == "outgoing":
                rows = db.execute("SELECT * FROM edges WHERE source=? ORDER BY line,kind,target LIMIT ? OFFSET ?", (ident, limit + 1, offset)).fetchall()
            else:
                rows = db.execute("SELECT * FROM edges WHERE target IN (?,?,?,?) ORDER BY source,line,kind LIMIT ? OFFSET ?",
                                  (ident, row["name"], row["qualname"], row["dotted"], limit + 1, offset)).fetchall()
            return {"symbol": self.record(row), "direction": direction,
                    "relationships": [{**dict(r), "certainty": "exact" if r["kind"] == "contains" else "syntactic_candidate"} for r in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None,
                    "limits": "Python AST only. Calls/imports/bases are source spellings; aliases, dynamic dispatch and external targets are not resolved. Incoming name matches may be unrelated."}

    def search(self, query: str, semantic: bool, limit: int):
        bounded(limit, 1, 20, "limit")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("query must contain 1–1000 characters")
        if type(semantic) is not bool:
            raise ValueError("semantic must be a boolean")
        if semantic and not self.embedder.url:
            raise ValueError("Semantic search needs embedding_url, embedding_model and embedding_revision; use semantic=false for lexical search")
        vector = self.embedder.embed([query])[0] if semantic else None
        with self.lock, self.connect() as db:
            if semantic:
                scores = []
                for row in db.execute("SELECT s.*, v.vector FROM vectors v JOIN symbols s ON s.id=v.id"):
                    other = json.loads(row["vector"])
                    if len(other) != len(vector):
                        raise ValueError("Query embedding dimensions changed; update embedding_revision and restart the host")
                    scores.append((sum(a * b for a, b in zip(vector, other, strict=True)), row))
                selected = sorted(scores, key=lambda item: (-item[0], item[1]["id"]))[:limit]
            else:
                # Quote every token: user text never becomes an FTS expression.
                terms = [p for p in query.split()[:32] if any(c.isalnum() for c in p)]
                if not terms:
                    return {"mode": "lexical", "hits": []}
                expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in terms)
                selected = [(r["score"], r) for r in db.execute("SELECT s.*, bm25(docs) AS score FROM docs JOIN symbols s ON s.id=docs.id WHERE docs MATCH ? ORDER BY score,s.id LIMIT ?", (expression, limit))]
            return {"mode": "semantic" if semantic else "lexical", "hits": [{**self.record(row), "docstring": row["doc"][:1000], "score": score} for score, row in selected]}
