"""Portable wrapper boundaries; no Docker daemon or production credentials."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location("factory_portable", DEPLOY / "factory-portable.py")
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "a space $literal"
        portable.bootstrap.init_directory(self.directory, "factory:test", "factory-unit-test", apply=True)

    def test_configuration_is_literal_and_ambient_project_is_ignored(self):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "production", "COMPOSE_PROFILES": "linear", "FACTORY_IMAGE": "bad:latest"}):
            install = portable.Installation(self.directory)
        self.assertEqual(install.project, "factory-unit-test")
        self.assertNotIn("COMPOSE_PROFILES", install.env)
        self.assertEqual(install.env["FACTORY_IMAGE"], "factory:test")
        self.assertEqual(install.env["FACTORY_ENV_DIR"], str(self.directory / "env"))
        self.assertIn(str(self.directory), install.args)

    def test_altered_identity_is_rejected_before_docker(self):
        path = self.directory / ".env"
        path.write_text(path.read_text().replace("factory-unit-test", "factory-another"))
        with self.assertRaises(ValueError):
            portable.Installation(self.directory)

    def test_missing_or_unhealthy_services_fail_status(self):
        install = portable.Installation(self.directory)
        rows = [{"Service": service, "State": "running", "Health": "healthy"} for service in portable.SERVICES]
        with patch.object(install, "compose", return_value=json.dumps(rows)), patch("builtins.print"):
            install.status()
        for field, value in (("State", "exited"), ("Health", "unhealthy")):
            rows[0][field] = value
            with patch.object(install, "compose", return_value=json.dumps(rows)), patch("builtins.print"):
                with self.assertRaises(RuntimeError):
                    install.status()

    def test_private_failure_never_exposes_captured_output(self):
        result = portable.subprocess.CompletedProcess([], 1, stdout="secret-material", stderr="secret-error")
        with patch.object(portable.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as error:
                portable.run(["docker", "exec"], private=True)
        self.assertNotIn("secret-material", str(error.exception))
        self.assertNotIn("secret-error", str(error.exception))

    def test_portable_profile_has_no_host_executor_socket_or_docker_socket(self):
        source = (DEPLOY / "compose.portable.yml").read_text()
        self.assertIn("bridge-executor-worker:\n    <<: *portable\n    volumes: !reset []", source)
        self.assertNotIn("docker.sock", source)
        self.assertIn("127.0.0.1:${FACTORY_CONSOLE_PORT", source)
        self.assertNotIn("FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED", source)


if __name__ == "__main__":
    unittest.main()
