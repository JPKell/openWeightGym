"""How long since someone last touched a key or the mouse at this machine's own screen.

Every system keeps that count for itself, and spotter only asks for it. It never reads the
keyboard or the mouse, never learns what was typed, and needs no permission to ask. Each system
is asked in its own way:

- Windows: GetLastInputInfo, for the session spotter runs in.
- macOS: the HID system's HIDIdleTime, the same for every account.
- Linux: this account's desktop. GNOME answers over D-Bus. Hyprland, Sway, KDE, niri, COSMIC and
  the other Wayland desktops answer through the standard ext-idle-notify protocol, and an X11
  desktop through its screensaver extension. logind is asked as well, for the local sessions of
  other accounts, and of this one when its desktop said nothing: GNOME, KDE and text consoles
  tell it when they go unused, and Hyprland and Sway do not.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import functools
import os
import re
import struct
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE
from pathlib import Path

IDLE = 10  # seconds unused before a Wayland desktop says so: how closely the count is known there, and no closer
AGAIN = 60  # seconds before a Wayland desktop that could not be asked is looked for again, as one may since have started


async def at_the_screen() -> float | None:
    """Seconds since a key or the mouse was last touched at this machine's own screen. None if nothing here would say."""
    if sys.platform == "win32":
        return windows()
    if sys.platform == "darwin":
        return await macos()
    own = await gnome()
    if own is None:
        own = wayland.seconds()
    if own is None and not wayland_sockets():  # under Wayland, X11 sees only the older programs, and would call a busy screen unused
        own = await asyncio.to_thread(x11, await session_environment())
    known = [seconds for seconds in (own, await logind(others_only=own is not None)) if seconds is not None]
    return min(known, default=None)


async def asked(*command: str) -> bytes | None:
    """What a command printed, or None if it could not be run, failed, or took over five seconds."""
    try:
        running = await asyncio.create_subprocess_exec(*command, stdout=PIPE, stderr=DEVNULL)
    except OSError:
        return None
    try:
        printed, _ = await asyncio.wait_for(running.communicate(), 5)
    except TimeoutError:
        running.kill()
        return None
    return None if running.returncode else printed


# ---- Windows and macOS -----------------------------------------------------------------------


def windows() -> float | None:
    """The count Windows keeps for the session spotter runs in. A Windows service runs in a session with no screen, and is told nothing."""
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32  # type: ignore[attr-defined]
    session = ctypes.c_uint()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)) or session.value == 0:
        return None

    class LastInput(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint), ("at", ctypes.c_uint)]

    last = LastInput(ctypes.sizeof(LastInput))
    if not user32.GetLastInputInfo(ctypes.byref(last)):
        return None
    return ((kernel32.GetTickCount() - last.at) & 0xFFFFFFFF) / 1000  # both are thousandths since the start, and wrap at 49 days


async def macos() -> float | None:
    """The count macOS keeps, in billionths of a second. Any account may read it, a background service too."""
    printed = await asked("ioreg", "-c", "IOHIDSystem")
    found = printed and re.search(rb'"HIDIdleTime" = (\d+)', printed)
    return int(found[1]) / 1e9 if found else None


# ---- Linux: this account's desktop -----------------------------------------------------------


async def gnome() -> float | None:
    """GNOME's count, asked of its compositor over this account's D-Bus."""
    printed = await asked("gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor", "--object-path",
                          "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime")  # fmt: skip
    found = printed and re.search(rb"uint64 (\d+)", printed)
    return int(found[1]) / 1000 if found else None  # it answers in thousandths of a second


def wayland_sockets() -> list[Path]:
    """Where a Wayland desktop of this account may be listening: the one this program was told of first, then any other."""
    folder = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    named = [folder / os.environ["WAYLAND_DISPLAY"]] if os.environ.get("WAYLAND_DISPLAY") else []
    # spotter usually starts before anyone signs in, so it is seldom told; the desktop's socket is looked for instead.
    return named + sorted(path for path in folder.glob("wayland-*") if path.suffix != ".lock" and path not in named)


def message(sender: int, opcode: int, *arguments: bytes) -> bytes:
    """One request in Wayland's own form: the object it is to, which request, its length, and what comes with it."""
    body = b"".join(arguments)
    return struct.pack("=II", sender, (8 + len(body)) << 16 | opcode) + body


def number(value: int) -> bytes:
    """A whole number as Wayland sends one: four bytes, in this machine's own order."""
    return struct.pack("=I", value)


def text(value: str) -> bytes:
    """Words as Wayland sends them: their length, then them, ended by a nought and padded to four bytes."""
    raw = value.encode() + b"\0"
    return number(len(raw)) + raw + b"\0" * (-len(raw) % 4)


async def event(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """The next event from the desktop: the object that sent it, which event it is, and what came with it."""
    sender, header = struct.unpack("=II", await reader.readexactly(8))
    return sender, header & 0xFFFF, await reader.readexactly((header >> 16) - 8)


class Wayland:
    """A Wayland desktop's own word on whether its screen is in use, through the ext-idle-notify protocol.

    The desktop says when nothing has been touched for IDLE seconds, and when something is touched
    again, and nothing more. Nobody can ask it how long it has been, so the question is put once and
    the line held open while spotter runs, as a screen locker holds it.
    """

    def __init__(self) -> None:
        self.listening: asyncio.Task | None = None
        self.looked = -AGAIN  # when a desktop was last looked for, on the clock that only goes forward
        self.answering: float | None = None  # when a desktop agreed to say
        self.unused_since: float | None = None  # when it said nothing had been touched, or None while something is

    def seconds(self) -> float | None:
        """Seconds since the screen was last used, or None if no desktop here answers. Starts looking, if it is time to."""
        if (self.listening is None or self.listening.done()) and time.monotonic() - self.looked >= AGAIN:
            self.looked = time.monotonic()
            self.listening = asyncio.create_task(self.listen())
        if self.unused_since is not None:
            return time.time() - self.unused_since + IDLE
        # Silence means something was touched in the last IDLE seconds, once it has been asked that long.
        return 0.0 if self.answering and time.time() - self.answering >= IDLE else None

    async def listen(self) -> None:
        """Ask each desktop found in turn, and keep listening to the first that can say. Until it goes, as it does at signing out."""
        for path in wayland_sockets():
            try:
                reader, writer = await asyncio.open_unix_connection(path)
            except OSError:
                continue  # left behind by a desktop that has gone
            try:
                if await self.follow(reader, writer):
                    return
            except (OSError, asyncio.IncompleteReadError, struct.error, UnicodeError):
                return
            finally:
                writer.close()
                self.answering = self.unused_since = None

    async def follow(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Put the question to one desktop and hear its answers. False if it cannot answer it."""
        # Objects are numbered by whoever makes them, in turn. 1 is the desktop itself. Ask it for
        # the list of what it offers (2), then to say when the list has all come (3).
        writer.write(message(1, 1, number(2)) + message(1, 0, number(3)))
        offered: dict[str, tuple[int, int]] = {}  # each thing on offer: the desktop's number for it, and its version
        while (said := await event(reader))[0] != 3:
            sender, kind, body = said
            if sender == 1 and kind == 0:
                return False  # the desktop refused something
            if sender == 2 and kind == 0:  # its number for the thing, the thing's name, padded to four, and its version
                name, length = struct.unpack_from("=II", body)
                (version,) = struct.unpack_from("=I", body, 8 + (length + 3) // 4 * 4)
                offered[body[8 : 8 + length - 1].decode()] = (name, version)
        if "wl_seat" not in offered or "ext_idle_notifier_v1" not in offered:
            return False
        seat, (notifier, version) = offered["wl_seat"][0], offered["ext_idle_notifier_v1"]
        version = min(version, 2)
        # Take a seat (4) and the notifier (5), and ask to be told when the seat goes IDLE seconds
        # untouched (6). From version 2 that is the keys and the mouse alone, as everywhere else,
        # and not also a program asking the screen to stay on, such as a video playing.
        writer.write(message(2, 0, number(seat), text("wl_seat"), number(1), number(4))
                     + message(2, 0, number(notifier), text("ext_idle_notifier_v1"), number(version), number(5))
                     + message(5, 2 if version >= 2 else 1, number(6), number(IDLE * 1000), number(4)))  # fmt: skip
        self.answering = time.time()
        while True:
            sender, kind, _ = await event(reader)
            if sender == 1 and kind == 0:
                return True
            if sender == 6:  # 0: nothing has been touched for IDLE seconds. 1: something has been since.
                self.unused_since = time.time() if kind == 0 else None


wayland = Wayland()  # one line to the desktop for the whole program


async def session_environment() -> dict[str, str]:
    """What this account's desktop told its services about itself on signing in, which spotter, started earlier, was not told."""
    printed = await asked("systemctl", "--user", "show-environment") or b""
    return dict(line.split("=", 1) for line in printed.decode(errors="replace").splitlines() if "=" in line)


class ScreenSaverInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]  # fmt: skip


@functools.cache
def x_libraries() -> tuple[ctypes.CDLL, ctypes.CDLL] | None:
    """X11's own library and its screensaver extension's, if this machine has them."""
    found = ctypes.util.find_library("X11"), ctypes.util.find_library("Xss")
    if not all(found):
        return None
    x, saver = (ctypes.CDLL(name) for name in found)
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
    x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ctypes.c_ulong, [ctypes.c_void_p]
    x.XCloseDisplay.argtypes = x.XFree.argtypes = [ctypes.c_void_p]
    saver.XScreenSaverAllocInfo.restype = ctypes.POINTER(ScreenSaverInfo)
    saver.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ScreenSaverInfo)]
    return x, saver


def x11(session: dict[str, str]) -> float | None:
    """An X11 desktop's own count, from its screensaver extension."""
    display = os.environ.get("DISPLAY") or session.get("DISPLAY")
    if not display or not (libraries := x_libraries()):
        return None
    if "XAUTHORITY" not in os.environ and session.get("XAUTHORITY"):
        os.environ["XAUTHORITY"] = session["XAUTHORITY"]  # the key to the display, which X11 looks for only here
    x, saver = libraries
    if not (opened := x.XOpenDisplay(display.encode())):
        return None
    try:
        info = saver.XScreenSaverAllocInfo()
        try:
            found = saver.XScreenSaverQueryInfo(opened, x.XDefaultRootWindow(opened), info)
            return info.contents.idle / 1000 if found else None
        finally:
            x.XFree(info)
    finally:
        x.XCloseDisplay(opened)


# ---- Linux: logind ---------------------------------------------------------------------------


async def logind(others_only: bool) -> float | None:
    """The least time any session at this machine's own screen has gone unused, as logind was told it. None if none has said.

    A desktop tells logind only once it has gone unused for its own wait, five minutes for GNOME,
    so this is the start of that wait, not the last touch. Sessions signed in from elsewhere, over
    SSH say, are not counted. `others_only` leaves out this account's own, whose desktop was asked.
    """
    printed = await asked("loginctl", "list-sessions", "--no-legend", "--no-pager")
    sessions = [line.split()[0] for line in (printed or b"").decode(errors="replace").splitlines() if line.split()]
    if not sessions:
        return None
    printed = await asked("loginctl", "show-session", *sessions, "--no-pager", "-p", "User", "-p", "Seat", "-p", "Remote", "-p", "IdleHint", "-p", "IdleSinceHint")
    found = []
    for block in (printed or b"").decode(errors="replace").split("\n\n"):
        said = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not said.get("Seat") or said.get("Remote") != "no" or (others_only and said.get("User") == str(os.getuid())):
            continue
        since = int(said.get("IdleSinceHint") or 0) / 1e6  # in millionths of a second since 1970. Nought: never said
        if since:
            found.append(time.time() - since if said.get("IdleHint") == "yes" else 0.0)
    return min(found, default=None)
