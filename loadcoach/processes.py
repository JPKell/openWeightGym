"""Linux ownership of one engine session, including children orphaned by wrapper exit."""
import asyncio
import ctypes
import os
from pathlib import Path
import signal
import sys
import time


def adopt_orphans() -> None:
    """Adopt grandchildren so SIGKILL of a wrapper cannot strand native child zombies."""
    if (sys.platform != "linux" or not hasattr(os, "pidfd_open") or not hasattr(os, "P_PIDFD")
            or not hasattr(signal, "pidfd_send_signal")):
        raise ValueError("Owned engine process groups require Linux with pidfd support.")
    descriptor = os.pidfd_open(os.getpid())  # fail before spawning on an unsupported kernel
    os.close(descriptor)
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_CHILD_SUBREAPER: only requested for an owned engine, never for legacy launches.
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot establish engine child ownership")


class ProcessGroup:
    """A fresh session whose leader PID is also its process-group/session ID.

    Reviewed engines must keep children in this session. This is lifecycle ownership,
    not a sandbox or a memory cgroup. Native code must separately enforce its RAM cap.
    """
    def __init__(self, process):
        """Capture the owned session leader identity before tracking its descendants."""
        self.process = process
        self.closed = False
        self.created_tick = time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK")
        leader = self._stat(process.pid)
        self.leader_start = leader[19] if leader else None

    @staticmethod
    def _stat(pid):
        """Read Linux process status fields, tolerating a process that already exited."""
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None

    def members(self) -> dict[int, str]:
        """Enumerate only processes still belonging to the owned session."""
        if self.closed:
            return {}
        leader = self._stat(self.process.pid)
        if leader and (self.leader_start is not None and leader[19] != self.leader_start or
                       int(leader[19]) > self.created_tick):
            # Linux cannot reuse the group ID while original members remain. A replaced
            # leader means this session is gone; never signal the new occupant of its PID.
            self.closed = True
            return {}
        found = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                columns = path.read_text().rsplit(")", 1)[1].split()
                if int(columns[2]) == self.process.pid and int(columns[3]) == self.process.pid:
                    found[int(path.parent.name)] = columns[0]
            except (OSError, ValueError, IndexError):
                continue  # exited between enumeration and read
        return found

    def signal(self, value: int) -> None:
        # Signal stable PID descriptors, not a numeric killpg target that might be recycled
        # after the last member exits between enumeration and signalling.
        """Signal verified session members through stable PID descriptors."""
        for pid in self.members():
            descriptor = None
            try:
                descriptor = os.pidfd_open(pid)
                current = self._stat(pid)
                if pid in self.members() and current and int(current[2]) == self.process.pid and int(current[3]) == self.process.pid:
                    signal.pidfd_send_signal(descriptor, value)
            except ProcessLookupError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def reap(self) -> None:
        # asyncio owns the direct child's wait status. Reap only adopted descendants.
        """Reap adopted descendants while leaving the direct child to asyncio."""
        for pid in self.members():
            if pid != self.process.pid:
                descriptor = None
                try:
                    descriptor = os.pidfd_open(pid)
                    if pid in self.members():
                        os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass  # still owned by a living parent, or already exited/reaped
                finally:
                    if descriptor is not None:
                        os.close(descriptor)

    async def wait_empty(self, seconds: float) -> bool:
        """Wait up to the deadline for every owned child to exit and be reaped."""
        deadline = time.monotonic() + seconds
        while True:
            self.reap()
            if not self.members() and self.process.returncode is not None:
                self.closed = True
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(.02)

    async def stop(self, seconds: float) -> None:
        """Terminate the owned session and retain its reservation if children survive."""
        if self.closed:
            return
        self.signal(signal.SIGTERM)
        if not await self.wait_empty(seconds):
            self.signal(signal.SIGKILL)
            if not await self.wait_empty(5):
                raise RuntimeError("Engine children did not exit; resource reservation remains held.")
        await self.process.wait()
