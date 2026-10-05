"""Optional Collector: real Compose rendering, stopped changes, cold round-trip."""
import argparse
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import bootstrap
import telemetry
import test_factory_production as production_tests
import test_factory_portable_state as state_tests
production, state = production_tests.production, state_tests.state


def feature():
    return {"image": "sha256:" + "d" * 64, "port": 4318, "enabled": True,
            "deployment_sha256": {name: state._hash(telemetry.HERE / name) for name in telemetry.FILES}}


def enable(directory):
    path = directory / bootstrap.MARKER
    metadata = json.loads(path.read_text())
    metadata["telemetry"] = feature()
    path.write_text(json.dumps(metadata))
    bootstrap._create_private(directory / telemetry.ENV_FILE, bootstrap.env_text({
        "POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces",
        "POSTHOG_PROJECT_TOKEN": "phc_synthetic_test_not_a_real_key"}))
    return metadata


class CollectorProductionTests(unittest.TestCase):
    setUp = production_tests.ProductionTests.setUp
    upgrade_backup = production_tests.ProductionTests.upgrade_backup
    def test_real_compose_isolation_and_pause(self):
        enable(self.directory)
        install = production.Production(self.directory)
        self.assertEqual(len(install.services), 15)
        self.assertEqual(len(install.provenance["source_images"]), 14)
        install.assert_config()  # Real docker compose config; never starts services.
        config = json.loads(install.compose("config", "--format", "json", private=True))
        for name, service in config["services"].items():
            keys = set(service.get("environment", {}))
            if name == telemetry.SERVICE:
                self.assertEqual(keys, {"POSTHOG_PROJECT_TOKEN", "POSTHOG_TRACES_ENDPOINT"})
            else:
                self.assertFalse(any(key.startswith("POSTHOG_") for key in keys))
        self.assertEqual(config["services"]["bridge"]["environment"]["FACTORY_OTEL_TRACES_ENDPOINT"], "http://otel-collector:4318/v1/traces")
        marker = self.directory / bootstrap.MARKER
        metadata = json.loads(marker.read_text())
        metadata["telemetry"]["enabled"] = False
        marker.write_text(json.dumps(metadata))
        paused = production.Production(self.directory)
        paused.assert_config()
        self.assertEqual(paused.env["FACTORY_OTEL_INTERNAL_ENDPOINT"], "")
        self.assertIn(telemetry.SERVICE, paused.services)
        metadata["telemetry"]["deployment_sha256"][telemetry.FILES[0]] = "tampered"
        marker.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "deployment bundle"):
            production.Production(self.directory)

    def test_configure_requires_stopped_fresh_backup_and_never_alters_source(self):
        snapshot = self.upgrade_backup()
        secret = Path(self.temp.name) / "collector.env"
        bootstrap._create_private(secret, bootstrap.env_text({
            "POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces",
            "POSTHOG_PROJECT_TOKEN": "phc_synthetic_test_not_a_real_key"}))
        args = argparse.Namespace(snapshot=snapshot, credentials=secret, port=4318, paused=False, apply=True)
        def docker(command, env=None):
            if command[:2] == ["image", "inspect"]:
                return feature()["image"]
            if command[0] == "ps":
                return "container"
            if command[:2] == ["container", "inspect"]:
                return json.dumps([{"State": {"Status": container_status}}])
            raise AssertionError(command)
        with patch.object(self.install, "preflight"), patch.object(state, "_docker", side_effect=docker), \
                patch.object(state, "_archive_check"), patch.object(state, "_no_writers"):
            container_status = "running"
            with self.assertRaisesRegex(ValueError, "stopped"):
                telemetry.configure(self.install, args)
            self.assertFalse((self.directory / telemetry.ENV_FILE).exists())
            container_status = "exited"
            args.apply = False
            telemetry.configure(self.install, args)
            self.assertFalse((self.directory / telemetry.ENV_FILE).exists())
            args.apply = True
            telemetry.configure(self.install, args)
        installed = production.Production(self.directory)
        self.assertEqual(installed.provenance, self.provenance)
        self.assertEqual((self.directory / telemetry.ENV_FILE).stat().st_mode & 0o777, 0o600)
        with patch.object(installed, "preflight"):
            with self.assertRaisesRegex(ValueError, "current cold backup"):
                telemetry.configure(installed, args)

    def test_secret_rejects_wrong_endpoint_and_extra_keys(self):
        enable(self.directory)
        path = self.directory / telemetry.ENV_FILE
        values = bootstrap.read_env(path)
        for change in ({"POSTHOG_TRACES_ENDPOINT": "https://untrusted.example/i/v1/traces"}, {"OPENAI_API_KEY": "synthetic"}):
            path.write_text(bootstrap.env_text({**values, **change}))
            with self.assertRaisesRegex(ValueError, "only a PostHog"):
                production.Production(self.directory)


class CollectorStateTests(unittest.TestCase):
    setUp = state_tests.PortableStateTests.setUp
    production = state_tests.PortableStateTests.production
    backup = state_tests.PortableStateTests.backup
    def test_collector_production_cold_round_trip(self):
        self.production()
        enable(self.source)
        config = json.loads(self.compose.return_value)
        config["services"][telemetry.SERVICE] = {"image": feature()["image"]}
        self.compose.return_value = json.dumps(config)
        self.backup()
        state.restore(self.snapshot, self.restored, "factory-restored", production=True, apply=True, port_offset=100)
        metadata, _ = bootstrap.validate_directory(self.restored)
        self.assertEqual(metadata["telemetry"]["port"], 4418)
        self.assertEqual(telemetry.credentials(self.restored / telemetry.ENV_FILE), telemetry.credentials(self.source / telemetry.ENV_FILE))
        self.assertFalse(any(command[:1] == ["start"] for command in self.docker.calls))

    def test_collector_portable_round_trip_and_unknown_secret_refusal(self):
        enable(self.source)
        config = json.loads(self.compose.return_value)
        config["services"][telemetry.SERVICE] = {"image": feature()["image"]}
        self.compose.return_value = json.dumps(config)
        extra = self.source / "env/unknown.env"
        bootstrap._create_private(extra, "unexpected")
        with self.assertRaisesRegex(ValueError, "known configuration"):
            self.backup()
        extra.unlink()
        self.backup()
        state.restore(self.snapshot, self.restored, "factory-restored", apply=True)
        self.assertTrue(telemetry.validate(json.loads((self.restored / bootstrap.MARKER).read_text()))["enabled"])


if __name__ == "__main__":
    unittest.main()
