"""Migration refusal boundaries and unified production behavior, with synthetic state."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import sys
import unittest
from unittest.mock import patch, Mock

import test_factory_production as fixtures
production = fixtures.production

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import profile_migration as migration
import migration_postgres as pg


class MigrationTests(unittest.TestCase):
    setUp = fixtures.ProductionTests.setUp
    upgrade_backup = fixtures.ProductionTests.upgrade_backup

    def unified(self):
        new = "sha256:" + "d" * 64
        previous = self.install.images.copy()
        self.metadata.update(layout=production.state.UNIFIED, image=new)
        self.values.update(FACTORY_IMAGE=new, FACTORY_POSTGRES_IMAGE=new, FACTORY_HATCHET_IMAGE=new)
        self.provenance.update(
            predecessors=[{"project": "factory-before", "installation_id": self.metadata["installation_id"]}],
            deployment_sha256={n: production.state._hash(production.HERE / n) for n in production.state.deployment_files(production.state.PRODUCTION, self.metadata)},
            image_upgrades=[{"previous_image": self.image, "image": new, "commit": "d" * 40,
                             "backup": "/private/backup", "backup_manifest_sha256": "e" * 64}],
            profile_migration={"format_version": 1, "layout": production.state.UNIFIED, "image": new,
                               "previous_project": "factory-before", "previous_installation_id": self.metadata["installation_id"],
                               "previous_images": previous, "previous_deployment_sha256": {"docker-compose.yml": "a" * 64},
                               "preview_sha256": "a" * 64, "snapshot_sha256": "e" * 64,
                               "hermes_archive_sha256": "f" * 64, "previous_profile_sha256": "b" * 64})
        for name, content in ((".env", production.bootstrap.env_text(self.values)),
                              (production.bootstrap.MARKER, json.dumps(self.metadata)),
                              (production.state.PROVENANCE, json.dumps(self.provenance))):
            (self.directory / name).write_text(content)
        return production.Production(self.directory)

    def test_unified_requires_receipt_and_preserves_original_source_pins(self):
        install = self.unified()
        self.assertEqual(set(install.images.values()), {self.values["FACTORY_IMAGE"]})
        self.assertEqual(len(install.durable_volumes), 8)
        self.assertIn("factory-hermes", install.services)
        self.assertEqual(install.provenance["source_images"], self.provenance["source_images"])
        self.assertEqual(install.args[-1], str(production.HERE / "compose.unified.yml"))
        for key in ("profile_migration", "predecessors", "image_upgrades"):
            bad = copy.deepcopy(self.provenance)
            del bad[key]
            (self.directory / production.state.PROVENANCE).write_text(json.dumps(bad))
            with self.subTest(key=key), self.assertRaises(ValueError):
                production.Production(self.directory)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose configuration rendering required")
    def test_rendered_unified_layout_uses_one_image_and_idle_hermes(self):
        install = self.unified()
        install.assert_config()
        config = json.loads(install.compose("config", "--format", "json", private=True))
        self.assertEqual({v["image"] for v in config["services"].values()}, {self.values["FACTORY_IMAGE"]})
        for role in ("postgres", "hatchet-lite"):
            self.assertEqual(config["services"][role]["entrypoint"][0], "/usr/bin/tini")
        self.assertEqual(config["services"]["factory-hermes"]["command"], ["/opt/hermes/.venv/bin/python", "/app/deploy/hermes-default-profiles.py", "sleep", "infinity"])
        config["services"]["factory-hermes"]["command"] = ["hermes", "gateway"]
        with patch.object(install, "compose", return_value=json.dumps(config)), self.assertRaisesRegex(ValueError, "idle"):
            install.assert_config()

    def test_unified_upgrade_updates_all_pins_and_retains_migration_history(self):
        install = self.unified()
        metadata = json.loads((self.directory / production.bootstrap.MARKER).read_text())
        metadata["telemetry"] = {"image": self.values["FACTORY_IMAGE"], "port": 4318, "enabled": True,
                                 "deployment_sha256": {n: production.state._hash(production.HERE / n) for n in production.telemetry.FILES}}
        (self.directory / production.bootstrap.MARKER).write_text(json.dumps(metadata))
        production.bootstrap._create_private(self.directory / production.telemetry.ENV_FILE,
            production.bootstrap.env_text({"POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces", "POSTHOG_PROJECT_TOKEN": "phc_synthetic_fixture"}))
        install = production.Production(self.directory)
        snapshot = Path(self.temp.name) / "unified-snapshot"
        snapshot.mkdir(mode=0o700)
        for folder in ("config", "config/env", "config/config", "config/keys", "config/keys/hatchet", "volumes"):
            (snapshot / folder).mkdir(mode=0o700)
        for name in production.state.config_files(production.state.PRODUCTION, metadata):
            production.bootstrap._create_private(snapshot / "config" / name, production.bootstrap._read_private(self.directory / name))
        for suffix in production.state.DURABLE_VOLUMES:
            production.bootstrap._create_private(snapshot / "volumes" / (suffix + ".tar"), "synthetic archive")
        manifest = {"format_version": 1, "profile": production.state.PRODUCTION, "project": install.project,
                    "installation_id": metadata["installation_id"], "factory_image": install.images["factory"],
                    "images": {v: v for v in install.images.values()}, "volumes": list(production.state.DURABLE_VOLUMES),
                    "deployment_sha256": {n: production.state._hash(production.HERE / n) for n in production.state.deployment_files(production.state.PRODUCTION, metadata)},
                    "sha256": {n: production.state._hash(snapshot / n) for n in production.state._private_files(snapshot)}}
        production.bootstrap._create_private(snapshot / "manifest.json", json.dumps(manifest))
        new = "sha256:" + "e" * 64
        info = {"Id": new, "Architecture": "amd64", "Config": {"Labels": {
            "org.opencontainers.image.source": "https://github.com/Driftward-LLC/factory", "org.opencontainers.image.revision": "f" * 40}}}
        with patch.object(install, "preflight"), patch.object(install, "assert_source_fenced"), patch.object(install, "service_rows", return_value=[]), \
                patch.object(production.state, "_no_writers"), patch.object(production.state, "_docker", return_value=json.dumps([info])), patch("builtins.print"):
            install.record_upgrade(new, snapshot, apply=True)
        updated = production.Production(self.directory)
        self.assertEqual(set(updated.images.values()), {new})
        self.assertEqual(updated.provenance["profile_migration"], install.provenance["profile_migration"])
        self.assertEqual(updated.provenance["source_images"], install.provenance["source_images"])

    def test_paused_resume_never_starts_execution_and_requires_effect_approval(self):
        install = self.unified()
        with patch.object(install, "preflight"), patch.object(install, "assert_source_fenced"), \
                patch.object(install, "service_rows", return_value=[]), patch.object(install, "compose") as compose, patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "approve-effects"):
                install.start(core=False, apply=True, paused=True)
            compose.assert_not_called()
            install.start(core=False, apply=True, paused=True, approve_effects=True)
            calls = [call.args for call in compose.call_args_list]
            self.assertEqual(calls[0], ("create", "--no-deps", "--no-build", "--pull", "never", "bridge-executor-worker"))
            self.assertEqual(calls[1][0], "up")
            self.assertNotIn("bridge-executor-worker", calls[1])
            self.assertIn("--no-deps", calls[1])
            self.assertIn("factory-hermes", calls[1])

    def test_paused_resume_refuses_running_executor(self):
        with patch.object(self.install, "preflight"), patch.object(self.install, "service_rows", return_value=[{"Service": "bridge-executor-worker", "State": "running"}]), \
                patch.object(self.install, "compose") as compose, patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "already stopped"):
                self.install.start(core=False, apply=True, paused=True, approve_effects=True)
            compose.assert_not_called()

    def test_migration_preserves_crlf_configuration_bytes_and_receipt_hashes(self):
        supplied = self.directory / "config"
        original = {}
        for name in production.bootstrap.ORGANIZATION_DEFAULTS:
            path = supplied / name
            original[name] = path.read_bytes().replace(b"\n", b"\r\n")
            path.write_bytes(original[name])
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in original.items()}
        plan = {"hermes": {"volume": "factory-dedicated-data"}, "previous_images": {"postgres": self.image},
                "configuration_sha256": hashes, "image": "sha256:" + "d" * 64,
                "snapshot_sha256": "e" * 64, "commit": "f" * 40}
        args = argparse.Namespace(approve_digest=migration.digest(plan), destination=Path(self.temp.name) / "destination",
                                  source=self.directory, configuration=supplied, snapshot=Path(self.temp.name) / "snapshot",
                                  image=plan["image"], project="factory-new")
        def archive(image, volume, work, name, **kwargs):
            production.bootstrap._create_private(work / name, "synthetic archive")
        runtime = Mock(SERVICES=())
        with patch.object(migration, "production_module", return_value=runtime), patch.object(migration, "source_stopped"), \
                patch.object(migration, "hermes_stopped"), patch.object(migration, "source_check"), \
                patch.object(migration.state, "_no_writers"), patch.object(migration.state, "_tar", side_effect=archive), \
                patch.object(migration.state, "_archive_check"), patch.object(migration.state, "VOLUMES", ()), \
                patch.object(migration.state, "DURABLE_VOLUMES", ()), patch.object(migration.pg, "migrate", return_value={"rows": 0}):
            migration.apply(args, plan, self.metadata, self.values, self.provenance, set())
        for name, content in original.items():
            self.assertEqual((args.destination / "config" / name).read_bytes(), content)
        receipt = json.loads((args.destination / production.state.PROVENANCE).read_text())["profile_migration"]
        self.assertEqual(receipt["configuration_sha256"], hashes)
        self.assertEqual(receipt["preview_sha256"], migration.digest(plan))
        self.assertEqual(receipt["previous_deployment_sha256"], self.provenance["deployment_sha256"])
        self.assertEqual({name: text.encode("utf-8") for name, text in production.bootstrap.configuration(args.destination / "config").items()}, original)

    def test_apply_rejects_stale_digest_before_mutation(self):
        args = argparse.Namespace(approve_digest="stale")
        with patch.object(migration, "source_stopped") as stopped, patch.object(migration, "create_volume") as volume:
            with self.assertRaisesRegex(ValueError, "digest"):
                migration.apply(args, {"project": "factory-test"}, {}, {}, {}, set())
            stopped.assert_not_called()
            volume.assert_not_called()

    def test_personal_or_shared_hermes_is_refused_before_copying(self):
        row = {"Id": "container", "Image": "image", "Config": {"Labels": {"com.docker.compose.project": "personal-hermes", "com.docker.compose.service": "hermes"}},
               "Mounts": [{"Destination": "/opt/data", "Type": "volume", "Name": "personal-hermes-data"}]}
        for project in ("personal-hermes", "factory-hermes"):
            row["Config"]["Labels"]["com.docker.compose.project"] = project
            with patch.object(migration.state, "_docker", return_value=json.dumps([row])), self.assertRaisesRegex(ValueError, "dedicated Factory"):
                migration.hermes_source(project)
        row["Config"]["Labels"]["com.docker.compose.project"] = "factory-hermes"
        with patch.object(migration.state, "_docker", return_value=json.dumps([row])), self.assertRaisesRegex(ValueError, "dedicated Factory named volume"):
            migration.hermes_source("factory-hermes")
        row["Mounts"][0]["Name"] = "factory-hermes-data"
        with patch.object(migration.state, "_docker", return_value=json.dumps([row])):
            self.assertEqual(migration.hermes_source("factory-hermes")["volume"], "factory-hermes-data")

    def test_source_must_be_complete_stopped_restart_disabled_owned_inventory(self):
        row = {"Config": {"Labels": {production.state.IDENTITY_LABEL: "owner", "com.docker.compose.service": "bridge"}},
               "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        with patch.object(migration.state, "_docker", side_effect=["id", json.dumps([row])]):
            migration.source_stopped("factory-old", "owner", ["bridge"])
        for mutate in (lambda r: r["State"].update(Status="running"),
                       lambda r: r["HostConfig"]["RestartPolicy"].update(Name="unless-stopped"),
                       lambda r: r["Config"]["Labels"].update({production.state.IDENTITY_LABEL: "foreign"})):
            altered = copy.deepcopy(row)
            mutate(altered)
            with patch.object(migration.state, "_docker", side_effect=["id", json.dumps([altered])]), self.assertRaises(ValueError):
                migration.source_stopped("factory-old", "owner", ["bridge"])
        with patch.object(migration.state, "_docker", return_value=""), self.assertRaisesRegex(ValueError, "incomplete"):
            migration.source_stopped("factory-old", "owner", ["bridge"])

    def test_postgres_normalization_is_limited_and_row_changes_refused(self):
        old = {"hatchet": {"columns": [{"table_schema": "public", "table_name": "test", "column_name": "created_at",
                                      "ordinal_position": 2, "column_default": None, "data_type": "timestamp without time zone"}],
                           "tables": [{"row_count": 3, "row_sha256": "a" * 64}]}}
        new = copy.deepcopy(old)
        new["hatchet"]["columns"][0].update(ordinal_position=1, column_default="NULL::timestamp without time zone")
        self.assertEqual(pg.compare(old, new), {"tables": 1, "rows": 3, "equivalent_null_defaults": 1})
        self.assertEqual(old["hatchet"]["columns"][0]["ordinal_position"], 2)
        for mutate in (lambda x: x["hatchet"]["tables"][0].update(row_sha256="b" * 64),
                       lambda x: x["hatchet"]["columns"][0].update(column_default="now()"),
                       lambda x: x["hatchet"]["columns"][0].update(column_name="other")):
            changed = copy.deepcopy(new)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, "differ"):
                pg.compare(old, changed)

    def test_archive_contents_propagates_exact_runtime_image(self):
        archive = Path(self.temp.name) / "links.tar"
        with tarfile.open(archive, "w") as handle:
            handle.addfile(tarfile.TarInfo("file"))
        with patch.object(migration.state, "_archive_check") as check:
            migration.archive_contents(archive, hermes=True, image=self.image)
        check.assert_called_once_with(archive, hermes=True, image=self.image)

    def test_hermes_links_are_scoped_and_cannot_be_written_through(self):
        archive = Path(self.temp.name) / "links.tar"
        def make(target, child=False):
            with tarfile.open(archive, "w") as handle:
                link = tarfile.TarInfo("./current")
                link.type, link.linkname = tarfile.SYMTYPE, target
                handle.addfile(link)
                if child:
                    handle.addfile(tarfile.TarInfo("./current/overwrite"))
        for target in ("/opt/data/releases/current", "releases/current", ".", "/opt/data/releases/../current-target", "releases/../current-target"):
            make(target)
            production.state._archive_check(archive, hermes=True)
            with self.assertRaises(ValueError):
                production.state._archive_check(archive)
        for target, child in (("/etc/shadow", False), ("../../etc/shadow", False),
                              ("/opt/data/../../etc", False), ("/opt/data/releases", True)):
            make(target, child)
            with self.subTest(target=target, child=child), self.assertRaises(ValueError):
                production.state._archive_check(archive, hermes=True)

    def test_hermes_chained_links_cannot_escape_through_parent_traversal(self):
        archive = Path(self.temp.name) / "links.tar"
        for target in ("a/../outside", "/opt/data/a/../outside"):
            with self.subTest(target=target):
                with tarfile.open(archive, "w") as handle:
                    for name, linkname in (("./a", "."), ("./b", target)):
                        link = tarfile.TarInfo(name)
                        link.type, link.linkname = tarfile.SYMTYPE, linkname
                        handle.addfile(link)
                with self.assertRaisesRegex(ValueError, "escapes"):
                    production.state._archive_check(archive, hermes=True)

    def test_apply_rejects_changed_cold_data_before_creating_destination_volumes(self):
        snapshot = self.upgrade_backup()
        plan = {"hermes": {"volume": "factory-dedicated-data"}}
        args = argparse.Namespace(approve_digest=migration.digest(plan), destination=Path(self.temp.name) / "destination",
                                  snapshot=snapshot, image=self.image, project="factory-new")
        def changed(image, volume, work, name, **kwargs):
            production.bootstrap._create_private(work / name, "changed source bytes")
        with patch.object(migration, "source_stopped"), patch.object(migration, "hermes_stopped"), \
                patch.object(migration.state, "_no_writers"), patch.object(migration.state, "_tar", side_effect=changed), \
                patch.object(migration, "create_volume") as volumes:
            with self.assertRaisesRegex(ValueError, "data changed"):
                migration.apply(args, plan, self.metadata, self.values, self.provenance, set())
            volumes.assert_not_called()
        self.assertFalse((args.destination / production.bootstrap.MARKER).exists())

    def test_sqlite_backup_requires_quiescence(self):
        snapshot = Path(self.temp.name) / "sqlite-snapshot"
        (snapshot / "volumes").mkdir(parents=True)
        for volume, filename in (("bridge-data", "bridge.db"), ("ingress-data", "ingress.db"), ("lifecycle-data", "outbox.db")):
            path = Path(self.temp.name) / filename
            with sqlite3.connect(path) as conn:
                conn.executescript("CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);")
            with tarfile.open(snapshot / "volumes" / (volume + ".tar"), "w") as handle:
                handle.add(path, arcname="./" + filename)
        self.assertEqual(len(migration.check_sqlite(snapshot)), 3)
        with sqlite3.connect(Path(self.temp.name) / "bridge.db") as conn:
            conn.execute("INSERT INTO factory_execution_leases VALUES (NULL)")
        with tarfile.open(snapshot / "volumes/bridge-data.tar", "w") as handle:
            handle.add(Path(self.temp.name) / "bridge.db", arcname="./bridge.db")
        with self.assertRaisesRegex(ValueError, "unresolved execution"):
            migration.check_sqlite(snapshot)

    def test_failure_output_never_contains_subprocess_details(self):
        with patch.object(pg.subprocess, "run", return_value=Mock(returncode=1, stdout=b"secret", stderr=b"secret")):
            with self.assertRaisesRegex(RuntimeError, "private output withheld") as result:
                pg.run(["synthetic"])
            self.assertNotIn("secret", str(result.exception))

    def test_created_execution_container_is_recognized_as_paused_by_upgrader(self):
        spec = importlib.util.spec_from_file_location("migration_auto", production.HERE / "factory-auto-deploy.py")
        auto = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(auto)
        install = Mock(services=["bridge-executor-worker", "bridge"])
        install.service_rows.return_value = [{"Service": "bridge-executor-worker", "State": "created"}, {"Service": "bridge", "State": "running", "Health": "healthy"}]
        self.assertEqual(auto.paused_execution(install), ("bridge-executor-worker",))
        auto.healthy(install, auto.paused_execution(install))
        install.preflight.assert_called_once()


    def naming_install(self):
        import check_factory_naming_migration as fixture
        install = self.unified()
        install.provenance["profile_migration"]["naming_migration"] = {
            "mapping": fixture.configuration(), "gateway_policy": "unbound",
            "source_predecessors": copy.deepcopy(install.provenance["predecessors"])}
        return install

    def test_naming_migration_full_start_is_unbound_even_in_preview(self):
        install = self.naming_install()
        with patch.object(install, "preflight") as preflight, patch.object(install, "compose") as compose:
            with self.assertRaisesRegex(ValueError, "unbound"):
                install.start(core=False, apply=False, approve_effects=True)
            preflight.assert_not_called()
            compose.assert_not_called()

    def test_naming_migration_scans_original_tenant_labels_and_refuses_live_old_owner(self):
        install = self.naming_install()
        mapping = install.provenance["profile_migration"]["naming_migration"]["mapping"]
        row = {"Config": {"Labels": {"com.docker.compose.project": "factory-prior",
                "com.docker.compose.service": "bridge", mapping["source_identity_label"]: self.metadata["installation_id"],
                mapping["source_tenant_label"]: self.tenant}},
               "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        def docker(command, env=None):
            if command[:2] == ["container", "inspect"]:
                return json.dumps([row])
            return "old-id" if command[-1] == "label=" + mapping["source_tenant_label"] + "=" + self.tenant else ""
        with patch.object(production.state, "_docker", side_effect=docker) as called:
            install.assert_tenant_fenced()
            self.assertTrue(any(production.TENANT_LABEL in call.args[0][-1] for call in called.call_args_list))
            self.assertTrue(any(mapping["source_tenant_label"] in call.args[0][-1] for call in called.call_args_list))
            row["State"]["Status"] = "running"
            with self.assertRaisesRegex(ValueError, "another installation"):
                install.assert_tenant_fenced()

    def test_naming_migration_fences_predecessor_with_original_installation_label(self):
        install = self.naming_install()
        receipt = install.provenance["profile_migration"]
        receipt["hermes_source"] = {}
        old_label = receipt["naming_migration"]["mapping"]["source_identity_label"]
        original = [{"Id": container, "Image": install.provenance["source_images"][service],
                     "Config": {"Labels": {"com.docker.compose.project": install.provenance["source_project"],
                                           "com.docker.compose.service": service}},
                     "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
                    for container, service in zip(install.provenance["source_container_ids"], production.SERVICES)]
        prior = {"Config": {"Labels": {"com.docker.compose.project": receipt["previous_project"],
                 "com.docker.compose.service": "bridge", old_label: receipt["previous_installation_id"]}},
                 "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        def docker(command, env=None):
            if command[:2] == ["container", "inspect"]:
                return json.dumps([prior] if command[2] == "prior" else original)
            return ("prior" if command[-1].endswith("=" + receipt["previous_project"])
                    else "\n".join(install.provenance["source_container_ids"]))
        with patch.object(production.state, "_docker", side_effect=docker), patch.object(production.state, "_no_writers"), \
                patch.object(migration, "hermes_stopped"), patch.object(install, "assert_tenant_fenced"):
            install.assert_source_fenced()
            prior["Config"]["Labels"][old_label] = "foreign"
            with self.assertRaisesRegex(ValueError, "predecessor"):
                install.assert_source_fenced()


    def test_inherited_legacy_predecessor_fencing_survives_a_new_restore(self):
        install = self.naming_install()
        receipt = install.provenance["profile_migration"]
        receipt["hermes_source"] = {}
        ancestor = {"project": "factory-older-restored", "installation_id": "11111111-2222-4333-8444-555555555555"}
        install.provenance["predecessors"].insert(0, ancestor)
        receipt["naming_migration"]["source_predecessors"].insert(0, copy.deepcopy(ancestor))
        old_label = receipt["naming_migration"]["mapping"]["source_identity_label"]
        source_rows = [{"Id": container, "Image": install.provenance["source_images"][service],
                        "Config": {"Labels": {"com.docker.compose.project": install.provenance["source_project"],
                                              "com.docker.compose.service": service}},
                        "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
                       for container, service in zip(install.provenance["source_container_ids"], production.SERVICES)]
        predecessors = {}
        for identity in install.provenance["predecessors"]:
            predecessors[identity["project"]] = {"Config": {"Labels": {"com.docker.compose.project": identity["project"],
                "com.docker.compose.service": "bridge", old_label: identity["installation_id"]}},
                "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        def docker(command, env=None):
            if command[:2] == ["container", "inspect"]:
                return json.dumps([predecessors[command[2]]] if command[2] in predecessors else source_rows)
            project = command[-1].rsplit("=", 1)[-1]
            return project if project in predecessors else "\n".join(install.provenance["source_container_ids"])
        def check():
            with patch.object(production.state, "_docker", side_effect=docker), patch.object(production.state, "_no_writers"), \
                    patch.object(migration, "hermes_stopped"), patch.object(install, "assert_tenant_fenced"):
                install.assert_source_fenced()
        check()
        # A subsequent new-generation restore appends this migration's project;
        # it must use the new label while every recorded old identity stays old.
        current = {"project": install.project, "installation_id": install.metadata["installation_id"]}
        install.provenance["predecessors"].append(current)
        predecessors[current["project"]] = {"Config": {"Labels": {"com.docker.compose.project": current["project"],
            "com.docker.compose.service": "bridge", production.state.IDENTITY_LABEL: current["installation_id"]}},
            "State": {"Status": "exited"}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        install.project = "factory-restored-neutral"
        check()
        for state, restart in (("running", "no"), ("exited", "unless-stopped")):
            with self.subTest(state=state, restart=restart):
                predecessors[ancestor["project"]]["State"]["Status"] = state
                predecessors[ancestor["project"]]["HostConfig"]["RestartPolicy"]["Name"] = restart
                with self.assertRaisesRegex(ValueError, "predecessor"):
                    check()


if __name__ == "__main__":
    unittest.main()
