import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import console, console_evidence, independent_review, store, work_evidence
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
        self.assertEqual(evidence["independent_review"]["status"], "not_requested")
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
        self.assertEqual(evidence["independent_review"]["status"], "not_requested")
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

    def review_record(self, state="pending", *, receipt=None, legacy=False):
        created = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        requirements = {"title": "Fixture", "description": "private requirements"}
        request = {"review_id": "fictional-review", "repository": "Driftward-LLC/capykit",
                   "pr_url": "https://github.com/Driftward-LLC/capykit/pull/801", "head_sha": "a" * 40,
                   "branch": self.job["branch"], "policy_digest": "b" * 64, "pass_number": 1,
                   "expected_base": "main", "requirements": requirements,
                   "requirements_digest": independent_review.requirements_digest(requirements)}
        if legacy:
            request["generator_session_id"] = "fictional-generator-session"
        else:
            request["generator_execution_id"] = f"factory-execution:{self.attempt['attempt_id']}:{self.job['job_id']}"
        review_job, _ = store.submit_job(self.conn, {"requested_action": "read_only_review", "idempotency_key": "fictional-review",
            "payload": {"repo": request["repository"], "review": request, "review_capability": "private-capability-not-for-console", "dry_run": False}}, internal_review=True)
        self.conn.execute("""INSERT INTO factory_independent_reviews
            (review_id,lineage_key,pass_number,source_attempt_id,source_job_id,job_id,repository,pr_url,
             head_sha,policy_digest,capability,state,result_json,result_digest,reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                "fictional-review", "fictional-lineage", 1, self.attempt["attempt_id"], self.job["job_id"], review_job["job_id"],
                "Driftward-LLC/capykit", "https://github.com/Driftward-LLC/capykit/pull/801", "a" * 40, "b" * 64,
                "private-capability-not-for-console", state, store.canonical_json(receipt) if receipt else None,
                store.payload_hash(receipt) if receipt else None, "review_inconclusive" if state == "needs_operator" else None,
                created, store.utc_now()))
        self.conn.commit()

    def review_receipt(self, verdict="passed"):
        return {"schema": "factory-independent-review-v1", "review_id": "fictional-review", "repository": "Driftward-LLC/capykit",
                "pr_url": "https://github.com/Driftward-LLC/capykit/pull/801", "head_sha": "a" * 40, "verdict": verdict,
                "reviewer_model": "gpt-6-astra", "reviewer_session_id": "fictional-review-session", "backend": "isolated_codex",
                "isolation": {"separate_session": True, "isolated_checkout": True, "isolated_browser": True, "write_credentials": False},
                "reason": "ok", "findings": [] if verdict == "passed" else [{"priority": 1, "file": "src/example.ts", "line": 7,
                                                                              "title": "Fictional finding", "body": "Bounded fixture evidence."}]}

    def test_review_pending_running_and_missing_receipt_are_distinct_and_readonly(self):
        self.commit()
        self.verify()
        self.review_record()
        before = self.conn.total_changes
        for state, expected in (("pending", "pending"), ("running", "running"), ("awaiting_inspection", "pending"), ("passed", "unavailable"), ("needs_operator", "needs_operator")):
            self.conn.execute("UPDATE factory_independent_reviews SET state=?", (state,))
            self.conn.commit()
            report = console.lifecycle(self.path, "ENG-801")
            review = report["verification_evidence"]["independent_review"]
            self.assertEqual(review["status"], expected)
            self.assertEqual(review["pass_count"], 1)
            self.assertEqual(review["max_passes"], 2)
            self.assertEqual(report["attempts"][0]["independent_review"], review)
            self.assertEqual(console.overview(self.path, "fixture")["recent_attempts"][0]["independent_review"], review)
        self.assertEqual(self.conn.total_changes, before + 5)
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "verified_ready")

    def test_review_work_supplements_terminal_attempt_outside_recent_without_changing_execution_counts(self):
        self.commit()
        self.verify()
        self.review_record()
        for index in range(31):
            attempt, _ = store.reserve_factory_attempt(self.conn, work_item_key=f"ENG-{900 + index}", mode="real",
                                                      request_id=f"recent-{index}", reason="fixture", actor="test")
            with self.conn:
                self.conn.execute("UPDATE factory_attempts SET status='completed' WHERE attempt_id=?", (attempt["attempt_id"],))
        self.conn.execute("UPDATE factory_attempts SET updated_at='2000-01-01T00:00:00Z' WHERE attempt_id=?", (self.attempt["attempt_id"],))
        self.conn.commit()
        for state in ("pending", "running", "changes_requested", "needs_operator"):
            self.conn.execute("UPDATE factory_independent_reviews SET state=?", (state,))
            self.conn.commit()
            report = console.overview(self.path, "fixture")
            self.assertEqual(report["active_attempts"], [])
            self.assertEqual(report["status_counts"], {"completed": 31, "verified_ready": 1})
            self.assertNotIn(self.attempt["attempt_id"], [row["attempt"]["attempt_id"] for row in report["recent_attempts"]])
            self.assertEqual([row["attempt"]["attempt_id"] for row in report["review_attempts"]], [self.attempt["attempt_id"]])
            self.assertTrue(report["review_scope"]["available"])
            self.assertEqual(report["review_scope"]["limit"], 100)
            self.assertFalse(report["review_scope"]["unfinished_truncated"])

    def test_recorded_passed_review_with_changed_head_is_in_review_work(self):
        self.commit()
        self.verify()
        self.review_record("passed", receipt=self.review_receipt())
        self.inspect("b" * 40)
        report = console.overview(self.path, "fixture")
        self.assertEqual(report["active_attempts"], [])
        self.assertEqual(len(report["review_attempts"]), 1)
        self.assertEqual(report["review_attempts"][0]["independent_review"]["status"], "stale")
        self.assertEqual(report["review_attempts"][0]["attempt"]["status"], "verified_ready")

    def test_review_pass_is_exact_head_and_never_exports_capability_or_full_receipt(self):
        self.commit()
        self.verify()
        self.review_record("passed", receipt=self.review_receipt())
        self.inspect()
        evidence = self.evidence()
        review = evidence["independent_review"]
        self.assertEqual(review["status"], "passed")
        self.assertEqual(review["reviewed_sha"], "a" * 40)
        self.assertEqual(review["reviewer_model"], "gpt-6-astra")
        self.assertEqual(review["reviewer_session_id"], "fictional-review-session")
        for private in ("capability", "private-capability", "isolation", "generator_session", "private requirements"):
            self.assertNotIn(private, json.dumps(review))
        inspection = store.get_pr_inspection(self.conn, "Driftward-LLC/capykit", "https://github.com/Driftward-LLC/capykit/pull/801")
        with patch.object(console_evidence, "get_pr_inspection", return_value={**inspection, "result": {**inspection["result"], "head_sha": "b" * 40}}):
            self.assertEqual(self.evidence()["independent_review"]["status"], "stale")
        self.assertEqual(self.evidence()["verification"]["result"], "passed")

    def test_legacy_review_uses_stored_request_even_with_null_source_session(self):
        self.commit()
        self.verify()
        self.review_record("passed", receipt=self.review_receipt(), legacy=True)
        self.inspect()
        self.assertIsNone(store.get_job(self.conn, self.job["job_id"])["hermes_session_id"])
        self.assertEqual(self.evidence()["independent_review"]["status"], "passed")

    def test_legacy_review_same_session_receipt_remains_unavailable(self):
        self.commit()
        self.verify()
        receipt = {**self.review_receipt(), "reviewer_session_id": "fictional-generator-session"}
        self.review_record("passed", receipt=receipt, legacy=True)
        self.inspect()
        self.assertEqual(self.evidence()["independent_review"]["status"], "unavailable")

    def test_malformed_stored_review_request_is_unavailable_not_an_exception(self):
        self.commit()
        self.verify()
        self.review_record("passed", receipt=self.review_receipt())
        self.inspect()
        row = self.conn.execute("SELECT job_id FROM factory_independent_reviews").fetchone()
        review_job = store.get_job(self.conn, row["job_id"])
        request = review_job["payload"]["review"]
        for malformed in (None, [], "invalid", {**request, "generator_session_id": "legacy"},
                          {**request, "generator_execution_id": "factory-execution:" + "a" * 73}):
            with self.subTest(request=malformed), patch.object(console_evidence, "get_job", return_value={
                **review_job, "payload": {**review_job["payload"], "review": malformed}}):
                self.assertEqual(self.evidence()["independent_review"]["status"], "unavailable")

    def test_changes_requested_findings_are_bounded_and_inconsistent_receipt_is_unavailable(self):
        self.commit()
        self.verify()
        self.review_record("changes_requested", receipt=self.review_receipt("changes_requested"))
        review = self.evidence()["independent_review"]
        self.assertEqual(review["status"], "changes_requested")
        self.assertEqual(review["findings"][0]["priority"], 1)
        self.conn.execute("UPDATE factory_independent_reviews SET state='passed'")
        self.conn.commit()
        self.assertEqual(self.evidence()["independent_review"]["status"], "unavailable")


class ConsoleReviewLineageTests(unittest.TestCase):
    def reviewed_repair(self):
        from test_independent_review import IndependentReviewTests
        from hatchet_hermes_bridge import independent_review as reviews
        case = IndependentReviewTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        first = case.reserve()
        case.complete(first, "changes_requested")
        self.assertEqual(reviews.sweep(case.conn)["repairs"], 1)
        repair_id = case.conn.execute("SELECT repair_attempt_id FROM factory_independent_reviews WHERE review_id=?", (first["review_id"],)).fetchone()[0]
        repair = store.get_factory_attempt(case.conn, repair_id)
        return case, first, repair, store.get_job(case.conn, repair["job_id"])

    def finish_repair(self, case, repair, job, sha="b" * 40):
        case.finish(job, sha=sha)
        case.intent.update(source_attempt_id=repair["attempt_id"], reviewed_head_sha=sha)
        case.inspect()

    def test_reserved_repair_supersedes_prior_findings_in_recent_and_review_queues(self):
        case, first, repair, job = self.reviewed_repair()
        report = console.overview(case.db, "fixture")
        self.assertEqual([row["attempt"]["attempt_id"] for row in report["active_attempts"]], [repair["attempt_id"]])
        self.assertEqual(report["review_attempts"], [])
        prior = next(row for row in report["recent_attempts"] if row["attempt"]["attempt_id"] == first["source_attempt_id"])["independent_review"]
        self.assertEqual(prior["status"], "superseded")
        self.assertEqual(prior["historical_status"], "changes_requested")
        self.assertTrue(prior["findings"])
        self.assertEqual(prior["reviewed_sha"], "a" * 40)

    def test_second_review_pending_passed_and_operator_stop_only_queue_current_review(self):
        from hatchet_hermes_bridge import independent_review as reviews
        for verdict in ("passed", "changes_requested", "inconclusive"):
            with self.subTest(verdict=verdict):
                case, first, repair, job = self.reviewed_repair()
                self.finish_repair(case, repair, job)
                second = reviews.reserve(case.conn, job["job_id"])
                report = console.overview(case.db, "fixture")
                self.assertEqual([(row["attempt"]["attempt_id"], row["independent_review"]["status"]) for row in report["review_attempts"]], [(repair["attempt_id"], "pending")])
                case.complete(second, verdict)
                report = console.overview(case.db, "fixture")
                self.assertEqual(report["active_attempts"], [])
                expected = [] if verdict == "passed" else [(repair["attempt_id"], "needs_operator" if verdict == "inconclusive" else "changes_requested")]
                self.assertEqual([(row["attempt"]["attempt_id"], row["independent_review"]["status"]) for row in report["review_attempts"]], expected)
                prior = next(row for row in report["recent_attempts"] if row["attempt"]["attempt_id"] == first["source_attempt_id"])["independent_review"]
                self.assertEqual(prior["status"], "superseded")
                self.assertTrue(prior["findings"])
                self.assertEqual(case.conn.execute("SELECT state FROM factory_independent_reviews WHERE review_id=?", (first["review_id"],)).fetchone()[0], "changes_requested")

    def test_unchanged_repair_inherits_operator_stop_without_approval_or_second_pass(self):
        from hatchet_hermes_bridge import independent_review as reviews
        case, first, repair, job = self.reviewed_repair()
        self.finish_repair(case, repair, job, "a" * 40)
        self.assertIsNone(reviews.reserve(case.conn, job["job_id"]))
        report = console.overview(case.db, "fixture")
        self.assertEqual([(row["attempt"]["attempt_id"], row["independent_review"]["status"]) for row in report["review_attempts"]], [(repair["attempt_id"], "needs_operator")])
        lifecycle = console.lifecycle(case.db, "ENG-8")
        inherited = lifecycle["attempts"][0]["independent_review"]
        self.assertEqual(inherited["status"], "needs_operator")
        self.assertEqual(inherited["inherited_from_attempt_id"], first["source_attempt_id"])
        self.assertEqual(inherited["pass_count"], 1)
        self.assertTrue(inherited["findings"])
        self.assertEqual(lifecycle["attempts"][1]["independent_review"]["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
