"""Bounded, text-free call activity and facts about the gateway's actual listeners."""

from array import array
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import time


def stamp(at=None):
    """Format a recorded or current instant as an explicit UTC timestamp."""
    return datetime.fromtimestamp(time.time() if at is None else at, timezone.utc).isoformat(timespec='seconds')


def version():
    """Read the gateway checkout's Git version with a bounded command."""
    try:
        commit = subprocess.run(['git', '-C', str(Path(__file__).resolve().parents[1]), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = 'unknown'
    return {'commit': commit, 'started_at': stamp(), 'started': time.time()}


def bound(listeners, port):
    """Check whether a supplied listener accepts connections on the selected port."""
    for listener in listeners:
        try:
            if listener.getsockname()[1] == port and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                return True
        except OSError:
            pass
    return False


class Window:
    """Exact one-second counters for sliding 1 h/24 h windows; fixed memory per number."""
    size = 86400

    def __init__(self):
        """Initialize the fixed ring of accepted and rejected attempt counters."""
        self.accepted = array('I', [0]) * self.size
        self.rejected = array('I', [0]) * self.size
        self.last = int(time.time())

    def advance(self):
        """Advance the fixed counter ring, clearing elapsed slots."""
        now = int(time.time())
        if now-self.last >= self.size:
            self.accepted = array('I', [0]) * self.size
            self.rejected = array('I', [0]) * self.size
        else:
            for second in range(self.last+1, now+1):
                self.accepted[second % self.size] = self.rejected[second % self.size] = 0
        self.last = max(now, self.last)
        return now % self.size

    def record(self, outcome):
        """Record one accepted or rejected attempt in bounded status history."""
        if outcome not in ('accepted', 'rejected'):
            return
        index = self.advance()
        values = self.accepted if outcome == 'accepted' else self.rejected
        values[index] = min(2**32-1, values[index]+1)

    def snapshot(self):
        """Return recent attempt counts and bounded entry history."""
        index = self.advance()
        def hour(values):
            """Sum the latest hour across the counter ring's wrap boundary."""
            start = (index-3599) % self.size
            return sum(values[start:index+1]) if start <= index else sum(values[start:])+sum(values[:index+1])
        return {'hour': {'accepted': hour(self.accepted), 'rejected': hour(self.rejected)},
                'day': {'accepted': sum(self.accepted), 'rejected': sum(self.rejected)}}


class PhoneActivity:
    """At most 256 number windows, and 50 masked attempts per window."""
    def __init__(self):
        """Initialize bounded voice-entry counters and recent-attempt storage."""
        self.rows = OrderedDict()

    def record(self, key, row):
        """Record one accepted or rejected attempt in bounded status history."""
        if key not in self.rows:
            if len(self.rows) >= 256:
                self.rows.popitem(last=False)
            self.rows[key] = Window(), deque(maxlen=50)
        window, ring = self.rows[key]
        self.rows.move_to_end(key)
        window.record(row['outcome'])
        ring.append(row)
        return ring

    def snapshot(self, key):
        """Return recent attempt counts and bounded entry history."""
        if key not in self.rows:
            return {'hour': {'accepted': 0, 'rejected': 0}, 'day': {'accepted': 0, 'rejected': 0}, 'attempts': []}
        window, ring = self.rows[key]
        return {**window.snapshot(), 'attempts': [dict(row, at=stamp(row['at'])) for row in reversed(ring) if row['at'] > time.time()-86400]}
