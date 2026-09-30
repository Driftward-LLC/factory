from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import portable_state as state

spec = importlib.util.spec_from_file_location("factory_cutover", ROOT / "deploy/factory-cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
sys.path.pop(0)

FACTORY_IMAGE = "sha256:" + "a" * 64
POSTGRES_IMAGE = "sha256:" + "b" * 64
HATCHET_IMAGE = "sha256:" + "c" * 64
TENANT = "a1111111-1111-4111-8111-111111111111"
TOKEN = "e30." + base64.urlsafe_b64encode(json.dumps({"sub": TENANT}).encode()).decode().rstrip("=") + ".signature"


def ledger(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE bridge_jobs (state TEXT);
        CREATE TABLE factory_attempts (status TEXT);
        CREATE TABLE factory_execution_leases (released_at TEXT, effect_observed_at TEXT);
        CREATE TABLE verification_leases (closed_at TEXT);
        INSERT INTO bridge_jobs VALUES ('ready_for_human_review');
        INSERT INTO factory_attempts VALUES ('verified_ready');
        INSERT INTO factory_execution_leases VALUES (NULL, 'observed');
    """)
    connection.commit()
    return connection


def write_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo("./" + name)
            member.mode = 0o600
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    path.chmod(0o600)


class FakeDocker:
    def __init__(self, project: str, rows: list[dict], files: dict[str, dict[str, bytes]]):
        self.project = project
        self.rows = rows
        self.calls = []
        self.archives = []
        self.foreign_writer = False
        self.destination_container = False
        self.networks = []
        self.volumes = {
            f"{project}-{suffix}": {
                "Name": f"{project}-{suffix}",
                "Labels": {"com.docker.compose.project": project,
                           "com.docker.compose.volume": "factory-" + suffix},
                "files": files[suffix],
            }
            for suffix in state.VOLUMES
        }

    def __call__(self, args, env=None):
        self.calls.append(args)
        if args[:2] == ["container", "inspect"]:
            return json.dumps([row for row in self.rows if row["Id"] in args[2:]])
        if args[:2] == ["image", "inspect"]:
            return args[-1]
        if args[:2] == ["volume", "ls"]:
            return "\n".join(self.volumes)
        if args[:2] == ["volume", "inspect"]:
            return json.dumps([{key: value for key, value in self.volumes[name].items() if key != "files"}
                               for name in args[2:]])
        if args[:2] == ["volume", "create"]:
            labels = dict(args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "--label")
            self.volumes.setdefault(args[-1], {"Name": args[-1], "Labels": labels, "files": {}})
            return args[-1]
        if args[:2] == ["network", "ls"]:
            return "\n".join(self.networks)
        if args[0] == "ps":
            filters = [args[index + 1] for index, value in enumerate(args) if value == "--filter"]
            if any(value.startswith("volume=") for value in filters):
                return "other-project-writer" if self.foreign_writer else ""
            if f"label=com.docker.compose.project={self.project}" in filters:
                rows = self.rows if "-aq" in args else [row for row in self.rows if row["State"]["Status"] == "running"]
                return "\n".join(row["Id"] for row in rows)
            return "destination-container" if self.destination_container else ""
        raise AssertionError(f"Unexpected Docker operation: {args}")

    def tar(self, image, volume, directory, filename, *, restore, env=None):
        self.archives.append((image, volume, restore))
        if restore:
            with tarfile.open(directory / filename) as archive:
                self.volumes[volume]["files"] = {
                    member.name.removeprefix("./"): archive.extractfile(member).read()
                    for member in archive if member.isfile()
                }
        else:
            write_archive(directory / filename, self.volumes[volume]["files"])


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env_dir, self.keys = self.root / "source-env", self.root / "source-keys"
        self.directory, self.snapshot = self.root / "destination", self.root / "snapshot"
        self.env_dir.mkdir(mode=0o700)
        self.keys.mkdir(mode=0o700)
        self.project = "legacy-fixture"
        self.envs = {name: {} for name in bootstrap.ENV_FILES}
        self.envs.update({
            "hatchet-workers.env": {"HATCHET_INTAKE_DRY_RUN": "false", "HATCHET_HERMES_BRIDGE_TOKEN": "bridge-secret"},
            "hatchet-bridge.env": {"HATCHET_HERMES_BRIDGE_TOKEN": "bridge-secret"},
            "hatchet-event-ingress.env": {"HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET": "signing-secret"},
            "hatchet-worker-bridge-executor.env": {
                "DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED": "1",
                "DRIFTWARD_HERMES_GRPC_TARGET": "unix:///run/driftward-hermes-factory/hermes.sock",
            },
            "factory-operator.env": {"HATCHET_INTAKE_DRY_RUN": "false"},
            "factory-linear-token.env": {"FACTORY_LINEAR_CLIENT_SECRET": "linear-secret"},
        })
        for name, values in self.envs.items():
            bootstrap._create_private(self.env_dir / name, "".join(f"{key}={value}\n" for key, value in values.items()))
        for name in cutover.KEYS:
            bootstrap._create_private(self.keys / name, f"private-key-fixture-{name}\n")
        rows = []
        for name in cutover.SERVICES:
            values = {}
            for filename, consumers in cutover.ENV_CONSUMERS.items():
                if name in consumers:
                    values.update(self.envs[filename])
            if name in (*cutover.ENV_CONSUMERS["hatchet-workers.env"], *cutover.ENV_CONSUMERS["factory-operator.env"]):
                values["HATCHET_CLIENT_TOKEN"] = TOKEN
            if name == "postgres":
                values.update(POSTGRES_USER="hatchet", POSTGRES_PASSWORD="db-password", POSTGRES_DB="hatchet")
            if name == "hatchet-lite":
                values.update(DEFAULT_TENANT_ID=TENANT, SERVER_AUTH_COOKIE_DOMAIN="localhost",
                              SERVER_AUTH_COOKIE_SECRETS="cookie-one cookie-two", SERVER_URL="http://localhost:8888",
                              ADMIN_EMAIL="admin@example.test", ADMIN_PASSWORD="admin-password", ADMIN_NAME="Admin",
                              DEFAULT_TENANT_NAME="Original Factory", DEFAULT_TENANT_SLUG="original-factory")
            mounts = [{"Type": "volume", "Name": f"{self.project}-{suffix}", "Destination": target, "RW": True}
                      for suffix, (consumer, target) in cutover.VOLUME_CONSUMERS.items() if consumer == name]
            if name == "hatchet-lite":
                mounts.append({"Type": "bind", "Source": str(self.keys), "Destination": "/keys", "RW": False})
            rows.append({"Id": "container-" + name, "Image": POSTGRES_IMAGE if name == "postgres" else HATCHET_IMAGE if name == "hatchet-lite" else FACTORY_IMAGE,
                         "Config": {"Env": [f"{key}={value}" for key, value in values.items()], "Labels": {
                             "com.docker.compose.project": self.project, "com.docker.compose.service": name}},
                         "Mounts": mounts, "State": {"Status": "exited"}})
        db = self.root / "bridge.db"
        ledger(db).close()
        files = {suffix: {"retained": f"original-{suffix}".encode()} for suffix in state.VOLUMES}
        files["bridge-data"] = {"bridge.db": db.read_bytes()}
        self.docker = FakeDocker(self.project, rows, files)
        docker_patch = patch.object(state, "_docker", self.docker)
        docker_patch.start()
        self.addCleanup(docker_patch.stop)
        tar_patch = patch.object(state, "_tar", self.docker.tar)
        tar_patch.start()
        self.addCleanup(tar_patch.stop)
        self.overrides = {name: {} for name in cutover.SERVICES}
        overrides_patch = patch.object(cutover, "compose_overrides", return_value=self.overrides)
        overrides_patch.start()
        self.addCleanup(overrides_patch.stop)
        output_patch = patch("builtins.print")
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def prepare(self, apply=False):
        return cutover.prepare(self.project, self.env_dir, self.keys, self.directory,
                               "factory-target", self.snapshot, port_offset=10000, apply=apply)

    def service(self, name):
        return next(row for row in self.docker.rows if row["Config"]["Labels"]["com.docker.compose.service"] == name)

    def test_preview_running_source_is_read_only(self):
        self.service("bridge")["State"]["Status"] = "running"
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        report = self.prepare()
        self.assertEqual(report["running_source_services"], ["bridge"])
        self.assertFalse(report["destination_will_start"])
        self.assertEqual(report["durable_volumes"], 7)
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertFalse(self.docker.archives)
        self.assertTrue(all(args[0] in {"ps", "container", "volume", "image", "network"} and args[:2] != ["volume", "create"]
                            for args in self.docker.calls))

    def test_source_inspection_preserves_identity_without_minting_credentials(self):
        source = cutover.inspect_source(self.project, self.env_dir, self.keys)
        self.assertEqual(source["image"], FACTORY_IMAGE)
        self.assertEqual(source["images"], {FACTORY_IMAGE, POSTGRES_IMAGE, HATCHET_IMAGE})
        self.assertEqual(source["values"]["HATCHET_DEFAULT_TENANT_ID"], TENANT)
        self.assertEqual(source["envs"]["hatchet-client.env"], {"HATCHET_CLIENT_TOKEN": TOKEN})
        self.assertEqual(source["values"]["HATCHET_POSTGRES_PASSWORD"], "db-password")
        self.assertEqual(source["envs"]["factory-linear-token.env"], self.envs["factory-linear-token.env"])
        self.assertEqual(source["keys"], {name: (self.keys / name).read_text() for name in cutover.KEYS})

    def test_reordered_inspection_inventory_does_not_signal_configuration_drift(self):
        inspections = 0

        def reordered(args, env=None):
            nonlocal inspections
            result = self.docker(args, env)
            if args[:2] == ["container", "inspect"]:
                inspections += 1
                rows = json.loads(result)
                if inspections % 2:
                    rows.reverse()
                    for row in rows:
                        row["Mounts"].reverse()
                result = json.dumps(rows)
            return result

        with patch.object(state, "_docker", reordered):
            result = self.prepare(apply=True)
        self.assertFalse(result["activation_approved"])
        self.assertEqual(len(self.docker.archives), 14)
        self.assertGreater(inspections, 2)

    def test_source_refuses_missing_duplicate_services_wrong_images_and_mounts(self):
        originals = copy.deepcopy(self.docker.rows)
        mutations = (
            lambda: self.docker.rows.pop(),
            lambda: self.docker.rows.append(copy.deepcopy(self.docker.rows[0])),
            lambda: self.service("bridge").update(Image="sha256:" + "d" * 64),
            lambda: self.service("bridge")["Mounts"][0].update(Name="foreign-data"),
            lambda: self.service("bridge")["Mounts"][0].update(Type="bind"),
            lambda: self.service("hatchet-lite")["Mounts"][-1].update(RW=True),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.docker.rows = copy.deepcopy(originals)
                mutate()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.directory.exists())
        self.docker.rows = originals

    def test_foreign_volume_labels_and_symlinked_key_are_rejected(self):
        volume = self.docker.volumes[f"{self.project}-bridge-data"]
        volume["Labels"]["com.docker.compose.project"] = "another-project"
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.prepare()
        volume["Labels"]["com.docker.compose.project"] = self.project
        key = self.keys / "master.key"
        key.unlink()
        key.symlink_to(self.keys / "private_ec256.key")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare()
        self.assertFalse(self.snapshot.exists())

    def test_unlabelled_legacy_volumes_require_explicit_review_and_matching_mounts(self):
        volume = self.docker.volumes[f"{self.project}-bridge-data"]
        volume["Labels"] = None
        with self.assertRaisesRegex(ValueError, "lack labels"):
            self.prepare()
        result = cutover.inspect_source(self.project, self.env_dir, self.keys, allow_unlabelled=True)
        self.assertEqual(result["unlabelled_volumes"], [f"{self.project}-bridge-data"])
        volume["Labels"] = {"com.docker.compose.project": "another-project"}
        with self.assertRaisesRegex(ValueError, "ownership"):
            cutover.inspect_source(self.project, self.env_dir, self.keys, allow_unlabelled=True)
        volume["Labels"] = None
        self.service("bridge")["Mounts"][0]["Name"] = "unrelated-data"
        with self.assertRaisesRegex(ValueError, "state mount"):
            cutover.inspect_source(self.project, self.env_dir, self.keys, allow_unlabelled=True)

    def test_refuses_runtime_credential_drift_and_worker_provider_credentials(self):
        executor = self.service("bridge-executor-worker")
        original = executor["Config"]["Env"][:]
        for values in (original + ["OPENAI_API_KEY=forbidden"],
                       [value for value in original if not value.startswith("HATCHET_CLIENT_TOKEN=")]):
            with self.subTest(values=len(values)):
                executor["Config"]["Env"] = values
                with self.assertRaises(ValueError):
                    self.prepare()
        executor["Config"]["Env"] = original
        path = self.env_dir / "hatchet-bridge.env"
        path.write_text("HATCHET_HERMES_BRIDGE_TOKEN=changed-secret\n")
        with self.assertRaisesRegex(ValueError, "configuration drift"):
            self.prepare()

    def test_only_compose_owned_values_are_normalized(self):
        path = self.env_dir / "hatchet-workers.env"
        path.write_text(path.read_text() + "HATCHET_HERMES_BRIDGE_URL=http://legacy-host:19877\n")
        for consumer in cutover.ENV_CONSUMERS["hatchet-workers.env"]:
            self.overrides[consumer]["HATCHET_HERMES_BRIDGE_URL"] = "http://bridge:19877"
            self.service(consumer)["Config"]["Env"].append("HATCHET_HERMES_BRIDGE_URL=http://bridge:19877")
        source = cutover.inspect_source(self.project, self.env_dir, self.keys)
        self.assertEqual(source["envs"]["hatchet-workers.env"]["HATCHET_HERMES_BRIDGE_URL"], "http://bridge:19877")
        self.assertEqual(source["envs"]["hatchet-workers.env"]["HATCHET_HERMES_BRIDGE_TOKEN"], "bridge-secret")
        path.write_text(path.read_text().replace("bridge-secret", "different-secret"))
        with self.assertRaisesRegex(ValueError, "configuration drift"):
            self.prepare()

    def test_apply_refuses_running_source_and_foreign_volume_writer_before_snapshot(self):
        for status in ("running", "paused", "restarting"):
            with self.subTest(status=status):
                self.service("bridge")["State"]["Status"] = status
                with self.assertRaises(ValueError):
                    self.prepare(apply=True)
                self.assertFalse(self.snapshot.exists())
        self.service("bridge")["State"]["Status"] = "exited"
        self.docker.foreign_writer = True
        with self.assertRaisesRegex(ValueError, "running container"):
            self.prepare(apply=True)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.docker.archives)

    def test_destination_collisions_never_copy_or_overwrite(self):
        for suffix in state.PROJECT_VOLUMES:
            name = "factory-target-" + suffix
            self.docker.volumes[name] = {"Name": name, "Labels": {}, "files": {}}
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "already has volumes"):
                self.prepare(apply=True)
            del self.docker.volumes[name]
        self.docker.destination_container = True
        with self.assertRaisesRegex(ValueError, "already has containers"):
            self.prepare(apply=True)
        self.docker.destination_container = False
        self.docker.networks = ["factory-target"]
        with self.assertRaisesRegex(ValueError, "network already exists"):
            self.prepare(apply=True)
        self.docker.networks = []
        self.directory.mkdir()
        with self.assertRaises(ValueError):
            self.prepare(apply=True)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.docker.archives)

    def test_source_restart_during_archive_retains_partial_snapshot_without_destination(self):
        original_tar = self.docker.tar

        def restarted(*args, **kwargs):
            original_tar(*args, **kwargs)
            self.service("bridge")["State"]["Status"] = "running"

        with patch.object(state, "_tar", side_effect=restarted):
            with self.assertRaisesRegex(ValueError, "stopped"):
                self.prepare(apply=True)
        self.assertTrue(self.snapshot.exists())
        self.assertFalse(self.directory.exists())
        self.assertEqual(len(self.docker.archives), 1)

    def test_source_configuration_changed_during_archive_is_rejected(self):
        original_tar = self.docker.tar

        def changed(*args, **kwargs):
            original_tar(*args, **kwargs)
            (self.keys / "master.key").write_text("changed-key-during-backup\n")

        with patch.object(state, "_tar", side_effect=changed):
            with self.assertRaises(ValueError):
                self.prepare(apply=True)
        self.assertFalse(self.directory.exists())

    def test_seven_volume_round_trip_preserves_source_and_leaves_destination_stopped(self):
        original = copy.deepcopy(self.docker.volumes)
        result = self.prepare(apply=True)
        metadata, values = bootstrap.validate_directory(self.directory)
        self.assertEqual(values["FACTORY_IMAGE"], FACTORY_IMAGE)
        self.assertEqual(values["FACTORY_POSTGRES_IMAGE"], POSTGRES_IMAGE)
        self.assertEqual(values["FACTORY_HATCHET_IMAGE"], HATCHET_IMAGE)
        self.assertEqual(values["HATCHET_DEFAULT_TENANT_ID"], TENANT)
        self.assertEqual(values["HATCHET_POSTGRES_PASSWORD"], "db-password")
        self.assertFalse(result["activation_approved"])
        self.assertEqual(metadata["project"], "factory-target")
        self.assertEqual(metadata["profile"], "same-host-production")
        for filename, contents in self.envs.items():
            self.assertEqual(bootstrap.read_env(self.directory / "env" / filename), contents)
        self.assertEqual(bootstrap.read_env(self.directory / "env/hatchet-client.env"), {"HATCHET_CLIENT_TOKEN": TOKEN})
        for name in cutover.KEYS:
            self.assertEqual((self.directory / "keys/hatchet" / name).read_bytes(), (self.keys / name).read_bytes())
        for suffix in state.VOLUMES:
            source_name, destination_name = f"{self.project}-{suffix}", f"factory-target-{suffix}"
            self.assertEqual(self.docker.volumes[source_name], original[source_name])
            self.assertEqual(self.docker.volumes[destination_name]["files"], original[source_name]["files"])
            self.assertEqual(self.docker.volumes[destination_name]["Labels"][state.IDENTITY_LABEL], metadata["installation_id"])
        self.assertEqual(len(self.docker.archives), 14)
        self.assertTrue(all(image == FACTORY_IMAGE for image, _, _ in self.docker.archives))
        self.assertFalse(any(args[0] in {"start", "stop", "restart", "compose", "pull"} for args in self.docker.calls))


class LegacyEnvTests(unittest.TestCase):
    def test_literals_are_preserved_and_ambiguous_syntax_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.env"
            bootstrap._create_private(path, "# comment\nEMPTY=\nRAW=two words=a\nSINGLE='$literal#value'\nDOUBLE=\"two words\"\n")
            self.assertEqual(cutover.legacy_env(path), {"EMPTY": "", "RAW": "two words=a", "SINGLE": "$literal#value", "DOUBLE": "two words"})
            invalid = ("A=one\nA=two\n", "export A=one\n", "A=$VALUE\n", 'A="${VALUE}"\n',
                       "A=one # comment\n", "A='unterminated\n", "A=foo\\bar\n", "A='first\nsecond'\n",
                       "A='quoted' trailing\n", "A=one\x00two\n")
            for contents in invalid:
                with self.subTest(contents=repr(contents)):
                    path.write_text(contents)
                    with self.assertRaises(ValueError):
                        cutover.legacy_env(path)


class LedgerTests(unittest.TestCase):
    def test_verification_in_flight_and_missing_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, archive = root / "bridge.db", root / "bridge.tar"
            conn = ledger(database)
            conn.execute("INSERT INTO verification_leases VALUES (NULL)")
            conn.commit()
            write_archive(archive, {"bridge.db": database.read_bytes()})
            with self.assertRaisesRegex(ValueError, "active/unknown"):
                cutover.ledger_summary(archive)
            conn.execute("DROP TABLE verification_leases")
            conn.commit()
            write_archive(archive, {"bridge.db": database.read_bytes()})
            with self.assertRaisesRegex(ValueError, "unsupported or unreadable"):
                cutover.ledger_summary(archive)
            conn.close()

    def test_observed_absorbing_lease_is_safe_but_unknown_or_active_work_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "bridge.db"
            conn = ledger(database)
            archive = root / "bridge.tar"
            write_archive(archive, {"bridge.db": database.read_bytes()})
            self.assertEqual(cutover.ledger_summary(archive)["attempts"], {"verified_ready": 1})
            for table, column, value in (("factory_attempts", "status", "effect_outcome_unknown"),
                                          ("factory_attempts", "status", "future-unknown-state"),
                                          ("bridge_jobs", "state", "running")):
                with self.subTest(value=value):
                    conn.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (value,))
                    conn.commit()
                    write_archive(archive, {"bridge.db": database.read_bytes()})
                    with self.assertRaisesRegex(ValueError, "active/unknown"):
                        cutover.ledger_summary(archive)
                    conn.execute(f"DELETE FROM {table} WHERE {column}=?", (value,))
                    conn.commit()
            conn.execute("INSERT INTO factory_execution_leases VALUES (NULL, NULL)")
            conn.commit()
            write_archive(archive, {"bridge.db": database.read_bytes()})
            with self.assertRaisesRegex(ValueError, "active/unknown"):
                cutover.ledger_summary(archive)
            conn.close()

    def test_wal_only_executing_work_cannot_disappear_during_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "bridge.db"
            conn = ledger(database)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("INSERT INTO factory_attempts VALUES ('effect_authorized')")
                conn.commit()
                # Establish that the dangerous row is genuinely absent from the main file.
                with sqlite3.connect(database.as_uri() + "?immutable=1", uri=True) as stale:
                    self.assertEqual(stale.execute("SELECT COUNT(*) FROM factory_attempts WHERE status='effect_authorized'").fetchone()[0], 0)
                archive = root / "bridge.tar"
                files = {path.name: path.read_bytes() for path in root.glob("bridge.db*")}
                self.assertIn("bridge.db-wal", files)
                write_archive(archive, files)
                with self.assertRaisesRegex(ValueError, "active/unknown"):
                    cutover.ledger_summary(archive)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
