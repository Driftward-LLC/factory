from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("factory_bootstrap", ROOT / "deploy/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class FactoryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.directory = self.parent / "private factory's $directory"

    def initialize(self, **kwargs):
        return bootstrap.init_directory(self.directory, "ghcr.io/example/factory:v1", "factory-test", **kwargs)

    def test_preview_creates_nothing(self):
        self.initialize()
        self.assertFalse(self.directory.exists())

    def test_exact_factory_project_is_repeat_safe(self):
        config = bootstrap.init_directory(self.directory, "factory:test", "factory")
        self.assertFalse(self.directory.exists())
        self.assertEqual(config["COMPOSE_PROJECT_NAME"], "factory")
        config = bootstrap.init_directory(self.directory, "factory:test", "factory", apply=True)
        self.assertEqual(config, bootstrap.init_directory(self.directory, "factory:test", "factory", apply=True))
        metadata, saved = bootstrap.validate_directory(self.directory)
        self.assertEqual(metadata["project"], "factory")
        self.assertEqual(saved["FACTORY_PROJECT_NAME"], "factory")

    def test_project_name_boundaries_remain_restricted(self):
        bootstrap.init_directory(self.directory, "factory:test", "factory-" + "a" * 49)
        for project in ("", "driftward-factory", "factory-", "factory_", "factoryx", "Factory",
                        "factory--test", "factory/test", "factory-" + "a" * 50):
            with self.subTest(project=project), self.assertRaises(ValueError):
                bootstrap.init_directory(self.directory, "factory:test", project, apply=True)
            self.assertFalse(self.directory.exists())

    def test_repeat_preserves_identity_secrets_and_private_permissions(self):
        config = self.initialize(apply=True)
        before = {path.relative_to(self.directory): path.read_bytes()
                  for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(config, self.initialize(apply=True))
        for path in self.directory.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            if path.is_file():
                self.assertEqual(before[path.relative_to(self.directory)], path.read_bytes())
        marker = json.loads((self.directory / bootstrap.MARKER).read_text())
        self.assertEqual(marker["project"], "factory-test")
        self.assertNotIn("password", json.dumps(marker))
        self.assertEqual(config["HATCHET_SERVER_URL"], "http://localhost:8888")
        self.assertNotIn("/etc/driftward", (self.directory / ".env").read_text())

    def test_secrets_are_scoped_and_external_credentials_never_inherited(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "must-not-be-copied", "OPENAI_API_KEY": "must-not-be-copied"}):
            self.initialize(apply=True)
        env = lambda name: bootstrap.read_env(self.directory / "env" / name)
        workers = env("hatchet-workers.env")
        bridge = env("hatchet-bridge.env")
        operator = env("factory-operator.env")
        self.assertEqual(workers["HATCHET_INTAKE_DRY_RUN"], "true")
        self.assertEqual(workers["HATCHET_HERMES_BRIDGE_TOKEN"], bridge["HATCHET_HERMES_BRIDGE_TOKEN"])
        self.assertNotIn("HATCHET_HERMES_INSPECTION_TOKEN", workers)
        self.assertNotIn("HATCHET_HERMES_RECOVERY_TOKEN", workers)
        self.assertNotIn("HATCHET_HERMES_BRIDGE_TOKEN", operator)
        self.assertEqual(env("hatchet-worker-bridge-executor.env")["DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED"], "0")
        self.assertNotIn("must-not-be-copied", "".join(path.read_text() for path in self.directory.rglob("*") if path.is_file()))

    def test_foreign_partial_symlink_and_changed_identity_are_refused(self):
        self.directory.mkdir()
        with self.assertRaises(ValueError):
            self.initialize(apply=True)
        self.directory.rmdir()
        elsewhere = self.parent / "elsewhere"
        elsewhere.mkdir()
        self.directory.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.initialize(apply=True)
        with self.assertRaises(ValueError):
            bootstrap.init_directory(self.directory / "child", "image:v1", "factory-test", apply=True)
        self.directory.unlink()
        self.initialize(apply=True)
        with self.assertRaises(ValueError):
            bootstrap.init_directory(self.directory, "image:v2", "factory-test", apply=True)
        with self.assertRaises(ValueError):
            bootstrap.init_directory(self.directory, "ghcr.io/example/factory:v1", "factory-other", apply=True)
        with self.assertRaises(ValueError):
            self.initialize(apply=True, port_offset=10)
        (self.directory / "env/hatchet-workers.env").unlink()
        with self.assertRaises(ValueError):
            self.initialize(apply=True)

    def test_invalid_new_configuration_is_rejected_before_creation(self):
        for image, project, offset in (("image:latest", "factory-test", 0),
                                       ("image", "factory-test", 0),
                                       ("image:v1", "driftward-factory", 0),
                                       ("image:v1", "factory-test", 60000)):
            with self.subTest(image=image, project=project, offset=offset), self.assertRaises(ValueError):
                bootstrap.init_directory(self.directory, image, project, apply=True, port_offset=offset)
            self.assertFalse(self.directory.exists())

    def test_token_install_is_explicit_and_never_replaces_credentials(self):
        self.initialize(apply=True)
        token_file = self.directory / "env/hatchet-client.env"
        bootstrap.finalize_directory(self.directory, "eyTest.eyPayload.signature")
        self.assertFalse(token_file.exists())
        bootstrap.finalize_directory(self.directory, "eyTest.eyPayload.signature", apply=True)
        bootstrap.finalize_directory(self.directory, "eyTest.eyPayload.signature", apply=True)
        self.assertEqual(bootstrap.read_env(token_file), {"HATCHET_CLIENT_TOKEN": "eyTest.eyPayload.signature"})
        with self.assertRaises(ValueError):
            bootstrap.finalize_directory(self.directory, "other.eyPayload.signature", apply=True)
        with self.assertRaises(ValueError):
            bootstrap.finalize_directory(self.directory, "bad\nHATCHET_INTAKE_DRY_RUN=false", apply=True)
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_token_capture_rejects_ambiguous_output_and_symlinks(self):
        self.assertEqual(bootstrap.extract_client_token("admin ready\neyToken.payload.signature\n"), "eyToken.payload.signature")
        with self.assertRaises(ValueError):
            bootstrap.extract_client_token("a.b.c\nd.e.f\n")
        self.initialize(apply=True)
        (self.directory / "env/hatchet-client.env").symlink_to(self.parent / "target")
        with self.assertRaises(ValueError):
            bootstrap.finalize_directory(self.directory, "a.b.c", apply=True)
        self.assertFalse((self.parent / "target").exists())

    def test_parser_never_evaluates_variables_and_rejects_public_files(self):
        config = self.initialize(apply=True)
        self.assertEqual(bootstrap.read_env(self.directory / ".env")["FACTORY_INSTALL_DIR"], str(self.directory))
        self.assertEqual(config["FACTORY_INSTALL_DIR"], str(self.directory))
        path = self.directory / "env/hatchet-workers.env"
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            bootstrap.read_env(path)

    def test_cli_never_prints_generated_credentials(self):
        output = io.StringIO()
        with patch("sys.argv", ["bootstrap.py", "init", "--directory", str(self.directory),
                                "--project", "factory-test", "--image", "image:v1", "--apply"]), \
                redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(bootstrap.main(), 0)
        config = bootstrap.read_env(self.directory / ".env")
        self.assertNotIn(config["HATCHET_ADMIN_PASSWORD"], output.getvalue())
        self.assertNotIn(config["HATCHET_POSTGRES_PASSWORD"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
