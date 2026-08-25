#!/usr/bin/env python3
"""Capture read-only safe-down barrier evidence across an exec boundary."""
from __future__ import annotations

import argparse, hashlib, json, os, stat, subprocess, sys
from pathlib import Path
from barrier_unit_state import UnitStateError, systemctl_value, unit_is_absent
import test_capability

HERE = Path(__file__).resolve().parent
ENV = "DRIFTWARD_SAFE_DOWN_BARRIER_EVIDENCE"
# Keep well above ordinary inherited descriptors and do not overwrite a later
# captured drop-in while remapping the first descriptor.
FIRST_FD = 100


class BarrierError(RuntimeError):
    pass


def _settings() -> tuple[Path, Path, Path, int, int]:
    cap = test_capability.load()
    if cap is None:
        return Path("/run/driftward-hatchet-bridge"), Path("/run/systemd/system"), Path("/var/lib/driftward-hatchet-bridge"), 0, 0
    return cap.runtime, cap.root / "run/systemd/system", cap.state, cap.uid, cap.gid


def _units() -> list[str]:
    try:
        pass_fds: tuple[int, ...] = ()
        if test_capability.load() is not None:
            pass_fds = (int(os.environ.get("BRIDGE_TEST_CAPABILITY_FD", "8")),)
        return subprocess.check_output([sys.executable, str(HERE / "writer-inventory.py"), "--kind", "all"], text=True, pass_fds=pass_fds).splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BarrierError("cannot load maintenance writer inventory") from exc


def _hash(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    while data := os.read(fd, 1 << 20):
        value.update(data)
    return value.hexdigest()


def _open(path: Path, *, mode: int, uid: int, gid: int, kind: str, unit: str | None = None) -> tuple[int, dict[str, object]]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise BarrierError(f"cannot open maintenance {kind}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode:
        os.close(fd); raise BarrierError(f"unsafe maintenance {kind}")
    return fd, {"kind": kind, "unit": unit, "path": str(path), "dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "gid": info.st_gid, "mode": mode, "sha256": _hash(fd)}


def _quiescent(entries: list[dict[str, object]], runtime: Path) -> None:
    for entry in entries:
        if entry["kind"] != "dropin":
            continue
        unit = str(entry["unit"])
        if unit_is_absent(unit):
            continue
        active = systemctl_value(unit, "ActiveState")
        dropins = systemctl_value(unit, "DropInPaths", single_line=False)
        if active not in ("inactive", "failed") or str(entry["path"]) not in dropins:
            raise BarrierError("maintenance barrier is not quiescent")
    for unit in ("driftward-hatchet-bridge-reconciler.service", "driftward-hatchet-bridge-reconciler.timer"):
        if systemctl_value(unit, "ActiveState") != "inactive":
            raise BarrierError("reconciler remains active")
    result = subprocess.run(["lsof", str(runtime / "bridge.db"), str(runtime / "bridge.db-wal"), str(runtime / "bridge.db-shm")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 1:
        raise BarrierError("cannot prove bridge DB has no holder")


def capture() -> tuple[list[int], dict[str, object]]:
    runtime, systemd, state, uid, gid = _settings()
    active = runtime / "maintenance.active"
    fds: list[int] = []
    try:
        fd, sentinel = _open(active, mode=0o600, uid=uid, gid=gid, kind="sentinel"); fds.append(fd)
        entries = [sentinel]
        for unit in _units():
            path = systemd / f"{unit}.d/90-driftward-bridge-maintenance.conf"
            fd, entry = _open(path, mode=0o644, uid=uid, gid=gid, kind="dropin", unit=unit)
            os.lseek(fd, 0, os.SEEK_SET)
            if f"ConditionPathExists=!{active}".encode() not in os.read(fd, 1 << 20):
                os.close(fd); raise BarrierError("maintenance drop-in condition is unsafe")
            fds.append(fd); entries.append(entry)
        _quiescent(entries, state)
        for index, fd in enumerate(fds):
            destination = FIRST_FD + index
            os.set_inheritable(fd, True)
            if fd != destination:
                os.dup2(fd, destination, inheritable=True); os.close(fd); fds[index] = destination
            entries[index]["fd"] = destination
        return fds, {"version": 1, "entries": entries}
    except UnitStateError as exc:
        for fd in fds:
            try: os.close(fd)
            except OSError: pass
        raise BarrierError(f"maintenance barrier unit-state proof failed: {exc}") from exc
    except BaseException:
        for fd in fds:
            try: os.close(fd)
            except OSError: pass
        raise


def valid() -> bool:
    try:
        evidence = json.loads(os.environ[ENV]); entries = evidence["entries"]
        if evidence.get("version") != 1 or not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            info = os.fstat(int(entry["fd"]))
            if (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (entry["dev"], entry["ino"], entry["uid"], entry["gid"], entry["mode"]) or _hash(int(entry["fd"])) != entry["sha256"]:
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=("check", "exec")); parser.add_argument("command", nargs=argparse.REMAINDER); args = parser.parse_args()
    if args.mode == "check": return 0 if valid() else 1
    if not args.command: parser.error("exec needs command")
    _, evidence = capture(); os.environ[ENV] = json.dumps(evidence, sort_keys=True, separators=(",", ":")); os.execvp(args.command[0], args.command)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (BarrierError, OSError) as exc: print(f"maintenance barrier proof failed: {exc}", file=sys.stderr); raise SystemExit(1)
