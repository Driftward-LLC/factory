"""Production operations boundaries with synthetic configuration and mocked Docker."""
import argparse
import base64
from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location("factory_production", DEPLOY / "factory-production.py")
production = importlib.util.module_from_spec(spec)
spec.loader.exec_module(production)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "candidate"
        self.image = "sha256:" + "a" * 64
        production.bootstrap.init_directory(self.directory, self.image, "factory-unit-production", apply=True)
        self.values = production.bootstrap.read_env(self.directory / ".env")
        self.values.update(FACTORY_POSTGRES_IMAGE="sha256:" + "b" * 64, FACTORY_HATCHET_IMAGE="sha256:" + "c" * 64)
        (self.directory / ".env").write_text(production.bootstrap.env_text(self.values))
        marker = self.directory / production.bootstrap.MARKER
        self.metadata = json.loads(marker.read_text())
        self.metadata["profile"] = "same-host-production"
        marker.write_text(json.dumps(self.metadata))
        self.tenant = self.values["HATCHET_DEFAULT_TENANT_ID"]
        payload = base64.urlsafe_b64encode(json.dumps({"sub": self.tenant}).encode()).decode().rstrip("=")
        production.bootstrap._create_private(self.directory / "env/hatchet-client.env", "HATCHET_CLIENT_TOKEN='synthetic." + payload + ".signature'\n")
        lock = Path("/tmp") / (".factory-tenant-" + self.tenant + ".factory-lock")
        self.addCleanup(lambda: lock.unlink(missing_ok=True))
        for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key"):  # gitleaks:allow -- filenames only
            production.bootstrap._create_private(self.directory / "keys/hatchet" / name, "synthetic key fixture")
        self.provenance = {
            "destination_project": self.values["FACTORY_PROJECT_NAME"], "source_project": "driftward-factory",
            "factory_image": self.image, "activation_approved": False, "tenant_id": self.tenant,
            "profile_sha256": production.state._hash(DEPLOY / "compose.production.yml"),
            "deployment_sha256": {name: production.state._hash(DEPLOY / name) for name in production.state.deployment_files(production.state.PRODUCTION)},
            "source_container_ids": [format(index, "064x") for index in range(1, len(production.SERVICES) + 1)],
            "source_images": {name: self.values.get({"postgres": "FACTORY_POSTGRES_IMAGE", "hatchet-lite": "FACTORY_HATCHET_IMAGE"}.get(name, "FACTORY_IMAGE")) for name in production.SERVICES},
        }
        production.bootstrap._create_private(self.directory / ".factory-cutover.json", json.dumps(self.provenance))
        self.install = production.Production(self.directory)

    def config(self):
        labels = {production.state.IDENTITY_LABEL: self.metadata["installation_id"], production.TENANT_LABEL: self.tenant}
        volumes = {"factory-" + suffix: {"name": self.install.project + "-" + suffix, "labels": labels} for suffix in production.state.PROJECT_VOLUMES}
        services = {}
        for name in production.SERVICES:
            mounts = [{"type": "volume", "source": "factory-" + suffix, "target": target, "read_only": readonly} for suffix, target, readonly in production.MOUNTS.get(name, [])]
            if name == "hatchet-lite":
                mounts.append({"type": "bind", "source": str(self.directory / "keys/hatchet"), "target": "/keys", "read_only": True})
            if name == "bridge-executor-worker":
                mounts.append({"type": "bind", "source": production.SOCKET, "target": production.SOCKET, "read_only": True})
            services[name] = {"image": self.provenance["source_images"][name], "labels": labels, "networks": {"factory": {}}, "volumes": mounts}
        services["bridge-executor-worker"]["environment"] = {"DRIFTWARD_HERMES_GRPC_TARGET": "unix://" + production.SOCKET + "/hermes.sock"}
        for name, key, target in (("hatchet-lite", "FACTORY_HATCHET_PORT", 8888), ("linear-webhook-proxy", "FACTORY_WEBHOOK_PORT", 19879), ("factory-operator", "FACTORY_CONSOLE_PORT", 19881)):
            services[name]["ports"] = [{"host_ip": "127.0.0.1", "published": self.values[key], "target": target}]
        return {"services": services, "volumes": volumes, "networks": {"factory": {"name": self.install.project, "labels": labels}}}

    def source_rows(self):
        return [{"Id": container, "Image": self.provenance["source_images"][name],
                 "Config": {"Labels": {"com.docker.compose.project": "driftward-factory", "com.docker.compose.service": name}},
                 "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
                for container, name in zip(self.provenance["source_container_ids"], production.SERVICES)]

    def test_constructor_scopes_three_files_and_ignores_ambient_profile(self):
        with patch.dict(os.environ, {"FACTORY_IMAGE": "wrong:latest", "COMPOSE_PROFILES": "bootstrap", "COMPOSE_PROJECT_NAME": "wrong"}):
            install = production.Production(self.directory)
        self.assertNotIn("COMPOSE_PROFILES", install.env)
        self.assertEqual(install.env["FACTORY_IMAGE"], self.image)
        self.assertEqual(install.args[install.args.index("--project-name") + 1], install.project)
        self.assertEqual([install.args[index + 1] for index, value in enumerate(install.args) if value == "-f"],
                         [str(DEPLOY / name) for name in ("docker-compose.yml", "compose.portable.yml", "compose.production.yml")])
        with self.assertRaisesRegex(ValueError, "dry-run only"):
            production.portable.Installation(self.directory)

    def upgrade_backup(self):
        snapshot = Path(self.temp.name) / "snapshot"
        snapshot.mkdir(mode=0o700)
        for folder in ("config", "config/env", "config/keys", "config/keys/hatchet", "volumes"):
            (snapshot / folder).mkdir(mode=0o700)
        for name in production.state.config_files(production.state.PRODUCTION):
            production.bootstrap._create_private(snapshot / "config" / name,
                                                  production.bootstrap._read_private(self.directory / name))
        for name in production.state.VOLUMES:
            production.bootstrap._create_private(snapshot / "volumes" / (name + ".tar"), "synthetic archive")
        hashes = {name: production.state._hash(snapshot / name) for name in production.state._private_files(snapshot)}
        manifest = {"format_version": 1, "profile": production.state.PRODUCTION,
                    "project": self.install.project, "installation_id": self.metadata["installation_id"],
                    "factory_image": self.image, "volumes": list(production.state.VOLUMES),
                    "images": {image: image for image in self.install.images.values()},
                    "deployment_sha256": self.provenance["deployment_sha256"], "sha256": hashes}
        production.bootstrap._create_private(snapshot / "manifest.json", json.dumps(manifest))
        return snapshot

    def test_image_upgrade_keeps_source_provenance_and_requires_stopped_writers(self):
        snapshot = self.upgrade_backup()
        image = "sha256:" + "d" * 64
        image_info = {"Id": image, "Architecture": "amd64", "Config": {"Labels": {
            "org.opencontainers.image.source": "https://github.com/Driftward-LLC/factory",
            "org.opencontainers.image.revision": "e" * 40,
            "org.opencontainers.image.version": "v2026.10.2"}}}
        with patch.object(self.install, "preflight"), patch.object(production.state, "_docker", return_value=json.dumps([image_info])), \
                patch.object(production.state, "_no_writers") as writers, patch.object(self.install, "assert_source_fenced"), \
                patch.object(self.install, "service_rows", return_value=[]), patch("builtins.print"):
            self.install.record_upgrade(image, snapshot)
            writers.assert_not_called()
            self.assertEqual(production.Production(self.directory).images["factory"], self.image)
            self.install.record_upgrade(image, snapshot, apply=True)
            writers.assert_called_once()
        upgraded = production.Production(self.directory)
        self.assertEqual(upgraded.images["factory"], image)
        self.assertEqual(upgraded.provenance["factory_image"], self.image)
        self.assertEqual(upgraded.provenance["source_images"], self.provenance["source_images"])
        self.assertEqual(upgraded.release_images, [self.image, image])
        ids = "\n".join(self.provenance["source_container_ids"])
        with patch.object(production.state, "_docker", side_effect=[ids, json.dumps(self.source_rows()), ""]), \
                patch.object(production.state, "_no_writers"):
            upgraded.assert_source_fenced()
        row = {"Image": self.image, "State": {"Status": "exited"}, "Config": {"Labels": {
            "com.docker.compose.service": "bridge", production.state.IDENTITY_LABEL: self.metadata["installation_id"],
            production.TENANT_LABEL: self.tenant}}}
        with patch.object(production.state, "_docker", side_effect=["old-container", json.dumps([row]), *("" for _ in production.state.VOLUMES)]):
            upgraded.assert_candidate_writers()
        row["State"]["Status"] = "running"
        with patch.object(production.state, "_docker", side_effect=["old-container", json.dumps([row])]):
            with self.assertRaisesRegex(ValueError, "inventory"):
                upgraded.assert_candidate_writers()

    def test_upgrade_rejects_stale_backup_unknown_files_and_incomplete_image(self):
        snapshot = self.upgrade_backup()
        with patch.object(self.install, "preflight"), patch.object(production.state, "_docker", return_value="[{}]"), \
                patch.object(production.state, "_no_writers") as writers:
            with self.assertRaisesRegex(ValueError, "release provenance"):
                self.install.record_upgrade("sha256:" + "d" * 64, snapshot, apply=True)
            writers.assert_not_called()
            production.bootstrap._create_private(snapshot / "unexpected", "unrecognized")
            with self.assertRaisesRegex(ValueError, "unknown or unsafe"):
                self.install.record_upgrade("sha256:" + "d" * 64, snapshot, apply=True)
            (snapshot / "unexpected").unlink()
            production.bootstrap._create_private(self.directory / ".env.changed", production.bootstrap.env_text({**self.values, "FACTORY_CONSOLE_PORT": "59999"}))
            os.replace(self.directory / ".env.changed", self.directory / ".env")
            with self.assertRaisesRegex(ValueError, "changed since"):
                self.install.record_upgrade("sha256:" + "d" * 64, snapshot, apply=True)
            writers.assert_not_called()

    def test_invalid_upgrade_history_fails_closed(self):
        for history in (None, {}, [{"image": "sha256:" + "d" * 64}], [{"previous_image": "wrong"}]):
            (self.directory / ".factory-cutover.json").write_text(json.dumps({**self.provenance, "image_upgrades": history}))
            with self.subTest(history=history), self.assertRaisesRegex(ValueError, "upgrade history"):
                production.Production(self.directory)

    def test_upgrade_running_service_or_volume_writer_never_changes_configuration(self):
        snapshot = self.upgrade_backup()
        image = "sha256:" + "d" * 64
        image_info = {"Id": image, "Architecture": "amd64", "Config": {"Labels": {
            "org.opencontainers.image.source": "https://github.com/Driftward-LLC/factory",
            "org.opencontainers.image.revision": "e" * 40}}}
        original = (self.directory / ".factory-cutover.json").read_bytes()
        for volume_error in (False, True):
            with self.subTest(volume_error=volume_error), patch.object(self.install, "preflight"), \
                    patch.object(production.state, "_docker", return_value=json.dumps([image_info])), \
                    patch.object(production.state, "_no_writers", side_effect=ValueError("volume writer") if volume_error else None), \
                    patch.object(self.install, "service_rows", return_value=[{"State": "running", "Service": "intake-worker"}]):
                with self.assertRaisesRegex(ValueError, "volume writer|every candidate service stopped"):
                    self.install.record_upgrade(image, snapshot, apply=True)
            self.assertEqual((self.directory / ".factory-cutover.json").read_bytes(), original)
            self.assertEqual(production.Production(self.directory).images["factory"], self.image)

    def test_provenance_drift_refused_without_docker(self):
        for key, value in (("destination_project", "factory-foreign"), ("profile_sha256", "bad"), ("deployment_sha256", {}), ("factory_image", "bad"), ("source_images", {}), ("activation_approved", "true")):
            altered = {**self.provenance, key: value}
            (self.directory / ".factory-cutover.json").write_text(json.dumps(altered))
            with self.subTest(key=key), patch.object(production.state, "_docker") as docker:
                with self.assertRaises(ValueError):
                    production.Production(self.directory)
                docker.assert_not_called()

    def test_nonobject_provenance_refused(self):
        for value in (None, [], "invalid"):
            (self.directory / ".factory-cutover.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                production.Production(self.directory)

    def test_tenant_must_match_saved_identity_and_client_token_before_lock(self):
        other = "00000000-0000-4000-8000-000000000000"
        payload = base64.urlsafe_b64encode(json.dumps({"sub": other}).encode()).decode().rstrip("=")
        token_path = self.directory / "env/hatchet-client.env"
        original = token_path.read_text()
        token_path.write_text("HATCHET_CLIENT_TOKEN='synthetic." + payload + ".signature'\n")
        with patch.object(production.portable, "installation_lock") as lock:
            with self.assertRaisesRegex(ValueError, "tenant"):
                production.Production(self.directory)
            token_path.write_text(original)
            changed = {**self.values, "HATCHET_DEFAULT_TENANT_ID": other}
            (self.directory / ".env").write_text(production.bootstrap.env_text(changed))
            with self.assertRaisesRegex(ValueError, "tenant"):
                production.Production(self.directory)
        lock.assert_not_called()

    def test_all_seven_storage_volumes_and_ownership_required(self):
        rows = [{"Name": self.install.project + "-" + suffix, "Labels": {
            production.state.IDENTITY_LABEL: self.metadata["installation_id"], "com.docker.compose.project": self.install.project,
            "com.docker.compose.volume": "factory-" + suffix}} for suffix in production.state.VOLUMES]
        with patch.object(self.install, "assert_owned_resources"), patch.object(production.state, "_docker", return_value=json.dumps(rows)) as docker:
            self.install.assert_storage()
            self.assertEqual(len(docker.call_args.args[0][2:]), 7)
        for altered in (rows[:-1], [*rows[:-1], {**rows[-1], "Labels": {}}]):
            with patch.object(self.install, "assert_owned_resources"), patch.object(production.state, "_docker", return_value=json.dumps(altered)):
                with self.assertRaises(production.ProductionRefusal):
                    self.install.assert_storage()

    def test_source_requires_stopped_unchanged_inventory_restart_disabled_and_no_writers(self):
        ids = "\n".join(self.provenance["source_container_ids"])
        with patch.object(production.state, "_docker", side_effect=[ids, json.dumps(self.source_rows()), ""]), patch.object(production.state, "_no_writers") as writers:
            self.install.assert_source_fenced()
            self.assertEqual(writers.call_args.args[0], ["driftward-factory-" + suffix for suffix in production.state.VOLUMES])
        for field, value in (("State", {"Status": "running"}), ("HostConfig", {"RestartPolicy": {"Name": "unless-stopped"}}), ("Image", "sha256:" + "f" * 64), ("Config", {"Labels": {}})):
            rows = self.source_rows()
            rows[0][field] = value
            with self.subTest(field=field), patch.object(production.state, "_docker", side_effect=[ids, json.dumps(rows)]), patch.object(production.state, "_no_writers"):
                with self.assertRaises(production.ProductionRefusal):
                    self.install.assert_source_fenced()
        with patch.object(production.state, "_docker", return_value=ids + "\n" + "f" * 64):
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                self.install.assert_source_fenced()
        with patch.object(production.state, "_docker", side_effect=[ids, json.dumps(self.source_rows())]), patch.object(production.state, "_no_writers", side_effect=ValueError("writer exists")):
            with self.assertRaises(ValueError):
                self.install.assert_source_fenced()

    def test_compose_boundary_accepts_known_configuration(self):
        with patch.object(self.install, "compose", return_value=json.dumps(self.config())) as compose:
            self.install.assert_config()
        compose.assert_called_once_with("config", "--format", "json", private=True)

    def test_restored_candidate_fences_its_immediate_predecessor_too(self):
        self.install.provenance["predecessors"] = [{"project": "factory-predecessor", "installation_id": self.metadata["installation_id"]}]
        ids = "\n".join(self.provenance["source_container_ids"])
        row = {"Config": {"Labels": {"com.docker.compose.project": "factory-predecessor", "com.docker.compose.service": "bridge", production.state.IDENTITY_LABEL: self.metadata["installation_id"]}},
               "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        for previous_rows, previous_ids in (([row], "predecessor-container"), ([], "")):
            responses = [ids, json.dumps(self.source_rows()), previous_ids] + ([json.dumps(previous_rows)] if previous_ids else []) + [""]
            with patch.object(production.state, "_docker", side_effect=responses), patch.object(production.state, "_no_writers") as writers:
                self.install.assert_source_fenced()
                self.assertEqual(writers.call_args.args[0], ["factory-predecessor-" + suffix for suffix in production.state.VOLUMES])
        for field, value in (("State", {"Status": "running"}), ("HostConfig", {"RestartPolicy": {"Name": "unless-stopped"}}), ("Config", {"Labels": {}})):
            bad = {**row, field: value}
            with self.subTest(field=field), patch.object(production.state, "_docker", side_effect=[ids, json.dumps(self.source_rows()), "predecessor-container", json.dumps([bad])]), patch.object(production.state, "_no_writers"):
                with self.assertRaisesRegex(ValueError, "predecessor"):
                    self.install.assert_source_fenced()

    def test_invalid_predecessor_identity_is_refused(self):
        ids = "\n".join(self.provenance["source_container_ids"])
        for value in ({}, [None], [{"project": self.install.project, "installation_id": self.metadata["installation_id"]}], [{"project": "factory-before", "installation_id": "invalid"}]):
            self.install.provenance["predecessors"] = value
            with patch.object(production.state, "_docker", side_effect=[ids, json.dumps(self.source_rows())]), patch.object(production.state, "_no_writers"):
                with self.assertRaises(ValueError):
                    self.install.assert_source_fenced()

    def test_sibling_same_tenant_must_be_stopped_and_restart_disabled(self):
        row = {"Config": {"Labels": {"com.docker.compose.project": "factory-sibling", "com.docker.compose.service": "bridge", production.state.IDENTITY_LABEL: "00000000-0000-4000-8000-000000000000", production.TENANT_LABEL: self.tenant}},
               "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        with patch.object(production.state, "_docker", side_effect=["sibling-id", json.dumps([row])]) as docker:
            self.install.assert_tenant_fenced()
        self.assertIn("label=" + production.TENANT_LABEL + "=" + self.tenant, docker.call_args_list[0].args[0])
        for field, value in (("State", {"Status": "running"}), ("HostConfig", {"RestartPolicy": {"Name": "unless-stopped"}}), ("Config", {"Labels": {}})):
            with self.subTest(field=field), patch.object(production.state, "_docker", side_effect=["sibling-id", json.dumps([{**row, field: value}])]):
                with self.assertRaises(ValueError):
                    self.install.assert_tenant_fenced()
        row["Config"]["Labels"].update({"com.docker.compose.project": self.install.project, production.state.IDENTITY_LABEL: self.metadata["installation_id"]})
        row["State"]["Status"], row["HostConfig"]["RestartPolicy"]["Name"] = "running", "unless-stopped"
        with patch.object(production.state, "_docker", side_effect=["own-id", json.dumps([row])]):
            self.install.assert_tenant_fenced()

    def test_candidate_rejects_unexpected_containers_and_foreign_volume_writers(self):
        with patch.object(production.state, "_docker", return_value=""):
            self.install.assert_candidate_writers()
        with patch.object(production.state, "_docker", side_effect=["", "foreign-writer"]):
            with self.assertRaisesRegex(ValueError, "foreign container"):
                self.install.assert_candidate_writers()
        for service, oneoff, image in (("keygen", "False", self.image), ("bridge", "True", self.image), ("bridge", "False", "foreign")):
            rows = [{"Image": image, "Config": {"Labels": {"com.docker.compose.service": service,
                "com.docker.compose.oneoff": oneoff, production.state.IDENTITY_LABEL: self.metadata["installation_id"]}}}]
            with patch.object(production.state, "_docker", side_effect=["candidate-id", json.dumps(rows)]):
                with self.assertRaisesRegex(ValueError, "inventory differs"):
                    self.install.assert_candidate_writers()

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose required for configuration render only")
    def test_actual_compose_render_matches_production_boundaries(self):
        self.install.assert_config()

    def test_compose_boundary_rejects_expanded_authority(self):
        mutations = [
            lambda c: c["services"].update(keygen={}),
            lambda c: c["services"]["bridge"].update(image="other:image"),
            lambda c: c["services"]["bridge"].update(privileged=True),
            lambda c: c["services"]["bridge"].update(volumes=[{"type": "bind", "source": "/", "target": "/host"}]),
            lambda c: c["services"]["bridge-executor-worker"]["volumes"][0].update(read_only=False),
            lambda c: c["services"]["bridge-executor-worker"]["volumes"][0].update(bind={"create_host_path": True}),
            lambda c: c["services"]["bridge-executor-worker"]["environment"].update(DRIFTWARD_HERMES_GRPC_TARGET="foreign:1234"),
            lambda c: c["services"]["intake-worker"].update(environment={"GITHUB_TOKEN": "synthetic"}),
            lambda c: c["services"]["factory-operator"]["ports"][0].update(host_ip="0.0.0.0"),
            lambda c: c["volumes"]["factory-bridge-data"].update(name="foreign-bridge-data"),
            lambda c: c["networks"]["factory"].update(external=True),
        ]
        for index, mutate in enumerate(mutations):
            config = self.config()
            mutate(config)
            with self.subTest(index=index), patch.object(self.install, "compose", return_value=json.dumps(config)):
                with self.assertRaises(production.ProductionRefusal):
                    self.install.assert_config()

    def test_missing_local_image_prevents_start(self):
        with patch.object(self.install, "assert_storage"), patch.object(self.install, "assert_config"), patch.object(self.install, "assert_candidate_writers"), \
                patch.object(production.state, "_docker", return_value="wrong-image"), patch.object(self.install, "assert_source_fenced") as fenced:
            with self.assertRaisesRegex(ValueError, "unavailable locally"):
                self.install.preflight()
        fenced.assert_not_called()

    def test_preview_and_unapproved_activation_never_start(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "compose") as compose, patch("builtins.print"):
            self.install.start(core=True, apply=False)
            self.install.start(core=False, apply=False, approve_effects=True)
            with self.assertRaisesRegex(ValueError, "approve-effects"):
                self.install.start(core=False, apply=True)
        compose.assert_not_called()

    def test_preview_never_creates_tenant_lock(self):
        with patch.object(self.install, "preflight"), patch.object(production.portable, "installation_lock") as lock, patch("builtins.print"):
            self.install.start(core=True, apply=False)
        lock.assert_not_called()

    def test_sibling_starts_share_tenant_lock_from_preflight_through_up(self):
        sibling = production.Production(self.directory)
        sibling.project, sibling.directory = "factory-sibling", Path(self.temp.name) / "sibling"
        observed = []

        def check_locked(*args, **kwargs):
            with self.assertRaisesRegex(ValueError, "another operation"):
                sibling.start(core=True, apply=True)
            observed.append(True)

        with patch.object(sibling, "preflight") as sibling_preflight, patch.object(sibling, "compose") as sibling_compose, \
                patch.object(self.install, "preflight", side_effect=check_locked), patch.object(self.install, "assert_source_fenced"), \
                patch.object(self.install, "service_rows", return_value=[]), patch.object(self.install, "compose", side_effect=check_locked), patch("builtins.print"):
            self.install.start(core=True, apply=True)
        self.assertEqual(len(observed), 2)
        sibling_preflight.assert_not_called()
        sibling_compose.assert_not_called()

    def test_core_starts_only_three_roles_without_dependencies_builds_pulls_or_migrations(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "assert_source_fenced") as fenced, \
                patch.object(self.install, "service_rows", return_value=[]), patch.object(self.install, "compose") as compose, patch("builtins.print"):
            self.install.start(core=True, apply=True)
        fenced.assert_called_once()
        compose.assert_called_once_with("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180", "--no-deps", *production.CORE, private=True)

    def test_core_refuses_when_effect_services_running(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "service_rows", return_value=[{"Service": "outbox-dispatcher", "State": "running"}]), patch.object(self.install, "compose") as compose:
            with self.assertRaisesRegex(ValueError, "effect-bearing"):
                self.install.start(core=True, apply=True)
        compose.assert_not_called()

    def test_source_race_refuses_immediately_before_start(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "service_rows", return_value=[]), \
                patch.object(self.install, "assert_source_fenced", side_effect=production.ProductionRefusal("source restarted")), patch.object(self.install, "compose") as compose:
            with self.assertRaisesRegex(ValueError, "source restarted"):
                self.install.start(core=True, apply=True)
        compose.assert_not_called()

    def test_full_start_requires_real_socket_and_explicit_effect_authority(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "assert_source_fenced"), \
                patch.object(production.portable, "installation_lock", return_value=nullcontext()), \
                patch.object(Path, "stat", return_value=os.stat_result((stat.S_IFSOCK, 0, 0, 1, 0, 0, 0, 0, 0, 0))), \
                patch.object(self.install, "compose") as compose, patch("builtins.print"):
            self.install.start(core=False, apply=True, approve_effects=True)
        compose.assert_called_once_with("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180", *production.SERVICES, private=True)

    def test_stop_preview_and_apply_preserve_data(self):
        args = argparse.Namespace(directory=self.directory, command="stop", apply=False)
        with patch.object(production, "Production", return_value=self.install), patch.object(self.install, "assert_owned_resources"), \
                patch.object(self.install, "assert_config"), patch.object(self.install, "compose") as compose, patch("builtins.print"):
            production.dispatch(args)
            compose.assert_not_called()
            args.apply = True
            production.dispatch(args)
        compose.assert_called_once_with("stop", private=True)

    def test_backup_and_restore_use_shared_cold_state_with_production_opt_in(self):
        args = argparse.Namespace(directory=self.directory, command="backup", snapshot=Path(self.temp.name) / "snapshot", apply=False)
        with patch.object(production, "Production", return_value=self.install), patch.object(self.install, "assert_storage") as storage, \
                patch.object(self.install, "assert_config"), patch.object(production.state, "backup") as backup:
            production.dispatch(args)
        storage.assert_called_once()
        backup.assert_called_once_with(self.install, args.snapshot, apply=False)
        args.command, args.project, args.port_offset = "restore", "factory-restored", 22000
        with patch.object(production, "Production") as constructor, patch.object(production.state, "restore") as restore:
            production.dispatch(args)
        constructor.assert_not_called()
        restore.assert_called_once_with(args.snapshot, args.directory, args.project, apply=False, port_offset=22000, production=True)

    def test_status_is_read_only_and_includes_linear_roles(self):
        with patch.object(self.install, "assert_owned_resources"), patch.object(self.install, "service_rows", return_value=[]) as rows, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.install.status()
        rows.assert_called_once()
        self.assertIn("factory-linear-token-refresh: missing", output.getvalue())

    def test_cli_errors_withhold_private_details(self):
        with patch.object(sys, "argv", ["production", "--directory", str(self.directory), "preview"]), \
                patch.object(production, "dispatch", side_effect=RuntimeError("SENSITIVE captured config")), patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(production.main(), 1)
        self.assertNotIn("SENSITIVE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
