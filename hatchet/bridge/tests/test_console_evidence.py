import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import console, console_evidence, store, work_evidence
from test_store import original_context, verification_receipt, verifier_payload
from test_support import connect_migrate


class ConsoleEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json")})
        env.start()
        self.addCleanup(env.stop)
        self.attempt, self.job, _ = store.reserve_factory_intake(
            self.conn, work_item_key="ENG-801", mode="real", request_id="console-evidence",
            reason="fixture", actor="test", payload={**verifier_payload("Driftward-LLC/capykit"), "implementation_prompt": "private requirements"},
        )

    def evidence(self):
        return console.lifecycle(self.path, "ENG-801")["verification_evidence"]

    def commit(self):
        store.transition_job(self.conn, self.job["job_id"], state="running")
        lease = store.claim_factory_execution(self.conn, self.job["job_id"], owner="executor")["lease"]
        store.authorize_factory_effect(self.conn, self.job["job_id"], lease)
        store.commit_factory_execution(self.conn, self.job["job_id"], lease, {
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/801",
            "commit_sha": "a" * 40, "branch": self.job["branch"],
        })

    def verify(self, *, passed=True):
        pending = store.transition_job(self.conn, self.job["job_id"], state="verification_pending", details={"verification_context": original_context(self.job)})
        store.acquire_verification_lease(self.conn, self.job["job_id"], attempt_id="verifier")
        store.claim_verification_lease(self.conn, self.job["job_id"], attempt_id="verifier")
        details = {"verification_attempt_id": "verifier", "reason_code": "verification_github_check_failed"}
        if passed:
            receipt = verification_receipt(pending, "a" * 40)
            store.record_verification_observation(self.conn, self.job["job_id"], attempt_id="verifier", receipt=receipt)
            details.update(reason_code="draft_pr_verified", verification_receipt=receipt)
        store.transition_job(self.conn, self.job["job_id"], state="ready_for_human_review" if passed else "failed", details=details, _factory_verifier_completion=True)

    def inspect(self, sha="a" * 40, *, state="open", merged=False):
        request = self.conn.execute("SELECT * FROM factory_inspection_requests ORDER BY created_at DESC LIMIT 1").fetchone()
        if request is None:
            with self.conn:
                store._queue_pr_inspection(self.conn, "ENG-801", "https://github.com/Driftward-LLC/capykit/pull/801", self.attempt["attempt_id"])
            request = self.conn.execute("SELECT * FROM factory_inspection_requests ORDER BY created_at DESC LIMIT 1").fetchone()
        return store.complete_pr_inspection(self.conn, request["job_id"], {
            "repository": "Driftward-LLC/capykit", "number": 801,
            "url": "https://github.com/Driftward-LLC/capykit/pull/801", "state": state,
            "merged": merged, "draft": True, "head_branch": self.job["branch"],
            "head_sha": sha, "base_ref": "main", "reason": "private inspection details",
        }, request_key=request["request_key"], capability=request["capability"])

    def test_admission_and_missing_receipts_are_explicit(self):
        evidence = self.evidence()
        self.assertEqual(evidence["schema"], "factory-console-verification-v1")
        self.assertEqual(evidence["attempt_id"], self.attempt["attempt_id"])
        self.assertEqual(evidence["specification"]["result"], "recorded")
        self.assertEqual(evidence["verification"]["result"], "unavailable")
        self.assertEqual(evidence["head_comparison"], "unavailable")
        self.assertEqual(evidence["independent_review"], "unavailable")
        self.assertIsNone(console.lifecycle(self.path, "ENG-999")["verification_evidence"])

    def test_matching_head_is_only_a_readonly_observation(self):
        self.commit()
        self.verify()
        self.inspect()
        before = self.conn.total_changes
        with patch.object(console, "connect_readonly", wraps=store.connect_readonly) as opened:
            evidence = self.evidence()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(evidence["head_comparison"], "same_head")
        self.assertEqual(evidence["verification"]["commit_sha"], "a" * 40)
        self.assertEqual(evidence["verification"]["local_evidence_source"], "executor_reported")
        self.assertEqual(evidence["independent_review"], "unavailable")
        for private in ("private requirements", "private inspection details", "capability", "required_local_commands", "hermes_session_id", "route"):
            self.assertNotIn(private, json.dumps(evidence))

    def test_changed_head_does_not_mutate_historical_verification(self):
        self.commit()
        self.verify()
        self.inspect("b" * 40)
        before = store.get_factory_work_evidence(self.conn, self.attempt["attempt_id"])
        evidence = self.evidence()
        self.assertEqual(evidence["head_comparison"], "head_changed")
        self.assertEqual(evidence["inspection"]["head_sha"], "b" * 40)
        self.assertEqual(evidence["verification"]["result"], "passed")
        self.assertEqual(store.get_factory_work_evidence(self.conn, self.attempt["attempt_id"]), before)
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "verified_ready")

    def test_older_inspection_cannot_claim_drift_after_verification(self):
        self.commit()
        self.inspect("b" * 40)
        self.verify()
        self.assertEqual(self.evidence()["head_comparison"], "inspection_predates_verification")

    def test_stale_and_pending_inspections_do_not_look_live(self):
        self.commit()
        self.verify()
        self.assertEqual(self.evidence()["inspection"]["status"], "pending")
        inspection = self.inspect()
        with patch.object(console_evidence, "get_pr_inspection", return_value={**inspection, "fresh": False}):
            evidence = self.evidence()
        self.assertEqual(evidence["inspection"]["status"], "stale")
        self.assertEqual(evidence["head_comparison"], "same_head")
        self.conn.execute("UPDATE factory_inspection_requests SET state='expired', result_json=NULL, result_digest=NULL, observed_at=NULL")
        self.conn.commit()
        self.assertEqual(self.evidence()["inspection"]["status"], "expired")
        self.assertEqual(self.evidence()["head_comparison"], "unavailable")

    def test_short_or_unknown_head_is_not_compared(self):
        self.commit()
        self.verify()
        inspection = self.inspect("abcdef0")
        self.assertIsNone(self.evidence()["inspection"]["head_sha"])
        self.assertEqual(self.evidence()["head_comparison"], "unavailable")
        with patch.object(console_evidence, "get_pr_inspection", return_value={**inspection, "result": {**inspection["result"], "head_sha": "a" * 40, "state": "unknown"}}):
            self.assertEqual(self.evidence()["head_comparison"], "unavailable")

    def test_invalid_inspection_is_bounded_and_does_not_hide_checkpoint(self):
        self.commit()
        self.verify()
        inspection = self.inspect()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        cases = [
            {**inspection, "observed_at": future},
            {**inspection, "observed_at": "2026-01-01T00:00:00"},
            {**inspection, "result": {**inspection["result"], "repository": "attacker/repo"}},
            {**inspection, "result": {**inspection["result"], "url": "https://github.com/attacker/repo/pull/801"}},
        ]
        for value in cases:
            with self.subTest(value=value), patch.object(console_evidence, "get_pr_inspection", return_value=value):
                evidence = self.evidence()
            self.assertEqual(evidence["inspection"]["status"], "invalid")
            self.assertEqual(evidence["head_comparison"], "unavailable")
            self.assertEqual(evidence["verification"]["result"], "passed")
        self.conn.execute("UPDATE factory_inspection_requests SET result_digest=?", ("b" * 64,))
        self.conn.commit()
        self.assertEqual(self.evidence()["inspection"]["status"], "invalid")

    def test_checkpoint_projection_rejects_invalid_or_future_contracts(self):
        self.commit()
        self.verify()
        evidence = store.get_factory_work_evidence(self.conn, self.attempt["attempt_id"])
        row = next(row for row in evidence["checkpoints"] if row["checkpoint_type"] == "verification_receipt")
        for changes in ({"contract_version": 2}, {"document_digest": "b" * 64}, {"created_at": "not-a-date"}):
            self.assertEqual(console_evidence._checkpoint([{**row, **changes}], "verification_receipt")["result"], "invalid")
        for changes in ({"checks_passed": True}, {"commit_sha": "abcdef0"}, {"schema": "unknown"}, {"result": []}):
            document = {**row["document"], **changes}
            changed = {**row, "document": document, "document_digest": work_evidence.digest(document)}
            self.assertEqual(console_evidence._checkpoint([changed], "verification_receipt")["result"], "invalid")
        document = {**row["document"], "private": "do not export"}
        changed = {**row, "document": document, "document_digest": work_evidence.digest(document)}
        self.assertNotIn("private", console_evidence._checkpoint([changed], "verification_receipt"))

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_repair_does_not_inherit_preceding_attempts_pass_or_failure(self):
        self.commit()
        self.verify(passed=False)
        report = console.lifecycle(self.path, "ENG-801")
        self.assertNotEqual(report["verification_evidence"]["attempt_id"], self.attempt["attempt_id"])
        self.assertEqual(report["verification_evidence"]["verification"]["result"], "unavailable")
        prior = console_evidence.attempt_evidence(self.conn, report["attempts"][1])
        self.assertEqual(prior["verification"]["result"], "failed")
        self.assertEqual(prior["head_comparison"], "unavailable")

    def test_dry_run_and_legacy_without_checkpoints_never_look_verified(self):
        store.reserve_factory_intake(self.conn, work_item_key="ENG-802", mode="dry-run", request_id="dry", reason="test", actor="test", payload={"repo": "Driftward-LLC/capykit"})
        dry = console.lifecycle(self.path, "ENG-802")["verification_evidence"]
        self.assertEqual(dry["verification"]["result"], "not_applicable")
        self.assertEqual(dry["inspection"]["status"], "unavailable")
        store.reserve_factory_attempt(self.conn, work_item_key="ENG-803", mode="real", request_id="legacy", reason="test", actor="test")
        legacy = console.lifecycle(self.path, "ENG-803")["verification_evidence"]
        self.assertFalse(legacy["available"])
        self.assertEqual(legacy["verification"]["result"], "unavailable")


if __name__ == "__main__":
    unittest.main()
