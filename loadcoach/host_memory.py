"""Linux host and service RAM observations, in MiB; no change to service limits."""
from pathlib import Path

MIB = 2**20


def reclaimable_memory_charge(current: int, text: str, *, include_active: bool = False) -> int:
    """Credit only clean, unmapped disk cache, never anonymous/kernel RAM.

    Startup defaults to inactive pages; admission may include active disk-cache pages,
    which are also reclaimable after unmapping. Counters overlap: conservatively subtract
    every exclusion from the selected file-LRU set.
    Unknown/malformed statistics give no credit. This never changes or reclaims a limit.
    https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files
    """
    required = {"anon", "kernel", "file", "inactive_file", "shmem", "file_mapped",
                "file_dirty", "file_writeback", "unevictable"}
    if include_active:
        required.add("active_file")
    try:
        pairs = [line.split() for line in text.splitlines()]
        values = {key: int(value) for key, value in pairs}
        if len(values) != len(pairs) or not required <= values.keys() or any(value < 0 for value in values.values()):
            return current
    except (ValueError, TypeError):
        return current
    excluded = sum(values[key] for key in ("shmem", "file_mapped", "file_dirty", "file_writeback", "unevictable"))
    file_lru = values["inactive_file"] + (values["active_file"] if include_active else 0)
    clean = max(0, min(current, file_lru, values["file"]) - excluded)
    credit = min(clean, max(0, current - values["anon"] - values["kernel"]))
    return current - credit


def numbers(path: Path) -> dict[str, int]:
    """Read numeric memory counters, tolerating unavailable host files."""
    try:
        result = {}
        for line in path.read_text().splitlines():
            key, separator, text = line.partition(':')
            cells = text.split()
            if separator and cells and cells[0].isdigit():
                result[key] = int(cells[0])
        return result
    except OSError:
        return {}



def resident_mib(pid: int, *, descendants: bool = True) -> int | None:
    """Current resident pages of this model process and its children (never file size)."""
    pending, seen, total = [pid], set(), 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        status = numbers(Path(f'/proc/{current}/status'))
        if 'VmRSS' not in status:
            return None
        total += status['VmRSS'] * 1024
        if descendants:
            try:
                pending.extend(map(int, Path(f'/proc/{current}/task/{current}/children').read_text().split()))
            except (OSError, ValueError):
                pass
    return (total + MIB - 1) // MIB


def memory_snapshot(proc: Path = Path('/proc'), cgroup: Path = Path('/sys/fs/cgroup')) -> dict:
    """Use MemAvailable and every cgroup-v2 ancestor's high/max limit and current usage.

    Missing host observations fail admission closed. Missing cgroup files mean there is no
    observed v2 ceiling; the operating system's existing enforcement remains authoritative.
    """
    values = numbers(proc / 'meminfo')
    result = {'total': values.get('MemTotal', 0) // 1024,
              'available': values.get('MemAvailable', 0) // 1024,
              'service_available': None, 'service_reclaimable_available': None,
              'service_limit': None, 'service_used': None,
              'service_scope': None}
    try:
        membership = next(line[3:] for line in (proc / 'self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root = cgroup.resolve()
        folder = (root / membership.lstrip('/')).resolve()
        if not folder.is_relative_to(root):
            return result
        own = folder
        while folder.is_relative_to(root):
            try:
                current = int((folder / 'memory.current').read_text().strip())
                limits = []
                for key in ('memory.high', 'memory.max'):
                    value = (folder / key).read_text().strip()
                    if value != 'max':
                        limits.append(int(value))
                if limits:
                    limit = min(limits)
                    try:
                        statistics = (folder / 'memory.stat').read_text()
                        current = max(current, int((folder / 'memory.current').read_text()))
                    except OSError:
                        statistics = ''
                    charged = reclaimable_memory_charge(current, statistics, include_active=True)
                    reclaimable_available = max(0, limit - charged) // MIB
                    if result['service_reclaimable_available'] is None or reclaimable_available < result['service_reclaimable_available']:
                        result['service_reclaimable_available'] = reclaimable_available
                    available = max(0, limit - current) // MIB
                    if result['service_available'] is None or available < result['service_available']:
                        result.update(service_available=available, service_limit=limit // MIB,
                                      service_used=(current + MIB - 1) // MIB,
                                      service_scope='self' if folder == own else 'ancestor')
            except (OSError, ValueError):
                pass
            if folder == root:
                break
            folder = folder.parent
    except (OSError, StopIteration):
        pass
    return result
