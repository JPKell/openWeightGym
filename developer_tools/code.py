"""Code tools: look at the git repositories it is given. Nothing is changed.

In a tool folder, they are `repositories` in the `[code]` section of the settings. Alone:

    python -m toollocker.tools.code /home/owgadmin/ai/suite /home/owgadmin/repos

Each is a repository, or a folder that holds repositories, in which case every repository
directly inside it is included.

Only files git tracks are searched, including staged additions and working-tree edits.
The Python graph and docstrings refresh automatically into a private derived SQLite cache.
Untracked files are excluded; do not track secrets in repositories granted to tool callers.
"""

from __future__ import annotations

import os
import argparse
import json
import subprocess
import tomllib
from pathlib import Path

from developer_tools.code_index import CodeIndex
from toollocker.tools import serve, tool

REPOS: list[str] = []  # set by `setup`
INDEXES: dict[str, CodeIndex] = {}
SETTINGS: dict = {}


def find_repositories(folder: Path) -> list[str]:
    """`folder` itself if it is a repository, otherwise the repositories directly inside it."""
    folder = folder.resolve()
    if (folder / ".git").exists():
        return [str(folder)]
    # A set, because a link to a repository beside the repository itself is the same one twice.
    return sorted({str(inner.resolve()) for inner in folder.iterdir() if (inner / ".git").exists()})


def setup(settings: dict) -> None:
    """Take the repositories this tool may read: `repositories` in its section of the settings."""
    global SETTINGS
    for index in INDEXES.values():
        index.close()
    INDEXES.clear()
    REPOS.clear()
    SETTINGS = dict(settings)
    for folder in settings.get("repositories", []):
        if not Path(folder).expanduser().is_dir():
            raise ValueError(f"code: {folder} is not a folder")
        REPOS.extend(found for found in find_repositories(Path(folder).expanduser()) if found not in REPOS)  # each one once
    if not REPOS:
        raise ValueError("code needs `repositories`, a list of git repositories or folders that hold them; none was found")
    for root in REPOS:
        index = CodeIndex(root, SETTINGS)
        INDEXES[root] = index
        index.start()


def selected(repo: str) -> str:
    """Select only an explicitly configured repository; refuse ambiguous folder names."""
    matches = [known for known in REPOS if repo in (known, Path(known).name)] if repo else REPOS[:1]
    if len(matches) != 1:
        raise ValueError("Choose one unambiguous repository path from repositories")
    return matches[0]


def indexed(repo: str) -> CodeIndex:
    """Refresh source before answering; failures refuse stale query results."""
    root = selected(repo)
    if root not in INDEXES:
        INDEXES[root] = CodeIndex(root, SETTINGS)
        INDEXES[root].start()
    index = INDEXES[root]
    index.refresh()
    return index


def git(repo: str, *arguments: str) -> str:
    """Run one git command in `repo` and return what it printed. Raises ValueError if git says no.

    `repo` is a repository's full path or its folder's name. Empty means the first one.
    """
    where = selected(repo)
    done = subprocess.run(
        ["git", "-C", where, "--no-pager", *arguments],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},  # so that even `status` writes nothing
    )
    if done.returncode and done.stderr:
        raise ValueError(done.stderr.strip())
    return done.stdout


@tool
def repositories() -> str:
    """List the repositories these tools may read, by full path. The folder's name is enough to pick one."""
    return "\n".join(REPOS)


@tool
def status(repo: str = "") -> str:
    """Which branch a repository is on, and which files have changes not yet committed.

    `repo` is a name from `repositories`. Without it, the first one listed there.
    """
    return git(repo, "status", "--short", "--branch")


@tool
def log(repo: str = "", count: int = 20, file: str = "") -> str:
    """The latest commits, newest first: short hash, date, author and subject.

    `count` is how many. `file` narrows it to commits that touched that file or folder.
    """
    return git(repo, "log", f"--max-count={int(count)}", "--date=short", "--format=%h %ad %an: %s", "--", *filter(None, [file]))


@tool
def diff(repo: str = "", commit: str = "") -> str:
    """What one commit changed, given its hash. Without `commit`, the changes not yet committed."""
    # A value git could take for an option is refused. `--output=FILE` would write a file.
    if commit.startswith("-"):
        raise ValueError("A commit is a hash or a name such as HEAD~2. It cannot start with a dash.")
    plain = ("--no-ext-diff", "--no-textconv")  # never run a program the repository names
    if commit:
        return git(repo, "show", *plain, "--date=short", "--format=%h %ad %an: %s%n%n%b", commit, "--")
    return git(repo, "diff", *plain, "HEAD", "--")


@tool
def search(pattern: str, repo: str = "", path: str = "") -> str:
    """Search a repository's files for a regular expression. Returns file, line number and line.

    `path` narrows the search to one file or folder inside the repository.
    """
    return git(repo, "grep", "-n", "-I", "-E", "-e", pattern, "--", *filter(None, [path])) or "Nothing found."


@tool
def index_status(repo: str = "") -> str:
    """Inspect automatic Python graph/docstring indexing: refresh time, excluded files, vector queue and errors.

    repo selects an allowlisted repository. Queries refresh tracked working-tree source;
    background cycles also refresh it and retry up to 16 pending embeddings per cycle.
    No repository code executes. The database is a disposable cache. This tool reports refresh
    failures without presenting cached data as current; other index queries refuse on failure.
    """
    root = selected(repo)
    try:
        index = indexed(repo)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as problem:
        if root not in INDEXES:
            raise
        index = INDEXES[root]
        index.refresh_error = f"Refresh failed ({type(problem).__name__}); cached rows may be stale"
    return json.dumps(index.status())


@tool
def symbols(query: str = "", repo: str = "", limit: int = 20, offset: int = 0) -> str:
    """Find Python modules, classes and functions by name/path substring; blank lists all, paged.

    Returns current symbol ids, source paths, line ranges and SHA-256 hashes. Follow next_offset;
    limit is 1–50. Use read_symbol for exact source or related for static relationships.
    """
    return json.dumps(indexed(repo).symbols(query, limit, offset))


@tool
def read_symbol(symbol: str, repo: str = "", offset: int = 0, lines: int = 60) -> str:
    """Read a current Python symbol id from symbols/docstring_search, with docstring and source citation.

    offset is a zero-based line offset within the symbol; lines is 1–100. Follow next_offset.
    Source is hash-checked against the graph; retry symbol discovery if the file changed.
    """
    return json.dumps(indexed(repo).symbol(symbol, offset, lines))


@tool
def related(symbol: str, repo: str = "", direction: str = "outgoing", limit: int = 20, offset: int = 0) -> str:
    """Inspect static Python contains/calls/imports/inherits edges for a current symbol id.

    direction is outgoing or incoming; incoming helps find possible change impact. Follow
    next_offset; limit is 1–50. Containment is exact. Other edges are syntactic candidates,
    not proven runtime dependencies: aliases/dynamic dispatch are unresolved and incoming
    name matches can be unrelated. Read source and run tests before concluding impact.
    """
    return json.dumps(indexed(repo).related(symbol, direction, limit, offset))


@tool
def docstring_search(query: str, repo: str = "", semantic: bool = False, limit: int = 10) -> str:
    """Search current Python symbol names/docstrings with source ids, hashes and line citations.

    Default lexical search needs no model. semantic=true uses explicitly configured embeddings
    and searches only completed vectors; inspect index_status for pending work or provider errors.
    Query is 1–1000 characters and limit 1–20. Semantic scores are cosine similarity, not confidence;
    lexical scores are SQLite BM25 (lower is better). Read cited source before relying on a hit.
    """
    index = indexed(repo)
    result = index.search(query, semantic, limit)
    result["index"] = index.status()
    return json.dumps(result)


TOOLS = (repositories, status, log, diff, search, index_status, symbols, read_symbol, related, docstring_search)


def main():
    """Run the compatible standalone code MCP host, with optional index settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--settings", help="TOML file containing [code] repository/index/embedding settings")
    parser.add_argument("--http", type=int)
    parser.add_argument("--key-file")
    args = parser.parse_args()
    if (args.http is None) != (args.key_file is None):
        parser.error("--http and --key-file go together")
    settings = tomllib.loads(Path(args.settings).read_text()).get("code", {}) if args.settings else {}
    if args.folders:
        settings["repositories"] = args.folders
    setup(settings)
    try:
        serve("code", *TOOLS, http=(args.http, args.key_file) if args.http is not None else None)
    finally:
        for index in INDEXES.values():
            index.close()


if __name__ == "__main__":
    main()
