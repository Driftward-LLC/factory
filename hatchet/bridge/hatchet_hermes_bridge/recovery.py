"""Crash-recoverable, atomic SQLite restoration used only by rollout rollback."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, tempfile
from pathlib import Path
import test_capability


class _OwnershipPolicy:
    uid = 0
    gid = 0

    def accepts(self, stat: os.stat_result) -> bool:
        return stat.st_uid == self.uid and stat.st_gid == self.gid and stat.st_mode & 0o777 == 0o600


# Private test seam only. The public API deliberately exposes no ownership,
# path, CLI, or environment override for production backup restoration.
_ownership_policy = _OwnershipPolicy()
RECOVERY_FAILPOINT_STAGES = {"prepared", "built", "checkpointed", "replaced", "fsynced"}


def _backup_ownership_policy() -> _OwnershipPolicy:
    cap = test_capability.load()
    if cap is None:
        return _ownership_policy
    policy = _OwnershipPolicy()
    policy.uid = cap.uid
    policy.gid = cap.gid
    return policy


def restore_verified_backup(backup: str, db: str, checksum: str, *, failpoint: str | None = None) -> None:
    # The subprocess crash harness has historically used the explicit `none`
    # sentinel for a normal recovery run. Normalize it before strict checking.
    if failpoint == "none":
        failpoint = None
    if failpoint is not None and failpoint not in RECOVERY_FAILPOINT_STAGES:
        raise ValueError(f"unknown recovery failpoint: {failpoint}")
    policy = _backup_ownership_policy()
    source_path, db_path = Path(backup), Path(db)
    source_stat = source_path.stat()
    if (
        not re.fullmatch(r"bridge-\d{8}T\d{6}Z-before-[0-9a-f]{40}\.sqlite", source_path.name)
        or not policy.accepts(source_stat)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        or _digest(source_path) != checksum
    ):
        raise RuntimeError("verified backup checksum or mode mismatch")
    journal = db_path.with_suffix(db_path.suffix + ".restore-journal")
    _write_journal(journal, {"backup": str(source_path), "target": str(db_path), "phase": "prepared"})
    _crash(failpoint, "prepared")
    fd, temp_name = tempfile.mkstemp(prefix=".bridge-restore.", dir=db_path.parent); os.close(fd)
    temp = Path(temp_name)
    try:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True); dest = sqlite3.connect(temp)
        try:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise RuntimeError("restored database integrity failed")
        finally: dest.close(); source.close()
        _fsync(temp); _crash(failpoint, "built")
        # Quiescence/maintenance lock are established by the caller. Checkpoint
        # any old live WAL before replacing, then remove only stopped sidecars.
        if db_path.exists():
            live = sqlite3.connect(db_path)
            try: live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally: live.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists(): sidecar.unlink()
        _write_journal(journal, {"backup": str(source_path), "target": str(db_path), "phase": "checkpointed"})
        _crash(failpoint, "checkpointed")
        os.replace(temp, db_path); _fsync(db_path); _fsync_dir(db_path.parent)
        _write_journal(journal, {"backup": str(source_path), "target": str(db_path), "phase": "replaced"})
        _crash(failpoint, "replaced")
        check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise RuntimeError("reopened database integrity failed")
        finally: check.close()
        journal.unlink(); _fsync_dir(db_path.parent); _crash(failpoint, "fsynced")
    finally:
        if temp.exists(): temp.unlink()


def _digest(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()
def _write_journal(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp"); temporary.write_text(json.dumps(value, sort_keys=True)); _fsync(temporary); os.replace(temporary, path); _fsync_dir(path.parent)
def _fsync(path: Path) -> None:
    fd=os.open(path, os.O_RDONLY); os.fsync(fd); os.close(fd)
def _fsync_dir(path: Path) -> None:
    fd=os.open(path, os.O_RDONLY); os.fsync(fd); os.close(fd)
def _crash(selected: str | None, stage: str) -> None:
    if selected == stage: os._exit(97)
