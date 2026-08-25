#!/usr/bin/env python3
"""Hermetic proof tests for absent planned writer units."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))
import barrier_evidence_exec as evidence
import barrier_unit_state as unit_state
import safe_down_recovery as safe


class BarrierEvidenceExecTests(unittest.TestCase):
    unit = "planned-factory.service"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.runtime = root / "run/driftward-hatchet-bridge"; self.runtime.mkdir(parents=True)
        self.systemd = root / "run/systemd/system"; self.systemd.mkdir(parents=True)
        self.state = root / "var/lib/driftward-hatchet-bridge"; self.state.mkdir(parents=True)
        self.active = self.runtime / "maintenance.active"; self.active.touch(); self.active.chmod(0o600)
        self.dropin = self.systemd / f"{self.unit}.d/90-driftward-bridge-maintenance.conf"; self.dropin.parent.mkdir()
        self.dropin.write_text(f"[Unit]\nConditionPathExists=!{self.active}\n"); self.dropin.chmod(0o644)
        self.fds: list[int] = []

    def tearDown(self) -> None:
        for fd in self.fds:
            try: os.close(fd)
            except OSError: pass
        self.temp.cleanup()

    def runner(self, load_state: str = "not-found", *, active: str = "inactive", dropins: str | None = None, raw_dropins: bool = False, error: bool = False):
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "lsof": return subprocess.CompletedProcess(command, 1, "", "")
            prop = command[command.index("-p") + 1]
            if error and prop == "LoadState": return subprocess.CompletedProcess(command, 1, "", "systemctl error")
            values = {"LoadState": load_state, "ActiveState": active, "DropInPaths": str(self.dropin) if dropins is None else dropins}
            output = values[prop] if raw_dropins and prop == "DropInPaths" else values[prop] + "\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        return run

    def capture(self, **kwargs: object) -> dict[str, object]:
        runner = self.runner(**kwargs)
        with mock.patch.object(evidence, "_settings", return_value=(self.runtime, self.systemd, self.state, os.geteuid(), os.getegid())), mock.patch.object(evidence, "_units", return_value=[self.unit]), mock.patch.object(evidence.subprocess, "run", side_effect=runner), mock.patch.object(unit_state.subprocess, "run", side_effect=runner):
            fds, record = evidence.capture()
        self.fds.extend(fds)
        return record

    def test_absent_planned_unit_keeps_validated_dropin_and_full_fd_chain(self) -> None:
        record = self.capture()
        old = os.environ.get(evidence.ENV)
        os.environ[evidence.ENV] = json.dumps({"version": 1, "entries": record["entries"]}, sort_keys=True, separators=(",", ":"))
        try:
            paths = safe.Paths(state=self.state, db=self.state / "bridge.db")
            runner = self.runner()
            with mock.patch.object(safe.subprocess, "run", side_effect=runner), mock.patch.object(unit_state.subprocess, "run", side_effect=runner):
                safe._revalidate_barrier_evidence(paths)
        finally:
            if old is None: os.environ.pop(evidence.ENV, None)
            else: os.environ[evidence.ENV] = old

    def test_absent_unit_without_valid_dropin_is_rejected(self) -> None:
        self.dropin.unlink()
        with self.assertRaises(evidence.BarrierError): self.capture()

    def test_absent_unit_with_tampered_dropin_is_rejected(self) -> None:
        self.dropin.write_text("[Unit]\nConditionPathExists=/unsafe\n")
        with self.assertRaises(evidence.BarrierError): self.capture()

    def test_loaded_unit_requires_loaded_dropin_and_quiescence(self) -> None:
        with self.assertRaises(evidence.BarrierError): self.capture(load_state="loaded", dropins="")
        with self.assertRaises(evidence.BarrierError): self.capture(load_state="loaded", active="active")

    def test_loaded_unit_accepts_multiline_dropin_paths(self) -> None:
        self.capture(load_state="loaded", dropins=f"/other/dropin\n{self.dropin}")
        self.capture(load_state="loaded", dropins=str(self.dropin), raw_dropins=True)

    def test_malformed_or_error_load_state_is_rejected(self) -> None:
        for kwargs in (
            {"load_state": "not-found\nextra"},
            {"load_state": "masked"},
            {"load_state": "error"},
            {"load_state": "bad-setting"},
            {"load_state": "transient"},
            {"load_state": "alias"},
            {"error": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(evidence.BarrierError): self.capture(**kwargs)


if __name__ == "__main__":
    unittest.main()
