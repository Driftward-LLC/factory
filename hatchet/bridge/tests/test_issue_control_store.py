import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store
from test_store import original_context, verification_receipt, verifier_payload
from test_support import connect_migrate


class IssueControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json")})
        env.start()
        self.addCleanup(env.stop)
        self.issue = "ENG-600"

    def intake(self, request_id="original"):
        return store.reserve_factory_intake(self.conn, work_item_key=self.issue, mode="real", request_id=request_id,
                                            reason="Ready intake", actor="intake-worker", payload=verifier_payload("Driftward-LLC/capykit"))

    def command(self, request_id="take-1", **overrides):
        control = store.get_factory_issue_control(self.conn, self.issue)
        return {"work_item_key": self.issue, "request_id": request_id, "actor": "operator@example.test", "reason": "Work manually",
                "expected_revision": control["revision"], "expected_snapshot_digest": control["snapshot_digest"], **overrides}

    def take(self):
        return store.take_factory_issue_control(self.conn, **self.command())

    def return_request(self):
        return {**self.command("return-1", reason="Fresh reviewed attempt"),
                "payload": {**verifier_payload("Driftward-LLC/capykit"), "trusted_snapshot_digest": "a" * 64}}

    def claim(self, job, *, authorize=False):
        store.transition_job(self.conn, job["job_id"], state="running")
        lease = store.claim_factory_execution(self.conn, job["job_id"], owner="executor")["lease"]
        if authorize:
            self.assertTrue(store.authorize_factory_effect(self.conn, job["job_id"], lease))
        return lease

    def commit(self, job, lease):
        return store.commit_factory_execution(self.conn, job["job_id"], lease, {
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/600", "commit_sha": "a" * 40, "branch": job["branch"],
        })

    def test_default_read_is_factory_and_does_not_create_ownership(self):
        result = store.get_factory_issue_control(self.conn, self.issue)
        self.assertEqual((result["mode"], result["revision"], result["history"]), ("factory", 0, []))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM factory_issue_controls").fetchone()[0], 0)

    def test_noncanonical_issue_aliases_cannot_bypass_ownership(self):
        self.take()
        for issue in ("eng-600", " ENG-600 ", "eng-0600"):
            for reserve in (store.reserve_factory_intake, store.reserve_factory_retry):
                with self.assertRaises(store.BridgeValidationError):
                    reserve(self.conn, work_item_key=issue, mode="real", request_id="alias-bypass", reason="test", actor="intake-worker", payload=verifier_payload())
        self.issue = "ENG-601"
        attempt, job, _ = self.intake()
        lease = self.claim(job)
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET work_item_key=? WHERE attempt_id=?", ("\teng-601\u2003", attempt["attempt_id"]))
        control = store.take_factory_issue_control(self.conn, **self.command("take-alias-601"))
        self.assertEqual(control["mode"], "handoff_pending")
        self.assertEqual(control["blockers"][0]["attempt_id"], attempt["attempt_id"])
        self.assertFalse(store.authorize_factory_effect(self.conn, job["job_id"], lease))
        self.assertFalse(store.claim_factory_execution(self.conn, job["job_id"], owner="alias-bypass")["claimed"])

    def test_queued_takeover_is_immediately_manual_and_fences_every_admission(self):
        attempt, job, _ = self.intake()
        delivery = store.claim_factory_outbox_delivery(self.conn, f"factory:accepted:{attempt['attempt_id']}")
        self.assertEqual(self.take()["mode"], "manual")
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "canceled")
        self.assertFalse(store.claim_factory_execution(self.conn, job["job_id"], owner="delayed")["claimed"])
        self.assertIsNone(store.claim_factory_outbox_delivery(self.conn, delivery["outbox_key"]))
        store.acknowledge_factory_outbox(self.conn, delivery["outbox_key"], delivery["delivery_lease_id"])
        self.assertEqual(self.conn.execute("SELECT state FROM factory_attempt_outbox").fetchone()[0], "suppressed")
        with self.assertRaises(store.BridgeConflict):
            self.intake("delayed-intake")
        with self.assertRaises(store.BridgeConflict):
            store.reserve_factory_retry(self.conn, work_item_key=self.issue, mode="real", request_id="root-retry", reason="retry", actor="root", payload=verifier_payload())
        with self.assertRaises(store.BridgeConflict):
            store.submit_job(self.conn, {"idempotency_key": "bypass", "requested_action": "create_draft_pr", "payload": {"linear_issue": self.issue, "repo": "Driftward-LLC/capykit"}})
        with self.assertRaises(store.BridgeConflict):
            store.transition_job(self.conn, job["job_id"], state="running")

    def test_claimed_handoff_waits_for_cooperative_settlement_not_lease_expiry(self):
        attempt, job, _ = self.intake()
        lease = self.claim(job)
        self.assertEqual(self.take()["mode"], "handoff_pending")
        self.assertFalse(store.authorize_factory_effect(self.conn, job["job_id"], lease))
        self.assertFalse(store.execution_may_effect(self.conn, job["job_id"], lease))
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00.000000Z'")
        self.assertEqual(store.get_factory_issue_control(self.conn, self.issue)["mode"], "handoff_pending")
        with self.assertRaises(store.BridgeConflict):
            store.return_factory_issue_control(self.conn, **self.return_request())
        store.settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False)
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "canceled")
        self.assertEqual(store.get_factory_issue_control(self.conn, self.issue)["mode"], "manual")

    def test_authorized_late_receipt_is_retained_without_resuming_old_work(self):
        attempt, job, _ = self.intake()
        lease = self.claim(job, authorize=True)
        self.assertEqual(self.take()["mode"], "handoff_pending")
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00.000000Z'")
        store.settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False)
        self.assertEqual(store.get_factory_issue_control(self.conn, self.issue)["mode"], "handoff_pending")
        committed = self.commit(job, lease)
        self.assertEqual(committed["state"], "effect_committed")
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "canceled_after_effect")
        self.assertEqual(store.get_factory_issue_control(self.conn, self.issue)["mode"], "manual")
        self.assertFalse(store._enqueue_factory_attempt_resume(self.conn, attempt, committed))
        with self.assertRaises(store.BridgeConflict):
            store.acquire_verification_lease(self.conn, job["job_id"], attempt_id="late-verifier")

    def test_takeover_revokes_verification_completion_and_automatic_repair(self):
        attempt, job, _ = self.intake()
        job = self.commit(job, self.claim(job, authorize=True))
        job = store.transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        store.acquire_verification_lease(self.conn, job["job_id"], attempt_id="verifier")
        store.claim_verification_lease(self.conn, job["job_id"], attempt_id="verifier")
        self.assertEqual(self.take()["mode"], "manual")
        receipt = verification_receipt(job, job["commit_sha"])
        for operation in (
            lambda: store.record_verification_observation(self.conn, job["job_id"], attempt_id="verifier", receipt=receipt),
            lambda: store.claim_verification_lease(self.conn, job["job_id"], attempt_id="verifier"),
            lambda: store.transition_job(self.conn, job["job_id"], state="failed", details={"verification_attempt_id": "verifier"}, _factory_verifier_completion=True),
        ):
            with self.assertRaises(store.BridgeConflict):
                operation()
        self.assertEqual(store.enqueue_verification_resume(self.conn, job["job_id"], stale_after_minutes=1), {"enqueued": False, "reason": "not_eligible"})
        failed_job = {**job, "reason_code": "verification_github_check_failed"}
        with patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "10"}):
            self.assertIsNone(store._reserve_factory_verification_repair(self.conn, failed_job=failed_job, factory_attempt=attempt, details={}))
        self.assertEqual(len(store.list_factory_attempts(self.conn, self.issue)), 1)

    def test_stale_preview_and_different_intent_conflict_but_duplicate_click_deduplicates(self):
        _, job, _ = self.intake()
        stale = self.command()
        self.claim(job)
        with self.assertRaisesRegex(store.BridgeConflict, "stale"):
            store.take_factory_issue_control(self.conn, **stale)
        request = self.command()
        first = store.take_factory_issue_control(self.conn, **request)
        self.assertEqual(store.take_factory_issue_control(self.conn, **request), first)
        for changed in ({"actor": "someone-else"}, {"reason": "different"}, {"work_item_key": "ENG-601"}, {"expected_revision": 99}):
            with self.assertRaises(store.BridgeConflict):
                store.take_factory_issue_control(self.conn, **{**request, **changed})
        self.assertEqual(len(first["history"]), 1)

    def test_handback_is_atomic_new_generation_and_old_messages_never_reactivate(self):
        old, old_job, _ = self.intake()
        self.take()
        request = self.return_request()
        with patch.object(store, "_record_factory_admission_evidence", side_effect=RuntimeError("admission failed")):
            with self.assertRaises(RuntimeError):
                store.return_factory_issue_control(self.conn, **request)
        self.assertEqual(store.get_factory_issue_control(self.conn, self.issue)["mode"], "manual")
        self.assertEqual(len(store.list_factory_attempts(self.conn, self.issue)), 1)
        result = store.return_factory_issue_control(self.conn, **request)
        self.assertTrue(result["created"])
        self.assertEqual(result["control"]["mode"], "factory")
        self.assertEqual(result["attempt"]["generation"], old["generation"] + 1)
        self.assertFalse(store.return_factory_issue_control(self.conn, **request)["created"])
        self.assertEqual(len(store.list_factory_attempts(self.conn, self.issue)), 2)
        self.assertFalse(store.claim_factory_execution(self.conn, old_job["job_id"], owner="old")["claimed"])
        with self.assertRaises(store.BridgeConflict):
            self.intake("never-previously-admitted-stale-event")
        with self.assertRaises(store.BridgeConflict):
            store.transition_job(self.conn, old_job["job_id"], state="running")
        self.assertTrue(store.claim_factory_execution(self.conn, result["job"]["job_id"], owner="new")["claimed"])
        recovered = store.get_factory_issue_control_command(self.conn, **{key: value for key, value in request.items() if key != "payload"}, action="return_control")
        self.assertEqual(recovered["attempt"]["attempt_id"], result["attempt"]["attempt_id"])
        self.assertEqual(len(result["control"]["history"]), 2)

    def test_legacy_active_job_prevents_false_safe_handoff(self):
        store.submit_job(self.conn, {"idempotency_key": "legacy", "requested_action": "create_draft_pr", "payload": {"repo": "Driftward-LLC/capykit", "linear_issue": self.issue}})
        control = self.take()
        self.assertEqual(control["mode"], "handoff_pending")
        self.assertEqual(control["blockers"][0]["kind"], "untracked_active_job")

    def test_control_and_audit_survive_reopen_and_history_cannot_be_rewritten(self):
        self.intake()
        self.take()
        with store.connect_runtime(self.path) as other:
            control = store.get_factory_issue_control(other, self.issue)
            self.assertEqual(control["mode"], "manual")
            self.assertEqual(control["history"][0]["actor"], "operator@example.test")
        other.close()
        for sql in ("UPDATE factory_issue_control_commands SET reason='replace'", "DELETE FROM factory_issue_control_commands", "INSERT OR REPLACE INTO factory_issue_control_commands SELECT * FROM factory_issue_control_commands"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.conn:
                    self.conn.execute(sql)

    def test_replay_fingerprint_binds_outer_approved_context(self):
        request = {**self.command(), "command_context_digest": "b" * 64}
        store.take_factory_issue_control(self.conn, **request)
        self.assertIsNotNone(store.get_factory_issue_control_command(self.conn, **request, action="take_control"))
        for changes in ({"command_context_digest": "c" * 64}, {"action": "return_control"}, {"actor": "another-operator"}):
            with self.assertRaises(store.BridgeConflict):
                store.get_factory_issue_control_command(self.conn, **{**request, "action": "take_control", **changes})

    def test_takeover_and_final_authorization_are_serialized(self):
        for sequence in range(4):
            self.issue = f"ENG-{700 + sequence}"
            _, job, _ = self.intake(f"intake-{sequence}")
            lease = self.claim(job)
            request = self.command(f"take-{sequence}")
            barrier = threading.Barrier(2)

            def authorize():
                conn = store.connect_runtime(self.path)
                try:
                    barrier.wait(timeout=5)
                    return store.authorize_factory_effect(conn, job["job_id"], lease)
                finally:
                    conn.close()

            def takeover():
                conn = store.connect_runtime(self.path)
                try:
                    barrier.wait(timeout=5)
                    try:
                        return store.take_factory_issue_control(conn, **request)
                    except store.BridgeConflict:
                        return None  # Grant won; operator must review changed snapshot.
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                grant, take = pool.submit(authorize), pool.submit(takeover)
                granted, taken = grant.result(), take.result()
            self.assertNotEqual(granted, taken is not None)
            control = store.get_factory_issue_control(self.conn, self.issue)
            self.assertEqual(control["held"], taken is not None)
            if taken:
                self.assertEqual(taken["mode"], "handoff_pending")


if __name__ == "__main__":
    unittest.main()
