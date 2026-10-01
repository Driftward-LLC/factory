import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store, work_evidence
from test_support import connect_migrate
from test_store import original_context, verification_receipt, verifier_payload


class WorkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json")})
        env.start()
        self.addCleanup(env.stop)

    def reserve(self, issue="ENG-801", **overrides):
        request = dict(work_item_key=issue, mode="real", request_id=f"intake:{issue}", reason="test", actor="intake-worker", payload={**verifier_payload("Driftward-LLC/capykit"), "implementation_prompt": "Implement the requested behavior"})
        request.update(overrides)
        return store.reserve_factory_intake(self.conn, **request)

    def evidence(self, attempt):
        return store.get_factory_work_evidence(self.conn, attempt["attempt_id"])

    def route(self, attempt, job):
        return {
            "analytics_schema_version": 1, "assignment_id": f"route.{attempt['attempt_id']}",
            "experiment_id": "factory-default-v1", "variant": "hermes", "assignment_method": "static",
            "intended_backend": "hermes", "intended_transport": "grpc_uds", "configured_provider": "unknown",
            "configured_model": "unknown", "configured_auth_mode": "unknown", "backend_version": None,
            "factory_release_sha": "b" * 40, "factory_release_version": "test",
            "routing_registry_version": None, "verifier_policy_digest": job["payload"]["verification_policy"]["digest"],
            "fallback_allowed": False,
        }

    def commit(self, job):
        store.transition_job(self.conn, job["job_id"], state="running")
        lease = store.claim_factory_execution(self.conn, job["job_id"], owner="executor")["lease"]
        self.assertTrue(store.authorize_factory_effect(self.conn, job["job_id"], lease))
        return store.commit_factory_execution(self.conn, job["job_id"], lease, {
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/801",
            "commit_sha": "a" * 40, "branch": job["branch"],
        })

    def pending(self, job):
        committed = self.commit(job)
        pending = store.transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(store.acquire_verification_lease(self.conn, job["job_id"], attempt_id="verifier")["acquired"])
        self.assertTrue(store.claim_verification_lease(self.conn, job["job_id"], attempt_id="verifier"))
        return pending

    def test_admission_is_private_immutable_and_stable_under_redelivery(self):
        payload = {**verifier_payload("Driftward-LLC/capykit"), "implementation_prompt": "private prompt with sk-proj-fixture-only", "source_event_key": "first", "arbitrary_transcript": "do not export"}
        attempt, job, _ = self.reserve(payload=payload)
        before = self.evidence(attempt)
        document = before["manifests"][0]["document"]
        self.assertEqual(document["input_digest"], job["payload_hash"])
        self.assertEqual(document["requirements_digest"], work_evidence.digest({"source": "implementation_prompt", "text": payload["implementation_prompt"]}))
        self.assertEqual(before["checkpoints"][0]["document"]["approval"], "unknown")
        self.assertFalse(before["replay"]["available"])
        self.assertIn("base_commit_sha", before["replay"]["missing"])
        self.assertIn("input_artifact_ref", before["replay"]["missing"])
        for secret in ("private prompt", "sk-proj-", "arbitrary_transcript", "do not export"):
            self.assertNotIn(secret, json.dumps(before))
        self.assertFalse(self.reserve(payload=payload)[2])
        self.assertFalse(self.reserve(request_id="redelivery", payload={**payload, "source_event_key": "second"})[2])
        self.assertEqual(before, self.evidence(attempt))
        self.assertIn("second", store.get_job(self.conn, job["job_id"])["payload"]["source_event_keys"])
        for row in before["manifests"] + before["checkpoints"]:
            self.assertEqual(row["document_digest"], work_evidence.digest(row["document"]))
        for table in ("factory_execution_manifests", "factory_work_checkpoints"):
            for sql in (f"UPDATE {table} SET document_json='{{}}'", f"DELETE FROM {table}", f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"), self.conn:
                    self.conn.execute(sql)
        self.assertEqual(before, self.evidence(attempt))
        with self.assertRaises(store.BridgeConflict), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            store._insert_work_evidence(self.conn, attempt_id=attempt["attempt_id"], kind="admission", document={**document, "mode": "dry-run"})

    def test_route_manifest_is_atomic_and_idempotent(self):
        attempt, job, _ = self.reserve()
        route = self.route(attempt, job)
        with patch.object(work_evidence, "execution_manifest", side_effect=RuntimeError("failpoint")):
            with self.assertRaises(RuntimeError):
                store.record_factory_execution_route(self.conn, job["job_id"], route)
        self.assertNotIn("execution_route_selected", [e["event_type"] for e in store.list_factory_attempt_events(self.conn, attempt["attempt_id"])])
        self.assertTrue(store.record_factory_execution_route(self.conn, job["job_id"], route))
        before = self.evidence(attempt)
        self.assertFalse(store.record_factory_execution_route(self.conn, job["job_id"], route))
        self.assertEqual(before, self.evidence(attempt))
        admission, execution = before["manifests"]
        self.assertEqual(execution["parent_manifest_id"], admission["manifest_id"])
        self.assertEqual(execution["document"]["route"], route)
        self.assertIn("route.backend_version", before["replay"]["missing"])
        with self.assertRaises(store.BridgeConflict):
            store.record_factory_execution_route(self.conn, job["job_id"], {**route, "configured_model": "changed"})

    def test_fenced_implementation_and_verified_receipts_link_to_execution_manifest(self):
        attempt, job, _ = self.reserve()
        store.record_factory_execution_route(self.conn, job["job_id"], self.route(attempt, job))
        pending = self.pending(job)
        evidence = self.evidence(attempt)
        self.assertEqual(len(evidence["checkpoints"]), 2)
        implementation = evidence["checkpoints"][-1]
        self.assertEqual(implementation["manifest_id"], evidence["manifests"][-1]["manifest_id"])
        self.assertEqual(implementation["document"]["commit_sha"], "a" * 40)
        receipt = verification_receipt(pending, "a" * 40)
        store.record_verification_observation(self.conn, job["job_id"], attempt_id="verifier", receipt=receipt)
        self.assertEqual(len(self.evidence(attempt)["checkpoints"]), 2)
        store.transition_job(self.conn, job["job_id"], state="ready_for_human_review", details={"verification_attempt_id": "verifier", "verification_receipt": receipt, "reason_code": "draft_pr_verified"}, _factory_verifier_completion=True)
        verified = self.evidence(attempt)["checkpoints"][-1]["document"]
        self.assertEqual(verified["result"], "passed")
        self.assertEqual(verified["receipt_digest"], store.get_job(self.conn, job["job_id"])["verification_digest"])
        self.assertEqual(verified["local_evidence_source"], "executor_reported")
        self.assertEqual(verified["review_approval"], "unknown")

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_failed_verification_preserves_checkpoint_and_repair_lineage(self):
        attempt, job, _ = self.reserve()
        self.pending(job)
        store.transition_job(self.conn, job["job_id"], state="failed", details={"verification_attempt_id": "verifier", "reason_code": "verification_github_check_failed"}, _factory_verifier_completion=True)
        failed = self.evidence(attempt)["checkpoints"][-1]["document"]
        self.assertEqual(failed["result"], "failed")
        self.assertIsNone(failed["receipt_digest"])
        self.assertIsNone(failed["checks_passed"])
        repair = store.list_factory_attempts(self.conn, attempt["work_item_key"])[0]
        manifest = self.evidence(repair)["manifests"][0]["document"]
        self.assertEqual(manifest["prior_attempt_id"], attempt["attempt_id"])
        self.assertEqual(manifest["action"], "repair_draft_pr")
        self.assertEqual(manifest["expected_head_sha"], "a" * 40)
        self.assertIsNone(manifest["base_commit_sha"])

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_repair_admission_failure_rolls_back_verification_and_checkpoint(self):
        attempt, job, _ = self.reserve()
        self.pending(job)
        with patch.object(work_evidence, "admission_manifest", side_effect=RuntimeError("failpoint")):
            with self.assertRaises(RuntimeError):
                store.transition_job(self.conn, job["job_id"], state="failed", details={"verification_attempt_id": "verifier", "reason_code": "verification_github_check_failed"}, _factory_verifier_completion=True)
        self.assertEqual(store.get_job(self.conn, job["job_id"])["state"], "verification_pending")
        self.assertEqual(len(self.evidence(attempt)["checkpoints"]), 2)
        self.assertEqual(len(store.list_factory_attempts(self.conn, attempt["work_item_key"])), 1)
        self.assertIsNone(self.conn.execute("SELECT closed_at FROM verification_leases").fetchone()[0])

    def test_admission_failure_rolls_back_job_attempt_evidence_and_outbox(self):
        for reserve in (store.reserve_factory_intake, store.reserve_factory_retry):
            with self.subTest(reserve=reserve.__name__), patch.object(work_evidence, "specification_snapshot", side_effect=RuntimeError("failpoint")):
                with self.assertRaises(RuntimeError):
                    reserve(self.conn, work_item_key="ENG-802", mode="real", request_id="failed", reason="test", actor="test", payload=verifier_payload())
            for table in ("bridge_jobs", "bridge_events", "factory_attempts", "factory_attempt_events", "factory_attempt_outbox", "factory_execution_manifests", "factory_work_checkpoints"):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_checkpoint_failure_rolls_back_effect_transition_and_lease(self):
        attempt, job, _ = self.reserve()
        with patch.object(work_evidence, "implementation_receipt", side_effect=RuntimeError("failpoint")):
            with self.assertRaises(RuntimeError):
                self.commit(job)
        self.assertEqual(store.get_job(self.conn, job["job_id"])["state"], "running")
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "effect_authorized")
        self.assertEqual(len(self.evidence(attempt)["checkpoints"]), 1)
        self.assertIsNone(self.conn.execute("SELECT released_at FROM factory_execution_leases").fetchone()[0])

    def test_stale_or_forged_effect_cannot_produce_checkpoint(self):
        attempt, job, _ = self.reserve()
        store.transition_job(self.conn, job["job_id"], state="running")
        lease = store.claim_factory_execution(self.conn, job["job_id"], owner="executor")["lease"]
        store.authorize_factory_effect(self.conn, job["job_id"], lease)
        details = {"pr_url": "https://github.com/Driftward-LLC/capykit/pull/801", "commit_sha": "a" * 40, "branch": job["branch"]}
        with self.assertRaises(store.BridgeConflict):
            store.commit_factory_execution(self.conn, job["job_id"], {**lease, "epoch": lease["epoch"] + 1}, details)
        with self.assertRaises(store.BridgeValidationError):
            store.commit_factory_execution(self.conn, job["job_id"], lease, {**details, "branch": "forged"})
        self.assertEqual(len(self.evidence(attempt)["checkpoints"]), 1)

    def test_dry_run_has_only_admission_evidence(self):
        attempt, job, _ = self.reserve(mode="dry-run", payload={"repo": "Driftward-LLC/capykit"})
        store.transition_job(self.conn, job["job_id"], state="running")
        store.transition_job(self.conn, job["job_id"], state="completed")
        evidence = self.evidence(attempt)
        self.assertEqual(evidence["manifests"][0]["document"]["mode"], "dry-run")
        self.assertEqual(evidence["checkpoints"][0]["document"]["result"], "unavailable")
        self.assertEqual(len(evidence["checkpoints"]), 1)

    def test_upgrade_preserves_history_without_fabricating_evidence(self):
        attempt, job, _ = self.reserve()
        # Disposable v5 fixture, including an accepted attempt predating these producers.
        self.conn.execute("DROP TABLE factory_work_checkpoints")
        self.conn.execute("DROP TABLE factory_execution_manifests")
        self.conn.execute("UPDATE bridge_schema_meta SET schema_version=5")
        self.conn.commit()
        with self.assertRaises(store.BridgeValidationError):
            store.connect_runtime(self.path)
        self.conn.close()
        with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "after-ddl"}):
            with self.assertRaises(store.BridgeError):
                connect_migrate(self.path)
        with sqlite3.connect(self.path) as failed_upgrade:
            self.assertEqual(failed_upgrade.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], 5)
            self.assertIsNone(failed_upgrade.execute("SELECT name FROM sqlite_master WHERE name='factory_execution_manifests'").fetchone())
        failed_upgrade.close()
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], 8)
        self.assertEqual(store.get_job(self.conn, job["job_id"])["job_id"], job["job_id"])
        self.assertFalse(self.evidence(attempt)["available"])
        store.record_factory_execution_route(self.conn, job["job_id"], self.route(attempt, job))
        self.commit(job)
        self.assertEqual(self.evidence(attempt)["checkpoints"], [])
        changes = self.conn.total_changes
        with store.connect_readonly(self.path) as readonly:
            self.assertEqual(store.get_factory_work_evidence(readonly, attempt["attempt_id"]), self.evidence(attempt))
        readonly.close()
        self.assertEqual(self.conn.total_changes, changes)


if __name__ == "__main__":
    unittest.main()
