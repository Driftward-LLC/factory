import json
import os
import fcntl
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import cli
from hatchet_hermes_bridge import runtime
from hatchet_hermes_bridge import store
from hatchet_hermes_bridge.legacy_artifacts import deterministic_branch, legacy_active_key
from hatchet_hermes_bridge.server import serve
from hatchet_hermes_bridge.store import (
    BridgeConflict,
    BridgeValidationError,
    classify_factory_import_conflicts,
    connect_readonly,
    connect_runtime,
    init_db,
)
from test_support import connect_migrate


ROOT = Path(__file__).resolve().parents[3]
CAPYKIT = "Driftward-LLC/capykit"
NOW = "2026-08-17T00:00:00Z"


def _snapshot(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0],
        tuple(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")),
    )


class RuntimeConnectionTests(unittest.TestCase):
    def test_every_application_runtime_writer_has_an_inventory_classification(self):
        callers = {
            path.stem for path in (ROOT / "hatchet" / "bridge" / "hatchet_hermes_bridge").glob("*.py")
            if path.name != "store.py" and "connect_runtime(" in path.read_text()
        }
        self.assertEqual(callers, {name.rsplit(".", 1)[-1] for name in runtime.WRITABLE_CALLER_INVENTORY})
        for classification in runtime.WRITABLE_CALLER_INVENTORY.values():
            self.assertTrue(classification == "operator-cli" or classification in runtime.WRITER_UNITS)

    def _capability_fd(self, lock: Path) -> tuple[int, int | None]:
        """Install a genuine inherited descriptor 3 and return its prior fd."""
        held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            prior = os.dup(3)
        except OSError:
            prior = None
        os.dup2(held, runtime.MAINTENANCE_FD)
        return held, prior

    def _restore_fd3(self, held: int, prior: int | None) -> None:
        if prior is None:
            os.close(runtime.MAINTENANCE_FD)
        else:
            os.dup2(prior, runtime.MAINTENANCE_FD); os.close(prior)
        fcntl.flock(held, fcntl.LOCK_UN); os.close(held)

    def test_maintenance_path_and_bypass_environment_are_ignored(self):
        self.assertEqual(runtime._FIXED_MAINTENANCE_LOCK, Path("/run/driftward-hatchet-bridge/maintenance.lock"))
        with patch.dict(os.environ, {"HATCHET_BRIDGE_MAINTENANCE_LOCK": "/tmp/attacker", "HATCHET_BRIDGE_MAINTENANCE_BYPASS": "yes"}):
            self.assertEqual(runtime._FIXED_MAINTENANCE_LOCK, Path("/run/driftward-hatchet-bridge/maintenance.lock"))

    def test_migration_capability_rejects_missing_wrong_or_unlocked_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "maintenance.lock"; wrong = Path(directory) / "wrong.lock"
            lock.touch()
            wrong_fd = os.open(wrong, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                prior = os.dup(3)
            except OSError:
                prior = None
            try:
                os.dup2(wrong_fd, runtime.MAINTENANCE_FD)
                with patch("hatchet_hermes_bridge.runtime._lock_manager", runtime._LockManager(lock)):
                    with self.assertRaisesRegex(RuntimeError, "inode mismatch"):
                        runtime.migration_capability()
                os.close(wrong_fd)
                unlocked = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
                os.dup2(unlocked, runtime.MAINTENANCE_FD)
                with patch("hatchet_hermes_bridge.runtime._lock_manager", runtime._LockManager(lock)):
                    # A normal, unheld fd must not be a migration capability.
                    # Hold a conflicting shared lock from another open description.
                    peer = os.open(lock, os.O_RDWR)
                    fcntl.flock(peer, fcntl.LOCK_SH)
                    with self.assertRaises(OSError): runtime.migration_capability()
                    fcntl.flock(peer, fcntl.LOCK_UN); os.close(peer)
            finally:
                if prior is None: os.close(runtime.MAINTENANCE_FD)
                else: os.dup2(prior, runtime.MAINTENANCE_FD); os.close(prior)

    def test_real_inherited_capability_and_shared_holder_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "maintenance.lock"
            held, prior = self._capability_fd(lock)
            try:
                with patch("hatchet_hermes_bridge.runtime._lock_manager", runtime._LockManager(lock)):
                    capability = runtime.migration_capability()
                    runtime.release(capability)
                    # A runtime holder is a distinct open-file description and
                    # prevents a new installer exclusive acquisition.
                    runtime_fd = runtime.acquire_shared()
                    contender = os.open(lock, os.O_RDWR)
                    with self.assertRaises(OSError): fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.close(contender); runtime.release(runtime_fd)
            finally:
                self._restore_fd3(held, prior)

    def test_only_init_command_selects_the_migration_connection(self):
        source = (ROOT / "hatchet" / "bridge" / "hatchet_hermes_bridge" / "cli.py").read_text()
        self.assertIn('connect_migrate(args.db) if args.command == "init" else connect_runtime(args.db)', source)
        for path in (ROOT / "hatchet" / "bridge" / "hatchet_hermes_bridge" / "server.py", ROOT / "hatchet" / "bridge" / "hatchet_hermes_bridge" / "operator.py"):
            self.assertNotIn("connect_migrate(", path.read_text())
    def test_runtime_requires_explicit_migration_and_never_backfills(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            with self.assertRaisesRegex(BridgeValidationError, "migration is required"):
                connect_runtime(path)

    def test_maintenance_lock_refuses_writable_runtime_but_not_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            migrated = connect_migrate(path); migrated.close()
            lock = Path(directory) / "maintenance.lock"; lock.mkdir()
            lock.rmdir(); lock.touch(); held=os.open(lock, os.O_RDWR); fcntl.flock(held, fcntl.LOCK_EX)
            with patch("hatchet_hermes_bridge.runtime._lock_manager", runtime._LockManager(lock)):
                with self.assertRaisesRegex(BridgeValidationError, "maintenance"):
                    connect_runtime(path)
                readonly=connect_readonly(path); readonly.close()
            fcntl.flock(held, fcntl.LOCK_UN); os.close(held)
            migrated = connect_migrate(path)
            before = _snapshot(migrated)
            migrated.close()
            runtime_conn = connect_runtime(path)
            self.assertEqual(_snapshot(runtime_conn), before)
            runtime_conn.close()
            self.assertEqual(cli.main(["--db", path, "reconcile", "--dry-run"]), 0)
            check = connect_runtime(path)
            self.assertEqual(_snapshot(check), before)
            check.close()
            migrated = connect_migrate(path)
            migrated.execute("UPDATE bridge_schema_meta SET schema_version=0 WHERE singleton=1")
            migrated.commit()
            migrated.close()
            with self.assertRaisesRegex(BridgeValidationError, "migration is required"):
                connect_runtime(path)

    def test_serve_startup_uses_runtime_connection_without_schema_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            conn = connect_migrate(path)
            before = _snapshot(conn)
            conn.close()

            class FakeServer:
                def __init__(self, *_):
                    self.conn = None
                def serve_forever(self):
                    return None
                def server_close(self):
                    return None

            with patch("hatchet_hermes_bridge.server.HTTPServer", FakeServer):
                serve(path, "127.0.0.1", 0)
            runtime = connect_runtime(path)
            self.assertEqual(_snapshot(runtime), before)
            runtime.close()

    def test_migration_failpoints_leave_an_unmigrated_database_logically_unchanged(self):
        for stage in ("after-ddl", "during-backfill", "during-resolution-insertion", "during-attestation-insertion", "before-version-update", "before-commit"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "bridge.db")
                seed = sqlite3.connect(path)
                seed.execute("CREATE TABLE preserved (value TEXT)")
                seed.execute("INSERT INTO preserved VALUES ('before')")
                seed.commit(); seed.close()
                with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": stage}):
                    with self.assertRaisesRegex(Exception, "injected migration failure"):
                        connect_migrate(path)
                reopened = sqlite3.connect(path)
                self.assertEqual(reopened.execute("SELECT value FROM preserved").fetchall(), [("before",)])
                self.assertEqual(reopened.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(), [("preserved",)])
                reopened.close()

    def test_unknown_migration_failpoint_fails_before_schema_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            seed = sqlite3.connect(path)
            seed.execute("CREATE TABLE preserved (value TEXT)")
            seed.execute("INSERT INTO preserved VALUES ('before')")
            seed.commit(); seed.close()
            with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "not-a-stage"}):
                with self.assertRaisesRegex(Exception, "unknown migration failpoint"):
                    connect_migrate(path)
            reopened = sqlite3.connect(path)
            self.assertEqual(reopened.execute("SELECT value FROM preserved").fetchall(), [("before",)])
            self.assertEqual(reopened.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(), [("preserved",)])
            reopened.close()

    def test_unknown_migration_failpoint_does_not_open_or_create_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "new" / "bridge.db")
            with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "not-a-stage"}):
                with self.assertRaisesRegex(Exception, "unknown migration failpoint"):
                    connect_migrate(path)
            self.assertFalse(Path(path).exists())
            self.assertFalse(Path(path).parent.exists())

    def test_migration_ignores_runtime_inspection_conflicts_when_rechecking_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            conn = connect_migrate(path)
            key = legacy_active_key("ENG-9", "real")
            payload = {"linear_issue": "ENG-9", "work_item_id": "ENG-9", "dry_run": False, "repo": CAPYKIT}
            conn.execute(
                "INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("failed-current-factory", key, "failed", "create_draft_pr", json.dumps(payload, sort_keys=True), "hash", deterministic_branch(key), None, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO factory_import_conflicts (conflict_key,work_item_key,job_id,reason,created_at) VALUES (?,?,?,?,?)",
                ("runtime-inspection:inspection_repository_not_canonical", "ENG-9", "runtime-inspection", "inspection_repository_not_canonical", NOW),
            )
            conn.commit()
            init_db(conn)
            self.assertIsNone(conn.execute("SELECT 1 FROM factory_import_conflicts WHERE conflict_key=?", ("failed-current-factory:legacy_artifact_reference_conflict",)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0], 1)
            conn.close()


class ConflictResolutionTests(unittest.TestCase):
    def _historical_conflict(self, conn, *, reason="legacy_artifact_reference_conflict", number=8):
        conn.execute("UPDATE bridge_schema_meta SET schema_version=3")
        key = "hatchet:work-item:linear:ENG-8:create_draft_pr:real:active"
        key = f"hatchet:work-item:linear:ENG-{number}:create_draft_pr:real:active"
        branch = deterministic_branch(key)
        job_id = f"legacy-eng{number}"
        issue = f"ENG-{number}"
        row = (job_id, key, "completed", "create_draft_pr", json.dumps({"linear_issue": issue, "repo": CAPYKIT, "dry_run": False}), "fixture", branch, f"https://github.com/Driftward-LLC/capykit/pull/{number}", NOW, NOW)
        conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        conn.execute("INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES (?,?,?,?,?,?)", (f"effect-{number}", job_id, "transition:completed", "completed", json.dumps({"effect": {"pr_url": f"https://github.com/Driftward-LLC/capykit/pull/{number}", "head_branch": branch}}), NOW))
        attempt_id = "legacy-" + job_id
        conn.execute("INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, issue, "create_draft_pr", "real", 1, "legacy:" + job_id, key, branch, "legacy_completed", job_id, "legacy-import", NOW, NOW))
        conn.execute("INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)", (f"imported-{number}", attempt_id, "legacy_imported", json.dumps({"job_id": job_id}), NOW))
        conn.execute("INSERT INTO factory_import_conflicts (conflict_key,work_item_key,job_id,reason,created_at) VALUES (?,?,?,?,?)", (f"{job_id}:{reason}", issue, job_id, reason, NOW))
        conn.commit()

    def _v3_38(self, conn):
        for number in range(8, 46):
            self._historical_conflict(conn, number=number)
        init_db(conn)
        old_rows = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("factory_import_conflicts", "factory_import_conflict_resolutions", "bridge_jobs", "bridge_events", "factory_attempts", "factory_attempt_events")
        }
        conn.execute("DROP TABLE factory_import_conflict_attestations")
        conn.execute("UPDATE bridge_schema_meta SET schema_version=3 WHERE singleton=1")
        conn.commit()
        return old_rows

    def test_resolution_is_append_only_idempotent_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            conn = connect_migrate(path)
            self._historical_conflict(conn)
            self.assertEqual(classify_factory_import_conflicts(conn), {"original": 1, "resolvable": 1, "unresolved": 0, "attested": 0, "attestation_required": 1})
            init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_resolutions").fetchone()[0], 1)
            init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_resolutions").fetchone()[0], 1)
            conn.execute("UPDATE bridge_jobs SET branch='codex/tampered' WHERE job_id='legacy-eng8'")
            conn.commit()
            self.assertEqual(classify_factory_import_conflicts(conn)["unresolved"], 1)
            conn.close()

    def test_resolution_replay_rejects_each_immutable_marker_field(self):
        immutable = {
            "resolution_id": "tampered", "job_id": "other-job", "evidence_digest": "0" * 64,
            "code_version": "other-version", "reason_code": "other-reason", "operator": "operator",
            "decision_json": "{}", "created_at": "not-a-time",
        }
        for field, value in immutable.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                conn = connect_migrate(str(Path(directory) / "bridge.db"))
                self._historical_conflict(conn)
                init_db(conn)
                conn.execute(f"UPDATE factory_import_conflict_resolutions SET {field}=?", (value,))
                conn.commit()
                self.assertEqual(classify_factory_import_conflicts(conn)["unresolved"], 1)
                conn.close()

    def test_unknown_or_conflicting_resolution_never_clears_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            conn = connect_migrate(path)
            self._historical_conflict(conn, reason="unknown_reason")
            self.assertEqual(classify_factory_import_conflicts(conn), {"original": 1, "resolvable": 0, "unresolved": 1, "attested": 0, "attestation_required": 0})
            conn.execute("UPDATE factory_import_conflicts SET reason='legacy_artifact_reference_conflict'")
            conn.execute("INSERT INTO factory_import_conflict_resolutions(conflict_key,resolution_id,job_id,evidence_digest,code_version,reason_code,operator,created_at) VALUES (?,?,?,?,?,?,?,?)", ("legacy-eng8:unknown_reason", "wrong", "legacy-eng8", "wrong", "test", "manual", "test", NOW))
            conn.commit()
            self.assertEqual(classify_factory_import_conflicts(conn)["unresolved"], 1)
            conn.close()

    def test_v3_38_row_migration_preserves_old_rows_and_attests_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_migrate(str(Path(directory) / "bridge.db"))
            old_rows = self._v3_38(conn)
            self.assertEqual(classify_factory_import_conflicts(conn), {"original": 38, "resolvable": 38, "unresolved": 0, "attested": 0, "attestation_required": 38})
            init_db(conn)
            self.assertEqual(conn.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 38)
            for table, rows in old_rows.items():
                self.assertEqual(conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(), rows)
            self.assertEqual(classify_factory_import_conflicts(conn), {"original": 38, "resolvable": 38, "unresolved": 0, "attested": 38, "attestation_required": 0})
            init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 38)
            conn.close()

    def test_semantic_identity_reuses_commit_only_release_and_changes_for_contract_or_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_migrate(str(Path(directory) / "bridge.db"))
            self._v3_38(conn)
            sha_one, sha_two = "1" * 40, "2" * 40
            with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": sha_one}):
                init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 38)
            with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": sha_two}):
                conn.execute("UPDATE bridge_schema_meta SET schema_version=3"); conn.commit()
                init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 38)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations WHERE producing_release_sha=?", (sha_one,)).fetchone()[0], 38)
            registry = json.loads((ROOT / "hatchet/workers/routing-registry.json").read_text())
            registry["routes"] = list(reversed(registry["routes"]))
            formatted = Path(directory) / "format-only.json"; formatted.write_text(json.dumps(registry, indent=4) + "\n")
            with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": sha_two, "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": str(formatted)}):
                conn.execute("UPDATE bridge_schema_meta SET schema_version=3"); conn.commit()
                init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 38)
            registry["routes"][0]["execution_enabled"] = not registry["routes"][0]["execution_enabled"]
            changed = Path(directory) / "semantic-change.json"; changed.write_text(json.dumps(registry))
            with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": sha_two, "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": str(changed)}):
                conn.execute("UPDATE bridge_schema_meta SET schema_version=3"); conn.commit()
                init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 76)
            changed_legacy = Path(store.__file__).with_name("legacy_artifacts.py").read_text().replace(
                'if mode == "real" and evidence.dry_run_sentinels:',
                'if evidence.dry_run_sentinels:',
                1,
            )
            changed_contract = store._resolver_contract_digest(source_overrides={"legacy_artifacts": changed_legacy})
            with patch.object(store, "_resolver_contract_digest", return_value=changed_contract):
                with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": sha_two, "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": str(changed)}):
                    conn.execute("UPDATE bridge_schema_meta SET schema_version=3"); conn.commit()
                    init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations").fetchone()[0], 114)
            conn.close()

    def test_resolver_implementation_contract_digest_binds_relevant_source_only(self):
        store_source = Path(store.__file__).read_text()
        legacy_source = Path(store.__file__).with_name("legacy_artifacts.py").read_text()
        digest = store._resolver_contract_digest()
        self.assertEqual(
            digest,
            store._resolver_contract_digest(source_overrides={
                "store": store_source + "\n# formatting-only release note\n",
                "legacy_artifacts": legacy_source + "\n\ndef unrelated_helper() -> None:\n    return None\n",
            }),
        )
        changed_legacy = legacy_source.replace(
            'if mode == "real" and evidence.dry_run_sentinels:',
            'if evidence.dry_run_sentinels:',
            1,
        )
        self.assertNotEqual(digest, store._resolver_contract_digest(source_overrides={"legacy_artifacts": changed_legacy}))
        changed_evidence = store_source.replace(
            'yield ("bridge", {"pr_url": source.get("pr_url"), "branch": source.get("branch")})',
            'yield ("bridge", {"pr_url": source.get("pull_url"), "branch": source.get("branch")})',
            1,
        )
        self.assertNotEqual(digest, store._resolver_contract_digest(source_overrides={"store": changed_evidence}))
        with patch.object(store, "RESOLVER_ATTESTATION_CONTRACT", {**store.RESOLVER_ATTESTATION_CONTRACT, "reason_code": "other"}):
            self.assertNotEqual(digest, store._resolver_contract_digest())
        with self.assertRaisesRegex(BridgeValidationError, "source is incomplete"):
            store._resolver_contract_digest(source_overrides={"legacy_artifacts": "def unrelated_helper():\n    return None\n"})

    def test_attestation_or_v3_tampering_aborts_the_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_migrate(str(Path(directory) / "bridge.db"))
            self._v3_38(conn)
            conn.execute("UPDATE factory_import_conflict_resolutions SET evidence_digest='0' || substr(evidence_digest,2) WHERE conflict_key=(SELECT conflict_key FROM factory_import_conflict_resolutions LIMIT 1)")
            conn.commit()
            with self.assertRaisesRegex(BridgeConflict, "immutable evidence"):
                init_db(conn)
            self.assertEqual(conn.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()[0], 3)
            self.assertNotIn("factory_import_conflict_attestations", {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            conn.close()

    def test_attestations_are_append_only_and_duplicate_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_migrate(str(Path(directory) / "bridge.db"))
            self._v3_38(conn); init_db(conn)
            row = conn.execute("SELECT * FROM factory_import_conflict_attestations LIMIT 1").fetchone()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("UPDATE factory_import_conflict_attestations SET evidence_digest='0' || substr(evidence_digest,2) WHERE conflict_key=?", (row[0],))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("DELETE FROM factory_import_conflict_attestations WHERE conflict_key=?", (row[0],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO factory_import_conflict_attestations VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(row))
            conn.close()

    def test_v3_attestation_failpoint_rolls_back_every_new_row_and_schema_change(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_migrate(str(Path(directory) / "bridge.db"))
            old_rows = self._v3_38(conn)
            with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "during-attestation-insertion"}):
                with self.assertRaisesRegex(Exception, "injected migration failure"):
                    init_db(conn)
            self.assertEqual(conn.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()[0], 3)
            self.assertNotIn("factory_import_conflict_attestations", {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            for table, rows in old_rows.items():
                self.assertEqual(conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(), rows)
            conn.close()

    def test_predeploy_distinguishes_derivable_v3_from_required_postmigration_attestations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            conn = connect_migrate(str(path)); self._v3_38(conn)
            conn.execute("INSERT INTO factory_import_conflicts (conflict_key,work_item_key,job_id,reason,created_at) VALUES (?,?,?,?,?)", ("runtime-inspection:inspection_repository_not_canonical", "ENG-11", "runtime-inspection", "inspection_repository_not_canonical", NOW))
            conn.commit()
            command = ["python3", str(ROOT / "hatchet/bridge/predeploy-check.py"), "--db", str(path), "--routing-registry", str(ROOT / "hatchet/workers/routing-registry.json"), "--require-zero-legacy-ready", "--require-zero-import-conflicts", "--validate-legacy-import"]
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "hatchet/bridge"), "DRIFTWARD_BRIDGE_RELEASE_VERSION": "3" * 40}
            initial = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            self.assertIn("legacy conflict attestations: attested=0 required=38", initial.stdout)
            required = subprocess.run([*command, "--require-current-attestations"], text=True, capture_output=True, check=False, env=env)
            self.assertEqual(required.returncode, 1, required.stdout + required.stderr)
            with patch.dict(os.environ, {"DRIFTWARD_BRIDGE_RELEASE_VERSION": "3" * 40}):
                init_db(conn)
            conn.close()
            post = subprocess.run([*command, "--require-current-attestations"], text=True, capture_output=True, check=False, env=env)
            self.assertEqual(post.returncode, 0, post.stdout + post.stderr)
            self.assertIn("legacy conflict attestations: attested=38 required=0", post.stdout)


class ImmutableUnitPolicyTests(unittest.TestCase):
    def test_bridge_units_execute_only_the_immutable_release(self):
        for name in (
            "driftward-hatchet-bridge.service",
            "driftward-hatchet-bridge-reconciler.service",
            "driftward-factory-operator.service",
            "driftward-factory-outbox-dispatcher.service",
        ):
            source = (ROOT / "hatchet" / "systemd" / name).read_text()
            self.assertNotIn("/root/driftward-host", source)
            self.assertNotIn("__REPO_ROOT__", source)
            self.assertIn("/docker/driftward/hatchet/releases/bridge/current", source)
        installer = (ROOT / "hatchet" / "bridge" / "install.sh").read_text()
        self.assertLess(installer.index("predeploy-check.py"), installer.index("publish-release.sh"))
        self.assertIn("s.backup(d)", installer)

    def test_factory_outbox_dispatcher_timer_has_future_calendar_triggers(self):
        source = (ROOT / "hatchet" / "systemd" / "driftward-factory-outbox-dispatcher.timer").read_text()
        self.assertIn("OnCalendar=*:*:00/30", source)
        self.assertIn("AccuracySec=1s", source)
        self.assertNotIn("OnUnitActiveSec", source)
