"""Per-engine memory cgroups below the worker's existing, delegated service budget."""
import os
from pathlib import Path
import secrets
import re

MIB = 2**20


def delegated_parent(proc: Path = Path("/proc"), mount: Path = Path("/sys/fs/cgroup")) -> Path:
    """Resolve and validate the service's delegated cgroup-v2 memory parent."""
    membership = next((line[3:] for line in (proc/"self/cgroup").read_text().splitlines() if line.startswith("0::")), "")
    root = mount.resolve()
    current = (root/membership.lstrip("/")).resolve()
    if (not membership.startswith("/") or not current.is_relative_to(root) or current.name != "supervisor"
            or not (current.parent.name.endswith(".service") or current.parent == root)):
        raise ValueError("LoadCoach needs a delegated memory subgroup for this engine. Run owg update, then owg restart loadcoach. Manual systemd services need Delegate=memory and DelegateSubgroup=supervisor (systemd 254+); containers need a finite delegated memory cgroup.")
    parent = current.parent
    if (parent/"cgroup.procs").read_text().strip() or "memory" not in (parent/"cgroup.controllers").read_text().split():
        raise ValueError("Worker memory delegation is unavailable or its root still contains processes.")
    limit = (parent/"memory.max").read_text().strip()
    if limit == "max" or int(limit) <= 0 or (parent/"memory.swap.max").read_text().strip() != "0":
        raise ValueError("Bounded engines require an existing finite worker MemoryMax and MemorySwapMax=0.")
    return parent


class MemoryGroup:
    """A generated child only. Never create a sibling service or enlarge an ancestor cap."""
    def __init__(self, path: Path, budget_mib: int):
        """Retain the owned cgroup path, configured memory cap and cleanup state."""
        self.path, self.budget_mib, self.closed = path, budget_mib, False

    @classmethod
    def create(cls, budget_mib: int, *, proc: Path = Path("/proc"), mount: Path = Path("/sys/fs/cgroup")):
        """Create an owned child cgroup with a hard memory cap and no swap."""
        if type(budget_mib) is not int or not 0 < budget_mib <= 2**31:
            raise ValueError("Engine memory budget must be a positive whole number of MiB.")
        parent = delegated_parent(proc, mount)
        budget = budget_mib*MIB
        if budget > int((parent/"memory.max").read_text()):
            raise ValueError("Engine RAM cap exceeds the worker's existing MemoryMax.")
        if "memory" not in (parent/"cgroup.subtree_control").read_text().split():
            (parent/"cgroup.subtree_control").write_text("+memory")
        path = parent/("engine-"+secrets.token_hex(12))
        path.mkdir(mode=0o700)
        try:
            for name, value in (("memory.max", budget), ("memory.high", budget),
                                ("memory.swap.max", 0), ("memory.oom.group", 1)):
                (path/name).write_text(str(value))
            group = cls(path, budget_mib)
            group.verify()
            return group
        except BaseException:
            path.rmdir()  # cgroup controls are virtual files, not recursively deleted
            raise

    def verify(self) -> None:
        """Check that the owned cgroup still enforces its expected memory contract."""
        if self.closed or (self.path/"memory.max").read_text().strip() != str(self.budget_mib*MIB) \
                or (self.path/"memory.swap.max").read_text().strip() != "0" \
                or (self.path/"memory.oom.group").read_text().strip() != "1":
            raise ValueError("Engine memory enforcement changed or is unavailable.")

    def observation(self) -> dict:
        """Read current, peak and limit memory counters from the owned cgroup."""
        return {"current_mib": (int((self.path/"memory.current").read_text())+MIB-1)//MIB,
                "peak_mib": (int((self.path/"memory.peak").read_text())+MIB-1)//MIB,
                "events": dict((key, int(value)) for key, value in
                               (line.split() for line in (self.path/"memory.events.local").read_text().splitlines()))}

    def close(self) -> None:
        """Remove an empty owned memory cgroup after child cleanup."""
        if self.closed:
            return
        events = dict(line.split() for line in (self.path/"cgroup.events").read_text().splitlines())
        if events.get("populated") != "0":
            raise RuntimeError("Engine cgroup still contains processes; reservation remains held.")
        self.path.rmdir()
        self.closed = True


def enter(path: str, budget_mib: int, *, proc: Path = Path("/proc"), mount: Path = Path("/sys/fs/cgroup")) -> None:
    """Called by the child before exec: no native runtime or model has started yet."""
    folder = Path(path).resolve()
    parent = delegated_parent(proc, mount)
    if folder.parent != parent or not re.fullmatch(r"engine-[a-f0-9]{24}", folder.name):
        raise ValueError("Engine cgroup must be a generated child of this worker's delegated service.")
    MemoryGroup(folder, budget_mib).verify()
    (folder/"cgroup.procs").write_text(str(os.getpid()))
    actual = next(line[3:] for line in (proc/"self/cgroup").read_text().splitlines() if line.startswith("0::"))
    if (mount.resolve()/actual.lstrip("/")).resolve() != folder:
        raise ValueError("Engine did not enter its bounded memory cgroup.")
