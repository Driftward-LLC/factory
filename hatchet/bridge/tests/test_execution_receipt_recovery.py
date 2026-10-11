import tempfile
import unittest
import os
import json
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store
from test_support import connect_migrate
from test_server import _PostHandler
from test_store import verifier_payload, original_context


class ExecutionReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect_migrate(str(Path(self.tmp.name) / "bridge.db"))
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json")})
        env.start()
        self.addCleanup(env.stop)
        self.attempt, self.job, _ = store.reserve_factory_retry(
            self.conn, work_item_key="ENG-600", mode="real", request_id="receipt-test", reason="test", actor="uid:0",
            payload=verifier_payload("Driftward-LLC/capykit"),
        )
        store.transition_job(self.conn, self.job["job_id"], state="running")
        self.lease = store.claim_factory_execution(self.conn, self.job["job_id"], owner="original-executor")["lease"]
        store.authorize_factory_effect(self.conn, self.job["job_id"], self.lease)
        store.record_factory_execution_analytics(self.conn, self.job["job_id"], self.lease, event_type="execution_backend_invoked", details={
            "analytics_schema_version": 1, "invocation_id": "test-invocation", "execution_backend": "hermes",
            "execution_transport": "grpc_uds", "backend_version": None, "configured_auth_mode": "unknown",
        })

    def take(self):
        control = store.get_factory_issue_control(self.conn, "ENG-600")
        return store.take_factory_issue_control(self.conn, work_item_key="ENG-600", request_id="takeover-1", actor="test-operator",
            reason="manual edit", expected_revision=control["revision"], expected_snapshot_digest=control["snapshot_digest"])

    def test_takeover_dispatches_only_receipt_recovery_and_original_receipt_settles_handoff(self):
        self.assertEqual(self.take()["mode"], "handoff_pending")
        key = f"factory:accepted:{self.attempt['attempt_id']}"
        wake = store.claim_factory_outbox_delivery(self.conn, key)
        self.assertIsNotNone(wake)
        self.assertEqual(wake["payload"]["job_id"], self.job["job_id"])
        self.assertFalse(store.claim_factory_execution(self.conn, self.job["job_id"], owner="another-executor")["claimed"])
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00Z'")
        observation = {"state": "completed", "cancellation_requested": True, "cancellation_acknowledged": True}
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], observation)
        control = store.get_factory_issue_control(self.conn, "ENG-600")
        self.assertEqual(control["mode"], "handoff_pending", "observation alone cannot settle execution")
        self.assertEqual(control["blockers"][0]["recovery"]["state"], "completed")
        details = {"pr_url": "https://github.com/Driftward-LLC/capykit/pull/600", "branch": self.job["branch"], "commit_sha": "a" * 40}
        with self.assertRaises(store.BridgeConflict):
            store.commit_factory_execution(self.conn, self.job["job_id"], {**self.lease, "fence_token": "wrong"}, details)
        store.commit_factory_execution(self.conn, self.job["job_id"], self.lease, details)
        self.assertEqual(store.get_factory_issue_control(self.conn, "ENG-600")["mode"], "manual")
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "canceled_after_effect")
        self.assertIsNone(store.claim_factory_outbox_delivery(self.conn, key))
        self.assertFalse(store.enqueue_execution_receipt_recovery(self.conn, self.job["job_id"], stale_after_minutes=1)["enqueued"])

    def test_recovery_coalesces_wakes_and_http_rejects_unbounded_evidence(self):
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00Z'")
            self.conn.execute("UPDATE factory_attempt_outbox SET acknowledged_at='2000-01-01T00:00:00Z',state='acknowledged'")
        first = store.enqueue_execution_receipt_recovery(self.conn, self.job["job_id"], stale_after_minutes=1)
        self.assertTrue(first["enqueued"])
        key = f"factory:accepted:{self.attempt['attempt_id']}"
        delivery = store.claim_factory_outbox_delivery(self.conn, key)
        self.assertFalse(store.enqueue_execution_receipt_recovery(self.conn, self.job["job_id"], stale_after_minutes=1)["enqueued"])
        row = self.conn.execute("SELECT delivery_lease_id FROM factory_attempt_outbox WHERE outbox_key=?", (key,)).fetchone()
        self.assertEqual(row[0], delivery["delivery_lease_id"])
        path = f"/jobs/{self.job['job_id']}/factory-execution/observation"
        for body in ({"state": "completed"}, {"state": "running", "cancellation_requested": True, "cancellation_acknowledged": True},
                     {"state": [], "cancellation_requested": False, "cancellation_acknowledged": False},
                     {"state": "not_found", "cancellation_requested": False, "cancellation_acknowledged": False, "raw_output": "private"}):
            handler = _PostHandler(self.conn, body, path)
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)

    def test_missing_receipt_never_releases_lease_or_permits_manual_editing(self):
        self.take()
        for state in ("not_found", "outcome_unknown", "unavailable"):
            store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
                "state": state, "cancellation_requested": False, "cancellation_acknowledged": False,
            })
            self.assertEqual(store.get_factory_issue_control(self.conn, "ENG-600")["mode"], "handoff_pending")
        self.assertIsNone(self.conn.execute("SELECT released_at FROM factory_execution_leases").fetchone()[0])

    def test_unavailable_observation_keeps_recovery_polling_until_original_receipt_arrives(self):
        store.settle_factory_execution(self.conn, self.job["job_id"], self.lease, effect_observed=False)
        for state in ("unavailable", "outcome_unknown", "unavailable"):
            store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
                "state": state, "cancellation_requested": False, "cancellation_acknowledged": False})
            with self.conn:
                self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00Z'")
                self.conn.execute("UPDATE factory_attempt_outbox SET state='acknowledged',acknowledged_at='2000-01-01T00:00:00Z'")
            self.assertTrue(store.enqueue_execution_receipt_recovery(self.conn, self.job["job_id"], stale_after_minutes=1)["enqueued"])
            self.assertFalse(store.claim_factory_execution(self.conn, self.job["job_id"], owner="another")["claimed"])
            self.assertIsNone(self.conn.execute("SELECT released_at FROM factory_execution_leases").fetchone()[0])
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
            "state": "completed", "cancellation_requested": False, "cancellation_acknowledged": False})
        result = store.commit_factory_execution(self.conn, self.job["job_id"], self.lease, {
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/600", "branch": self.job["branch"],
            "commit_sha": "a" * 40, "verification_context": original_context(self.job)})
        self.assertEqual(result["state"], "verification_pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)

    def test_receipt_commit_persists_validation_atomically_and_rolls_back_invalid_context(self):
        details = {"pr_url": "https://github.com/Driftward-LLC/capykit/pull/600", "branch": self.job["branch"], "commit_sha": "a" * 40}
        with self.assertRaises(store.BridgeValidationError):
            store.commit_factory_execution(self.conn, self.job["job_id"], self.lease, {**details, "verification_context": {"raw": "invalid"}})
        self.assertEqual(store.get_job(self.conn, self.job["job_id"])["state"], "running")
        context = original_context(self.job)
        # Inject failure after the effect transition to prove the encompassing
        # transaction rolls back artifact state, checkpoint and lease together.
        transition = store.transition_job
        def fail_pending(conn, ref, *, state, **kwargs):
            if state == "verification_pending":
                raise RuntimeError("interrupted validation persistence")
            return transition(conn, ref, state=state, **kwargs)
        with patch.object(store, "transition_job", side_effect=fail_pending), self.assertRaises(RuntimeError):
            store.commit_factory_execution(self.conn, self.job["job_id"], self.lease, {**details, "verification_context": context})
        self.assertEqual(store.get_job(self.conn, self.job["job_id"])["state"], "running")
        result = store.commit_factory_execution(self.conn, self.job["job_id"], self.lease, {**details, "verification_context": context})
        self.assertEqual(result["state"], "verification_pending")
        self.assertEqual(result["verification_context"], context)

    def test_terminal_outcome_stops_receipt_polling_without_releasing_hold_or_lease(self):
        self.take()
        outcome = {
            "schema": "factory-executor-outcome-v1", "job_id": self.job["job_id"], "state": "incomplete",
            "reason_code": "hermes_execution_incomplete", "finished_at": "2026-10-10T00:00:00.000Z",
            "process_exit_code": 0, "process_signal": None, "timed_out": False, "buffer_exceeded": False,
            "effect_outcome": "unknown", "containment": "unconfirmed", "artifact": None,
        }
        observation = {"state": "incomplete", "cancellation_requested": True, "cancellation_acknowledged": False, "outcome": outcome}
        for change in ({"job_id": "wrong"}, {"raw_output": "secret"}, {"containment": "confirmed"},
                       {"process_exit_code": True}, {"process_signal": "arbitrary"}, {"timed_out": "yes"},
                       {"finished_at": "2026-99-99T99:99:99.000Z"},
                       {"artifact": {"branch": self.job["branch"], "pr_url": "https://github.com/other/repo/pull/1", "commit_sha": None}}):
            with self.subTest(change=change), self.assertRaises(store.BridgeValidationError):
                store.record_execution_receipt_observation(self.conn, self.job["job_id"], {**observation, "outcome": {**outcome, **change}})
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], observation)
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], observation)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_events WHERE event_type='execution_receipt_observed'").fetchone()[0], 1)
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00Z'")
            self.conn.execute("UPDATE factory_attempt_outbox SET acknowledged_at='2000-01-01T00:00:00Z',state='acknowledged'")
        self.assertFalse(store.enqueue_execution_receipt_recovery(self.conn, self.job["job_id"], stale_after_minutes=1)["enqueued"])
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
            "state": "unavailable", "cancellation_requested": False, "cancellation_acknowledged": False,
        })
        control = store.get_factory_issue_control(self.conn, "ENG-600")
        self.assertEqual(control["mode"], "handoff_pending")
        self.assertEqual(control["blockers"][0]["recovery"]["state"], "incomplete")
        self.assertIsNone(self.conn.execute("SELECT released_at FROM factory_execution_leases").fetchone()[0])
        self.assertFalse(store.claim_factory_execution(self.conn, self.job["job_id"], owner="another")["claimed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)
        event = self.conn.execute("SELECT details_json FROM factory_attempt_events WHERE event_type='execution_receipt_observed'").fetchone()[0]
        self.assertEqual(json.loads(event)["outcome"], outcome)


if __name__ == "__main__":
    unittest.main()
