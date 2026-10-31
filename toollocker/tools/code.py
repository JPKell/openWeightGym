"""Code tools: look at the git repositories it is given. Nothing is changed.

In a tool folder, they are `repositories` in the `[code]` section of the settings. Alone:

    python -m toollocker.tools.code /home/jpk/ai/suite /home/jpk/repos

Each is a repository, or a folder that holds repositories, in which case every repository
directly inside it is included.

Everything is asked of git, so only files git tracks are searched. A file that was never
committed, such as `.env` or a database, is not seen.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from toollocker.tools import arguments, serve, tool

REPOS: list[str] = []  # set by `setup`


def find_repositories(folder: Path) -> list[str]:
    """`folder` itself if it is a repository, otherwise the repositories directly inside it."""
    folder = folder.resolve()
    if (folder / ".git").exists():
        return [str(folder)]
    # A set, because a link to a repository beside the repository itself is the same one twice.
    return sorted({str(inner.resolve()) for inner in folder.iterdir() if (inner / ".git").exists()})


def setup(settings: dict) -> None:
    """Take the repositories this tool may read: `repositories` in its section of the settings."""
    for folder in settings.get("repositories", []):
        if not Path(folder).expanduser().is_dir():
            raise ValueError(f"code: {folder} is not a folder")
        REPOS.extend(found for found in find_repositories(Path(folder).expanduser()) if found not in REPOS)  # each one once
    if not REPOS:
        raise ValueError("code needs `repositories`, a list of git repositories or folders that hold them; none was found")


def git(repo: str, *arguments: str) -> str:
    """Run one git command in `repo` and return what it printed. Raises ValueError if git says no.

    `repo` is a repository's full path or its folder's name. Empty means the first one.
    """
    where = next((known for known in REPOS if repo in ("", known, Path(known).name)), None)
    if where is None:
        raise ValueError(f"{repo!r} is not a repository this tool may read. They are: {', '.join(REPOS)}")
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


if __name__ == "__main__":
    USAGE = "python -m toollocker.tools.code FOLDER [FOLDER ...] [--http PORT --key-file FILE]"
    folders, http = arguments(USAGE)
    try:
        setup({"repositories": folders})
    except ValueError:
        raise SystemExit(f"usage: {USAGE}   (no repository found)") from None
    serve("code", repositories, status, log, diff, search, http=http)
