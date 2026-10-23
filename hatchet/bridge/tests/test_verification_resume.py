import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store
from test_server import _PostHandler
from test_store import original_context, verification_receipt, verifier_payload
from test_support import connect_migrate


class VerificationResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(lambda: self.conn.close())
        env = patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json")})
        env.start()
        self.addCleanup(env.stop)
        self.sequence = 800

    def pending(self, *, state="verification_pending", stale=True, acknowledged=True):
        self.sequence += 1
        issue = f"ENG-{self.sequence}"
        attempt, job, _ = store.reserve_factory_intake(
            self.conn, work_item_key=issue, mode="real", request_id=f"intake:{issue}", reason="test",
            actor="intake-worker", payload=verifier_payload("Driftward-LLC/capykit"),
        )
        self.commit(job)
        if state == "verification_pending":
            store.transition_job(self.conn, job["job_id"], state=state, details={"verification_context": original_context(job)})
        if acknowledged:
            self.deliver(attempt)
        if stale:
            with self.conn:
                self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00.000000Z' WHERE job_id=?", (job["job_id"],))
        return store.get_factory_attempt(self.conn, attempt["attempt_id"]), store.get_job(self.conn, job["job_id"])

    def commit(self, job):
        store.transition_job(self.conn, job["job_id"], state="running")
        lease = store.claim_factory_execution(self.conn, job["job_id"], owner="executor")["lease"]
        self.assertTrue(store.authorize_factory_effect(self.conn, job["job_id"], lease))
        return store.commit_factory_execution(self.conn, job["job_id"], lease, {
            "pr_url": job["payload"].get("factory_repair", {}).get("pr_url") or f"https://github.com/Driftward-LLC/capykit/pull/{self.sequence}",
            "commit_sha": "a" * 40, "branch": job["branch"],
        })

    def deliver(self, attempt):
        row = store.claim_factory_outbox_delivery(self.conn, f"factory:accepted:{attempt['attempt_id']}")
        self.assertIsNotNone(row)
        store.acknowledge_factory_outbox(self.conn, row["outbox_key"], row["delivery_lease_id"])
        return row

    def outbox(self, attempt):
        row = self.conn.execute("SELECT * FROM factory_attempt_outbox WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        return dict(row) if row else None

    def enqueue(self, job):
        return store.enqueue_verification_resume(self.conn, job["job_id"], stale_after_minutes=30)

    def wake_events(self, attempt):
        return self.conn.execute("SELECT * FROM factory_attempt_events WHERE attempt_id=? AND event_type='verification_recovery_wake_enqueued'", (attempt["attempt_id"],)).fetchall()

    def acquire(self, job, owner="verifier"):
        return store.acquire_verification_lease(self.conn, job["job_id"], attempt_id=owner)

    def claim(self, job, owner="verifier"):
        return store.claim_verification_lease(self.conn, job["job_id"], attempt_id=owner)

    def fail(self, job, owner="verifier"):
        return store.transition_job(self.conn, job["job_id"], state="failed", details={"verification_attempt_id": owner}, _factory_verifier_completion=True)

    def test_missing_or_acknowledged_outbox_is_rearmed_without_changing_job_or_attempt(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                attempt, job = self.pending()
                if missing:
                    with self.conn:
                        self.conn.execute("DELETE FROM factory_attempt_outbox WHERE attempt_id=?", (attempt["attempt_id"],))
                self.assertEqual(self.enqueue(job), {"enqueued": True, "reason": "enqueued"})
                queued = self.outbox(attempt)
                self.assertEqual(queued["state"], "pending")
                self.assertIsNone(queued["delivery_lease_id"])
                self.assertEqual(json.loads(queued["payload_json"]), {"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]})
                self.assertEqual(store.get_job(self.conn, job["job_id"]), job)
                self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"]), attempt)
                self.assertEqual(len(self.wake_events(attempt)), 1)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM verification_leases WHERE job_id=?", (job["job_id"],)).fetchone()[0], 0)
                self.assertEqual(self.enqueue(job), {"enqueued": False, "reason": "already_queued"})
                self.assertEqual(len(self.wake_events(attempt)), 1)

    def test_pending_retrying_and_inflight_deliveries_keep_original_lease_and_event(self):
        for state in ("pending", "retrying", "delivering", "expired_delivery"):
            with self.subTest(state=state):
                attempt, job = self.pending(acknowledged=False)
                if state != "pending":
                    row = store.claim_factory_outbox_delivery(self.conn, self.outbox(attempt)["outbox_key"])
                    if state == "retrying":
                        store.fail_factory_outbox_delivery(self.conn, row["outbox_key"], row["delivery_lease_id"])
                    if state == "expired_delivery":
                        with self.conn:
                            self.conn.execute("UPDATE factory_attempt_outbox SET delivery_lease_expires_at='2000-01-01T00:00:00.000000Z' WHERE attempt_id=?", (attempt["attempt_id"],))
                before = self.outbox(attempt)
                self.assertEqual(self.enqueue(job), {"enqueued": False, "reason": "already_queued"})
                self.assertEqual(self.outbox(attempt), before)
                self.assertEqual(self.wake_events(attempt), [])
                if state == "delivering":
                    store.acknowledge_factory_outbox(self.conn, before["outbox_key"], before["delivery_lease_id"])
                    self.assertEqual(self.outbox(attempt)["state"], "acknowledged")

    def test_acknowledged_rearm_fences_stale_delivery_ack_and_default_receipt_recovery_still_resets(self):
        attempt, job = self.pending(acknowledged=False)
        old = self.deliver(attempt)
        self.assertTrue(self.enqueue(job)["enqueued"])
        store.acknowledge_factory_outbox(self.conn, old["outbox_key"], old["delivery_lease_id"])
        self.assertEqual(self.outbox(attempt)["state"], "pending")
        current = store.claim_factory_outbox_delivery(self.conn, old["outbox_key"])
        with self.conn:
            self.assertTrue(store._enqueue_factory_attempt_resume(self.conn, attempt, job))
        store.acknowledge_factory_outbox(self.conn, current["outbox_key"], current["delivery_lease_id"])
        self.assertEqual(self.outbox(attempt)["state"], "pending")
        self.assertIsNone(self.outbox(attempt)["delivery_lease_id"])

    def test_enqueue_and_event_rollback_together_for_missing_and_acknowledged_rows(self):
        for missing in (False, True):
            attempt, job = self.pending()
            if missing:
                with self.conn:
                    self.conn.execute("DELETE FROM factory_attempt_outbox WHERE attempt_id=?", (attempt["attempt_id"],))
            before = self.outbox(attempt)
            with patch.object(store, "_append_factory_attempt_event", side_effect=RuntimeError("interrupted enqueue")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    self.enqueue(job)
            self.assertEqual(self.outbox(attempt), before)
            self.assertEqual(self.wake_events(attempt), [])
            self.assertTrue(self.enqueue(job)["enqueued"])

    def test_restart_and_delivery_delay_do_not_expire_a_preacquired_verifier_lease(self):
        attempt, job = self.pending(state="effect_committed")
        self.assertTrue(self.enqueue(job)["enqueued"])
        self.conn.close()
        self.conn = store.connect_runtime(self.path)
        self.assertEqual(self.enqueue(job)["reason"], "already_queued")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM verification_leases").fetchone()[0], 0)
        future = datetime.now(timezone.utc) + timedelta(minutes=20)
        with patch.object(store, "datetime", wraps=datetime) as clock:
            clock.now.return_value = future
            self.deliver(attempt)
            self.assertTrue(self.acquire(job)["acquired"])
            self.assertTrue(self.claim(job))
            store.transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
            self.assertEqual(self.fail(job)["state"], "failed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bridge_jobs WHERE requested_action IN ('create_draft_pr','repair_draft_pr')").fetchone()[0], 1)
        self.assertEqual(store.get_job(self.conn, job["job_id"])["pr_url"], job["pr_url"])

    def test_active_lease_is_not_requeued_and_expired_lease_replacement_fences_old_owner(self):
        attempt, job = self.pending()
        self.assertTrue(self.acquire(job, "old")["acquired"])
        self.assertTrue(self.claim(job, "old"))
        self.assertEqual(self.enqueue(job), {"enqueued": False, "reason": "lease_active"})
        with self.conn:
            self.conn.execute("UPDATE verification_leases SET expires_at='2000-01-01T00:00:00.000000Z' WHERE job_id=?", (job["job_id"],))
        self.assertTrue(self.enqueue(job)["enqueued"])
        self.deliver(attempt)
        self.assertTrue(self.acquire(job, "new")["acquired"])
        self.assertFalse(self.claim(job, "old"))
        self.assertTrue(self.claim(job, "new"))
        with self.assertRaises(store.BridgeValidationError):
            self.fail(job, "old")
        self.assertEqual(self.fail(job, "new")["state"], "failed")

    def test_cancellation_before_acquire_claim_observation_and_completion_is_fenced(self):
        for phase in ("enqueue", "acquire", "claim", "observation", "completion"):
            with self.subTest(phase=phase):
                attempt, job = self.pending()
                if phase in {"claim", "observation", "completion"}:
                    self.assertTrue(self.acquire(job)["acquired"])
                if phase in {"observation", "completion"}:
                    self.assertTrue(self.claim(job))
                if phase == "completion":
                    store.record_verification_observation(self.conn, job["job_id"], attempt_id="verifier", receipt=verification_receipt(job, job["commit_sha"]))
                canceled = store.cancel_factory_attempt(self.conn, attempt["attempt_id"], reason="stop", actor="test")
                self.assertEqual(canceled["status"], "canceled_after_effect")
                self.assertEqual(self.enqueue(job), {"enqueued": False, "reason": "not_eligible"})
                if phase == "enqueue":
                    continue
                with self.assertRaises(store.BridgeValidationError):
                    if phase == "acquire":
                        self.acquire(job)
                    elif phase == "claim":
                        self.claim(job)
                    elif phase == "observation":
                        store.record_verification_observation(self.conn, job["job_id"], attempt_id="verifier", receipt=verification_receipt(job, job["commit_sha"]))
                    else:
                        store.transition_job(self.conn, job["job_id"], state="ready_for_human_review", details={"verification_attempt_id": "verifier", "verification_receipt": verification_receipt(job, job["commit_sha"]), "reason_code": "draft_pr_verified"}, _factory_verifier_completion=True)
                self.assertEqual(store.get_job(self.conn, job["job_id"])["state"], "verification_pending")
                self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "canceled_after_effect")

    def test_terminal_superseded_and_noncurrent_generations_cannot_resume_or_finish(self):
        for status in ("superseded", "cancellation_requested", "failed", "verified_ready", "completed", "noncurrent"):
            with self.subTest(status=status):
                attempt, job = self.pending()
                self.assertTrue(self.acquire(job)["acquired"])
                self.assertTrue(self.claim(job))
                with self.conn:
                    self.conn.execute("UPDATE factory_attempts SET status=? WHERE attempt_id=?", ("superseded" if status == "noncurrent" else status, attempt["attempt_id"]))
                if status == "noncurrent":
                    store.reserve_factory_retry(self.conn, work_item_key=attempt["work_item_key"], mode="real", request_id="new-generation", reason="test", actor="test", payload=verifier_payload("Driftward-LLC/capykit"))
                    with self.conn:
                        self.conn.execute("UPDATE factory_attempts SET status='verification_pending' WHERE attempt_id=?", (attempt["attempt_id"],))
                self.assertEqual(self.enqueue(job)["reason"], "not_eligible")
                for action in (lambda: self.acquire(job), lambda: self.claim(job), lambda: self.fail(job)):
                    with self.assertRaises(store.BridgeValidationError):
                        action()

    def test_invalid_artifacts_staleness_and_request_types_do_not_queue(self):
        for field, value in (("pr_url", "https://github.com/other/repo/pull/1"), ("branch", "other-branch"), ("commit_sha", "not-a-sha")):
            attempt, job = self.pending()
            with self.conn:
                self.conn.execute(f"UPDATE bridge_jobs SET {field}=? WHERE job_id=?", (value, job["job_id"]))
            self.assertEqual(self.enqueue(job)["reason"], "not_eligible")
            self.assertEqual(self.outbox(attempt)["state"], "acknowledged")
        attempt, job = self.pending(stale=False)
        self.assertEqual(self.enqueue(job)["reason"], "not_stale")
        for value in (True, 0, 10081, "30", None, 1.5):
            with self.assertRaises(store.BridgeValidationError):
                store.enqueue_verification_resume(self.conn, job["job_id"], stale_after_minutes=value)
        self.assertEqual(self.wake_events(attempt), [])

    def test_dry_run_non_effect_terminal_and_wrong_lineage_jobs_are_not_eligible(self):
        for case in ("dry-run", "non-effect", "terminal", "wrong-lineage"):
            with self.subTest(case=case):
                attempt, job = self.pending()
                with self.conn:
                    if case == "dry-run":
                        self.conn.execute("UPDATE factory_attempts SET mode='dry-run' WHERE attempt_id=?", (attempt["attempt_id"],))
                        self.conn.execute("UPDATE bridge_jobs SET payload_json=? WHERE job_id=?", (json.dumps({**job["payload"], "dry_run": True}), job["job_id"]))
                    elif case == "non-effect":
                        self.conn.execute("UPDATE bridge_jobs SET requested_action='inspect_draft_pr_state' WHERE job_id=?", (job["job_id"],))
                    elif case == "terminal":
                        self.conn.execute("UPDATE bridge_jobs SET state='ready_for_human_review' WHERE job_id=?", (job["job_id"],))
                    else:
                        self.conn.execute("UPDATE factory_attempts SET action='inspect_draft_pr_state' WHERE attempt_id=?", (attempt["attempt_id"],))
                self.assertEqual(self.enqueue(job), {"enqueued": False, "reason": "not_eligible"})
                self.assertEqual(self.outbox(attempt)["state"], "acknowledged")
                self.assertEqual(self.wake_events(attempt), [])

    def test_concurrent_connections_enqueue_one_wake_and_one_audit_event(self):
        attempt, job = self.pending()
        barrier = threading.Barrier(2)

        def enqueue():
            conn = store.connect_runtime(self.path)
            try:
                barrier.wait(timeout=5)
                return store.enqueue_verification_resume(conn, job["job_id"], stale_after_minutes=30)
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(enqueue) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(result["reason"] for result in results), ["already_queued", "enqueued"])
        self.assertEqual(sum(result["enqueued"] for result in results), 1)
        self.assertEqual(len(self.wake_events(attempt)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0], 1)

    def test_legacy_manual_verification_is_retained_but_automatic_wake_is_skipped(self):
        job, _ = store.submit_job(self.conn, {"idempotency_key": "legacy-test", "requested_action": "create_draft_pr", "payload": verifier_payload("Driftward-LLC/capykit")})
        store.transition_job(self.conn, job["job_id"], state="running")
        job = store.transition_job(self.conn, job["job_id"], state="effect_committed", details={"pr_url": "https://github.com/Driftward-LLC/capykit/pull/1", "commit_sha": "abc123"})
        self.assertEqual(self.enqueue(job)["reason"], "legacy_job")
        self.assertTrue(self.acquire(job)["acquired"])
        self.assertTrue(self.claim(job))
        attempt, imported = self.pending()
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET actor='legacy-import' WHERE attempt_id=?", (attempt["attempt_id"],))
        self.assertEqual(self.enqueue(imported)["reason"], "legacy_job")
        self.assertTrue(self.acquire(imported)["acquired"])

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_repair_wake_reuses_source_branch_and_pr_not_the_generation_reserved_branch(self):
        attempt, job = self.pending()
        self.assertTrue(self.acquire(job)["acquired"])
        self.assertTrue(self.claim(job))
        store.transition_job(self.conn, job["job_id"], state="failed", details={"verification_attempt_id": "verifier", "reason_code": "verification_github_check_failed"}, _factory_verifier_completion=True)
        repair = store.list_factory_attempts(self.conn, attempt["work_item_key"])[0]
        repair_job = store.get_job(self.conn, repair["job_id"])
        self.assertNotEqual(repair["branch"], repair_job["branch"])
        repaired = self.commit(repair_job)
        self.deliver(repair)
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00.000000Z' WHERE job_id=?", (repaired["job_id"],))
        job_count = self.conn.execute("SELECT COUNT(*) FROM bridge_jobs").fetchone()[0]
        self.assertTrue(self.enqueue(repaired)["enqueued"])
        self.assertEqual(repaired["pr_url"], job["pr_url"])
        self.assertEqual(repaired["branch"], job["branch"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bridge_jobs").fetchone()[0], job_count)

    def test_new_create_generation_uses_committed_artifact_not_prior_pr_summary(self):
        previous, prior_job = self.pending()
        store.cancel_factory_attempt(self.conn, previous["attempt_id"], reason="supersede", actor="test")
        current, job, _ = store.reserve_factory_retry(
            self.conn, work_item_key=previous["work_item_key"], mode="real", request_id="new-create",
            reason="test", actor="test", payload=verifier_payload("Driftward-LLC/capykit"),
            prior_attempt_id=previous["attempt_id"], supersede_pr_url=prior_job["pr_url"],
        )
        self.sequence += 1
        committed = self.commit(job)
        self.assertNotEqual(committed["pr_url"], prior_job["pr_url"])
        self.deliver(current)
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET prior_pr_url=? WHERE attempt_id=?", (prior_job["pr_url"], current["attempt_id"]))
            self.conn.execute("UPDATE bridge_jobs SET updated_at='2000-01-01T00:00:00.000000Z' WHERE job_id=?", (job["job_id"],))
        self.assertTrue(self.enqueue(committed)["enqueued"])
        self.assertEqual(json.loads(self.outbox(current)["payload_json"])["job_id"], job["job_id"])

    def test_verifier_helpers_preserve_enclosing_transaction_and_reads_are_write_fenced(self):
        _, job = self.pending()
        queries = []
        self.conn.set_trace_callback(queries.append)
        self.assertTrue(self.acquire(job)["acquired"])
        self.assertTrue(self.claim(job))
        self.conn.set_trace_callback(None)
        begins = [query for query in queries if query.startswith("BEGIN")]
        self.assertEqual(begins, ["BEGIN IMMEDIATE", "BEGIN IMMEDIATE"])
        self.conn.execute("BEGIN IMMEDIATE")
        self.fail(job)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store.get_job(self.conn, job["job_id"])["state"], "verification_pending")
        self.assertIsNone(self.conn.execute("SELECT closed_at FROM verification_leases WHERE job_id=?", (job["job_id"],)).fetchone()[0])

    def test_http_resume_requires_exact_body_and_uses_existing_authenticated_route(self):
        _, job = self.pending()
        path = f"/jobs/{job['job_id']}/verification/resume"
        forbidden = _PostHandler(self.conn, {"stale_after_minutes": 30}, path)
        forbidden._require_auth = lambda: False
        forbidden.do_POST()
        self.assertIsNone(forbidden.response)
        for body in ({}, {"stale_after_minutes": True}, {"stale_after_minutes": 30, "verification_attempt_id": "caller-owner"}):
            handler = _PostHandler(self.conn, body, path)
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
        handler = _PostHandler(self.conn, {"stale_after_minutes": 30}, path)
        handler.do_POST()
        self.assertEqual(handler.response, (200, {"enqueued": True, "reason": "enqueued"}))


if __name__ == "__main__":
    unittest.main()
