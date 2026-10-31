"""File tools: list and read what is under the folders it is given. Nothing is written.

In a tool folder, its folders are `folders` in the `[files]` section of the settings. Alone:

    python -m toollocker.tools.files /home/jpk/notes /home/jpk/projects/site

Two kinds of file are never shown, wherever they are: hidden ones, whose name starts with a dot,
and private ones, which only their owner may read. So `.env`, `.git`, `.ssh`, key files and
LoadCoach's own database stay out of reach even inside a folder that is named.
"""

from __future__ import annotations

from pathlib import Path

from toollocker.tools import arguments, serve, tool

ROOTS: list[Path] = []  # set by `setup`
MAX_READ = 256 * 1024  # characters; also bounds a single newline-free input line


def setup(settings: dict) -> None:
    """Take the folders this tool may read: `folders` in its section of the settings."""
    ROOTS[:] = [Path(folder).expanduser().resolve() for folder in settings.get("folders", [])]
    if not ROOTS or not all(root.is_dir() for root in ROOTS):
        raise ValueError("files needs `folders`, a list of folders that exist")


def inside(path: str) -> Path:
    """The real place `path` leads to, if this tool may show it. Raises ValueError if not.

    The real place is where it ends up once every link and `..` has been followed, so a link
    that points out of the folders is refused like any other path outside them.
    """
    real = Path(path).resolve()
    for root in ROOTS:
        if real.is_relative_to(root):
            below = real.relative_to(root).parts
            # Every step from the folder down to the file must be shown, not only the last.
            for depth, part in enumerate(below, 1):
                step = root.joinpath(*below[:depth])
                if part.startswith(".") or not step.exists() or not step.stat().st_mode & 0o004:
                    raise ValueError(f"{path} does not exist, or is hidden or private.")
            return real
    raise ValueError(f"{path} is outside the folders this tool may read: {', '.join(map(str, ROOTS))}")


@tool
def list_folder(folder: str = "") -> str:
    """List what is in a folder, by full path. Folders end in a slash; files show their size in bytes.

    Without `folder`, lists the folders this tool may read.
    """
    if not folder:
        return "\n".join(f"{root}/" for root in ROOTS)
    real = inside(folder)
    if not real.is_dir():
        raise ValueError(f"{folder} is not a folder.")
    lines = []
    for entry in sorted(real.iterdir()):
        try:
            inside(str(entry))
        except ValueError:
            continue  # hidden, private, or a link that leads outside
        lines.append(f"{entry}/" if entry.is_dir() else f"{entry}  {entry.stat().st_size}")
    return "\n".join(lines) or "Nothing there."


@tool
def read_file(file: str, start: int = 1, lines: int = 400) -> str:
    """Read a text file, by full path. Each line comes back with its number in front.

    Returns `lines` lines beginning at line `start`. To read on, ask again with a later `start`.
    Requests allow at most 2,000 lines and 262,144 characters, starting within 1,000,000 lines.
    """
    real = inside(file)
    if not real.is_file():
        raise ValueError(f"{file} is not a file.")
    first = max(int(start), 1)
    count = int(lines)
    if first > 1_000_000 or not 1 <= count <= 2000:
        raise ValueError("Read 1–2000 lines, starting within the first 1,000,000 lines.")
    result, total = [], 0
    with real.open(errors="replace") as text:
        for number in range(1, first + count):
            line = text.readline(MAX_READ + 1)
            if not line:
                break
            if len(line) > MAX_READ:
                raise ValueError("A line exceeds the text read limit of 262,144 characters.")
            if number >= first:
                total += len(line)
                if total > MAX_READ:
                    raise ValueError("The excerpt exceeds 262,144 characters. Request fewer lines.")
                result.append(f"{number}\t{line}")
    return "".join(result) or "No lines there."


if __name__ == "__main__":
    USAGE = "python -m toollocker.tools.files FOLDER [FOLDER ...] [--http PORT --key-file FILE]"
    folders, http = arguments(USAGE)
    try:
        setup({"folders": folders})
    except ValueError:
        raise SystemExit("usage: " + USAGE) from None
    serve("files", list_folder, read_file, http=http)
