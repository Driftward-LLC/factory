from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import ui_verification as ui

REQUEST = {"source_commit": "a" * 40, "preview_image": "sha256:" + "b" * 64,
           "path": "/", "objective": "Create a synthetic agent", "model": "test-model",
           "timeout_seconds": 30, "steps": [{"action": "expect", "text": "Create agent"}]}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installation = self.root / "installation"
        bootstrap.init_directory(self.installation, "factory:test", "factory-verifier-test", apply=True)
        self.plugin = self.root / "plugin"
        (self.plugin / ".codex-plugin").mkdir(parents=True)
        (self.plugin / "skills/verify-app").mkdir(parents=True)
        (self.plugin / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "driftward-engineering", "version": "0.1.0-fixture"}))
        (self.plugin / "skills/verify-app/SKILL.md").write_text("Fixture instructions\n")

    def pilot(self, fixture=True):
        with patch.object(ui, "image_identity", return_value="sha256:" + "a" * 64):
            return ui.Pilot(self.installation, copy.deepcopy(REQUEST), "sha256:" + "a" * 64, self.plugin, fixture=fixture)

    def test_request_is_closed_and_requires_exact_revision_and_assertions(self):
        self.assertEqual(ui.validate_request(copy.deepcopy(REQUEST)), REQUEST)
        for name, value in (("source_commit", "main"), ("preview_image", "repo:latest"),
                            ("path", "//production"), ("path", "/../secret"), ("path", "/?token=secret"),
                            ("model", "x\nkey=value"), ("timeout_seconds", True), ("timeout_seconds", 601),
                            ("steps", []), ("steps", [{"action": "evaluate", "script": "evil"}]),
                            ("steps", [{"action": "press", "key": "Enter"}]),
                            ("steps", [{"action": "click", "role": "madeup", "name": "x"}]),
                            ("objective", "x" * 2001)):
            with self.subTest(name=name, value=value):
                request = {**REQUEST, name: value}
                with self.assertRaises(ValueError):
                    ui.validate_request(request)
        with self.assertRaises(ValueError):
            ui.validate_request({**REQUEST, "command": "arbitrary shell"})

    def test_plugin_is_snapshotted_and_symlinks_rejected(self):
        raw, identity = ui.instructions(self.plugin)
        self.assertEqual(identity["verify_skill_sha256"], ui.digest(raw))
        path = self.plugin / "skills/verify-app/SKILL.md"
        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            ui.instructions(self.plugin)

    def test_image_must_be_explicitly_prepared_and_match_source(self):
        row = {"Id": REQUEST["preview_image"], "Os": "linux", "Architecture": "amd64",
               "Config": {"Labels": {"io.driftward.factory.ui-preview": "1", "org.opencontainers.image.revision": "a" * 40}}}
        with patch.object(ui, "command", return_value=json.dumps([row])):
            self.assertEqual(ui.image_identity(REQUEST["preview_image"], preview=True, commit="a" * 40), REQUEST["preview_image"])
            with self.assertRaises(ValueError):
                ui.image_identity(REQUEST["preview_image"], preview=True, commit="c" * 40)
            with self.assertRaises(ValueError):
                ui.image_identity("image:mutable", preview=True)
            with self.assertRaises(ValueError):
                ui.image_identity(REQUEST["preview_image"], preview=False)
        row["Config"]["Volumes"] = {"/data": {}}
        with patch.object(ui, "command", return_value=json.dumps([row])):
            with self.assertRaisesRegex(ValueError, "durable volumes"):
                ui.image_identity(REQUEST["preview_image"], preview=True)

    def test_ambient_deployment_and_credentials_do_not_enter_pilot(self):
        with patch.dict(os.environ, {"COMPOSE_FILE": "bad.yml", "CODEX_API_KEY": "secret",
                                     "HOME": "/root", "HATCHET_CLIENT_TOKEN": "secret", "DOCKER_HOST": "tcp://remote:2375"}):
            pilot = self.pilot()
        for key in ("COMPOSE_FILE", "CODEX_API_KEY", "HOME", "HATCHET_CLIENT_TOKEN", "DOCKER_HOST"):
            self.assertNotIn(key, pilot.env)
        self.assertIn("--env-file", pilot.args)
        self.assertIn("/dev/null", pilot.args)

    def test_lock_serializes_and_rejects_unsafe_file(self):
        with ui.lock(self.installation):
            with self.assertRaisesRegex(ValueError, "already running"):
                with ui.lock(self.installation):
                    pass
        path = self.installation / ".ui-verification.lock"
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            with ui.lock(self.installation):
                pass

    def test_model_message_cannot_replace_checks(self):
        passed = {"status": "passed", "completed_steps": 2, "viewports": [1280, 390]}
        agent = {"status": "completed", "browser_calls": 2, "assessment": "no_findings"}
        self.assertEqual(ui.outcome({}, agent, fixture=False, steps=1), "checks_failed")
        self.assertEqual(ui.outcome(passed, {}, fixture=False, steps=1), "agent_blocked")
        self.assertEqual(ui.outcome(passed, agent, fixture=False, steps=1), "observed")
        self.assertEqual(ui.outcome(passed, agent, fixture=True, steps=1), "fixture_passed")
        self.assertEqual(ui.outcome(passed, {**agent, "assessment": "findings"}, fixture=False, steps=1), "needs_attention")
        self.assertEqual(ui.outcome(passed, {**agent, "assessment": "invented"}, fixture=False, steps=1), "agent_blocked")

    def test_receipt_is_bound_to_request_and_cannot_be_a_symlink(self):
        path = self.root / "evidence.json"
        path.write_text(json.dumps({"schema": 1, "request_sha256": "a"}))
        self.assertEqual(ui.safe_json(path, "a")["schema"], 1)
        with self.assertRaises(ValueError):
            ui.safe_json(path, "b")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            ui.safe_json(link, "a")

    def test_failure_timeout_cancellation_and_cleanup_failure_never_pass(self):
        for error in (RuntimeError("secret output"), subprocess.TimeoutExpired("private command", 1), InterruptedError()):
            for cleanup_ok in (True, False):
                with self.subTest(error=type(error).__name__, cleanup_ok=cleanup_ok):
                    pilot = self.pilot()
                    def compose(*args, **kwargs):
                        if args[0] == "down":
                            if cleanup_ok:
                                return ""
                            raise RuntimeError("cleanup failed")
                        raise error
                    with patch.object(ui.os, "geteuid", return_value=0), patch.object(ui.os, "chown"), \
                         patch.object(ui, "command", return_value=""), patch.object(pilot, "compose", side_effect=compose):
                        report = pilot.run(b"synthetic-key")
                    self.assertFalse(report["factory_verified"])
                    self.assertEqual(report["status"], "blocked")
                    self.assertEqual(report["cleanup_complete"], cleanup_ok)
                    if not cleanup_ok:
                        self.assertEqual(report["reason"], "containment_unconfirmed")
                    self.assertNotIn("secret output", json.dumps(report))
                    self.assertFalse(list((pilot.root / "secrets").iterdir()))

    def test_unresolved_previous_run_blocks_before_creating_new_state(self):
        pilot = self.pilot()
        with patch.object(ui.os, "geteuid", return_value=0), patch.object(ui, "command", return_value="old-container"):
            with self.assertRaisesRegex(ValueError, "prior UI run"):
                pilot.run(b"")
        self.assertFalse(pilot.root.exists())

    def test_cleanup_rejects_foreign_container(self):
        pilot = self.pilot()
        with patch.object(ui, "command", side_effect=["container-id", json.dumps([{"Config": {"Labels": {}}}])]):
            with self.assertRaises(ValueError):
                pilot.assert_ownership()

    def test_pilot_is_opt_in_and_has_no_control_plane_credentials_or_socket(self):
        compose = (ROOT / "deploy/compose.ui-verifier.yml").read_text()
        self.assertIn("profiles: [ui-verification]", compose)
        self.assertEqual(compose.count("internal: true"), 2)
        self.assertNotIn("ports:", compose)
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("env_file:", compose)
        self.assertNotIn("/root", compose)
        self.assertEqual(compose.count("FACTORY_UI_API_KEY"), 1)
        self.assertNotIn("compose.ui-verifier.yml", (ROOT / "deploy/factory-production.py").read_text())
        runner = (ROOT / "deploy/ui-verifier/runner.cjs").read_text()
        self.assertIn("'features.shell_tool': false", runner)
        self.assertIn("'mcp_servers.browser.required': true", runner)
        self.assertNotIn("browser_run_code", runner)
        self.assertNotIn("browser_evaluate", runner)


if __name__ == "__main__":
    unittest.main()
