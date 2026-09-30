"""Render synthetic Compose configuration only; never contact the Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = {
    "hatchet-workers.env": "HATCHET_HERMES_BRIDGE_TOKEN=fixture-bridge\nHATCHET_INTAKE_DRY_RUN=true\n",
    "hatchet-client.env": "HATCHET_CLIENT_TOKEN=fixture-client\n",
    "hatchet-bridge.env": "HATCHET_HERMES_BRIDGE_TOKEN=fixture-bridge\n",
    "hatchet-verifier.env": "HATCHET_HERMES_VERIFIER_TOKEN=fixture-verifier\n",
    "hatchet-event-ingress.env": "HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET=fixture-webhook\n",
    "hatchet-worker-bridge-executor.env": "HATCHET_HERMES_INSPECTION_TOKEN=fixture-inspection\nDRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED=0\n",
    "hatchet-worker-reconciliation.env": "HATCHET_HERMES_RECOVERY_TOKEN=fixture-recovery\n",
    "factory-lifecycle-writer.env": "FACTORY_LIFECYCLE_ACTIVATION_WATERMARK=2026-01-01T00:00:00Z\n",
    "factory-linear-token.env": "FACTORY_LINEAR_CLIENT_ID=fixture-linear\nFACTORY_LINEAR_CLIENT_SECRET=fixture-linear-secret\n",
    "factory-operator.env": "HATCHET_FACTORY_OPERATOR_TOKEN=fixture-operator\nHATCHET_INTAKE_DRY_RUN=true\n",
}
LINEAR_SERVICES = {"linear-webhook-proxy", "factory-lifecycle-writer", "factory-linear-token-refresh"}
SOCKET_DIRECTORY = "/run/driftward-hermes-factory"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is required to render the profile")
class ProductionProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.env_directory = self.directory / "env"
        self.env_directory.mkdir()
        for name, content in ENV_FILES.items():
            (self.env_directory / name).write_text(content)
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "FACTORY_PROJECT_NAME": "factory-profile-test",
            "FACTORY_IMAGE": "sha256:" + "c" * 64,
            "FACTORY_POSTGRES_IMAGE": "sha256:" + "a" * 64,
            "FACTORY_HATCHET_IMAGE": "sha256:" + "b" * 64,
            "FACTORY_INSTALLATION_ID": "fixture-installation",
            "FACTORY_INSTALL_DIR": str(self.directory),
            "FACTORY_ENV_DIR": str(self.env_directory),
            "FACTORY_SOURCE_ROOT": str(ROOT),
            "HATCHET_DEFAULT_TENANT_ID": "fixture-tenant",
            "HATCHET_POSTGRES_PASSWORD": "fixture-postgres",
            "HATCHET_SERVER_AUTH_COOKIE_SECRETS": "fixture-cookie",
            "HATCHET_ADMIN_PASSWORD": "fixture-admin",
        }

    def render(self, *, production=True, check=True):
        args = ["docker", "compose", "--project-directory", str(self.directory)]
        files = ["docker-compose.yml", "compose.portable.yml"]
        if production:
            files.append("compose.production.yml")
        for name in files:
            args.extend(["-f", str(ROOT / "deploy" / name)])
        result = subprocess.run([*args, "config", "--format", "json"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        if not check:
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_production_restores_only_local_gateway_and_linear_services(self):
        config = self.render()
        services = config["services"]
        self.assertEqual(services["postgres"]["image"], self.env["FACTORY_POSTGRES_IMAGE"])
        self.assertEqual(services["hatchet-lite"]["image"], self.env["FACTORY_HATCHET_IMAGE"])
        self.assertTrue(LINEAR_SERVICES <= services.keys())
        for name in LINEAR_SERVICES:
            self.assertFalse(services[name].get("profiles"))
        executor = services["bridge-executor-worker"]
        self.assertEqual(executor["environment"]["DRIFTWARD_HERMES_GRPC_TARGET"],
                         f"unix://{SOCKET_DIRECTORY}/hermes.sock")
        self.assertEqual(executor["volumes"], [{
            "type": "bind", "source": SOCKET_DIRECTORY, "target": SOCKET_DIRECTORY,
            "read_only": True, "bind": {"create_host_path": False},
        }])
        operator = services["factory-operator"]
        self.assertEqual(operator["environment"]["HATCHET_FACTORY_OPERATOR_LIFECYCLE_DB"],
                         "/var/lib/driftward-factory-lifecycle-writer/outbox.db")
        self.assertEqual(operator["depends_on"]["factory-lifecycle-writer"]["condition"], "service_healthy")
        self.assertTrue({"bridge", "event-ingress"} <= operator["depends_on"].keys())
        for name, service in services.items():
            self.assertEqual(service["labels"]["io.driftward.factory.installation"], "fixture-installation")
            if name not in {"postgres", "hatchet-lite"}:
                self.assertEqual(service["image"], self.env["FACTORY_IMAGE"])
            self.assertEqual(set(service["networks"]), {"factory"})
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    self.assertIn((name, mount["source"]), {
                        ("hatchet-lite", str(self.directory / "keys/hatchet")),
                        ("bridge-executor-worker", SOCKET_DIRECTORY),
                    })
                else:
                    self.assertTrue(config["volumes"][mount["source"]]["name"].startswith("factory-profile-test-"))
            for port in service.get("ports", []):
                self.assertEqual(port["host_ip"], "127.0.0.1")
            if name.endswith("-worker"):
                env = service["environment"]
                self.assertEqual(env["HATCHET_CLIENT_TOKEN"], "fixture-client")
                self.assertEqual(env["HATCHET_HERMES_BRIDGE_TOKEN"], "fixture-bridge")
                self.assertFalse(any(key.startswith(("FACTORY_LINEAR_", "LINEAR_", "OPENAI_", "ANTHROPIC_",
                                                    "GITHUB_", "GH_", "BROWSERBASE_", "MCP_")) for key in env))

    def test_gateway_cannot_be_redirected_by_environment(self):
        self.env["DRIFTWARD_HERMES_GRPC_TARGET"] = "dns:///foreign-host:50051"
        path = self.env_directory / "hatchet-worker-bridge-executor.env"
        path.write_text(ENV_FILES[path.name] + "DRIFTWARD_HERMES_GRPC_TARGET=unix:///foreign/socket\n")
        executor = self.render()["services"]["bridge-executor-worker"]
        self.assertEqual(executor["environment"]["DRIFTWARD_HERMES_GRPC_TARGET"],
                         f"unix://{SOCKET_DIRECTORY}/hermes.sock")

    def test_production_preserves_explicit_execution_mode(self):
        config = self.render()["services"]
        self.assertEqual(config["intake-worker"]["environment"]["HATCHET_INTAKE_DRY_RUN"], "true")
        self.assertEqual(config["factory-operator"]["environment"]["HATCHET_INTAKE_DRY_RUN"], "true")
        self.assertEqual(config["bridge-executor-worker"]["environment"]["DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED"], "0")
        path = self.env_directory / "hatchet-workers.env"
        path.write_text(ENV_FILES[path.name].replace("DRY_RUN=true", "DRY_RUN=false"))
        path = self.env_directory / "hatchet-worker-bridge-executor.env"
        path.write_text(ENV_FILES[path.name].replace("ENABLED=0", "ENABLED=1"))
        config = self.render()["services"]
        self.assertEqual(config["intake-worker"]["environment"]["HATCHET_INTAKE_DRY_RUN"], "false")
        self.assertEqual(config["bridge-executor-worker"]["environment"]["DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED"], "1")

    def test_every_production_env_file_is_required(self):
        for name, content in ENV_FILES.items():
            with self.subTest(name=name):
                path = self.env_directory / name
                path.unlink()
                result = self.render(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                path.write_text(content)

    def test_production_requires_explicit_environment_directory(self):
        self.env.pop("FACTORY_ENV_DIR")
        result = self.render(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FACTORY_ENV_DIR", result.stderr)

    def test_production_requires_source_infrastructure_images(self):
        for key in ("FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE"):
            with self.subTest(key=key):
                image = self.env.pop(key)
                result = self.render(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)
                self.env[key] = image

    def test_portable_stays_isolated_without_production_overlay(self):
        services = self.render(production=False)["services"]
        self.assertFalse(LINEAR_SERVICES & services.keys())
        executor = services["bridge-executor-worker"]
        self.assertFalse(executor.get("volumes"))
        self.assertNotIn("DRIFTWARD_HERMES_GRPC_TARGET", executor["environment"])
        self.assertEqual(executor["environment"]["DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED"], "0")
        operator = services["factory-operator"]
        self.assertEqual(operator["environment"]["HATCHET_FACTORY_OPERATOR_LIFECYCLE_DB"], "")
        self.assertNotIn("factory-lifecycle-writer", operator["depends_on"])


if __name__ == "__main__":
    unittest.main()
