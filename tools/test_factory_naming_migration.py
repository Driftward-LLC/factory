"""Synthetic naming migration refusals and immutable cold-state rehearsal."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import check_factory_naming_migration as fixture

naming = fixture.naming


class NamingMappingTests(unittest.TestCase):
    def setUp(self):
        self.config = fixture.configuration()

    def test_reviewed_mapping_is_exact_and_source_labels_must_match(self):
        self.assertEqual(naming.validate_mapping(self.config, fixture.INFO), self.config)
        for key, value in (("format_version", 2), ("source_identity_label", "io.foreign.installation"),
                           ("source_tenant_label", "io.foreign.tenant"), ("gateway_policy", "bound"),
                           ("source_environment_prefix", "FACTORY_"), ("agent_trace_seed", "missing-placeholder"),
                           ("agent_trace_seed", "synthetic:{attempt_id}:{attempt_id}"),
                           ("agent_trace_seed", "synthetic:{unknown}:{attempt_id}")):
            with self.subTest(key=key, value=value):
                bad = {**self.config, key: value}
                with self.assertRaises(naming.pg.MigrationRefusal):
                    naming.validate_mapping(bad, fixture.INFO)
        with self.assertRaises(naming.pg.MigrationRefusal):
            naming.validate_mapping({**self.config, "unreviewed": "ignored"}, fixture.INFO)

    def test_duplicate_environment_targets_and_non_path_replacements_are_refused(self):
        for change in (
            {"environment_keys": {"SYNTHETIC_ONE": "FACTORY_HOME", "SYNTHETIC_TWO": "FACTORY_HOME"}},
            {"environment_keys": {"OTHER_HOME": "FACTORY_HOME"}},
            {"environment_keys": {"SYNTHETIC_HOME": "HATCHET_HOME"}},
            {"environment_values": {"synthetic": "factory"}},
            {"environment_values": {"/synthetic": "/factory\nsecret"}},
        ):
            with self.subTest(change=change), self.assertRaises(naming.pg.MigrationRefusal):
                naming.validate_mapping({**self.config, **change}, fixture.INFO)

    def test_environment_mapping_is_exact_and_execution_is_disabled(self):
        values = {"SYNTHETIC_HOME": "/synthetic/executor", "SYNTHETIC_HERMES_CREATE_DRAFT_PR_ENABLED": "1",
                  "HATCHET_CLIENT_TOKEN": "synthetic-test-token", "FACTORY_PRESERVED": "/synthetic/executor/child"}
        before = copy.deepcopy(values)
        self.assertEqual(naming.environment(values, self.config), {
            "FACTORY_HOME": "/factory/executor", "FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED": "0",
            "HATCHET_CLIENT_TOKEN": "synthetic-test-token", "FACTORY_PRESERVED": "/synthetic/executor/child"})
        self.assertEqual(values, before)
        self.assertEqual(naming.environment({"FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED": "true"}, self.config),
                         {"FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED": "0"})

    def test_unmapped_source_environment_and_existing_target_collisions_are_refused(self):
        for values in ({"SYNTHETIC_UNKNOWN": "private"},
                       {"SYNTHETIC_HOME": "/synthetic/executor", "FACTORY_HOME": "/factory/executor"},
                       {"FACTORY_HOME": "/factory/executor", "SYNTHETIC_HOME": "/synthetic/executor"}):
            with self.subTest(values=values), self.assertRaises(naming.pg.MigrationRefusal):
                naming.environment(values, self.config)


    def test_reviewer_gate_is_disabled_and_retired_selectors_are_removed(self):
        values = {"FACTORY_REVIEWER_ENABLED": "1", "FACTORY_HERMES_MODEL": "old-model",
                  "FACTORY_HERMES_PROVIDER": "old-provider"}
        self.assertEqual(naming.environment(values, self.config), {"FACTORY_REVIEWER_ENABLED": "0"})

    def test_disabled_execution_configuration_keeps_linear_identity(self):
        supplied = {"organization.json": json.dumps({"linear": {"team_key": "OPS"}, "github": {"enabled": False}}),
                    "routing-registry.json": json.dumps({"routes": [{"execution_enabled": False}]}),
                    "independent-review-policy.json": json.dumps({"enabled": False, "automatic_repairs_allowed": False})}
        naming.disabled_execution_configuration(supplied)
        for filename, content in (
            ("organization.json", {"github": {"enabled": True}}),
            ("routing-registry.json", {"routes": [{"execution_enabled": True}]}),
            ("independent-review-policy.json", {"enabled": True, "automatic_repairs_allowed": False}),
            ("independent-review-policy.json", {"enabled": False, "automatic_repairs_allowed": True}),
        ):
            with self.subTest(filename=filename), self.assertRaises(naming.pg.MigrationRefusal):
                naming.disabled_execution_configuration({**supplied, filename: json.dumps(content)})

    def test_postgres_gate_checks_all_known_dispatch_stores_and_reports_only_counts(self):
        with patch.object(naming.pg, "psql", return_value="0") as query:
            self.assertEqual(set(naming.postgres_quiescent("synthetic")), {row[0] for row in naming.POSTGRES_PENDING})
            self.assertEqual(query.call_count, len(naming.POSTGRES_PENDING))
            self.assertFalse(any('"v1_step_slot_request"' in call.args[1] for call in query.call_args_list))
        def pending(container, sql):
            return "1" if '"WorkflowTriggerCronRef"' in sql else "0"
        with patch.object(naming.pg, "psql", side_effect=pending), self.assertRaisesRegex(naming.pg.MigrationRefusal, 'WorkflowTriggerCronRef.*1'):
            naming.postgres_quiescent("synthetic")


class NamingStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_journal_archive_retains_unknown_intents_and_rejects_unsafe_input(self):
        archive = self.root / "journal.tar"
        with tarfile.open(archive, "x") as handle:
            for name in ("./one/intent.json", "./one/result.json", "./two/intent.json", "./two/cancel"):
                body = b'{"historical":true}'
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(body), 0o600
                handle.addfile(member, io.BytesIO(body))
        archive.chmod(0o600)
        before = archive.read_bytes()
        evidence = naming.journal_evidence(archive)
        self.assertEqual((evidence["intents"], evidence["results"], evidence["intents_without_results"]), (2, 1, 1))
        self.assertEqual(archive.read_bytes(), before)
        archive.chmod(0o644)
        with self.assertRaises(naming.pg.MigrationRefusal):
            naming.journal_evidence(archive)
        archive.chmod(0o600)
        with tarfile.open(archive, "w") as handle:
            member = tarfile.TarInfo("../escape")
            handle.addfile(member)
        with self.assertRaises(ValueError):
            naming.journal_evidence(archive)

    def database(self, volume):
        path = self.root / (volume + ".db")
        fixture.seed(path, volume)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_each_supported_store_is_quiescent_without_modification(self):
        for volume in naming.SQLITE:
            conn = self.database(volume)
            before = fixture.rows(conn)
            self.assertTrue(naming.quiescent(conn, volume))
            self.assertEqual(fixture.rows(conn), before)

    def test_pending_work_and_active_leases_are_refused_in_every_queue(self):
        changes = {
            "bridge-data": (
                "UPDATE factory_attempts SET status='running'",
                "UPDATE factory_attempts SET status=NULL",
                "UPDATE factory_execution_leases SET released_at=NULL",
                "UPDATE bridge_jobs SET state='effect_outcome_unknown'",
                "UPDATE verification_leases SET closed_at=NULL",
                "UPDATE factory_attempt_outbox SET acknowledged_at=NULL",
                "UPDATE factory_inspection_outbox SET acknowledged_at=NULL",
                "UPDATE factory_inspection_requests SET state='pending'",
                "UPDATE factory_independent_reviews SET state='running'",
            ),
            "ingress-data": (
                "UPDATE ingress_events SET hatchet_published=0",
                "UPDATE ingress_events SET hatchet_published=NULL",
                "UPDATE ingress_events SET publish_lease_id='active'",
            ),
            "lifecycle-data": (
                "UPDATE lifecycle_outbox SET delivery_state='pending'",
                "UPDATE lifecycle_outbox SET lease_owner='active'",
                "UPDATE lifecycle_outbox SET delivery_state=NULL",
            ),
        }
        for volume, statements in changes.items():
            conn = self.database(volume)
            for statement in statements:
                with self.subTest(volume=volume, statement=statement):
                    conn.execute(statement)
                    before = fixture.rows(conn)
                    with self.assertRaises(naming.pg.MigrationRefusal):
                        naming.quiescent(conn, volume)
                    self.assertEqual(fixture.rows(conn), before)
                    conn.rollback()

    def test_unsupported_missing_or_ambiguous_schema_and_prior_marker_are_refused(self):
        for volume, table in (("bridge-data", "bridge_schema_meta"), ("ingress-data", "ingress_schema_meta")):
            conn = self.database(volume)
            for statement in (f"UPDATE {table} SET schema_version=999", f"DELETE FROM {table}",
                              f"INSERT INTO {table} SELECT * FROM {table}"):
                with self.subTest(volume=volume, statement=statement):
                    conn.execute(statement)
                    with self.assertRaises(naming.pg.MigrationRefusal):
                        naming.quiescent(conn, volume)
                    conn.rollback()
        conn = self.database("lifecycle-data")
        conn.execute("DROP TABLE lifecycle_outbox")
        with self.assertRaises(naming.pg.MigrationRefusal):
            naming.quiescent(conn, "lifecycle-data")
        for volume in naming.SQLITE:
            path = self.root / (volume + ".db")
            path.unlink()
            conn = self.database(volume)
            conn.execute("CREATE TABLE factory_naming_generation (generation TEXT)")
            with self.subTest(volume=volume), self.assertRaises(naming.pg.MigrationRefusal):
                naming.quiescent(conn, volume)

    def test_invalid_foreign_keys_refuse_before_a_clone_is_written(self):
        conn = self.database("bridge-data")
        conn.executescript("CREATE TABLE parent (id INTEGER PRIMARY KEY); CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES (1);")
        with self.assertRaisesRegex(naming.pg.MigrationRefusal, "integrity"):
            naming.quiescent(conn, "bridge-data")

    def test_disk_backup_clone_and_byte_identical_rollback_without_docker(self):
        with patch.object(naming.state, "_docker", side_effect=AssertionError("Docker must not run")):
            report = fixture.rehearse(self.root)
        self.assertEqual(report["gateway_journal"], "UNBOUND")
        self.assertEqual(report["services_started"], 0)
        self.assertTrue(report["source_unchanged"] and report["snapshot_unchanged"] and report["rollback_byte_identical"])
        self.assertEqual(set(report["stores"]), set(naming.SQLITE))
        self.assertEqual(json.loads((self.root / "report.json").read_text()), report)
        for volume in naming.SQLITE:
            with self.subTest(volume=volume), self.assertRaises(naming.pg.MigrationRefusal):
                naming.sqlite_archive(self.root / "clone/volumes" / (volume + ".tar"), volume)

    def test_wal_rows_survive_checkpoint_in_new_archive_only(self):
        conn = self.database("bridge-data")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO immutable_history VALUES ('wal-event','{}')")
        conn.commit()
        source, output = self.root / "wal.tar", self.root / "clone.tar"
        with tarfile.open(source, "x") as handle:
            for suffix in ("", "-wal", "-shm"):
                handle.add(self.root / ("bridge-data.db" + suffix), arcname="./bridge.db" + suffix)
        before = source.read_bytes()
        naming.sqlite_archive(source, "bridge-data", output=output, config=fixture.configuration())
        self.assertEqual(source.read_bytes(), before)
        result, _ = fixture.archive_rows(output, "bridge.db")
        self.assertIn(("wal-event", "{}"), result["immutable_history"])
        with tarfile.open(output) as handle:
            self.assertEqual(handle.getnames(), ["./bridge.db"])

    def test_unsafe_archive_paths_and_pending_source_never_create_output(self):
        source, output = self.root / "unsafe.tar", self.root / "clone.tar"
        with tarfile.open(source, "x") as handle:
            entry = tarfile.TarInfo("../bridge.db")
            entry.size = 6
            handle.addfile(entry, io.BytesIO(b"unsafe"))
        with self.assertRaises(ValueError):
            naming.sqlite_archive(source, "bridge-data", output=output, config=fixture.configuration())
        self.assertFalse(output.exists())
        conn = self.database("bridge-data")
        conn.execute("UPDATE factory_attempt_outbox SET acknowledged_at=NULL")
        conn.commit()
        source.unlink()
        with tarfile.open(source, "x") as handle:
            handle.add(self.root / "bridge-data.db", arcname="./bridge.db")
        before = source.read_bytes()
        with self.assertRaises(naming.pg.MigrationRefusal):
            naming.sqlite_archive(source, "bridge-data", output=output, config=fixture.configuration())
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
