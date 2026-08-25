"""Private test-only migration utility; never imported by production code."""
from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path

from hatchet_hermes_bridge import runtime
from hatchet_hermes_bridge.store import connect_migrate as _connect_migrate

# Imported only by test modules.  This private dependency injection keeps all
# runtime and migration fixture connections away from the root-owned production
# lock path under non-root CI.
_test_manager = runtime._LockManager(Path(tempfile.mkdtemp(prefix="bridge-test-lock.")) / "maintenance.lock")
runtime._lock_manager = _test_manager

def connect_migrate(db_path: str):
    """Supply an inherited exclusive capability for an isolated test database."""
    manager = _test_manager
    manager.prepare()
    held = manager.open()
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        try: prior = os.dup(runtime.MAINTENANCE_FD)
        except OSError: prior = None
        os.dup2(held, runtime.MAINTENANCE_FD)
        return _connect_migrate(db_path)
    finally:
        if prior is None: os.close(runtime.MAINTENANCE_FD)
        else: os.dup2(prior, runtime.MAINTENANCE_FD); os.close(prior)
        fcntl.flock(held, fcntl.LOCK_UN); os.close(held)
