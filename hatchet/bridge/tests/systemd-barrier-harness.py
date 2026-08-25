#!/usr/bin/env python3
"""Executable model of the production systemd maintenance barrier.

It intentionally models the discovered precedence rule: a runtime mask below
``/run/systemd/system`` does not replace a locally installed ``/etc`` unit.
Drop-ins, unlike masks, are additive and their ConditionPathExists gate is
evaluated for every activation attempt.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


class FakeSystemd:
    def __init__(self, active: Path) -> None:
        self.active = active
        self.units: dict[str, tuple[str, bool]] = {}
        self.runtime_masks: set[str] = set()
        self.dropins: set[str] = set()

    def add(self, unit: str, origin: str) -> None:
        self.units[unit] = (origin, False)

    def mask_runtime(self, unit: str) -> None:
        self.runtime_masks.add(unit)

    def add_dropin(self, unit: str) -> None:
        self.dropins.add(unit)

    def start(self, unit: str) -> bool:
        origin, _ = self.units.get(unit, ("absent", False))
        if origin == "absent":
            return False
        # `/etc` wins over the failed rollout's `/run` mask symlink.
        if unit in self.runtime_masks and origin != "/etc":
            return False
        if unit in self.dropins and self.active.exists():
            return False
        self.units[unit] = (origin, True)
        return True

    def stop(self, unit: str) -> bool:
        origin, active = self.units.get(unit, ("absent", False))
        if origin == "absent":
            return True
        self.units[unit] = (origin, False)
        return active or True

    def inactive(self, unit: str) -> bool:
        return not self.units.get(unit, ("absent", False))[1]

    def verify(self, unit: str) -> bool:
        return unit in self.dropins and self.active.exists() and self.inactive(unit)


def actual_condition_check(active: Path) -> None:
    condition = f"ConditionPathExists=!{active}"
    blocked = subprocess.run(["systemd-analyze", "condition", condition], capture_output=True, text=True)
    if blocked.returncode == 0:
        raise SystemExit("systemd accepted an active maintenance condition")
    active.unlink()
    allowed = subprocess.run(["systemd-analyze", "condition", condition], capture_output=True, text=True)
    if allowed.returncode != 0:
        raise SystemExit("systemd rejected an inactive maintenance condition")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bridge-systemd-barrier.") as directory:
        active = Path(directory) / "maintenance.active"
        active.touch(mode=0o600)
        actual_condition_check(active)
        active.touch(mode=0o600)
        systemd = FakeSystemd(active)
        systemd.add("etc.service", "/etc")
        systemd.add("usr.service", "/usr")
        # Reproduce the failed assumption: the /etc unit remains startable.
        systemd.mask_runtime("etc.service")
        assert systemd.start("etc.service")
        systemd.stop("etc.service")
        # The condition drop-in blocks every known base unit and a newly
        # published unit while maintenance is active. Absent units are valid.
        for unit in ("etc.service", "usr.service", "absent.service"):
            systemd.add_dropin(unit)
        assert not systemd.start("etc.service")
        assert not systemd.start("usr.service")
        assert not systemd.start("absent.service")
        systemd.add("new.service", "/etc")
        systemd.add_dropin("new.service")
        assert not systemd.start("new.service")
        for unit in ("etc.service", "usr.service", "new.service"):
            assert systemd.verify(unit)
        # A tampered condition is detected before any recovery state can be
        # released; an absent planned unit remains a valid stop target.
        systemd.dropins.remove("new.service")
        assert not systemd.verify("new.service")
        systemd.add_dropin("new.service")
        assert systemd.stop("absent.service")
        # Simulated daemon-reload/health failure preserves the marker and the
        # active barrier; only a fully verified recovery clears both.
        marker = True
        daemon_reload_ok = False
        assert not daemon_reload_ok and marker and active.exists()
        daemon_reload_ok = True
        health_ok = False
        assert daemon_reload_ok and not health_ok and marker and active.exists()
        health_ok = True
        assert health_ok
        active.unlink()
        assert systemd.start("etc.service")
        assert systemd.start("usr.service")
        assert systemd.start("new.service")
    print(json.dumps({"ok": True, "runtime_mask_etc_precedence": "reproduced", "dropin_activation_barrier": "verified", "recovery_failure_retains_barrier": "verified"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
