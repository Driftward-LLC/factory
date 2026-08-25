#!/usr/bin/env python3
"""Proof and capability handling for the one-way safe-down recovery.

The public command only accepts evidence identifiers.  Production paths are
constants; tests exercise the private ``Paths`` object rather than redirecting
the production command with environment variables.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from barrier_unit_state import UnitStateError, systemctl_value, unit_is_absent
import test_capability

STATE = Path("/var/lib/driftward-hatchet-bridge")
BACKUPS = Path("/root/driftward-backups/hatchet")
DB = STATE / "bridge.db"
MARKER = STATE / "recovery-blocked"
ARCHIVE = STATE / "recovery-incidents"
TRANSITION = STATE / "safe-down-transition.json"
RELEASE_ROOT = Path("/docker/driftward/hatchet/releases/bridge")
ROUTING = Path("/etc/driftward/hatchet-routing-registry.json")
LOCK = Path("/run/driftward-hatchet-bridge/maintenance.lock")
_BARRIER_EVIDENCE_ENV = "DRIFTWARD_SAFE_DOWN_BARRIER_EVIDENCE"
BACKUP_NAME = re.compile(r"bridge-\d{8}T\d{6}Z-before-([0-9a-f]{40})\.sqlite\Z")
SHA_LINE = re.compile(r"([0-9a-f]{64})\s+\*?([^\s]+)\Z")
MARKER_TEXT = b"rollback recovery blocked: stop-writers restore-services\n"
ROLLBACK_MARKER_REASON = "safe-down fresh-install-failed"
ROLLBACK_MARKER_TEXT = f"rollback recovery blocked: {ROLLBACK_MARKER_REASON}\n".encode()
TRANSITION_VERSION = 1
SUPERSEDE_MIN_VERSION = 2
TRANSITION_DECISION = "safe_down_fresh_install"
MARKER_DESCENDANT_VERSION = 1
MARKER_LINEAGE_VERSION = 1

# One inventory feeds staging, rollback, proof and tests.  Python owns the
# descriptor-safe proof, but it does not carry a second hand-maintained list.
INVENTORY = json.loads((Path(__file__).with_name("publication-targets.json")).read_text())
TARGETS = tuple(INVENTORY["targets"] + [f"/etc/systemd/system/{unit}" for unit in INVENTORY["units"]])
SOURCE_REPOSITORY = "Driftward-LLC/factory"
SOURCE_REPOSITORY_PATH = Path("/root/driftward/repos/factory")


class ProofError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    state: Path = STATE
    backups: Path = BACKUPS
    db: Path = DB
    marker: Path = MARKER
    archive: Path = ARCHIVE
    transition: Path = TRANSITION
    release_root: Path = RELEASE_ROOT
    routing: Path = ROUTING
    lock: Path = LOCK
    targets: tuple[str, ...] = TARGETS
    repository: Path = SOURCE_REPOSITORY_PATH
    # Private test seam.  The production CLI always constructs Paths() and is
    # therefore fixed to root:root; no public flag or environment can weaken
    # this policy.
    expected_uid: int = 0
    expected_gid: int = 0


def _command_paths() -> Paths:
    """Fixed production paths, or the FD-bound private test namespace."""
    cap = test_capability.load()
    if cap is None: return Paths()
    return Paths(
        state=cap.state, backups=cap.backups, db=cap.state / "bridge.db", marker=cap.state / "recovery-blocked",
        archive=cap.state / "recovery-incidents", transition=cap.state / "safe-down-transition.json", release_root=cap.release,
        routing=cap.etc / "hatchet-routing-registry.json", lock=cap.runtime / "maintenance.lock",
        targets=tuple(str(cap.root / value.lstrip("/")) for value in TARGETS), repository=cap.repository,
        expected_uid=cap.uid, expected_gid=cap.gid,
    )


def _code_commit(paths: Paths) -> str:
    cap = test_capability.load()
    if cap is not None: return cap.release_version
    return subprocess.check_output(["git", "-C", str(paths.repository), "rev-parse", "HEAD"], text=True).strip()


def _regular_root_0600(fd: int, label: str, *, allow_links: bool = False, expected_uid: int = 0, expected_gid: int = 0) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_gid != expected_gid or stat.S_IMODE(info.st_mode) != 0o600 or (not allow_links and info.st_nlink != 1):
        raise ProofError(f"unsafe {label} metadata")
    return info


def _open_held(path: Path, label: str, *, allow_links: bool = False, expected_uid: int = 0, expected_gid: int = 0) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ProofError(f"cannot open {label}") from exc
    try:
        return fd, _regular_root_0600(fd, label, allow_links=allow_links, expected_uid=expected_uid, expected_gid=expected_gid)
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        part = os.read(fd, 1024 * 1024)
        if not part:
            return b"".join(chunks)
        chunks.append(part)


def _hash(fd: int) -> str:
    return hashlib.sha256(_read(fd)).hexdigest()


def _canonical_backup(value: str, paths: Paths) -> Path:
    supplied = Path(value)
    if ".." in supplied.parts:
        raise ProofError("backup traversal")
    try:
        if stat.S_ISLNK(os.lstat(supplied).st_mode):
            raise ProofError("backup symlink")
    except FileNotFoundError as exc:
        raise ProofError("backup is missing") from exc
    resolved = supplied.resolve(strict=True)
    if resolved.parent != paths.backups.resolve(strict=True) or not BACKUP_NAME.fullmatch(resolved.name):
        raise ProofError("backup is not a canonical protected backup")
    return resolved


def _matches_checksum_sidecar(contents: bytes, checksum: str, backup: Path) -> bool:
    """Accept only the two literal sha256sum names for a proven backup.

    ``backup`` has already passed the protected-directory, no-symlink, and
    filename proof.  The sidecar field remains opaque text: it is never
    resolved or otherwise interpreted.  GNU ``sha256sum`` may emit either the
    basename or the absolute path it was given, with an optional binary marker.
    """
    try:
        line = contents.decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    # Keep compatibility with a final newline while rejecting a second/extra
    # entry.  The filename grammar itself does not permit embedded whitespace.
    if line.endswith("\n"):
        line = line[:-1]
    if "\n" in line:
        return False
    parsed = SHA_LINE.fullmatch(line)
    return bool(
        parsed
        and parsed.group(1) == checksum
        and parsed.group(2) in (backup.name, str(backup))
    )


def _sqlite_digest_fd(fd: int) -> dict[str, Any]:
    """Digest an already-open SQLite file without reopening its pathname."""
    uri = f"file:/proc/self/fd/{fd}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        if con.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ProofError("sqlite integrity check failed")
        if con.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise ProofError("safe-down proof requires schema zero")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_import_conflict_resolutions'").fetchone():
            raise ProofError("resolution schema proves migration")
        tables = ("bridge_jobs", "factory_attempts", "factory_execution_leases", "factory_attempt_outbox", "factory_inspection_outbox", "factory_attempt_events", "factory_import_conflicts")
        digest: dict[str, Any] = {"schema": 0, "tables": {}}
        for table in tables:
            rows = con.execute(f"SELECT * FROM {table}").fetchall()
            encoded = sorted(hashlib.sha256(json.dumps(tuple(row), default=str, separators=(",", ":")).encode()).hexdigest() for row in rows)
            digest["tables"][table] = {"count": len(rows), "digest": hashlib.sha256("".join(encoded).encode()).hexdigest()}
        active = (
            con.execute("SELECT COUNT(*) FROM bridge_jobs WHERE state IN ('accepted','running','effect_committed','verification_pending')").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM factory_attempts WHERE status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','cancellation_requested','effect_outcome_unknown')").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM factory_execution_leases WHERE released_at IS NULL").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM factory_attempt_outbox WHERE acknowledged_at IS NULL").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM factory_inspection_outbox WHERE acknowledged_at IS NULL").fetchone()[0],
        )
        if any(active):
            raise ProofError("safe-down proof requires zero active work")
        return digest
    finally:
        con.close()


def _sqlite_digest(path: Path, paths: Paths, label: str) -> dict[str, Any]:
    fd, _ = _open_held(path, label, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        digest = _sqlite_digest_fd(fd)
        _ensure_fd_matches(fd, path, label)
        return digest
    finally:
        os.close(fd)


def _target_state(path: Path, marker_ns: int, paths: Paths, *, require_pre_marker: bool = True) -> dict[str, Any]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"path": str(path), "state": "absent"}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProofError(f"unsafe publication target {path}")
    if info.st_uid != paths.expected_uid or info.st_gid != paths.expected_gid or info.st_nlink != 1:
        raise ProofError(f"unsafe publication target ownership {path}")
    allowed = 0o600 if path.suffix == ".env" else (0o644 if "/systemd/system/" in str(path) else 0o640)
    if stat.S_IMODE(info.st_mode) != allowed or (require_pre_marker and (info.st_mtime_ns >= marker_ns or info.st_ctime_ns >= marker_ns)):
        raise ProofError(f"unexpected publication target state {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
            raise ProofError(f"publication target changed while opening {path}")
        return {"path": str(path), "state": "present", "mode": allowed, "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "sha256": _hash(fd)}
    finally:
        os.close(fd)


def _publication_state(paths: Paths, marker_ns: int, failed_commit: str, *, require_pre_marker: bool = True) -> tuple[list[dict[str, Any]], str]:
    states: list[dict[str, Any]] = []
    required_absent = {str(paths.routing), str(paths.release_root / "current"), str(paths.release_root / failed_commit)}
    for item in (*paths.targets, str(paths.release_root / failed_commit)):
        state = _target_state(Path(item), marker_ns, paths, require_pre_marker=require_pre_marker)
        if state["path"] in required_absent and state["state"] != "absent":
            raise ProofError(f"pre-publication target exists: {state['path']}")
        states.append(state)
    canonical = json.dumps(states, sort_keys=True, separators=(",", ":")).encode()
    return states, hashlib.sha256(canonical).hexdigest()


def _publication_semantic_state(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rollback cannot restore ctime, but retains mtime and all other proof."""
    return [{key: value for key, value in state.items() if key != "ctime_ns"} for state in states]


def _v1_reconciled_publication_journal(paths: Paths, record: dict[str, Any]) -> bool:
    """Recognize only a completed v1 rollback from publication_commit.py.

    This is a proof input for the post-rollback target comparison, never an
    adoption shortcut: DB and every target are still reread below.
    """
    commit = record.get("code_commit")
    if record.get("version") != TRANSITION_VERSION or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return False
    path = paths.state / f"publication-{commit}.journal"
    try:
        fd, _ = _open_held(path, "safe-down publication journal", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
        try:
            raw = _read(fd)
        finally:
            os.close(fd)
        if not raw.endswith(b"\n"):
            return False
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (ProofError, UnicodeDecodeError, ValueError):
        return False
    if not rows or not all(isinstance(row, dict) for row in rows) or b"".join(_record_bytes(row) for row in rows) != raw:
        return False
    prepared, terminal = rows[0], rows[-1]
    if prepared.get("status") != "prepared" or prepared.get("commit") != commit or prepared.get("transaction") != commit or not isinstance(prepared.get("snapshot"), dict):
        return False
    if terminal != {"status": "rolled_back", "recovery": "safe_down_retry_fresh_install", "publication_boundary": terminal.get("publication_boundary")} or terminal.get("publication_boundary") not in {"prepared", "applied", "committed"}:
        return False
    # The transaction's snapshot must describe the original exact state.  The
    # transition itself is the independently archived authority used below.
    return True


def _ensure_fd_matches(fd: int, path: Path, label: str) -> os.stat_result:
    try:
        expected = os.stat(path)
        actual = os.fstat(fd)
    except OSError as exc:
        raise ProofError(f"missing {label} capability") from exc
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        raise ProofError(f"{label} capability inode mismatch")
    return actual


def _exclusive_lock(fd: int, paths: Paths) -> None:
    _ensure_fd_matches(fd, paths.lock, "maintenance lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ProofError("maintenance lock is not exclusively held") from exc


def _revalidate_barrier_evidence(paths: Paths) -> None:
    """Reprove the shell-captured barrier FDs immediately before superseding.

    The lock serializes cooperative maintenance writers.  These descriptors
    close their pathname race; they intentionally do not claim to defend
    against a malicious root process.
    """
    try:
        evidence = json.loads(os.environ[_BARRIER_EVIDENCE_ENV]); entries = evidence["entries"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProofError("maintenance barrier evidence is unavailable") from exc
    if evidence.get("version") != 1 or not isinstance(entries, list) or not entries or entries[0].get("kind") != "sentinel":
        raise ProofError("maintenance barrier evidence is malformed")
    units: list[str] = []
    for entry in entries:
        try:
            fd = int(entry["fd"]); path = Path(str(entry["path"])); expected = (int(entry["dev"]), int(entry["ino"]), int(entry["uid"]), int(entry["gid"]), int(entry["mode"])); digest = str(entry["sha256"])
            held = os.fstat(fd); named = os.stat(path, follow_symlinks=False)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ProofError("maintenance barrier descriptor changed") from exc
        if stat.S_ISLNK(named.st_mode) or (held.st_dev, held.st_ino, held.st_uid, held.st_gid, stat.S_IMODE(held.st_mode)) != expected or (named.st_dev, named.st_ino, named.st_uid, named.st_gid) != expected[:4] or _hash(fd) != digest:
            raise ProofError("maintenance barrier descriptor changed")
        if entry.get("kind") == "dropin":
            unit = entry.get("unit")
            if not isinstance(unit, str): raise ProofError("maintenance barrier evidence is malformed")
            try:
                absent = unit_is_absent(unit)
                active = systemctl_value(unit, "ActiveState") if not absent else ""
                dropins = systemctl_value(unit, "DropInPaths", single_line=False) if not absent else ""
            except UnitStateError as exc:
                raise ProofError("maintenance barrier unit-state proof failed") from exc
            if not absent and (active not in ("inactive", "failed") or str(path) not in dropins): raise ProofError("maintenance barrier is no longer quiescent")
            units.append(unit)
    if len(units) != len(set(units)):
        raise ProofError("maintenance barrier evidence is malformed")
    for unit in ("driftward-hatchet-bridge-reconciler.service", "driftward-hatchet-bridge-reconciler.timer"):
        try:
            active = systemctl_value(unit, "ActiveState")
        except UnitStateError as exc:
            raise ProofError("maintenance barrier unit-state proof failed") from exc
        if active != "inactive":
            raise ProofError("reconciler is no longer quiescent")
    result = subprocess.run(["lsof", str(paths.db), str(paths.db.with_name("bridge.db-wal")), str(paths.db.with_name("bridge.db-shm"))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 1:
        raise ProofError("cannot prove bridge DB has no holder")


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        _write_all(fd, data, "new immutable record")
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes, label: str) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise ProofError(f"cannot write {label}")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _matching_publication_journal(paths: Paths, record: dict[str, Any]) -> bool:
    """Permit replay only to roll back this transition's own journal."""
    commit = record.get("code_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit): return False
    path = paths.state / f"publication-{commit}.journal"
    try:
        fd, _ = _open_held(path, "safe-down publication journal", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    except ProofError:
        return False
    try:
        rows = [json.loads(line) for line in _read(fd).decode("utf-8").splitlines()]
    except (UnicodeDecodeError, ValueError):
        return False
    finally:
        os.close(fd)
    return bool(rows and rows[0].get("status") == "prepared" and rows[0].get("commit") == commit)


def _record_for_proof(backup: Path, backup_info: os.stat_result, marker_info: os.stat_result, marker_sha: str, checksum: str, ledger: dict[str, Any], states: list[dict[str, Any]], state_digest: str, code_commit: str, timestamp: str) -> dict[str, Any]:
    identity = f"{marker_sha}-{marker_info.st_dev:x}-{marker_info.st_ino:x}-{marker_info.st_mtime_ns}"
    return {
        "version": TRANSITION_VERSION, "decision": TRANSITION_DECISION, "incident": identity,
        "marker": {"sha256": marker_sha, "dev": marker_info.st_dev, "ino": marker_info.st_ino, "mtime_ns": marker_info.st_mtime_ns},
        "backup": {"path": str(backup), "basename": backup.name, "sha256": checksum, "dev": backup_info.st_dev, "ino": backup_info.st_ino},
        "ledger": ledger, "publication_targets": {"digest": state_digest, "states": states},
        "code_commit": code_commit, "operator": "root", "timestamp": timestamp,
    }


def _handoff(fd: int, destination: int) -> None:
    os.set_inheritable(fd, True)
    if fd != destination:
        os.dup2(fd, destination, inheritable=True)


def _record_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _record_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _handoff_recovery_evidence(transition_fd: int, marker_fd: int, backup_fd: int, checksum_fd: int) -> None:
    """Duplicate descriptor-safe recovery evidence onto the fixed exec FDs."""
    # Duplicate before assigning fixed numbers: inherited descriptors can
    # themselves initially occupy 4..7.
    copies = [os.dup(fd) for fd in (transition_fd, marker_fd, backup_fd, checksum_fd)]
    try:
        _handoff(copies[1], 5); _handoff(copies[2], 6); _handoff(copies[3], 7); _handoff(copies[0], 4)
    finally:
        for fd in copies:
            if fd not in (4, 5, 6, 7): os.close(fd)


def _marker_identity(fd: int, paths: Paths) -> dict[str, int | str]:
    info = _regular_root_0600(fd, "recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    return {"sha256": _hash(fd), "dev": info.st_dev, "ino": info.st_ino, "mtime_ns": info.st_mtime_ns}


def _marker_bytes_for_record(record: dict[str, Any]) -> bytes:
    descendant = record.get("marker_descendant")
    if descendant is None:
        lineage = record.get("marker_lineage")
        if lineage is None:
            return MARKER_TEXT
        if not isinstance(lineage, dict) or not isinstance(lineage.get("reason"), str):
            raise ProofError("unsupported marker lineage")
        return f"rollback recovery blocked: {lineage['reason']}\n".encode()
    if not isinstance(descendant, dict) or descendant.get("reason") != ROLLBACK_MARKER_REASON:
        raise ProofError("unsupported marker descendant")
    return ROLLBACK_MARKER_TEXT


def _marker_identity_is_canonical(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"sha256", "dev", "ino", "mtime_ns"}
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
        and all(isinstance(value.get(key), int) and value[key] >= 0 for key in ("dev", "ino", "mtime_ns"))
    )


def _marker_descendant_path(parent: dict[str, Any], parent_sha256: str, paths: Paths) -> Path:
    return _incident_dir(parent, paths) / f"marker-descendant-v{parent['version']}-{parent_sha256}.json"


def _descendant_body(value: dict[str, Any]) -> bytes:
    return _record_bytes(value)


def _validate_descendant_attestation(child: dict[str, Any], parent: dict[str, Any], parent_sha256: str, paths: Paths) -> None:
    descendant = child.get("marker_descendant")
    if not isinstance(descendant, dict):
        raise ProofError("successor marker changed without descendant attestation")
    required = {"version", "path", "sha256", "reason", "parent_transition_sha256", "old_marker", "current_marker", "proof_sha256"}
    if set(descendant) != required or descendant.get("version") != MARKER_DESCENDANT_VERSION or descendant.get("reason") != ROLLBACK_MARKER_REASON or descendant.get("parent_transition_sha256") != parent_sha256 or not _marker_identity_is_canonical(descendant.get("old_marker")) or not _marker_identity_is_canonical(descendant.get("current_marker")):
        raise ProofError("invalid marker descendant reference")
    path = _marker_descendant_path(parent, parent_sha256, paths)
    if descendant.get("path") != path.name:
        raise ProofError("marker descendant path mismatch")
    attestation, body = _read_record(path, "marker descendant attestation", paths)
    if _record_hash(body) != descendant.get("sha256"):
        raise ProofError("marker descendant hash mismatch")
    proof = dict(attestation); supplied_proof = proof.pop("proof_sha256", None)
    if not isinstance(supplied_proof, str) or supplied_proof != _record_hash(_descendant_body(proof)):
        raise ProofError("marker descendant proof digest mismatch")
    expected = {
        "version": MARKER_DESCENDANT_VERSION,
        "kind": "safe_down_marker_descendant",
        "parent_transition_sha256": parent_sha256,
        "parent_generation": parent["version"],
        "parent_code_commit": parent["code_commit"],
        "old_marker": parent["marker"],
        "current_marker": child["marker"],
        "reason": ROLLBACK_MARKER_REASON,
        "backup_sha256": parent["backup"]["sha256"],
        "ledger_sha256": _record_hash(_descendant_body(parent["ledger"])),
        "publication_targets_sha256": parent["publication_targets"]["digest"],
    }
    expected["proof_sha256"] = _record_hash(_descendant_body(expected))
    if attestation != expected or descendant.get("old_marker") != parent["marker"] or descendant.get("current_marker") != child["marker"] or descendant.get("proof_sha256") != supplied_proof:
        raise ProofError("marker descendant attestation does not bind successor")


def _validate_descendant_parent_live_proof(record: dict[str, Any], paths: Paths) -> None:
    """Reprove every v1 input except its intentionally descended marker."""
    backup = _canonical_backup(str(record.get("backup", {}).get("path", "")), paths)
    backup_fd, backup_info = _open_held(backup, "backup", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    checksum_fd, _ = _open_held(backup.with_suffix(backup.suffix + ".sha256"), "backup checksum", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        if _hash(backup_fd) != record["backup"].get("sha256") or (backup_info.st_dev, backup_info.st_ino) != (record["backup"].get("dev"), record["backup"].get("ino")):
            raise ProofError("backup no longer proves safe-down")
        if not _matches_checksum_sidecar(_read(checksum_fd), record["backup"]["sha256"], backup):
            raise ProofError("backup checksum proof changed")
    finally:
        os.close(backup_fd); os.close(checksum_fd)
    live = _sqlite_digest(paths.db, paths, "bridge ledger")
    archived = _sqlite_digest(backup, paths, "backup")
    if live != archived or live != record.get("ledger"):
        raise ProofError("safe-down ledger proof changed")
    failed = BACKUP_NAME.fullmatch(backup.name)
    assert failed is not None
    states, digest = _publication_state(paths, int(record["marker"]["mtime_ns"]), failed.group(1))
    if digest != record.get("publication_targets", {}).get("digest") or states != record.get("publication_targets", {}).get("states"):
        raise ProofError("publication target proof changed")


def _read_current_marker(paths: Paths) -> tuple[dict[str, int | str], bytes]:
    fd, _ = _open_held(paths.marker, "recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        identity = _marker_identity(fd, paths)
        contents = _read(fd)
        _ensure_fd_matches(fd, paths.marker, "recovery marker")
        return identity, contents
    finally:
        os.close(fd)


def _descendant_attestation_record(parent: dict[str, Any], parent_sha256: str, current_marker: dict[str, int | str]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "version": MARKER_DESCENDANT_VERSION,
        "kind": "safe_down_marker_descendant",
        "parent_transition_sha256": parent_sha256,
        "parent_generation": parent["version"],
        "parent_code_commit": parent["code_commit"],
        "old_marker": parent["marker"],
        "current_marker": current_marker,
        "reason": ROLLBACK_MARKER_REASON,
        "backup_sha256": parent["backup"]["sha256"],
        "ledger_sha256": _record_hash(_descendant_body(parent["ledger"])),
        "publication_targets_sha256": parent["publication_targets"]["digest"],
    }
    record["proof_sha256"] = _record_hash(_descendant_body(record))
    return record


def _validate_descendant_request(parent: dict[str, Any], parent_sha256: str, requested: object, paths: Paths) -> tuple[dict[str, int | str], dict[str, Any]]:
    """Consume the deliberately narrow, operator-supplied exceptional proof.

    A v1 marker can never be made to prove bytes written later at the same
    inode.  This accepts that fact only for the reviewed rollback terminal
    marker and binds the exact old and current descriptor identities into a
    new immutable record before a v2 can reference it.
    """
    if parent.get("version") != TRANSITION_VERSION or parent.get("marker_descendant") is not None:
        raise ProofError("descendant attestation is v1-only")
    if not isinstance(requested, dict) or set(requested) != {"old_marker", "current_marker", "reason"}:
        raise ProofError("descendant attestation arguments are malformed")
    old_marker = requested["old_marker"]
    current_marker = requested["current_marker"]
    if not _marker_identity_is_canonical(old_marker) or not _marker_identity_is_canonical(current_marker) or requested["reason"] != ROLLBACK_MARKER_REASON:
        raise ProofError("descendant attestation arguments are malformed")
    if old_marker != parent.get("marker"):
        raise ProofError("descendant attestation old marker does not match v1")
    observed, contents = _read_current_marker(paths)
    if contents != ROLLBACK_MARKER_TEXT or observed != current_marker:
        raise ProofError("current marker does not match descendant attestation")
    # Same inode is only an extra incident-specific constraint.  Content hash
    # and nanosecond timestamp remain the actual current-marker identity.
    if (observed["dev"], observed["ino"]) != (old_marker["dev"], old_marker["ino"]):
        raise ProofError("descendant marker is not the recorded in-place marker")
    _validate_descendant_parent_live_proof(parent, paths)
    path = _marker_descendant_path(parent, parent_sha256, paths)
    attestation = _descendant_attestation_record(parent, parent_sha256, observed)
    if path.exists():
        existing, body = _read_record(path, "marker descendant attestation", paths)
        if existing != attestation or _record_hash(body) != _record_hash(_descendant_body(attestation)):
            raise ProofError("marker descendant attestation was reused or tampered")
    else:
        test_capability.audit("safe-down-marker-descendant-attestation-write")
        test_capability.kill_if("marker-descendant-attestation-write")
        _write_new(path, _descendant_body(attestation), 0o600)
        _fsync_dir(path.parent); _fsync_dir(paths.archive)
        test_capability.audit("safe-down-marker-descendant-attestation-fsynced")
        test_capability.kill_if("marker-descendant-attestation-fsynced")
    # Close the attestation-to-active-link race without ever editing a v1.
    final_identity, final_contents = _read_current_marker(paths)
    if final_contents != ROLLBACK_MARKER_TEXT or final_identity != observed:
        raise ProofError("current marker changed after descendant attestation")
    return observed, attestation


def _valid_marker_reason(reason: object) -> bool:
    return isinstance(reason, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9 -]{0,120}", reason))


def _marker_lineage_path(parent: dict[str, Any], parent_sha256: str, generation: int, paths: Paths) -> Path:
    if generation < 1:
        raise ProofError("invalid marker lineage generation")
    return _incident_dir(parent, paths) / f"marker-lineage-v{parent['version']}-{parent_sha256}-g{generation}.json"


def _marker_lineage_journal_path(parent: dict[str, Any], parent_sha256: str, generation: int, paths: Paths) -> Path:
    return _marker_lineage_path(parent, parent_sha256, generation, paths).with_suffix(".journal")


def _marker_lineage_artifact(parent: dict[str, Any], parent_sha256: str, generation: int, old_marker: dict[str, int | str], current_marker: dict[str, int | str], reason: str) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "version": MARKER_LINEAGE_VERSION,
        "kind": "safe_down_marker_lineage",
        "parent_transition_sha256": parent_sha256,
        "parent_generation": parent["version"],
        "parent_code_commit": parent["code_commit"],
        "generation": generation,
        "old_marker": old_marker,
        "current_marker": current_marker,
        "reason": reason,
        "backup_sha256": parent["backup"]["sha256"],
        "ledger_sha256": _record_hash(_descendant_body(parent["ledger"])),
        "publication_targets_sha256": parent["publication_targets"]["digest"],
    }
    artifact["proof_sha256"] = _record_hash(_descendant_body(artifact))
    return artifact


def _validate_marker_lineage(child: dict[str, Any], parent: dict[str, Any], parent_sha256: str, paths: Paths) -> None:
    lineage = child.get("marker_lineage")
    if not isinstance(lineage, dict):
        raise ProofError("successor marker changed without marker lineage")
    required = {"version", "generation", "path", "sha256", "reason", "parent_transition_sha256", "old_marker", "current_marker", "proof_sha256"}
    if set(lineage) != required or lineage.get("version") != MARKER_LINEAGE_VERSION or lineage.get("parent_transition_sha256") != parent_sha256 or not _valid_marker_reason(lineage.get("reason")) or not _marker_identity_is_canonical(lineage.get("old_marker")) or not _marker_identity_is_canonical(lineage.get("current_marker")):
        raise ProofError("invalid marker lineage reference")
    generation = lineage.get("generation")
    if not isinstance(generation, int) or generation < 1:
        raise ProofError("marker lineage generation is invalid")
    current, expected_ref = _marker_lineage_reference(parent, parent_sha256, paths) or (None, None)
    if current is None or expected_ref is None or child.get("marker") != current or lineage != expected_ref:
        raise ProofError("marker lineage does not bind successor")


def _marker_lineage_reference(parent: dict[str, Any], parent_sha256: str, paths: Paths) -> tuple[dict[str, int | str], dict[str, Any]] | None:
    incident = _incident_dir(parent, paths)
    prefix = f"marker-lineage-v{parent['version']}-{parent_sha256}-g"
    candidates: list[tuple[int, Path]] = []
    for path in incident.glob(prefix + "*.json"):
        match = re.fullmatch(re.escape(prefix) + r"(\d+)\.json", path.name)
        if match: candidates.append((int(match.group(1)), path))
    if not candidates:
        return None
    candidates.sort()
    if [generation for generation, _ in candidates] != list(range(1, len(candidates) + 1)):
        raise ProofError("marker lineage generations are not canonical")
    previous = parent["marker"]
    artifact: dict[str, Any] = {}
    body = b""
    generation = 0
    path = incident
    for generation, path in candidates:
        artifact, body = _read_record(path, "marker lineage", paths)
        if not _valid_marker_reason(artifact.get("reason")) or not _marker_identity_is_canonical(artifact.get("current_marker")):
            raise ProofError("marker lineage is malformed")
        expected = _marker_lineage_artifact(parent, parent_sha256, generation, previous, artifact["current_marker"], artifact["reason"])
        if artifact != expected:
            raise ProofError("marker lineage is not canonical")
        previous = artifact["current_marker"]
    current, contents = _read_current_marker(paths)
    if current != artifact["current_marker"] or contents != f"rollback recovery blocked: {artifact['reason']}\n".encode():
        raise ProofError("current marker does not match marker lineage")
    return current, {
        "version": MARKER_LINEAGE_VERSION,
        "generation": generation,
        "path": path.name,
        "sha256": _record_hash(body),
        "reason": artifact["reason"],
        "parent_transition_sha256": parent_sha256,
        "old_marker": parent["marker"],
        "current_marker": current,
        "proof_sha256": artifact["proof_sha256"],
    }


def _incident_dir(record: dict[str, Any], paths: Paths) -> Path:
    incident = record.get("incident")
    if not isinstance(incident, str) or not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]+-[0-9a-f]+-\d+", incident):
        raise ProofError("invalid transition incident identity")
    path = paths.archive / incident
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProofError("transition incident archive missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.expected_uid or info.st_gid != paths.expected_gid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProofError("unsafe transition incident archive")
    return path


def _archive_transition_path(record: dict[str, Any], body_hash: str, paths: Paths) -> Path:
    version = record.get("version")
    incident = _incident_dir(record, paths)
    if version == TRANSITION_VERSION:
        return incident / "transition.json"
    if not isinstance(version, int) or version < SUPERSEDE_MIN_VERSION:
        raise ProofError("unsupported transition version")
    parent = record.get("parent")
    if not isinstance(parent, dict) or parent.get("generation") != version - 1 or not re.fullmatch(r"[0-9a-f]{64}", str(parent.get("sha256", ""))):
        raise ProofError("invalid successor parent identity")
    # The successor filename is deterministic from the immutable parent, not
    # from its own body, so replay can recover a crash before the active swap.
    return incident / f"transition-v{version}-{parent['sha256']}.json"


def _read_record(path: Path, label: str, paths: Paths) -> tuple[dict[str, Any], bytes]:
    fd, _ = _open_held(path, label, allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        body = _read(fd)
    finally:
        os.close(fd)
    try:
        record = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProofError(f"invalid {label}") from exc
    if not isinstance(record, dict) or _record_bytes(record) != body:
        raise ProofError(f"noncanonical {label}")
    return record, body


def _validate_chain(record: dict[str, Any], body: bytes, paths: Paths) -> list[tuple[dict[str, Any], bytes]]:
    """Return immutable v1..current records after archive-link lineage proof."""
    if record.get("decision") != TRANSITION_DECISION:
        raise ProofError("unsupported transition decision")
    body_hash = _record_hash(body)
    archive_path = _archive_transition_path(record, body_hash, paths)
    archived, archived_body = _read_record(archive_path, "archived transition", paths)
    if archived_body != body:
        raise ProofError("active transition is not the archived record")
    version = record.get("version")
    if version == TRANSITION_VERSION:
        return [(record, body)]
    if not isinstance(version, int) or version < SUPERSEDE_MIN_VERSION:
        raise ProofError("unsupported transition version")
    parent = record.get("parent")
    if not isinstance(parent, dict):
        raise ProofError("successor parent is missing")
    parent_hash = parent.get("sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(parent_hash)) or parent.get("generation") != version - 1 or not re.fullmatch(r"[0-9a-f]{40}", str(parent.get("code_commit", ""))):
        raise ProofError("invalid successor parent")
    # A successor stores the exact archive filename of its parent.  It is
    # checked as a basename so it cannot be used as a path traversal seam.
    parent_archive = parent.get("archive")
    if not isinstance(parent_archive, str) or Path(parent_archive).name != parent_archive:
        raise ProofError("invalid successor parent archive")
    parent_path = _incident_dir(record, paths) / parent_archive
    parent_record, parent_body = _read_record(parent_path, "parent archived transition", paths)
    if _record_hash(parent_body) != parent_hash or parent_record.get("version") != version - 1 or parent_record.get("code_commit") != parent.get("code_commit"):
        raise ProofError("successor parent archive mismatch")
    chain = _validate_chain(parent_record, parent_body, paths)
    for key in ("incident", "backup", "ledger", "publication_targets"):
        if record.get(key) != parent_record.get(key):
            raise ProofError("successor proof does not preserve parent evidence")
    if record.get("marker") != parent_record.get("marker"):
        # The operator-attested incident is deliberately one-time and only
        # bridges v1 to v2.  Every later generation is strict again.
        if version != SUPERSEDE_MIN_VERSION or parent_record.get("version") != TRANSITION_VERSION:
            raise ProofError("successor marker changed outside v1 descendant attestation")
        if record.get("marker_descendant") is not None:
            if record.get("marker_lineage") is not None:
                raise ProofError("successor has conflicting marker authorities")
            _validate_descendant_attestation(record, parent_record, str(parent_hash), paths)
        else:
            _validate_marker_lineage(record, parent_record, str(parent_hash), paths)
    elif record.get("marker_descendant") != parent_record.get("marker_descendant") or record.get("marker_lineage") != parent_record.get("marker_lineage"):
        raise ProofError("successor marker descendant lineage changed")
    return [*chain, (record, body)]


def _validate_live_proof(record: dict[str, Any], paths: Paths) -> None:
    backup = _canonical_backup(str(record.get("backup", {}).get("path", "")), paths)
    marker = record.get("marker")
    if not isinstance(marker, dict):
        raise ProofError("transition marker proof missing")
    marker_fd, marker_info = _open_held(paths.marker, "recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    backup_fd, backup_info = _open_held(backup, "backup", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    checksum_fd, _ = _open_held(backup.with_suffix(backup.suffix + ".sha256"), "backup checksum", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        if _read(marker_fd) != _marker_bytes_for_record(record) or _hash(marker_fd) != marker.get("sha256") or (marker_info.st_dev, marker_info.st_ino, marker_info.st_mtime_ns) != (marker.get("dev"), marker.get("ino"), marker.get("mtime_ns")):
            raise ProofError("marker no longer proves safe-down")
        if _hash(backup_fd) != record["backup"].get("sha256") or (backup_info.st_dev, backup_info.st_ino) != (record["backup"].get("dev"), record["backup"].get("ino")):
            raise ProofError("backup no longer proves safe-down")
        if not _matches_checksum_sidecar(_read(checksum_fd), record["backup"]["sha256"], backup):
            raise ProofError("backup checksum proof changed")
        for fd in (marker_fd, backup_fd, checksum_fd): os.fsync(fd)
    finally:
        for fd in (marker_fd, backup_fd, checksum_fd): os.close(fd)
    _fsync_dir(paths.backups)
    live = _sqlite_digest(paths.db, paths, "bridge ledger")
    archived = _sqlite_digest(backup, paths, "backup")
    if live != archived or live != record.get("ledger"):
        raise ProofError("safe-down ledger proof changed")
    failed = BACKUP_NAME.fullmatch(backup.name)
    assert failed is not None
    states, digest = _publication_state(paths, marker_info.st_mtime_ns, failed.group(1))
    if digest != record.get("publication_targets", {}).get("digest") or states != record.get("publication_targets", {}).get("states"):
        raise ProofError("publication target proof changed")


def _clean_commit(commit: str, paths: Paths) -> tuple[str, str, str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ProofError("successor commit is malformed")
    cap = test_capability.load()
    if cap is not None:
        # The non-root harness attests one committed successor through its
        # inherited capability.  Production never reaches this seam (root
        # rejects test selectors), while direct tests exercise real git dirty,
        # untracked, HEAD, and object failure paths.
        if commit != cap.release_version:
            raise ProofError("hermetic successor commit does not match capability")
        # The hermetic release fixture intentionally models the reviewed
        # squash object as one parent.  Root never accepts this test seam.
        parent = "a" * 40
        return commit, hashlib.sha1(("tree:" + commit).encode()).hexdigest(), parent, hashlib.sha1(("tree:" + parent).encode()).hexdigest()
    try:
        git = ["git", "-c", f"safe.directory={paths.repository}", "-C", str(paths.repository)]
        dirty = subprocess.check_output([*git, "status", "--porcelain", "--untracked-files=all"], text=True)
        head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        actual = subprocess.check_output([*git, "rev-parse", f"{commit}^{{commit}}"], text=True).strip()
        tree = subprocess.check_output([*git, "rev-parse", f"{commit}^{{tree}}"], text=True).strip()
        parents = subprocess.check_output([*git, "rev-list", "--parents", "-n", "1", commit], text=True).split()
        parent_tree = subprocess.check_output([*git, "rev-parse", f"{parents[1]}^{{tree}}"], text=True).strip() if len(parents) == 2 else ""
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProofError("successor repository or commit is unavailable") from exc
    if dirty or head != commit or actual != commit or not re.fullmatch(r"[0-9a-f]{40}", tree) or parents[:1] != [commit] or len(parents) != 2 or not re.fullmatch(r"[0-9a-f]{40}", parents[1]) or not re.fullmatch(r"[0-9a-f]{40}", parent_tree):
        raise ProofError("successor repository is dirty or HEAD is not the supplied commit")
    return head, tree, parents[1], parent_tree


def _github_release_evidence(pr: int, successor_commit: str, approved_pr_head: str, local_parent: str, local_parent_tree: str, paths: Paths) -> dict[str, Any]:
    """Read only sanitized merged-PR facts; the raw gh response is never stored."""
    if pr < 1:
        raise ProofError("successor PR is malformed")
    cap = test_capability.load()
    if cap is not None:
        # Narrow hermetic seam.  It exists only behind the inherited non-root
        # test capability; root rejects every test override before this point.
        tree = hashlib.sha1(("tree:" + successor_commit).encode()).hexdigest()
        return {"number": pr, "url": f"https://github.com/{SOURCE_REPOSITORY}/pull/{pr}", "state": "MERGED", "base": "main", "base_repo": SOURCE_REPOSITORY, "head_repo": SOURCE_REPOSITORY, "base_sha": local_parent, "base_tree": local_parent_tree, "head": approved_pr_head, "merge": successor_commit, "merge_tree": tree, "merge_parents": [local_parent], "current": successor_commit, "merged_at": "2026-08-17T00:00:00Z"}
    try:
        raw = subprocess.check_output(["gh", "api", f"repos/{SOURCE_REPOSITORY}/pulls/{pr}"], text=True)
        value = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ProofError("merged PR provenance cannot be verified") from exc
    merge = (value.get("merge_commit_sha") or "").lower(); head_data = value.get("head") or {}; base_data = value.get("base") or {}
    head = (head_data.get("sha") or "").lower(); base = base_data.get("ref") or ""; base_sha = (base_data.get("sha") or "").lower()
    base_repo = ((base_data.get("repo") or {}).get("full_name") or ""); head_repo = ((head_data.get("repo") or {}).get("full_name") or "")
    expected_url = f"https://github.com/{SOURCE_REPOSITORY}/pull/{pr}"
    if value.get("state") != "closed" or value.get("merged") is not True or not value.get("merged_at") or value.get("html_url") != expected_url or base != "main" or head != approved_pr_head or merge != successor_commit or base_sha != local_parent or base_repo != SOURCE_REPOSITORY or head_repo != SOURCE_REPOSITORY:
        raise ProofError("PR is not the merged successor commit on main")
    try:
        commit = json.loads(subprocess.check_output(["gh", "api", f"repos/{SOURCE_REPOSITORY}/git/commits/{merge}"], text=True))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ProofError("GitHub merge commit provenance cannot be verified") from exc
    merge_tree = ((commit.get("tree") or {}).get("sha") or "").lower(); parents = [str(item.get("sha", "")).lower() for item in commit.get("parents", [])]
    if commit.get("sha", "").lower() != merge or not re.fullmatch(r"[0-9a-f]{40}", merge_tree) or parents != [local_parent]:
        raise ProofError("GitHub merge commit is malformed")
    try:
        base_commit = json.loads(subprocess.check_output(["gh", "api", f"repos/{SOURCE_REPOSITORY}/git/commits/{base_sha}"], text=True))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ProofError("GitHub base commit provenance cannot be verified") from exc
    base_tree = ((base_commit.get("tree") or {}).get("sha") or "").lower()
    if base_commit.get("sha", "").lower() != base_sha or base_tree != local_parent_tree:
        raise ProofError("GitHub base commit is malformed")
    return {"number": pr, "url": str(value.get("html_url", "")), "state": "MERGED", "base": base, "base_repo": base_repo, "head_repo": head_repo, "base_sha": base_sha, "base_tree": base_tree, "head": head, "merge": merge, "merge_tree": merge_tree, "merge_parents": parents, "current": successor_commit, "merged_at": str(value["merged_at"])}


def _validate_review_evidence(evidence: dict[str, Any], pr: int, successor_commit: str, approved_pr_head: str, local_parent: str, local_parent_tree: str, local_tree: str) -> None:
    required = {"number", "url", "state", "base", "base_repo", "head_repo", "base_sha", "base_tree", "head", "merge", "merge_tree", "merge_parents", "current", "merged_at"}
    if set(evidence) != required or evidence.get("number") != pr or evidence.get("url") != f"https://github.com/{SOURCE_REPOSITORY}/pull/{pr}" or evidence.get("state") != "MERGED" or evidence.get("base") != "main" or evidence.get("base_repo") != SOURCE_REPOSITORY or evidence.get("head_repo") != SOURCE_REPOSITORY or evidence.get("head") != approved_pr_head or evidence.get("base_sha") != local_parent or evidence.get("base_tree") != local_parent_tree or evidence.get("merge") != successor_commit or evidence.get("current") != successor_commit or evidence.get("merge_tree") != local_tree or evidence.get("merge_parents") != [local_parent] or not isinstance(evidence.get("merged_at"), str) or not evidence["merged_at"]:
        raise ProofError("merged PR provenance is malformed")


def _revalidate_reviewed_checkout(evidence: dict[str, Any], pr: int, successor_commit: str, approved_pr_head: str, paths: Paths) -> tuple[str, str, str, str]:
    """Close the GitHub-receipt to archive-write checkout race.

    This repeats the production clean/untracked, HEAD, squash-parent, and
    tree checks after provenance capture.  The only test verifier seam returns
    a receipt; root CLI callers cannot skip this local revalidation.
    """
    head, tree, parent, parent_tree = _clean_commit(successor_commit, paths)
    _validate_review_evidence(evidence, pr, successor_commit, approved_pr_head, parent, parent_tree, tree)
    return head, tree, parent, parent_tree


def _append_journal(path: Path, row: dict[str, Any], paths: Paths) -> None:
    data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise ProofError("cannot append supersession journal") from exc
    try:
        _regular_root_0600(fd, "supersession journal", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
        _write_all(fd, data, "supersession journal")
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _journal_rows(path: Path, paths: Paths) -> list[dict[str, Any]]:
    fd, _ = _open_held(path, "supersession journal", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        try:
            raw = _read(fd)
            if not raw.endswith(b"\n"):
                raise ProofError("invalid supersession journal")
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProofError("invalid supersession journal") from exc
    finally:
        os.close(fd)
    if not rows or not all(isinstance(row, dict) for row in rows) or b"".join(_record_bytes(row) for row in rows) != raw:
        raise ProofError("invalid supersession journal")
    return rows


def _supersession_journal_state(path: Path, prepared: dict[str, Any], committed: dict[str, Any], paths: Paths) -> str:
    """Accept only the exact fsynced two-record supersession protocol."""
    if not path.exists():
        return "absent"
    rows = _journal_rows(path, paths)
    if rows == [prepared]:
        return "prepared"
    if rows == [prepared, committed]:
        return "committed"
    raise ProofError("supersession journal sequence is not canonical")


def _marker_lineage_journal_state(path: Path, prepared: dict[str, Any], paths: Paths) -> tuple[str, dict[str, Any] | None]:
    if not path.exists():
        return "absent", None
    rows = _journal_rows(path, paths)
    if rows == [prepared]:
        return "prepared", None
    if len(rows) == 2 and rows[0] == prepared:
        committed = rows[1]
        expected = {**prepared, "status": "committed", "new_marker": committed.get("new_marker")}
        if committed == expected and _marker_identity_is_canonical(committed.get("new_marker")):
            return "committed", committed
    raise ProofError("marker lineage journal sequence is not canonical")


def _temporary_marker_identity(path: Path, paths: Paths) -> dict[str, int | str]:
    fd, info = _open_held(path, "temporary recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        if info.st_nlink != 1:
            raise ProofError("temporary recovery marker has links")
        return {"path": path.name, "sha256": _hash(fd), "dev": info.st_dev, "ino": info.st_ino, "mtime_ns": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode), "nlink": info.st_nlink}
    finally:
        os.close(fd)


def _temporary_marker_matches(path: Path, expected: object, contents: bytes, paths: Paths) -> None:
    if not isinstance(expected, dict) or set(expected) != {"path", "sha256", "dev", "ino", "mtime_ns", "mode", "nlink"} or expected.get("path") != path.name or expected.get("mode") != 0o600 or expected.get("nlink") != 1:
        raise ProofError("prepared temporary marker identity is malformed")
    if _temporary_marker_identity(path, paths) != expected:
        raise ProofError("prepared temporary marker identity changed")
    fd, _ = _open_held(path, "temporary recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        if _read(fd) != contents:
            raise ProofError("prepared temporary marker bytes changed")
    finally:
        os.close(fd)


def _unbound_lineage_event_id(marker: dict[str, int | str], reason: str) -> str:
    """Bind an unbound incident to this exact input descriptor and request."""
    return _record_hash(_descendant_body({"old_marker": marker, "reason": reason}))


def _unbound_lineage_root(marker: dict[str, int | str], reason: str, paths: Paths) -> tuple[Path, str]:
    event_id = _unbound_lineage_event_id(marker, reason)
    root = paths.archive / f"unbound-marker-lineage-{event_id}"
    if not root.exists():
        os.mkdir(root, 0o700); _fsync_dir(paths.archive)
    info = os.stat(root, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.expected_uid or info.st_gid != paths.expected_gid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProofError("unsafe unbound marker lineage root")
    return root, event_id


def _unbound_lineage_paths(root: Path, event_id: str) -> tuple[Path, Path, Path]:
    stem = f"marker-lineage-unbound-{event_id}"
    return root / f"{stem}.json", root / f"{stem}.journal", root / f".{stem}.marker.tmp"


def _unbound_lineage_artifact(event_id: str, old_marker: dict[str, int | str], current_marker: dict[str, int | str], reason: str) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "version": MARKER_LINEAGE_VERSION,
        "kind": "safe_down_unbound_marker_lineage",
        "incident": event_id,
        "generation": 1,
        "old_marker": old_marker,
        "current_marker": current_marker,
        "reason": reason,
    }
    artifact["proof_sha256"] = _record_hash(_descendant_body(artifact))
    return artifact


def _unbound_lineage_journal(root: Path, paths: Paths, *, require_committed: bool = True) -> tuple[Path, dict[str, Any], dict[str, Any] | None, str, dict[str, int | str]]:
    """Read one committed unbound journal without trusting its archive file."""
    match = re.fullmatch(r"unbound-marker-lineage-([0-9a-f]{64})", root.name)
    if match is None:
        raise ProofError("invalid unbound marker lineage incident")
    event_id = match.group(1)
    info = os.stat(root, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != paths.expected_uid or info.st_gid != paths.expected_gid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProofError("unsafe unbound marker lineage root")
    artifact_path, journal_path, temporary = _unbound_lineage_paths(root, event_id)
    try:
        entries = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise ProofError("cannot inspect unbound marker lineage incident") from exc
    if entries not in ({journal_path.name}, {artifact_path.name, journal_path.name}):
        raise ProofError("unbound marker lineage incident has extra or missing evidence")
    rows = _journal_rows(journal_path, paths)
    if not rows or not isinstance(rows[0].get("old_marker"), dict) or not _marker_identity_is_canonical(rows[0]["old_marker"]) or not _valid_marker_reason(rows[0].get("new_reason")):
        raise ProofError("unbound marker lineage journal is malformed")
    old_marker = rows[0]["old_marker"]
    reason = rows[0]["new_reason"]
    if _unbound_lineage_event_id(old_marker, reason) != event_id:
        raise ProofError("unbound marker lineage incident identity changed")
    temp_marker = rows[0].get("temp_marker")
    if not isinstance(temp_marker, dict) or temp_marker.get("path") != temporary.name:
        raise ProofError("unbound marker lineage temporary identity is malformed")
    prepared = {
        "version": MARKER_LINEAGE_VERSION, "kind": "rollback_marker_lineage", "status": "prepared",
        "generation": 1, "event_id": event_id, "old_marker": old_marker, "new_reason": reason,
        "new_sha256": hashlib.sha256(f"rollback recovery blocked: {reason}\n".encode()).hexdigest(),
        "transition_sha256": None, "temp_marker": temp_marker,
    }
    state, committed = _marker_lineage_journal_state(journal_path, prepared, paths)
    if require_committed and (state != "committed" or committed is None):
        raise ProofError("unbound marker lineage event is not committed")
    return artifact_path, prepared, committed, event_id, old_marker


def _validate_unbound_lineage_event(root: Path, paths: Paths) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one fully committed immutable unbound incident and its journal.

    A no-active update may use a completed event only as provenance for its
    *input* marker.  It never extends or rewrites that event.
    """
    artifact_path, prepared, committed, event_id, old_marker = _unbound_lineage_journal(root, paths)
    assert committed is not None
    if not artifact_path.exists():
        raise ProofError("unbound marker lineage archive is missing")
    artifact, body = _read_record(artifact_path, "unbound marker lineage", paths)
    expected_artifact = _unbound_lineage_artifact(event_id, old_marker, committed["new_marker"], prepared["new_reason"])
    if artifact != expected_artifact or _record_hash(body) != _record_hash(_descendant_body(expected_artifact)):
        raise ProofError("unbound marker lineage archive is not canonical")
    return artifact, committed


def _recover_unbound_lineage_archive(root: Path, paths: Paths) -> tuple[dict[str, Any], dict[str, Any]]:
    """Finish only the immutable archive step after a committed marker rename."""
    artifact_path, prepared, committed, event_id, old_marker = _unbound_lineage_journal(root, paths)
    assert committed is not None
    artifact = _unbound_lineage_artifact(event_id, old_marker, committed["new_marker"], prepared["new_reason"])
    if artifact_path.exists():
        return _validate_unbound_lineage_event(root, paths)
    _write_new(artifact_path, _descendant_body(artifact), 0o600)
    _fsync_dir(root); _fsync_dir(paths.archive)
    return artifact, committed


def _recover_unbound_lineage_after_rename(root: Path, current_marker: dict[str, int | str], current_contents: bytes, paths: Paths) -> tuple[dict[str, Any], dict[str, Any]]:
    """Commit only the exact prepared temp descriptor published before SIGKILL."""
    artifact_path, prepared, committed, _, _ = _unbound_lineage_journal(root, paths, require_committed=False)
    if committed is not None or artifact_path.exists():
        raise ProofError("unbound marker lineage prepared replay is ambiguous")
    expected_temp = prepared.get("temp_marker")
    expected_current = {key: value for key, value in expected_temp.items() if key in {"sha256", "dev", "ino", "mtime_ns"}} if isinstance(expected_temp, dict) else None
    contents = f"rollback recovery blocked: {prepared['new_reason']}\n".encode()
    if current_marker != expected_current or current_contents != contents:
        raise ProofError("unbound marker lineage prepared event did not publish exact temporary descriptor")
    journal = _unbound_lineage_paths(root, prepared["event_id"])[1]
    committed = {**prepared, "status": "committed", "new_marker": current_marker}
    _append_journal(journal, committed, paths)
    return _recover_unbound_lineage_archive(root, paths)


def _unbound_marker_provenance(current_marker: dict[str, int | str], current_contents: bytes, requested_event_id: str, paths: Paths) -> None:
    """Require canonical initial input or exact output of one committed event."""
    matching_output = 0
    try:
        roots = list(paths.archive.glob("unbound-marker-lineage-*"))
    except OSError as exc:
        raise ProofError("cannot inspect unbound marker lineage archive") from exc
    for root in roots:
        # The current requested event may be mid-flight after SIGKILL.  Its
        # exact journal is replayed below; another incomplete event is never
        # borrowed as authority for this request.
        if root.name == f"unbound-marker-lineage-{requested_event_id}":
            continue
        try:
            artifact, committed = _validate_unbound_lineage_event(root, paths)
        except ProofError as exc:
            # The only recoverable partial event is a fully committed journal
            # whose archive write was interrupted after atomic publication.
            if str(exc) != "unbound marker lineage archive is missing":
                if str(exc) != "unbound marker lineage event is not committed":
                    raise
                artifact, committed = _recover_unbound_lineage_after_rename(root, current_marker, current_contents, paths)
            else:
                _, prepared, committed_before_archive, _, _ = _unbound_lineage_journal(root, paths)
                assert committed_before_archive is not None
                if committed_before_archive["new_marker"] != current_marker or current_contents != f"rollback recovery blocked: {prepared['new_reason']}\n".encode():
                    raise ProofError("unbound marker lineage output changed before archive replay")
                artifact, committed = _recover_unbound_lineage_archive(root, paths)
        if committed["new_marker"] == current_marker:
            if current_contents != f"rollback recovery blocked: {artifact['reason']}\n".encode():
                raise ProofError("unbound marker lineage output bytes changed")
            matching_output += 1
    if matching_output > 1:
        raise ProofError("multiple unbound marker lineage events own current marker")
    if current_contents == MARKER_TEXT:
        return
    if matching_output != 1:
        raise ProofError("untracked recovery marker cannot enter unbound lineage")


def _update_unbound_marker_lineage(reason: str, current_marker: dict[str, int | str], current_contents: bytes, paths: Paths) -> dict[str, Any]:
    root, event_id = _unbound_lineage_root(current_marker, reason, paths)
    _unbound_marker_provenance(current_marker, current_contents, event_id, paths)
    artifact_path, journal, temporary = _unbound_lineage_paths(root, event_id)
    contents = f"rollback recovery blocked: {reason}\n".encode()
    if journal.exists() or artifact_path.exists():
        # A committed event necessarily changed the marker descriptor, so an
        # existing root can only be the exact crash-replay event for this input.
        if artifact_path.exists():
            raise ProofError("unbound marker lineage incident was already archived")
        entries = {entry.name for entry in root.iterdir()}
        if entries not in ({journal.name}, {journal.name, temporary.name}):
            raise ProofError("unbound marker lineage incident has extra or missing evidence")
        rows = _journal_rows(journal, paths)
        if not rows:
            raise ProofError("unbound marker lineage journal is malformed")
        prepared = rows[0]
        if prepared.get("event_id") != event_id or prepared.get("old_marker") != current_marker or prepared.get("new_reason") != reason:
            raise ProofError("unbound marker lineage journal does not match requested rollback")
    else:
        if temporary.exists():
            raise ProofError("orphan temporary marker requires operator investigation")
        test_capability.audit("safe-down-marker-lineage-temp-create")
        test_capability.kill_if("marker-lineage-temp-create")
        _write_new(temporary, contents, 0o600)
        test_capability.audit("safe-down-marker-lineage-temp-write")
        test_capability.kill_if("marker-lineage-temp-write")
        test_capability.audit("safe-down-marker-lineage-temp-fsynced")
        test_capability.kill_if("marker-lineage-temp-fsynced")
        temporary_identity = _temporary_marker_identity(temporary, paths)
        test_capability.audit("safe-down-marker-lineage-temp-fstat")
        test_capability.kill_if("marker-lineage-temp-fstat")
        prepared = {
            "version": MARKER_LINEAGE_VERSION, "kind": "rollback_marker_lineage", "status": "prepared",
            "generation": 1, "event_id": event_id, "old_marker": current_marker, "new_reason": reason,
            "new_sha256": hashlib.sha256(contents).hexdigest(), "transition_sha256": None,
            "temp_marker": temporary_identity,
        }
    journal_state, committed = _marker_lineage_journal_state(journal, prepared, paths)
    if journal_state == "absent":
        _append_journal(journal, prepared, paths)
    test_capability.audit("safe-down-marker-lineage-prepared")
    test_capability.kill_if("marker-lineage-prepared")
    if journal_state == "committed":
        assert committed is not None
        if current_marker != committed["new_marker"] or current_contents != contents:
            raise ProofError("committed unbound marker lineage no longer matches marker")
    else:
        expected_temp = prepared.get("temp_marker")
        if temporary.exists():
            _temporary_marker_matches(temporary, expected_temp, contents, paths)
            test_capability.audit("safe-down-marker-lineage-rename")
            test_capability.kill_if("marker-lineage-rename")
            os.replace(temporary, paths.marker); _fsync_dir(paths.state)
            test_capability.audit("safe-down-marker-lineage-dir-fsynced")
            test_capability.kill_if("marker-lineage-dir-fsynced")
        current_marker, current_contents = _read_current_marker(paths)
        expected_current = {key: value for key, value in expected_temp.items() if key in {"sha256", "dev", "ino", "mtime_ns"}} if isinstance(expected_temp, dict) else None
        if current_marker != expected_current or current_contents != contents:
            raise ProofError("prepared unbound marker lineage did not publish exact temporary descriptor")
        committed = {**prepared, "status": "committed", "new_marker": current_marker}
        _append_journal(journal, committed, paths)
        test_capability.audit("safe-down-marker-lineage-committed")
        test_capability.kill_if("marker-lineage-committed")
    assert committed is not None
    artifact = _unbound_lineage_artifact(event_id, prepared["old_marker"], committed["new_marker"], reason)
    if artifact_path.exists():
        existing, body = _read_record(artifact_path, "unbound marker lineage", paths)
        if existing != artifact or _record_hash(body) != _record_hash(_descendant_body(artifact)):
            raise ProofError("unbound marker lineage archive replay mismatch")
    else:
        _write_new(artifact_path, _descendant_body(artifact), 0o600)
        _fsync_dir(root); _fsync_dir(paths.archive)
    test_capability.audit("safe-down-marker-lineage-archive")
    test_capability.kill_if("marker-lineage-archive")
    return committed


def update_marker_lineage(reason: str, *, paths: Paths = Paths(), barrier_evidence: bool = False) -> dict[str, Any]:
    """Atomically replace a rollback marker and preserve an append-only proof.

    The caller must already hold the installer lock and maintenance barrier.
    This function intentionally does not inspect or alter services, the DB, or
    publication targets; it only writes marker lineage under that barrier.
    """
    if not _valid_marker_reason(reason):
        raise ProofError("rollback marker reason is malformed")
    _exclusive_lock(3, paths)
    if not barrier_evidence:
        raise ProofError("marker lineage requires inherited maintenance barrier evidence")
    _revalidate_barrier_evidence(paths)
    active: dict[str, Any] | None = None
    active_hash: str | None = None
    if paths.transition.exists():
        active, active_body = _read_record(paths.transition, "active transition", paths)
        _validate_chain(active, active_body, paths)
        active_hash = _record_hash(active_body)
    current_marker, current_contents = _read_current_marker(paths)
    if active is None:
        return _update_unbound_marker_lineage(reason, current_marker, current_contents, paths)
    else:
        automatic = _marker_lineage_reference(active, active_hash, paths)
        if automatic is None:
            pending_paths = list(_incident_dir(active, paths).glob(f"marker-lineage-v{active['version']}-{active_hash}-g*.journal"))
            if pending_paths:
                if len(pending_paths) != 1:
                    raise ProofError("ambiguous marker lineage journal")
                rows = _journal_rows(pending_paths[0], paths)
                if not rows or not _marker_identity_is_canonical(rows[0].get("old_marker")):
                    raise ProofError("marker lineage journal is malformed")
                old_marker = rows[0]["old_marker"]
            else:
                _validate_live_proof(active, paths)
                old_marker = active["marker"]
                if current_marker != old_marker or current_contents != _marker_bytes_for_record(active):
                    raise ProofError("active transition does not prove existing recovery marker")
        else:
            old_marker, _ = automatic
        root = _incident_dir(active, paths)
    contents = f"rollback recovery blocked: {reason}\n".encode()
    prefix = f"marker-lineage-v{active['version']}-{active_hash}-g" if active is not None else "marker-lineage-unbound-g"
    journals: list[tuple[int, Path]] = []
    for path in root.glob(prefix + "*.journal"):
        match = re.fullmatch(re.escape(prefix) + r"(\d+)\.journal", path.name)
        if match: journals.append((int(match.group(1)), path))
    journals.sort()
    if journals:
        last_rows = _journal_rows(journals[-1][1], paths)
        if len(last_rows) == 2 and last_rows[0].get("new_reason") == reason and last_rows[1].get("new_marker") == current_marker and current_contents == contents:
            return last_rows[1]
    pending = [(generation, path) for generation, path in journals if len(_journal_rows(path, paths)) == 1]
    if len(pending) > 1:
        raise ProofError("multiple marker lineage events are prepared")
    generation = pending[0][0] if pending else (journals[-1][0] + 1 if journals else 1)
    journal = pending[0][1] if pending else root / f"{prefix}{generation}.journal"
    temporary = root / f".{prefix}{generation}.marker.tmp"
    if journal.exists():
        rows = _journal_rows(journal, paths); prepared = rows[0]
        if prepared.get("version") != MARKER_LINEAGE_VERSION or prepared.get("kind") != "rollback_marker_lineage" or prepared.get("status") != "prepared" or prepared.get("generation") != generation or prepared.get("new_reason") != reason or prepared.get("new_sha256") != hashlib.sha256(contents).hexdigest() or prepared.get("transition_sha256") != active_hash or prepared.get("old_marker") != old_marker:
            raise ProofError("marker lineage journal does not match requested rollback")
    else:
        if temporary.exists():
            # A SIGKILL before prepared is deliberately fail-closed: there is
            # no durable authority tying this inode to an intended transition.
            raise ProofError("orphan temporary marker requires operator investigation")
        test_capability.audit("safe-down-marker-lineage-temp-create")
        test_capability.kill_if("marker-lineage-temp-create")
        _write_new(temporary, contents, 0o600)
        test_capability.audit("safe-down-marker-lineage-temp-write")
        test_capability.kill_if("marker-lineage-temp-write")
        test_capability.audit("safe-down-marker-lineage-temp-fsynced")
        test_capability.kill_if("marker-lineage-temp-fsynced")
        temporary_identity = _temporary_marker_identity(temporary, paths)
        test_capability.audit("safe-down-marker-lineage-temp-fstat")
        test_capability.kill_if("marker-lineage-temp-fstat")
        prepared = {"version": MARKER_LINEAGE_VERSION, "kind": "rollback_marker_lineage", "status": "prepared", "generation": generation, "event_id": f"{prefix}{generation}", "old_marker": old_marker, "new_reason": reason, "new_sha256": hashlib.sha256(contents).hexdigest(), "transition_sha256": active_hash, "temp_marker": temporary_identity}
    journal_state, committed = _marker_lineage_journal_state(journal, prepared, paths)
    if journal_state == "absent":
        _append_journal(journal, prepared, paths)
    test_capability.audit("safe-down-marker-lineage-prepared")
    test_capability.kill_if("marker-lineage-prepared")
    if journal_state == "committed":
        assert committed is not None
        current, current_contents = _read_current_marker(paths)
        if current != committed["new_marker"] or current_contents != contents:
            raise ProofError("committed marker lineage no longer matches marker")
    else:
        expected_temp = prepared.get("temp_marker")
        if temporary.exists():
            _temporary_marker_matches(temporary, expected_temp, contents, paths)
            test_capability.audit("safe-down-marker-lineage-rename")
            test_capability.kill_if("marker-lineage-rename")
            os.replace(temporary, paths.marker); _fsync_dir(paths.state)
            test_capability.audit("safe-down-marker-lineage-dir-fsynced")
            test_capability.kill_if("marker-lineage-dir-fsynced")
        current, current_contents = _read_current_marker(paths)
        expected_current = {key: value for key, value in expected_temp.items() if key in {"sha256", "dev", "ino", "mtime_ns"}} if isinstance(expected_temp, dict) else None
        if current != expected_current or current_contents != contents:
            raise ProofError("prepared marker lineage did not publish exact temporary descriptor")
        committed = {**prepared, "status": "committed", "new_marker": current}
        _append_journal(journal, committed, paths)
        test_capability.audit("safe-down-marker-lineage-committed")
        test_capability.kill_if("marker-lineage-committed")
    assert committed is not None
    if active is not None and active_hash is not None:
        artifact = _marker_lineage_artifact(active, active_hash, generation, old_marker, committed["new_marker"], reason)
        path = _marker_lineage_path(active, active_hash, generation, paths)
        if path.exists():
            existing, body = _read_record(path, "marker lineage", paths)
            if existing != artifact or _record_hash(body) != _record_hash(_descendant_body(artifact)):
                raise ProofError("marker lineage archive replay mismatch")
        else:
            _write_new(path, _descendant_body(artifact), 0o600)
            _fsync_dir(path.parent); _fsync_dir(paths.archive)
        test_capability.audit("safe-down-marker-lineage-archive")
        test_capability.kill_if("marker-lineage-archive")
    return committed


def supersede(*, parent_sha256: str, parent_generation: int, parent_code_commit: str, backup_arg: str, checksum: str, marker_sha: str, marker_mtime: str, successor_commit: str, successor_pr: int, approved_pr_head: str, paths: Paths = Paths(), release_verifier: Callable[..., dict[str, Any]] | None = None, barrier_evidence: bool = False, descendant_attestation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Durably replace only the active link with a reviewed successor record."""
    if not re.fullmatch(r"[0-9a-f]{64}", parent_sha256) or parent_generation < 1 or not re.fullmatch(r"[0-9a-f]{40}", parent_code_commit):
        raise ProofError("parent transition identity is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or not re.fullmatch(r"[0-9a-f]{64}", marker_sha) or not re.fullmatch(r"\d{10}", marker_mtime) or not re.fullmatch(r"[0-9a-f]{40}", approved_pr_head):
        raise ProofError("explicit safe-down evidence is malformed")
    _exclusive_lock(3, paths)
    if barrier_evidence:
        _revalidate_barrier_evidence(paths)
    active_fd, active_info = _open_held(paths.transition, "active transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    try:
        active_body = _read(active_fd)
        if active_info.st_nlink < 2:
            raise ProofError("active transition is not archive-linked")
    finally:
        os.close(active_fd)
    active, reopened_body = _read_record(paths.transition, "active transition", paths)
    if reopened_body != active_body:
        raise ProofError("active transition changed while opening")
    active_hash = _record_hash(active_body)
    # A rerun after a crash may already have switched the active link to the
    # intended successor.  Otherwise the named parent must still be active.
    if active_hash == parent_sha256:
        parent_record, parent_body = active, active_body
    else:
        parent_record = None; parent_body = b""
    if parent_record is not None:
        if parent_record.get("version") != parent_generation or parent_record.get("code_commit") != parent_code_commit:
            raise ProofError("named parent transition does not match active link")
        _validate_chain(parent_record, parent_body, paths)
        current_marker: dict[str, int | str] | None = None
        descendant: dict[str, Any] | None = None
        lineage: dict[str, Any] | None = None
        if descendant_attestation is None:
            automatic = _marker_lineage_reference(parent_record, parent_sha256, paths)
            if automatic is None:
                _validate_live_proof(parent_record, paths)
            else:
                current_marker, lineage = automatic
                _validate_descendant_parent_live_proof(parent_record, paths)
        else:
            current_marker, attestation = _validate_descendant_request(parent_record, parent_sha256, descendant_attestation, paths)
            descendant = {
                "version": MARKER_DESCENDANT_VERSION,
                "path": _marker_descendant_path(parent_record, parent_sha256, paths).name,
                "sha256": _record_hash(_descendant_body(attestation)),
                "reason": ROLLBACK_MARKER_REASON,
                "parent_transition_sha256": parent_sha256,
                "old_marker": parent_record["marker"],
                "current_marker": current_marker,
                "proof_sha256": attestation["proof_sha256"],
            }
        backup = _canonical_backup(backup_arg, paths)
        expected_marker = current_marker if current_marker is not None else parent_record["marker"]
        if backup != _canonical_backup(str(parent_record["backup"]["path"]), paths) or checksum != parent_record["backup"].get("sha256") or marker_sha != expected_marker.get("sha256") or marker_mtime != str(int(expected_marker["mtime_ns"] // 1_000_000_000)):
            raise ProofError("explicit supersession evidence does not match parent")
        _, tree, parent, parent_tree = _clean_commit(successor_commit, paths)
        evidence = (release_verifier or _github_release_evidence)(successor_pr, successor_commit, approved_pr_head, parent, parent_tree, paths)
        _validate_review_evidence(evidence, successor_pr, successor_commit, approved_pr_head, parent, parent_tree, tree)
        successor = {
            **{key: parent_record[key] for key in ("decision", "incident", "backup", "ledger", "publication_targets", "operator")},
            "marker": current_marker if current_marker is not None else parent_record["marker"],
            "version": parent_generation + 1, "code_commit": successor_commit,
            "parent": {"sha256": parent_sha256, "generation": parent_generation, "code_commit": parent_code_commit, "archive": _archive_transition_path(parent_record, parent_sha256, paths).name},
            "reviewed_release": {**evidence, "tree": tree, "github_evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if descendant is not None:
            successor["marker_descendant"] = descendant
        elif lineage is not None:
            successor["marker_lineage"] = lineage
        elif parent_record.get("marker_descendant") is not None:
            successor["marker_descendant"] = parent_record["marker_descendant"]
        elif parent_record.get("marker_lineage") is not None:
            successor["marker_lineage"] = parent_record["marker_lineage"]
        # A crash after the immutable archive write has already chosen the
        # timestamp.  Reuse that exact record on replay instead of deriving a
        # fresh body that could never safely replace it.
        replay_archive = _incident_dir(parent_record, paths) / f"transition-v{parent_generation + 1}-{parent_sha256}.json"
        if replay_archive.exists():
            archived_successor, _ = _read_record(replay_archive, "successor archive", paths)
            expected = dict(successor); expected.pop("timestamp")
            observed = dict(archived_successor); observed.pop("timestamp", None)
            if expected != observed:
                raise ProofError("successor archive replay mismatch")
            successor = archived_successor
    else:
        # Idempotent replay after the atomic active-link rename: reconstruct
        # the parent from the current successor and verify every supplied fact.
        if active.get("version") != parent_generation + 1 or active.get("parent", {}).get("sha256") != parent_sha256 or active.get("parent", {}).get("generation") != parent_generation or active.get("parent", {}).get("code_commit") != parent_code_commit or active.get("code_commit") != successor_commit:
            raise ProofError("active transition is stale or not this successor replay")
        successor = active
        _validate_chain(successor, active_body, paths)
        _validate_live_proof(successor, paths)
        if descendant_attestation is not None:
            chain = _validate_chain(successor, active_body, paths)
            if len(chain) != 2 or successor.get("marker_descendant") is None:
                raise ProofError("active successor is not the requested descendant attestation replay")
            parent_record = chain[0][0]
            current_marker, _ = _validate_descendant_request(parent_record, parent_sha256, descendant_attestation, paths)
            if successor.get("marker") != current_marker:
                raise ProofError("descendant attestation replay marker mismatch")
        backup = _canonical_backup(backup_arg, paths)
        if backup != _canonical_backup(str(successor["backup"]["path"]), paths) or checksum != successor["backup"].get("sha256") or marker_sha != successor["marker"].get("sha256") or marker_mtime != str(int(successor["marker"]["mtime_ns"] // 1_000_000_000)):
            raise ProofError("explicit supersession evidence does not match successor replay")
        _, tree, parent, parent_tree = _clean_commit(successor_commit, paths)
        evidence = (release_verifier or _github_release_evidence)(successor_pr, successor_commit, approved_pr_head, parent, parent_tree, paths)
        _validate_review_evidence(evidence, successor_pr, successor_commit, approved_pr_head, parent, parent_tree, tree)
        if tree != evidence["merge_tree"] or successor.get("reviewed_release", {}).get("tree") != tree or successor.get("reviewed_release", {}).get("github_evidence_sha256") != hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            raise ProofError("successor PR evidence changed")
    # The receipt was captured above, possibly through the hermetic verifier
    # seam.  Reprove the checkout at the final persistence boundary so no
    # changed HEAD/tree/parent or new untracked file can reach the archive or
    # even the prepared supersession journal.
    _, final_tree, final_parent, final_parent_tree = _revalidate_reviewed_checkout(evidence, successor_pr, successor_commit, approved_pr_head, paths)
    if successor.get("reviewed_release", {}).get("merge_tree") != final_tree or successor.get("reviewed_release", {}).get("base_sha") != final_parent or successor.get("reviewed_release", {}).get("base_tree") != final_parent_tree:
        raise ProofError("successor reviewed checkout changed before persistence")
    successor_body = _record_bytes(successor)
    successor_hash = _record_hash(successor_body)
    incident = _incident_dir(successor, paths)
    archive = _archive_transition_path(successor, successor_hash, paths)
    if archive.exists():
        old, old_body = _read_record(archive, "successor archive", paths)
        if old_body != successor_body:
            raise ProofError("successor archive replay mismatch")
    else:
        _write_new(archive, successor_body, 0o600); _fsync_dir(incident); _fsync_dir(paths.archive)
    test_capability.audit("safe-down-supersession-archive")
    test_capability.kill_if("supersession-archive")
    journal = incident / f"supersession-v{successor['version']}-{parent_sha256}.journal"
    prepared = {"status": "prepared", "parent_sha256": parent_sha256, "successor_sha256": successor_hash, "generation": successor["version"]}
    committed = {"status": "active_link_committed", "parent_sha256": parent_sha256, "successor_sha256": successor_hash, "generation": successor["version"]}
    journal_state = _supersession_journal_state(journal, prepared, committed, paths)
    if journal_state == "absent":
        _append_journal(journal, prepared, paths)
    test_capability.audit("safe-down-supersession-journal-prepared")
    test_capability.kill_if("supersession-journal-prepared")
    temporary = paths.state / f".safe-down-transition-v{successor['version']}-{successor_hash}.tmp"
    if temporary.exists():
        try:
            temp_info = os.stat(temporary, follow_symlinks=False)
            archive_info = os.stat(archive, follow_symlinks=False)
        except OSError as exc:
            raise ProofError("temporary successor link is unavailable") from exc
        if stat.S_ISLNK(temp_info.st_mode) or not stat.S_ISREG(temp_info.st_mode) or stat.S_IMODE(temp_info.st_mode) != 0o600 or (temp_info.st_dev, temp_info.st_ino) != (archive_info.st_dev, archive_info.st_ino):
            raise ProofError("temporary successor link was tampered")
    else:
        os.link(archive, temporary, follow_symlinks=False); _fsync_dir(paths.state)
    test_capability.audit("safe-down-supersession-link-temp")
    test_capability.kill_if("supersession-link-temp")
    _, current_body = _read_record(paths.transition, "active transition", paths)
    if current_body != successor_body:
        if _record_hash(current_body) != parent_sha256:
            raise ProofError("active transition changed during supersession")
        os.replace(temporary, paths.transition); _fsync_dir(paths.state)
    elif temporary.exists():
        os.unlink(temporary); _fsync_dir(paths.state)
    test_capability.audit("safe-down-supersession-active-link")
    test_capability.kill_if("supersession-active-link")
    journal_state = _supersession_journal_state(journal, prepared, committed, paths)
    if journal_state == "prepared":
        _append_journal(journal, committed, paths)
    test_capability.audit("safe-down-supersession-journal-committed")
    test_capability.kill_if("supersession-journal-committed")
    return successor


def prepare(backup_arg: str, checksum: str, marker_sha: str, marker_mtime: str, *, paths: Paths = Paths(), code_commit: str | None = None) -> int:
    """Create/verify an append-only transition record and leave it open on FD4."""
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or not re.fullmatch(r"[0-9a-f]{64}", marker_sha) or not re.fullmatch(r"\d{10}", marker_mtime):
        raise ProofError("explicit safe-down evidence is malformed")
    _exclusive_lock(3, paths)
    backup = _canonical_backup(backup_arg, paths)
    marker_fd, marker_info = _open_held(paths.marker, "recovery marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    backup_fd, backup_info = _open_held(backup, "backup", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    checksum_fd, _ = _open_held(backup.with_suffix(backup.suffix + ".sha256"), "backup checksum", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    handed_off = False
    try:
        # A reviewed successor has already made the active link the immutable
        # recovery authority.  Do not reconstruct a v1-shaped record over it:
        # prove the current record and hand that exact archive-linked object to
        # the installer instead.  ``is_symlink`` includes a dangling symlink,
        # which must still be rejected by the O_NOFOLLOW open below.
        if paths.transition.exists() or paths.transition.is_symlink():
            transition_fd, transition_info = _open_held(paths.transition, "active transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
            try:
                active_body = _read(transition_fd)
                try:
                    active = json.loads(active_body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ProofError("invalid active transition") from exc
                if not isinstance(active, dict) or _record_bytes(active) != active_body:
                    raise ProofError("noncanonical active transition")
                if transition_info.st_nlink < 2:
                    raise ProofError("active transition is not archive-linked")
                try:
                    active_backup_evidence = active["backup"]
                    active_marker = active["marker"]
                    if not isinstance(active_backup_evidence, dict) or not isinstance(active_marker, dict):
                        raise TypeError
                    active_backup = _canonical_backup(str(active_backup_evidence["path"]), paths)
                except (KeyError, TypeError) as exc:
                    raise ProofError("active transition recovery evidence is malformed") from exc
                if (_hash(marker_fd) != marker_sha or str(int(marker_info.st_mtime)) != marker_mtime or
                    _hash(backup_fd) != checksum or not _matches_checksum_sidecar(_read(checksum_fd), checksum, backup)):
                    raise ProofError("explicit recovery evidence no longer matches held descriptors")
                if (backup != active_backup or checksum != active_backup_evidence.get("sha256") or
                    marker_sha != active_marker.get("sha256") or marker_mtime != str(int(active_marker.get("mtime_ns", -1) // 1_000_000_000))):
                    raise ProofError("explicit recovery evidence does not match active transition")
                # Fsync the exact evidence that will cross exec before any
                # branch chooses v1 replay versus strict v2+ adoption.
                for fd in (transition_fd, marker_fd, backup_fd, checksum_fd): os.fsync(fd)
                _fsync_dir(paths.backups)
                _handoff_recovery_evidence(transition_fd, marker_fd, backup_fd, checksum_fd)
                # Fixed-FD assignment can replace this numeric descriptor;
                # its duplicate now owns the exec capability and must remain
                # open through installer adoption.
                transition_fd = -1
            finally:
                if transition_fd >= 0: os.close(transition_fd)

            # A v1 publish that was interrupted after its journal was written
            # is the sole deferred-proof case.  The installer first restores
            # that canonical transaction, then invokes the same strict adopt
            # path.  v2+ never receives this exception.
            if active.get("version") == TRANSITION_VERSION and _matching_publication_journal(paths, active):
                _validate_chain(active, active_body, paths)
                _validate_transition_checkout(active, paths, code_commit)
                _validate_held_recovery_evidence(active, paths)
            else:
                # Reuse the installer's complete FD-bound validation so active
                # v2+ records reprove reviewed provenance, descendant markers,
                # ledger, and publication state before they are handed off.
                adopt(paths=paths, code_commit=code_commit)
            handed_off = True
            test_capability.audit("safe-down-prepare-existing")
            test_capability.kill_if("prepare-existing")
            return 4

        marker_bytes = _read(marker_fd)
        if marker_bytes != MARKER_TEXT or _hash(marker_fd) != marker_sha or str(int(marker_info.st_mtime)) != marker_mtime:
            raise ProofError("recovery marker identity/reason mismatch")
        if _hash(backup_fd) != checksum:
            raise ProofError("backup checksum mismatch")
        if not _matches_checksum_sidecar(_read(checksum_fd), checksum, backup):
            raise ProofError("backup checksum sidecar mismatch")
        # Fsync held evidence before it is used for the migration decision.
        for fd in (marker_fd, backup_fd, checksum_fd): os.fsync(fd)
        _fsync_dir(paths.backups)
        failed = BACKUP_NAME.fullmatch(backup.name)
        assert failed is not None
        if code_commit is None:
            code_commit = _code_commit(paths)
        # A journal bound to this immutable transition is the sole exception
        # to the ordinary pre-publication proof.  Publication necessarily
        # migrates the live database before it writes the journal's first
        # target, so checking schema zero first would turn a recoverable
        # SIGKILL into a permanent block.  The record is reconstructed below
        # and must byte-match the archive-linked transition before it is used.
        allow_publication_journal_replay = False
        if allow_publication_journal_replay:
            prior = replay.get("publication_targets", {})
            live_digest = replay.get("ledger")
            states = prior.get("states")
            state_digest = prior.get("digest")
            if not isinstance(live_digest, dict) or not isinstance(states, list) or not isinstance(state_digest, str):
                raise ProofError("invalid replay transition proof")
        else:
            backup_digest = _sqlite_digest(backup, paths, "backup")
            live_digest = _sqlite_digest(paths.db, paths, "bridge ledger")
            if live_digest != backup_digest:
                raise ProofError("live ledger differs from verified backup")
            states, state_digest = _publication_state(paths, marker_info.st_mtime_ns, failed.group(1))
        identity = f"{marker_sha}-{marker_info.st_dev:x}-{marker_info.st_ino:x}-{marker_info.st_mtime_ns}"
        if paths.archive.exists() and (paths.archive.is_symlink() or not paths.archive.is_dir()):
            raise ProofError("unsafe incident archive path")
        paths.archive.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive_info = paths.archive.stat()
        if archive_info.st_uid != paths.expected_uid or archive_info.st_gid != paths.expected_gid or stat.S_IMODE(archive_info.st_mode) != 0o700:
            raise ProofError("unsafe incident archive metadata")
        os.chmod(paths.archive, 0o700)
        _fsync_dir(paths.state)
        test_capability.audit("safe-down-archive")
        test_capability.kill_if("archive")
        incident = paths.archive / identity
        try:
            os.mkdir(incident, 0o700)
            created = True
        except FileExistsError:
            created = False
        marker_archive = incident / "marker"
        if created:
            _write_new(marker_archive, marker_bytes, 0o600)
            _fsync_dir(incident)
        else:
            fd, _ = _open_held(marker_archive, "archived marker", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
            try:
                if _read(fd) != marker_bytes: raise ProofError("incident archive marker replay mismatch")
            finally: os.close(fd)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        transition = incident / "transition.json"
        if transition.exists():
            fd, _ = _open_held(transition, "transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
            try:
                old = _read(fd)
            finally: os.close(fd)
            try:
                previous = json.loads(old)
                timestamp = previous["timestamp"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ProofError("invalid archived transition") from exc
        record = _record_for_proof(backup, backup_info, marker_info, marker_sha, checksum, live_digest, states, state_digest, code_commit, timestamp)
        body = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if transition.exists():
            if old != body: raise ProofError("incident transition replay mismatch")
        else:
            _write_new(transition, body, 0o600); _fsync_dir(incident); _fsync_dir(paths.archive)
        test_capability.audit("safe-down-transition")
        test_capability.kill_if("transition")
        # The active record is a hard link to the immutable archived record;
        # it is opened and inherited, never reopened by pathname in adoption.
        os.link(transition, paths.transition, follow_symlinks=False); os.chmod(paths.transition, 0o600); _fsync_dir(paths.state)
        transition_fd, _ = _open_held(paths.transition, "active transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
        if os.fstat(transition_fd).st_nlink < 2: raise ProofError("transition is not archive-linked")
        # All evidence remains held through archive creation and exec.  The
        # installer revalidates these exact file descriptions before staging.
        _handoff_recovery_evidence(transition_fd, marker_fd, backup_fd, checksum_fd)
        handed_off = True
        test_capability.audit("safe-down-prepare")
        return 4
    finally:
        # Handed-off descriptors are intentionally retained over exec.
        if not handed_off:
            for fd in (marker_fd, backup_fd, checksum_fd): os.close(fd)


def _validate_held_recovery_evidence(record: dict[str, Any], paths: Paths) -> tuple[Path, os.stat_result]:
    """Validate the immutable transition and inherited marker/backup FDs."""
    try:
        backup = _canonical_backup(str(record["backup"]["path"]), paths)
        marker = record["marker"]
    except (KeyError, TypeError) as exc:
        raise ProofError("transition recovery evidence is malformed") from exc
    marker_info = _ensure_fd_matches(5, paths.marker, "marker")
    backup_info = _ensure_fd_matches(6, backup, "backup")
    _ensure_fd_matches(7, backup.with_suffix(backup.suffix + ".sha256"), "backup checksum")
    if _read(5) != _marker_bytes_for_record(record) or _hash(5) != marker.get("sha256") or (marker_info.st_dev, marker_info.st_ino, marker_info.st_mtime_ns) != (marker.get("dev"), marker.get("ino"), marker.get("mtime_ns")):
        raise ProofError("held marker no longer proves safe-down")
    if _hash(6) != record["backup"].get("sha256") or (backup_info.st_dev, backup_info.st_ino) != (record["backup"].get("dev"), record["backup"].get("ino")):
        raise ProofError("held backup no longer proves safe-down")
    if not _matches_checksum_sidecar(_read(7), record["backup"]["sha256"], backup):
        raise ProofError("held backup checksum mismatch")
    for fd in (4, 5, 6, 7):
        os.fsync(fd)
    _fsync_dir(paths.backups)
    return backup, marker_info


def _validate_transition_checkout(record: dict[str, Any], paths: Paths, code_commit: str | None) -> str:
    """Prove that a transition is adoptable by this exact checked-out code."""
    if code_commit is None:
        code_commit = _code_commit(paths)
    if record.get("version") == TRANSITION_VERSION and record.get("code_commit") != code_commit:
        raise ProofError("transition code commit mismatch")
    if record.get("version", 0) >= SUPERSEDE_MIN_VERSION:
        reviewed = record.get("reviewed_release", {})
        _, tree, parent, parent_tree = _clean_commit(code_commit, paths)
        receipt_keys = {"number", "url", "state", "base", "base_repo", "head_repo", "base_sha", "base_tree", "head", "merge", "merge_tree", "merge_parents", "current", "merged_at"}
        evidence = {key: reviewed.get(key) for key in receipt_keys}
        _validate_review_evidence(evidence, int(reviewed.get("number", 0)), code_commit, str(reviewed.get("head", "")), parent, parent_tree, tree)
        if reviewed.get("github_evidence_sha256") != hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            raise ProofError("successor GitHub evidence hash changed")
        if record.get("code_commit") != code_commit or reviewed.get("current") != code_commit or reviewed.get("tree") != tree or reviewed.get("merge_tree") != tree:
            raise ProofError("successor transition code commit mismatch")
    return code_commit


def adopt(*, paths: Paths = Paths(), code_commit: str | None = None) -> dict[str, Any]:
    """Installer-side revalidation using inherited FD3 and FD4 only."""
    _exclusive_lock(3, paths)
    info = _ensure_fd_matches(4, paths.transition, "transition")
    _regular_root_0600(4, "transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    if info.st_nlink < 2: raise ProofError("transition capability is not archive-linked")
    try:
        record = json.loads(_read(4).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProofError("invalid transition") from exc
    body = _record_bytes(record) if isinstance(record, dict) else b""
    if not isinstance(record, dict) or _read(4) != body:
        raise ProofError("noncanonical transition")
    _validate_chain(record, body, paths)
    _validate_transition_checkout(record, paths, code_commit)
    backup, marker_info = _validate_held_recovery_evidence(record, paths)
    live = _sqlite_digest(paths.db, paths, "bridge ledger"); archived = _sqlite_digest(backup, paths, "backup")
    if live != archived or live != record.get("ledger"):
        raise ProofError("safe-down ledger proof changed")
    reconciled_v1 = _v1_reconciled_publication_journal(paths, record)
    states, digest = _publication_state(paths, marker_info.st_mtime_ns, BACKUP_NAME.fullmatch(backup.name).group(1), require_pre_marker=not reconciled_v1)  # type: ignore[union-attr]
    expected_states = record.get("publication_targets", {}).get("states")
    if reconciled_v1:
        publication_matches = isinstance(expected_states, list) and _publication_semantic_state(states) == _publication_semantic_state(expected_states)
    else:
        publication_matches = digest == record.get("publication_targets", {}).get("digest") and states == expected_states
    if not publication_matches:
        raise ProofError("publication target proof changed")
    test_capability.audit("installer-adoption")
    test_capability.kill_if("installer-adoption")
    return record


def v1_publication_replay_needed(*, paths: Paths = Paths()) -> None:
    """Gate the installer's existing publication rollback before strict adopt.

    This is intentionally v1-only.  It does not bless the live state or
    mutate it; the checked-in publication transaction owns reconciliation and
    rollback, after which ``adopt`` performs its ordinary strict proof.
    """
    _exclusive_lock(3, paths)
    info = _ensure_fd_matches(4, paths.transition, "transition")
    _regular_root_0600(4, "transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    if info.st_nlink < 2: raise ProofError("transition capability is not archive-linked")
    try: record = json.loads(_read(4).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc: raise ProofError("invalid transition") from exc
    body = _record_bytes(record) if isinstance(record, dict) else b""
    if not isinstance(record, dict) or _read(4) != body or record.get("version") != TRANSITION_VERSION:
        raise ProofError("publication journal replay is not a v1 transition")
    _validate_chain(record, body, paths)
    _validate_held_recovery_evidence(record, paths)
    if not _matching_publication_journal(paths, record):
        raise ProofError("publication journal is not a valid v1 replay")


def adopted_transition_version(*, paths: Paths = Paths()) -> int:
    """Return the exact inherited transition version for installer dispatch."""
    _exclusive_lock(3, paths)
    info = _ensure_fd_matches(4, paths.transition, "transition")
    _regular_root_0600(4, "transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    if info.st_nlink < 2: raise ProofError("transition capability is not archive-linked")
    try:
        record = json.loads(_read(4).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProofError("invalid transition") from exc
    body = _record_bytes(record) if isinstance(record, dict) else b""
    if not isinstance(record, dict) or _read(4) != body:
        raise ProofError("noncanonical transition")
    _validate_chain(record, body, paths)
    version = record.get("version")
    if not isinstance(version, int):
        raise ProofError("transition version is invalid")
    return version


def complete(*, paths: Paths = Paths()) -> None:
    """Append a completion record, fsync it, then durably clear active state."""
    # By this point the new bridge is running its normal shared runtime lock
    # for the health check, so reacquiring the exclusive lock would deadlock
    # the final commit.  FD4 is still the immutable transition capability
    # inherited from the lock-held adoption session.
    _ensure_fd_matches(4, paths.transition, "transition")
    _regular_root_0600(4, "transition", allow_links=True, expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
    record = json.loads(_read(4).decode("utf-8"))
    incident = paths.archive / record["incident"]
    if not incident.is_dir() or incident.is_symlink(): raise ProofError("incident archive missing")
    completion = incident / "completion.json"
    data = (json.dumps({"decision": "new_release_health_committed", "incident": record["incident"], "transition_sha256": hashlib.sha256(_read(4)).hexdigest()}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if completion.exists():
        fd, _ = _open_held(completion, "completion", expected_uid=paths.expected_uid, expected_gid=paths.expected_gid)
        try:
            if _read(fd) != data: raise ProofError("completion replay conflict")
        finally: os.close(fd)
    else:
        _write_new(completion, data, 0o600); _fsync_dir(incident); _fsync_dir(paths.archive)
    # Marker removal is the final commit point.  The archived marker and
    # transition remain append-only evidence after the active links are gone.
    # Transition is an auxiliary active link; the marker is the final visible
    # recovery state and is deliberately unlinked last.
    os.unlink(paths.transition); _fsync_dir(paths.state)
    os.unlink(paths.marker); _fsync_dir(paths.state)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--backup", required=True); prep.add_argument("--checksum", required=True)
    prep.add_argument("--marker-sha256", required=True); prep.add_argument("--marker-mtime", required=True)
    prep.add_argument("--exec-install", action="store_true")
    sup = sub.add_parser("supersede")
    sup.add_argument("--parent-transition-sha256", required=True); sup.add_argument("--parent-generation", required=True, type=int)
    sup.add_argument("--parent-code-commit", required=True); sup.add_argument("--backup", required=True); sup.add_argument("--checksum", required=True)
    sup.add_argument("--marker-sha256", required=True); sup.add_argument("--marker-mtime", required=True)
    sup.add_argument("--successor-commit", required=True); sup.add_argument("--successor-pr", required=True, type=int); sup.add_argument("--approved-pr-head", required=True)
    sup.add_argument("--attest-descendant-marker", action="store_true")
    sup.add_argument("--old-marker-sha256"); sup.add_argument("--old-marker-dev", type=int); sup.add_argument("--old-marker-ino", type=int); sup.add_argument("--old-marker-mtime-ns", type=int)
    sup.add_argument("--marker-dev", type=int); sup.add_argument("--marker-ino", type=int); sup.add_argument("--marker-mtime-ns", type=int); sup.add_argument("--terminal-marker-reason")
    lineage = sub.add_parser("update-marker-lineage")
    lineage.add_argument("--reason", required=True)
    sub.add_parser("adopt")
    sub.add_parser("adopted-transition-version")
    sub.add_parser("v1-publication-replay-needed")
    sub.add_parser("complete")
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            prepare(args.backup, args.checksum, args.marker_sha256, args.marker_mtime, paths=_command_paths())
            if args.exec_install:
                script = Path(__file__).with_name("install.sh")
                os.execv("/bin/bash", ["bash", str(script), "--recover-safe-down"])
        elif args.mode == "adopt":
            adopt(paths=_command_paths())
        elif args.mode == "v1-publication-replay-needed":
            v1_publication_replay_needed(paths=_command_paths())
        elif args.mode == "adopted-transition-version":
            print(adopted_transition_version(paths=_command_paths()))
        elif args.mode == "update-marker-lineage":
            update_marker_lineage(args.reason, paths=_command_paths(), barrier_evidence=True)
        elif args.mode == "supersede":
            descendant = None
            if args.attest_descendant_marker:
                values = (args.old_marker_sha256, args.old_marker_dev, args.old_marker_ino, args.old_marker_mtime_ns, args.marker_dev, args.marker_ino, args.marker_mtime_ns, args.terminal_marker_reason)
                if any(value is None for value in values):
                    raise ProofError("complete descendant marker attestation identity is required")
                descendant = {
                    "old_marker": {"sha256": args.old_marker_sha256, "dev": args.old_marker_dev, "ino": args.old_marker_ino, "mtime_ns": args.old_marker_mtime_ns},
                    "current_marker": {"sha256": args.marker_sha256, "dev": args.marker_dev, "ino": args.marker_ino, "mtime_ns": args.marker_mtime_ns},
                    "reason": args.terminal_marker_reason,
                }
            elif any(value is not None for value in (args.old_marker_sha256, args.old_marker_dev, args.old_marker_ino, args.old_marker_mtime_ns, args.marker_dev, args.marker_ino, args.marker_mtime_ns, args.terminal_marker_reason)):
                raise ProofError("descendant marker values require explicit attestation mode")
            supersede(parent_sha256=args.parent_transition_sha256, parent_generation=args.parent_generation, parent_code_commit=args.parent_code_commit,
                      backup_arg=args.backup, checksum=args.checksum, marker_sha=args.marker_sha256, marker_mtime=args.marker_mtime,
                      successor_commit=args.successor_commit, successor_pr=args.successor_pr, approved_pr_head=args.approved_pr_head, paths=_command_paths(), barrier_evidence=True, descendant_attestation=descendant)
        else:
            complete(paths=_command_paths())
    except ProofError as exc:
        print(f"safe-down proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
