"""Authoritative writer inventory and fixed advisory-lock maintenance barrier."""
from __future__ import annotations
import fcntl, os
from pathlib import Path
import test_capability

_FIXED_MAINTENANCE_LOCK = Path("/run/driftward-hatchet-bridge/maintenance.lock")
MAINTENANCE_FD = 3  # fixed inherited capability; never environment/CLI selected
DIRECT_DB_WRITERS = ("driftward-hatchet-bridge.service","driftward-hatchet-bridge-reconciler.service","driftward-hatchet-bridge-reconciler.timer","driftward-factory-outbox-dispatcher.service","driftward-factory-outbox-dispatcher.timer","driftward-factory-operator.service")
HTTP_MUTATION_CLIENTS = ("driftward-hatchet-worker-bridge-executor.service","driftward-hatchet-worker-reconciliation.service")
WRITER_UNITS = DIRECT_DB_WRITERS + HTTP_MUTATION_CLIENTS
# Keep this deliberately explicit: a new application caller of connect_runtime
# must be classified before it can ship.  Tests compare this inventory to the
# source tree instead of relying on a best-effort operational audit.
WRITABLE_CALLER_INVENTORY = {
    "hatchet_hermes_bridge.server": "driftward-hatchet-bridge.service",
    "hatchet_hermes_bridge.operator": "driftward-factory-operator.service",
    "hatchet_hermes_bridge.cli": "operator-cli",
}

def _maintenance_lock() -> Path:
    cap = test_capability.load()
    return cap.runtime / "maintenance.lock" if cap is not None else _FIXED_MAINTENANCE_LOCK


class _LockManager:
    def __init__(self, path: Path): self.path = path
    def prepare(self) -> None: self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    def stat(self): return os.stat(self.path)
    def open(self) -> int: return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)

# Private test code may patch this dependency. Public runtime/CLI APIs accept
# neither a lock path nor a bypass and always use this fixed manager in prod.
_lock_manager = _LockManager(_maintenance_lock())

def acquire_shared() -> int:
    _lock_manager.prepare()
    fd = _lock_manager.open()
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError("bridge maintenance is in progress; writable runtime access is unavailable")
    return fd

def migration_capability() -> int:
    """Duplicate the installer-owned exclusive lock descriptor.

    The fixed descriptor is a capability, not a user-selectable bypass.  A
    duplicate retains the same open-file description and therefore the
    installer's flock ownership for the complete migration connection lifetime.
    """
    try:
        fd = os.dup(MAINTENANCE_FD)
    except OSError as exc:
        raise RuntimeError("installer exclusive maintenance capability is required") from exc
    try:
        capability_stat = os.fstat(fd)
        lock_stat = _lock_manager.stat()
        if (capability_stat.st_dev, capability_stat.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
            raise RuntimeError("installer maintenance capability inode mismatch")
        # This succeeds only for the inherited exclusive open-file description
        # (or an equivalent root-operated explicit lock, outside runtime scope).
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        raise
    return fd

def release(fd: int) -> None:
    try: fcntl.flock(fd, fcntl.LOCK_UN)
    finally: os.close(fd)
