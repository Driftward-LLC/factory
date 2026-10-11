import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import interrupted_resolution as recovery, store
from hatchet_hermes_bridge.operator import OperatorHandler
from test_store import verifier_payload
from test_support import connect_migrate

ACTOR = "uid:0;pid:123;account:root"


class InterruptedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        self.attempt, self.job, _ = store.reserve_factory_retry(
            self.conn, work_item_key="ENG-600", mode="real", request_id="fixture", reason="fixture", actor="test",
            payload=verifier_payload("Driftward-LLC/capykit"))
        self.aid = self.attempt["attempt_id"]
        self.absence = None
        store.transition_job(self.conn, self.job["job_id"], state="running")
        self.lease = store.claim_factory_execution(self.conn, self.job["job_id"], owner="executor")["lease"]
        store.authorize_factory_effect(self.conn, self.job["job_id"], self.lease)
        store.record_factory_execution_analytics(self.conn, self.job["job_id"], self.lease,
            event_type="execution_backend_invoked", details={"analytics_schema_version": 1, "invocation_id": "test-invocation",
                "execution_backend": "hermes", "execution_transport": "grpc_uds", "backend_version": None,
                "configured_auth_mode": "unknown"})
        store.settle_factory_execution(self.conn, self.job["job_id"], self.lease, effect_observed=False)
        control = store.get_factory_issue_control(self.conn, "ENG-600")
        store.take_factory_issue_control(self.conn, work_item_key="ENG-600", request_id="take-fixture", reason="manual",
            actor="operator", expected_revision=control["revision"], expected_snapshot_digest=control["snapshot_digest"])
        self.url = "https://github.com/Driftward-LLC/capykit/pull/600"
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET prior_pr_url=? WHERE attempt_id=?", (self.url, self.aid))
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00Z'")
        self.evidence = {"schema": recovery.SCHEMA, "attempt_id": self.aid, "job_id": self.job["job_id"],
            "lease_id": self.lease["lease_id"], "lease_epoch": self.lease["epoch"], "observed_at": store.utc_now(),
            "runtime_ref": "test/disposable-executor-1", "containment_method": "isolated_runtime_retired",
            "evidence_sha256": "b" * 64, "workspace_snapshot_sha256": "c" * 64,
            "checks": {key: True for key in recovery.CHECKS}}
        self.inspection()

    def inspection(self, **changes):
        result = {"repository": "Driftward-LLC/capykit", "number": 600, "url": self.url, "state": "open", "merged": False,
                  "draft": True, "head_branch": self.job["branch"], "head_sha": "a" * 40, "base_ref": "main", "reason": "ok", **changes}
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO factory_inspection_requests (request_key,repository,number,url,prior_attempt_id,state,result_json,result_digest,observed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("inspection-test", result["repository"], 600, self.url, self.aid, "completed", store.canonical_json(result),
                 store.payload_hash(result), store.utc_now(), store.utc_now(), store.utc_now()))

    def preview(self, **changes):
        return recovery.preview(self.conn, self.aid, **{"attestation": self.evidence, "reason": "reviewed containment", "actor": ACTOR,
                                                       "absence_evidence": self.absence, **changes})

    def command(self):
        return {"attestation": self.evidence, "reason": "reviewed containment", "actor": ACTOR,
                **({"absence_evidence": self.absence} if self.absence is not None else {}),
                "request_id": "resolve-fixture", "snapshot_digest": self.preview()["snapshot_digest"]}

    def no_prior_pr(self):
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET prior_pr_url=NULL WHERE attempt_id=?", (self.aid,))
            self.conn.execute("DELETE FROM factory_inspection_requests")
        self.absence = {"kind": "github_branch_pr_absence_v1", "repository": "Driftward-LLC/capykit",
                        "head_branch": self.job["branch"], "branch_exists": False, "pull_requests": [], "observed_at": store.utc_now()}

    def partial_pr(self, **artifact_changes):
        artifact = {"branch": self.job["branch"], "pr_url": self.url, "commit_sha": "a" * 40, **artifact_changes}
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
            "state": "incomplete", "cancellation_requested": True, "cancellation_acknowledged": False,
            "outcome": {"schema": "factory-executor-outcome-v1", "job_id": self.job["job_id"], "state": "incomplete",
                "reason_code": "hermes_execution_incomplete", "finished_at": "2026-10-10T00:00:00.000Z",
                "process_exit_code": 0, "process_signal": None, "timed_out": False, "buffer_exceeded": False,
                "effect_outcome": "unknown", "containment": "unconfirmed", "artifact": artifact}})
        return artifact

    def test_partial_create_pr_can_be_inspected_and_resolved_without_claiming_success(self):
        self.no_prior_pr()
        self.absence = None
        artifact = self.partial_pr()
        self.assertTrue(recovery.inspect(self.conn, self.aid, actor=ACTOR)["created"])
        self.assertEqual([row[0] for row in self.conn.execute("SELECT requested_action FROM bridge_jobs WHERE job_id!=?",
                         (self.job["job_id"],))], ["inspect_draft_pr_state"])
        with self.assertRaises(store.BridgeConflict): self.preview()
        self.inspection()
        self.assertEqual(self.preview()["context"]["artifact_target"], artifact)
        self.assertIsNone(store.get_factory_attempt(self.conn, self.aid)["prior_pr_url"])
        self.assertEqual(store.get_factory_issue_control(self.conn, "ENG-600")["mode"], "handoff_pending")
        self.test_preview_readonly_and_resolution_preserves_unknown_outcome()
        record = json.loads(self.conn.execute("SELECT evidence_json FROM factory_recovery_requests").fetchone()[0])
        self.assertEqual(record["inspection"]["result"]["url"], self.url)

    def test_partial_pr_still_requires_containment_freshness_and_exact_head(self):
        self.no_prior_pr()
        absence = self.absence
        self.absence = None
        self.partial_pr()
        self.inspection()
        with self.assertRaises(store.BridgeValidationError): self.preview(absence_evidence=absence)
        self.test_incomplete_stale_unbound_and_nonroot_evidence_rejected()
        self.test_inspection_must_be_fresh_after_containment_and_match_branch()
        self.inspection(head_sha="d" * 40)
        with self.assertRaisesRegex(store.BridgeConflict, "branch and head"): self.preview()
        self.inspection()
        command = self.command()
        self.partial_pr(commit_sha="d" * 40)
        with self.assertRaisesRegex(store.BridgeConflict, "observations conflict"):
            recovery.resolve(self.conn, self.aid, **command)

    def test_partial_pr_rejects_conflicting_prior_pr_and_fences_late_writes(self):
        self.partial_pr(pr_url="https://github.com/Driftward-LLC/capykit/pull/601")
        with self.assertRaisesRegex(store.BridgeConflict, "observations conflict"):
            recovery.inspect(self.conn, self.aid, actor=ACTOR)

    def test_partial_pr_late_commits_and_replay_remain_fenced(self):
        self.no_prior_pr()
        self.absence = None
        self.partial_pr()
        self.inspection()
        self.test_late_commit_heartbeat_replay_and_intake_remain_fenced()

    def test_partial_pr_cannot_override_a_completed_receipt(self):
        self.no_prior_pr()
        self.absence = None
        self.partial_pr()
        self.inspection()
        store.record_execution_receipt_observation(self.conn, self.job["job_id"], {
            "state": "completed", "cancellation_requested": True, "cancellation_acknowledged": True})
        with self.assertRaisesRegex(store.BridgeConflict, "receipt recovery"): self.preview()

    def test_no_pr_inspect_uses_ledger_identity_and_does_not_queue_work(self):
        self.no_prior_pr()
        before = self.conn.total_changes
        result = recovery.inspect(self.conn, self.aid, actor=ACTOR)
        self.assertEqual(result, {"created": False, "absence_evidence_target": {
            "repository": "Driftward-LLC/capykit", "head_branch": self.job["branch"]}})
        self.assertEqual(self.conn.total_changes, before)

    def test_no_pr_requires_fresh_bound_absence_after_containment(self):
        self.no_prior_pr()
        invalid = [None, {}, dict(self.absence, repository="other/repo"), dict(self.absence, head_branch="wrong"),
                   dict(self.absence, branch_exists=True), dict(self.absence, branch_exists=0),
                   dict(self.absence, pull_requests=[{"state": "closed"}]), dict(self.absence, extra="field"),
                   dict(self.absence, observed_at="2000-01-01T00:00:00Z"),
                   dict(self.absence, observed_at="2999-01-01T00:00:00Z"),
                   dict(self.absence, observed_at="2026-10-08T00:00:00")]
        for absence in invalid:
            with self.subTest(absence=absence), self.assertRaises((store.BridgeConflict, store.BridgeValidationError)):
                self.preview(absence_evidence=absence)
        with self.assertRaises(store.BridgeConflict):
            self.preview(attestation={**self.evidence, "observed_at": store.utc_now()})
        before = self.conn.total_changes
        result = self.preview()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(result["context"]["inspection"]["result"], self.absence)
        self.assertEqual(result["context"]["inspection"]["source"], "trusted_root_absence_attestation")

    def test_absence_cannot_bypass_known_pr_or_repair_target(self):
        absence = {"kind": "github_branch_pr_absence_v1", "repository": "Driftward-LLC/capykit",
                   "head_branch": self.job["branch"], "branch_exists": False, "pull_requests": [], "observed_at": store.utc_now()}
        with self.assertRaises(store.BridgeValidationError): self.preview(absence_evidence=absence)
        self.no_prior_pr()
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET requested_action='repair_draft_pr' WHERE job_id=?", (self.job["job_id"],))
        with self.assertRaises(store.BridgeConflict): self.preview()

    def test_no_pr_absence_expires_before_the_attestation(self):
        self.no_prior_pr()
        now = datetime.now(timezone.utc)
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET cancel_requested_at=?", ((now - timedelta(seconds=200)).isoformat(),))
        self.evidence["observed_at"] = (now - timedelta(seconds=150)).isoformat()
        self.absence["observed_at"] = (now - timedelta(seconds=130)).isoformat()
        with self.assertRaisesRegex(store.BridgeConflict, "fresh branch and PR absence"):
            self.preview()

    def test_no_pr_resolution_keeps_unknown_failure_hold_and_no_retry(self):
        self.no_prior_pr()
        result = recovery.resolve(self.conn, self.aid, **self.command())
        self.assertEqual(result["control"]["mode"], "manual")
        self.assertEqual(result["effect_outcome"], "unknown")
        job = store.get_job(self.conn, self.job["job_id"])
        self.assertEqual((job["state"], job["reason_code"]), ("failed", "operator_interrupted_outcome_unknown"))
        self.assertIsNone(job["pr_url"])
        self.assertIsNone(job["verification_receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)
        row = self.conn.execute("SELECT * FROM factory_recovery_requests").fetchone()
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["intent"]["absence_evidence"], self.absence)
        self.assertEqual(row["evidence_digest"], store.payload_hash(evidence))

    def test_no_pr_absence_changes_require_new_preview_and_bind_idempotency(self):
        self.no_prior_pr()
        command = self.command()
        newer = {**self.absence, "observed_at": store.utc_now()}
        with self.assertRaisesRegex(store.BridgeConflict, "preview changed"):
            recovery.resolve(self.conn, self.aid, **{**command, "absence_evidence": newer})
        self.test_repeated_command_survives_restart_and_stale_evidence_but_not_changed_intent()
        with self.assertRaises(store.BridgeConflict):
            recovery.resolve(self.conn, self.aid, **{**command, "absence_evidence": newer})

    def test_no_pr_late_commits_and_replay_remain_fenced(self):
        self.no_prior_pr()
        self.test_late_commit_heartbeat_replay_and_intake_remain_fenced()

    def test_no_pr_audit_failure_is_atomic(self):
        self.no_prior_pr()
        self.test_audit_failure_rolls_back_every_state_and_outbox_change()

    def test_no_pr_receipt_resolution_race_has_one_winner(self):
        self.no_prior_pr()
        self.test_receipt_and_resolution_race_has_one_winner()

    def test_no_pr_cannot_override_receipt_or_live_lease(self):
        self.no_prior_pr()
        self.test_known_receipt_or_live_lease_cannot_be_overridden()

    def test_no_pr_http_guards_and_confirm(self):
        self.no_prior_pr()
        self.test_root_http_preview_confirm_and_spoofed_actor_rejection()

    def test_preview_readonly_and_resolution_preserves_unknown_outcome(self):
        before = self.conn.total_changes
        with store.connect_readonly(self.path) as conn:
            result = recovery.preview(conn, self.aid, attestation=self.evidence, reason="reviewed containment", actor=ACTOR)
        conn.close()
        self.assertEqual(before, self.conn.total_changes)
        self.assertIn("not_machine_verified", result["containment_basis"])
        result = recovery.resolve(self.conn, self.aid, **self.command())
        self.assertEqual(result["control"]["mode"], "manual")
        self.assertTrue(result["control"]["automatic_intake_blocked"])
        self.assertEqual(result["effect_outcome"], "unknown")
        job = store.get_job(self.conn, self.job["job_id"])
        self.assertEqual((job["state"], job["reason_code"]), ("failed", "operator_interrupted_outcome_unknown"))
        self.assertIsNone(job["pr_url"])
        self.assertIsNone(job["verification_receipt"])
        lease = self.conn.execute("SELECT * FROM factory_execution_leases").fetchone()
        self.assertIsNotNone(lease["released_at"])
        self.assertIsNone(lease["effect_observed_at"])
        self.assertEqual(lease["fence_token"], self.lease["fence_token"], "keep old authority as historical evidence")
        record = self.conn.execute("SELECT * FROM factory_recovery_requests").fetchone()
        self.assertEqual(record["evidence_digest"], store.payload_hash(json.loads(record["evidence_json"])))
        events = store.list_factory_attempt_events(self.conn, self.aid)
        self.assertEqual(events[-1]["event_type"], recovery.EVENT)
        self.assertTrue(any(e["event_type"] == "effect_authorized_outcome_unknown" for e in events))

    def test_late_commit_heartbeat_replay_and_intake_remain_fenced(self):
        key = f"factory:accepted:{self.aid}"
        delivery = store.claim_factory_outbox_delivery(self.conn, key)
        self.assertIsNotNone(delivery)
        recovery.resolve(self.conn, self.aid, **self.command())
        store.acknowledge_factory_outbox(self.conn, key, delivery["delivery_lease_id"])
        self.assertEqual(self.conn.execute("SELECT state FROM factory_attempt_outbox WHERE outbox_key=?", (key,)).fetchone()[0], "suppressed")
        self.assertFalse(store.heartbeat_factory_execution(self.conn, self.job["job_id"], self.lease))
        self.assertFalse(store.authorize_factory_effect(self.conn, self.job["job_id"], self.lease))
        self.assertFalse(store.claim_factory_execution(self.conn, self.job["job_id"], owner="old")["claimed"])
        with self.assertRaises(store.BridgeConflict):
            store.commit_factory_execution(self.conn, self.job["job_id"], self.lease,
                {"pr_url": self.url, "commit_sha": "a" * 40, "branch": self.job["branch"]})
        self.assertIsNone(store.claim_factory_outbox_delivery(self.conn, f"factory:accepted:{self.aid}"))
        with self.assertRaises(store.BridgeConflict):
            store.transition_job(self.conn, self.job["job_id"], state="running")
        with self.assertRaises(store.BridgeConflict):
            store.reserve_factory_intake(self.conn, work_item_key="ENG-600", mode="real", request_id="late", actor="intake",
                reason="late", payload=verifier_payload("Driftward-LLC/capykit"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)

    def test_repeated_command_survives_restart_and_stale_evidence_but_not_changed_intent(self):
        command = self.command()
        first = recovery.resolve(self.conn, self.aid, **command)
        with store.connect_runtime(self.path) as other:
            with patch.object(recovery, "preview", side_effect=AssertionError("must recover before freshness checks")):
                again = recovery.resolve(other, self.aid, **{**command, "actor": "uid:0;pid:456;account:root"})
        other.close()
        self.assertEqual(again, {**first, "created": False})
        for change in ({"reason": "changed"}, {"snapshot_digest": "d" * 64},
                       {"attestation": {**self.evidence, "workspace_snapshot_sha256": "d" * 64}}):
            with self.assertRaises(store.BridgeConflict):
                recovery.resolve(self.conn, self.aid, **{**command, **change})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_recovery_requests").fetchone()[0], 1)

    def test_incomplete_stale_unbound_and_nonroot_evidence_rejected(self):
        invalid = [dict(self.evidence, observed_at="2000-01-01T00:00:00Z"), dict(self.evidence, observed_at="2999-01-01T00:00:00Z"),
                   dict(self.evidence, observed_at="2026-10-04T00:00:00"), dict(self.evidence, lease_epoch=99),
                   dict(self.evidence, checks={key: False for key in recovery.CHECKS}),
                   dict(self.evidence, containment_method="no_process_visible"), dict(self.evidence, raw_output="not allowed")]
        for evidence in invalid:
            with self.subTest(evidence=evidence), self.assertRaises((store.BridgeConflict, store.BridgeValidationError)):
                self.preview(attestation=evidence)
        with self.assertRaises(store.BridgeValidationError):
            self.preview(actor="uid:1000;pid:123;account:user")
        self.assertEqual(store.get_factory_issue_control(self.conn, "ENG-600")["mode"], "handoff_pending")

    def test_changed_head_or_ownership_rejects_confirmation(self):
        command = self.command()
        self.inspection(head_sha="d" * 40)
        with self.assertRaisesRegex(store.BridgeConflict, "preview changed"):
            recovery.resolve(self.conn, self.aid, **command)
        command = self.command()
        with self.conn:
            self.conn.execute("UPDATE factory_issue_controls SET revision=revision+1")
        with self.assertRaisesRegex(store.BridgeConflict, "preview changed"):
            recovery.resolve(self.conn, self.aid, **command)

    def test_inspection_must_be_fresh_after_containment_and_match_branch(self):
        with self.conn:
            self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2000-01-01T00:00:00Z'")
        with self.assertRaises(store.BridgeConflict): self.preview()
        self.inspection(head_branch="another-branch")
        with self.assertRaises(store.BridgeConflict): self.preview()
        self.inspection()
        evidence = {**self.evidence, "observed_at": store.utc_now()}
        with self.assertRaises(store.BridgeConflict): self.preview(attestation=evidence)

    def test_known_receipt_or_live_lease_cannot_be_overridden(self):
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2999-01-01T00:00:00Z'")
        with self.assertRaises(store.BridgeConflict): self.preview()
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00Z',effect_observed_at=?", (store.utc_now(),))
        with self.assertRaisesRegex(store.BridgeConflict, "receipt recovery"): self.preview()

    def test_inspection_refresh_only_queues_typed_read_and_never_an_attempt(self):
        result = recovery.inspect(self.conn, self.aid, actor=ACTOR)
        self.assertTrue(result["created"])
        self.assertFalse(recovery.inspect(self.conn, self.aid, actor=ACTOR)["created"])
        jobs = [dict(row) for row in self.conn.execute("SELECT requested_action FROM bridge_jobs WHERE job_id!=?", (self.job["job_id"],))]
        self.assertEqual(jobs, [{"requested_action": "inspect_draft_pr_state"}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)
        with self.assertRaises(store.BridgeConflict): self.preview()

    def test_other_unresolved_execution_still_blocks_manual_editing(self):
        other, other_job, _ = store.reserve_factory_retry(self.conn, work_item_key="ENG-601", mode="real", request_id="other",
            actor="test", reason="test", payload={**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-601"})
        store.transition_job(self.conn, other_job["job_id"], state="running")
        store.claim_factory_execution(self.conn, other_job["job_id"], owner="other-executor")
        # Fixture for multiple historical unresolved attempts on one issue.
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET work_item_key='ENG-600',generation=2 WHERE attempt_id=?", (other["attempt_id"],))
        result = recovery.resolve(self.conn, self.aid, **self.command())
        self.assertEqual(result["control"]["mode"], "handoff_pending")
        self.assertEqual(result["control"]["blockers"][0]["attempt_id"], other["attempt_id"])

    def test_audit_failure_rolls_back_every_state_and_outbox_change(self):
        command = self.command()
        with patch.object(store, "_append_factory_attempt_event", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError): recovery.resolve(self.conn, self.aid, **command)
        self.assertEqual(store.get_job(self.conn, self.job["job_id"])["state"], "running")
        self.assertIsNone(self.conn.execute("SELECT released_at FROM factory_execution_leases").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_recovery_requests").fetchone()[0], 0)
        recovery.resolve(self.conn, self.aid, **command)

    def test_receipt_and_resolution_race_has_one_winner(self):
        command = self.command()
        gate = threading.Barrier(2)
        def run(receipt):
            conn = store.connect_runtime(self.path)
            try:
                gate.wait(timeout=5)
                try:
                    if receipt:
                        store.commit_factory_execution(conn, self.job["job_id"], self.lease,
                            {"pr_url": self.url, "commit_sha": "a" * 40, "branch": self.job["branch"]})
                    else:
                        recovery.resolve(conn, self.aid, **command)
                    return True
                except store.BridgeConflict: return False
            finally: conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, True), pool.submit(run, False)
            self.assertEqual(sum((a.result(), b.result())), 1)
        self.assertEqual(store.get_factory_issue_control(self.conn, "ENG-600")["mode"], "manual")

    def test_root_http_preview_confirm_and_spoofed_actor_rejection(self):
        class Handler:
            do_POST = OperatorHandler.do_POST
            def _auth(self): return True
            def _paths(inner): return self.path, "unused", "unused", "unused"
            def _body(inner): return inner.body
            def _actor(inner): return inner.actor
            def _reply(inner, status, value): inner.response = (status, value)
        h = Handler(); h.actor = ACTOR
        h.path = f"/v1/attempts/{self.aid}/interrupted-resolution/preview"
        h.body = {"attestation": self.evidence, "reason": "reviewed containment"}
        if self.absence is not None:
            h.body["absence_evidence"] = None
            h.do_POST(); self.assertEqual(h.response[0], 400)
            h.body["absence_evidence"] = self.absence
        h.do_POST(); self.assertEqual(h.response[0], 200)
        digest = h.response[1]["snapshot_digest"]
        h.body["actor"] = ACTOR
        h.do_POST(); self.assertEqual(h.response[0], 400)
        del h.body["actor"]
        h.actor = "uid:1000;pid:123;account:user"
        h.do_POST(); self.assertEqual(h.response[0], 400)
        h.actor = ACTOR; h.path = h.path.replace("/preview", "/confirm")
        h.body.update(request_id="http-confirm", snapshot_digest=digest)
        h.do_POST(); self.assertEqual(h.response[0], 200)
        self.assertEqual(h.response[1]["control"]["mode"], "manual")


if __name__ == "__main__":
    unittest.main()
