#!/usr/bin/env python3
"""Private, inherited capability for hermetic bridge recovery tests.

This is intentionally not a general test configuration interface.  A caller
must explicitly select an inherited read-only descriptor and present its
expected inode and nonce.  Its pathname, contents, owner, and private parent
are revalidated before production executables accept alternate paths or a fake
command directory.  Root never accepts test selection or overrides.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MODE = "driftward-hatchet-bridge-hermetic-test-v1"
OVERRIDE_PREFIXES = ("HATCHET_BRIDGE_TEST_", "BRIDGE_TEST_")
CAPABILITY_FD_ENV = "BRIDGE_TEST_CAPABILITY_FD"
CAPABILITY_INODE_ENV = "BRIDGE_TEST_CAPABILITY_INODE"
CAPABILITY_NONCE_ENV = "BRIDGE_TEST_CAPABILITY_NONCE"


class CapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class TestCapability:
    root: Path
    repository: Path
    fake_bin: Path
    audit: Path
    capability_path: Path
    uid: int
    gid: int
    release_version: str
    kill_at: str | None
    signal_at: str | None
    prebuilt_trigger: Path

    @property
    def state(self) -> Path: return self.root / "var/lib/driftward-hatchet-bridge"
    @property
    def backups(self) -> Path: return self.root / "root/driftward-backups/hatchet"
    @property
    def release(self) -> Path: return self.root / "docker/driftward/hatchet/releases/bridge"
    @property
    def etc(self) -> Path: return self.root / "etc/driftward"
    @property
    def systemd(self) -> Path: return self.root / "etc/systemd/system"
    @property
    def runtime(self) -> Path: return self.root / "run/driftward-hatchet-bridge"
    @property
    def runtime_systemd(self) -> Path: return self.root / "run/systemd/system"


def _read(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while part := os.read(fd, 1 << 20): chunks.append(part)
    return b"".join(chunks)


def _inside(root: Path, value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute(): raise CapabilityError(f"test capability {name} must be absolute")
    resolved = path.resolve(strict=True)
    try: resolved.relative_to(root)
    except ValueError as exc: raise CapabilityError(f"test capability {name} escapes root") from exc
    return resolved


def _regular(fd: int, uid: int, gid: int, label: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise CapabilityError(f"unsafe {label} capability metadata")
    return info


def reject_root_overrides() -> None:
    if os.geteuid() == 0 and any(name.startswith(OVERRIDE_PREFIXES) for name in os.environ):
        raise CapabilityError("root invocation rejects bridge test overrides")


def _selector() -> tuple[int, int, str] | None:
    selected = tuple(os.environ.get(name) for name in (CAPABILITY_FD_ENV, CAPABILITY_INODE_ENV, CAPABILITY_NONCE_ENV))
    if selected == (None, None, None):
        return None
    if any(value is None for value in selected):
        raise CapabilityError("incomplete test capability selector")
    try:
        fd = int(selected[0])  # type: ignore[arg-type]
        expected_inode = int(selected[1])  # type: ignore[arg-type]
    except ValueError as exc:
        raise CapabilityError("invalid test capability selector") from exc
    expected_nonce = selected[2]
    if fd < 3 or expected_inode < 1 or not isinstance(expected_nonce, str) or len(expected_nonce) != 64:
        raise CapabilityError("invalid test capability selector")
    return fd, expected_inode, expected_nonce


def load() -> TestCapability | None:
    """Return only an explicitly selected non-root test capability.

    Normal processes may inherit arbitrary descriptors from a shell, runner,
    or supervisor.  In particular, FD 8 is not a capability unless the
    hermetic harness has supplied its number plus the expected inode and nonce.
    """
    reject_root_overrides()
    if os.geteuid() == 0:
        return None
    selector = _selector()
    if selector is None:
        return None
    fd, expected_inode, expected_nonce = selector
    try:
        info = _regular(fd, os.geteuid(), os.getegid(), "test")
    except OSError as exc:
        raise CapabilityError("missing selected test capability") from exc
    if info.st_ino != expected_inode:
        raise CapabilityError("test capability inode selector mismatch")
    try:
        value = json.loads(_read(fd).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CapabilityError("malformed test capability") from exc
    required = {"mode", "root", "repository", "fake_bin", "audit", "capability_path", "prebuilt_trigger", "uid", "gid", "release_version", "nonce", "content_sha256"}
    if set(value) - (required | {"kill_at", "signal_at"}) or not required <= set(value) or value["mode"] != MODE:
        raise CapabilityError("unexpected test capability schema")
    if value["uid"] != os.geteuid() or value["gid"] != os.getegid() or value["nonce"] != expected_nonce:
        raise CapabilityError("test capability identity mismatch")
    root = Path(value["root"]).resolve(strict=True)
    root_info = root.stat()
    if not root.is_dir() or root_info.st_uid != os.geteuid() or root_info.st_gid != os.getegid() or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise CapabilityError("unsafe test capability root")
    capability_path = _inside(root, value["capability_path"], "pathname")
    path_info = os.stat(capability_path, follow_symlinks=False)
    if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
        raise CapabilityError("test capability inode mismatch")
    digest_value = value.pop("content_sha256", None)
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if hashlib.sha256(canonical).hexdigest() != digest_value:
        raise CapabilityError("test capability content digest mismatch")
    cap = TestCapability(
        root=root, repository=_inside(root, value["repository"], "repository"), fake_bin=_inside(root, value["fake_bin"], "fake_bin"),
        audit=_inside(root, value["audit"], "audit"), capability_path=capability_path, uid=value["uid"], gid=value["gid"],
        release_version=value["release_version"], kill_at=value.get("kill_at"), signal_at=value.get("signal_at"), prebuilt_trigger=_inside(root, value["prebuilt_trigger"], "prebuilt trigger"),
    )
    for path, label in ((cap.repository, "repository"), (cap.fake_bin, "fake command directory")):
        meta = path.stat()
        if not path.is_dir() or meta.st_uid != cap.uid or meta.st_gid != cap.gid or stat.S_IMODE(meta.st_mode) not in (0o700, 0o755):
            raise CapabilityError(f"unsafe test {label}")
    audit_info = os.stat(cap.audit, follow_symlinks=False)
    if stat.S_ISLNK(audit_info.st_mode) or not stat.S_ISREG(audit_info.st_mode) or audit_info.st_uid != cap.uid or audit_info.st_gid != cap.gid or stat.S_IMODE(audit_info.st_mode) != 0o600:
        raise CapabilityError("unsafe test audit")
    trigger_info = os.stat(cap.prebuilt_trigger, follow_symlinks=False)
    if stat.S_ISLNK(trigger_info.st_mode) or not stat.S_ISREG(trigger_info.st_mode) or trigger_info.st_uid != cap.uid or trigger_info.st_gid != cap.gid or stat.S_IMODE(trigger_info.st_mode) != 0o644:
        raise CapabilityError("unsafe test prebuilt trigger")
    if not isinstance(cap.release_version, str) or len(cap.release_version) != 40 or any(c not in "0123456789abcdef" for c in cap.release_version):
        raise CapabilityError("invalid test release version")
    return cap


def require() -> TestCapability:
    cap = load()
    if cap is None: raise CapabilityError("missing inherited bridge test capability")
    return cap


def shell() -> str:
    cap = require()
    selector = _selector()
    assert selector is not None
    fd, inode, nonce = selector
    values = {
        "BRIDGE_TEST_MODE": "1", "BRIDGE_TEST_ROOT": str(cap.root), "BRIDGE_TEST_REPOSITORY": str(cap.repository),
        "BRIDGE_TEST_FAKE_BIN": str(cap.fake_bin), "BRIDGE_TEST_AUDIT": str(cap.audit), "BRIDGE_TEST_UID": str(cap.uid),
        "BRIDGE_TEST_GID": str(cap.gid), "BRIDGE_TEST_RELEASE_VERSION": cap.release_version,
        "DRIFTWARD_BRIDGE_RELEASE_VERSION": cap.release_version,
        "BRIDGE_TEST_PREBUILT_TRIGGER": str(cap.prebuilt_trigger),
        CAPABILITY_FD_ENV: str(fd), CAPABILITY_INODE_ENV: str(inode), CAPABILITY_NONCE_ENV: nonce,
    }
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())


def audit(event: str, **extra: Any) -> None:
    cap = load()
    if cap is None: return
    fds: dict[str, dict[str, int]] = {}
    for number in (3, 4, 5, 6, 7, 8):
        try:
            meta = os.fstat(number); fds[str(number)] = {"dev": meta.st_dev, "ino": meta.st_ino}
        except OSError: pass
    row = {"event": event, "pid": os.getpid(), "fds": fds, **extra}
    fd = os.open(cap.audit, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _regular(fd, cap.uid, cap.gid, "test audit")
        os.write(fd, (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally: os.close(fd)


def kill_if(point: str) -> None:
    cap = load()
    if cap is not None and cap.kill_at == point:
        audit("sigkill", point=point)
        # The test wrapper starts a fresh process session.  Killing that whole
        # session models host SIGKILL precisely: no shell EXIT trap can turn a
        # child interruption into a synthetic rollback before the rerun.
        os.killpg(os.getpgrp(), signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=("shell", "maybe-shell", "assert", "guard", "audit", "kill-if", "signal-if")); parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    try:
        if args.mode == "shell": print(shell())
        elif args.mode == "maybe-shell":
            cap = load()
            if cap is None: return 1
            print(shell())
        elif args.mode == "assert": require()
        elif args.mode == "guard": reject_root_overrides()
        elif args.mode == "audit":
            if not args.value: parser.error("audit needs event")
            audit(args.value)
        elif args.mode == "kill-if":
            if not args.value: parser.error("kill-if needs point")
            kill_if(args.value)
        else:
            if not args.value: parser.error("signal-if needs point")
            cap = load()
            if cap is None or cap.signal_at != args.value: return 1
            audit("signal", point=args.value)
    except CapabilityError as exc:
        print(f"bridge test capability failed: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
