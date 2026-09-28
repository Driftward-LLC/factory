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

    def test_foreign_docker_resources_are_rejected_before_stop_or_migration(self):
        install = portable.Installation(self.directory)
        responses = ["factory-unit-test-bridge-data\n", "", "", json.dumps([{"Labels": {"io.driftward.factory.installation": "another-installation"}}])]
        with patch.object(portable, "run", side_effect=responses), patch.object(install, "compose") as compose:
            with self.assertRaisesRegex(ValueError, "collides"):
                install.up()
        compose.assert_not_called()

    def test_owned_docker_resources_can_be_reused(self):
        install = portable.Installation(self.directory)
        responses = ["factory-unit-test-bridge-data\n", "", "", json.dumps([{"Labels": {"io.driftward.factory.installation": install.metadata["installation_id"]}}])]
        with patch.object(portable, "run", side_effect=responses):
            install.assert_owned_resources()

    def test_real_execution_cannot_be_enabled_by_editing_env(self):
        install = portable.Installation(self.directory)
        path = self.directory / "env" / "hatchet-workers.env"
        path.write_text(path.read_text().replace("HATCHET_INTAKE_DRY_RUN='true'", "HATCHET_INTAKE_DRY_RUN='false'"))
        with patch.object(install, "assert_owned_resources"), patch.object(install, "compose") as compose:
            with self.assertRaisesRegex(ValueError, "dry-run only"):
                install.up()
        compose.assert_not_called()

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

    def test_release_metadata_requires_published_digest_and_supported_platform(self):
        metadata = {"FACTORY_RELEASE_VALIDATION_ONLY": "false", "FACTORY_RELEASE_SCHEMA": "1",
                    "FACTORY_RELEASE_PLATFORM": "linux/amd64", "FACTORY_IMAGE": "ghcr.io/driftward-llc/factory@sha256:" + "a" * 64}
        with patch.object(Path, "is_file", return_value=True), patch.object(portable, "read_env", return_value=metadata):
            self.assertEqual(portable.release_image(), metadata["FACTORY_IMAGE"])
            for key, value in (("FACTORY_RELEASE_VALIDATION_ONLY", "true"), ("FACTORY_RELEASE_PLATFORM", "windows/amd64"), ("FACTORY_IMAGE", "factory:latest")):
                original = metadata[key]
                metadata[key] = value
                with self.assertRaises(ValueError):
                    portable.release_image()
                metadata[key] = original

    def test_portable_profile_has_no_host_executor_socket_or_docker_socket(self):
        source = (DEPLOY / "compose.portable.yml").read_text()
        self.assertIn("bridge-executor-worker:\n    <<: *portable\n    volumes: !reset []", source)
        self.assertNotIn("docker.sock", source)
        self.assertIn("127.0.0.1:${FACTORY_CONSOLE_PORT", source)
        self.assertNotIn("FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED", source)

    def test_lifecycle_operations_are_serialized(self):
        with portable.installation_lock(self.directory):
            with self.assertRaisesRegex(ValueError, "another operation"):
                with portable.installation_lock(self.directory):
                    self.fail("second operation obtained the lock")
        with portable.installation_lock(self.directory):
            pass

    def test_key_generation_uses_host_operator_identity(self):
        install = portable.Installation(self.directory)
        portable.bootstrap.finalize_directory(self.directory, "fixture.worker.token", apply=True)

        def compose(*args, **kwargs):
            if "keygen" in args:
                self.assertEqual(args, ("run", "--rm", "--no-deps", "--user", "1001:1002", "keygen"))
                for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key"):
                    (self.directory / "keys" / "hatchet" / name).write_text("fixture")
            return ""

        with patch.object(install, "assert_owned_resources"), patch.object(install, "status"), \
                patch.object(install, "compose", side_effect=compose) as invoked, \
                patch.object(portable.os, "geteuid", return_value=1001), \
                patch.object(portable.os, "getegid", return_value=1002), patch("builtins.print"):
            install.up()
        self.assertEqual(sum("keygen" in call.args for call in invoked.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
