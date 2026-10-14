import copy
import json
import sqlite3
import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import MagicMock

from hatchet_hermes_bridge import independent_review as reviews, store
import test_review_repair as fixtures
REPO = fixtures.REPO


class IndependentReviewTests(unittest.TestCase):
    observe = fixtures.ReviewRepairTests.observe
    finish = fixtures.ReviewRepairTests.finish
    inspect = fixtures.ReviewRepairTests.inspect

    def setUp(self):
        payload = fixtures.verifier_payload(REPO)
        payload["instructions"] = "Reject stale observations and keep the draft PR unchanged."
        with patch.object(fixtures, "verifier_payload", return_value=payload):
            fixtures.ReviewRepairTests.setUp(self)
        self.config = {"schema": "factory-independent-review-policy-v1", "enabled": True,
                       "automatic_repairs_allowed": True, "max_passes": 2, "repositories": [REPO]}
        self.config["digest"] = store.payload_hash(self.config)
        self.policy_patch = patch.object(reviews, "policy", return_value=self.config)
        self.policy_patch.start(); self.addCleanup(self.policy_patch.stop)
        payload = self.job["payload"]
        payload["instructions"] = "Reject stale observations and keep the draft PR unchanged."
        payload["independent_review_enrollment"] = {"policy_digest": self.config["digest"]}
        self.conn.execute("UPDATE bridge_jobs SET payload_json=?,hermes_session_id='generator-session' WHERE job_id=?",
                          (store.canonical_json(payload), self.job["job_id"]))
        self.conn.commit()
        self.inspect()

    def reserve(self):
        return reviews.reserve(self.conn, self.intent["source_attempt_id"] and self.job["job_id"])

    def receipt(self, row, verdict="passed"):
        return {"schema": "factory-independent-review-v1", "review_id": row["review_id"], "repository": REPO,
                "pr_url": row["pr_url"], "head_sha": row["head_sha"], "verdict": verdict,
                "reviewer_model": "gpt-6-astra", "reviewer_session_id": f"reviewer-{row['pass_number']}",
                "backend": "isolated_codex", "isolation": {"separate_session": True, "isolated_checkout": True,
                "isolated_browser": True, "write_credentials": False}, "reason": "ok",
                "findings": [] if verdict == "passed" else [{"priority": 1, "file": "src/main.ts", "line": 12,
                    "title": "Reject changed heads", "body": "Recheck the exact remote commit before updating."}]}

    def complete(self, row, verdict="passed", changes=None):
        self.assertTrue(reviews.claim(self.conn, row["job_id"], row["capability"]))
        result = {**self.receipt(row, verdict), **(changes or {})}
        output = reviews.complete(self.conn, row["job_id"], row["capability"], result)
        self.inspect()
        reviews._settle_receipt(self.conn, row["review_id"])
        return output

    def test_default_policy_does_not_enroll_historical_verified_rows(self):
        self.conn.execute("UPDATE bridge_jobs SET payload_json=json_remove(payload_json,'$.independent_review_enrollment') WHERE job_id=?", (self.job["job_id"],)); self.conn.commit()
        self.assertIsNone(self.reserve())
        self.assertEqual(reviews.sweep(self.conn), {"reserved": 0, "repairs": 0})

    def test_reservation_outbox_and_exact_head_pass_preserve_verified_ready(self):
        row = self.reserve()
        self.assertEqual(self.reserve()["review_id"], row["review_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 1)
        outbox = store.list_factory_inspection_outbox(self.conn)
        self.assertTrue(any(item["job_id"] == row["job_id"] for item in outbox))
        self.complete(row)
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "review_passed")
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "verified_ready")
        self.assertEqual(store.get_job(self.conn, self.job["job_id"])["state"], "ready_for_human_review")
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.assertEqual(reviews.complete(self.conn, row["job_id"], row["capability"], self.receipt(row))["state"], "review_passed")

    def test_concurrent_reservations_converge_and_delete_replace_cannot_reset_budget(self):
        def reserve(_):
            conn = store.connect_runtime(self.db)
            try: return reviews.reserve(conn, self.job["job_id"])
            finally: conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(reserve, range(2)))
        self.assertEqual(rows[0]["review_id"], rows[1]["review_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM factory_independent_reviews"); self.conn.commit()
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT OR REPLACE INTO factory_independent_reviews SELECT * FROM factory_independent_reviews"); self.conn.commit()
        self.conn.rollback()

    def test_long_review_waits_for_fresh_inspection_without_reinvocation(self):
        row = self.reserve()
        self.assertTrue(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.assertEqual(reviews.complete(self.conn, row["job_id"], row["capability"], self.receipt(row))["state"], "review_pending")
        self.assertFalse(reviews._settle_receipt(self.conn, row["review_id"]))
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.inspect(); self.assertTrue(reviews._settle_receipt(self.conn, row["review_id"]))

    def test_wrong_capability_and_model_or_session_never_pass(self):
        row = self.reserve()
        with self.assertRaises(store.BridgeValidationError): reviews.claim(self.conn, row["job_id"], "not-the-reserved-capability")
        for changes in ({"reviewer_model": "gpt-6.1-sol"}, {"reviewer_session_id": "generator-session"}, {"head_sha": "b" * 40}):
            request = store.get_job(self.conn, row["job_id"])["payload"]["review"]
            with self.assertRaises(store.BridgeValidationError): reviews._receipt({**self.receipt(row), **changes}, request)
        self.complete(row, changes={"reviewer_model": "gpt-6.1-sol"})
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "needs_operator")

    def test_changes_repair_second_pass_and_budget_survive_new_head(self):
        row = self.reserve(); self.complete(row, "changes_requested")
        self.assertEqual(reviews.sweep(self.conn)["repairs"], 1)
        repair = self.conn.execute("SELECT repair_attempt_id FROM factory_independent_reviews").fetchone()[0]
        attempt = store.get_factory_attempt(self.conn, repair)
        job = store.get_job(self.conn, attempt["job_id"])
        self.assertNotIn("approval", job["payload"]["factory_review_repair"])
        self.assertEqual(job["payload"]["factory_artifact_fence"]["kind"], "policy_review_repair_v1")
        self.finish(job, sha="b" * 40)
        self.conn.execute("UPDATE bridge_jobs SET hermes_session_id='repair-session' WHERE job_id=?", (job["job_id"],)); self.conn.commit()
        self.intent.update(source_attempt_id=repair, reviewed_head_sha="b" * 40)
        self.inspect()
        second = reviews.reserve(self.conn, job["job_id"])
        self.assertEqual(second["pass_number"], 2)
        self.complete(second, "changes_requested")
        projected = reviews.projection(self.conn, job["job_id"])
        self.assertEqual((projected["state"], projected["reason"], projected["passes_used"]), ("changes_requested", "review_budget_exhausted", 2))
        self.assertEqual(reviews.sweep(self.conn)["repairs"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 2)

    def test_head_drift_manual_hold_and_ambiguous_restart_fail_closed(self):
        row = self.reserve()
        self.assertTrue(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))
        reviews.complete(self.conn, row["job_id"], row["capability"], self.receipt(row))
        self.inspect(head_sha="b" * 40)
        reviews._settle_receipt(self.conn, row["review_id"])
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["reason"], "review_head_changed")

    def test_unavailable_backend_preserves_reason_and_uses_no_repair(self):
        row = self.reserve()
        self.complete(row, "inconclusive", {"findings": [], "reason": "review_backend_unavailable",
            "isolation": {"separate_session": False, "isolated_checkout": False, "isolated_browser": False, "write_credentials": False}})
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["reason"], "review_backend_unavailable")
        self.assertEqual(reviews.sweep(self.conn)["repairs"], 0)

    def test_policy_revocation_stops_reserved_repair_effect(self):
        row = self.reserve(); self.complete(row, "changes_requested"); reviews.sweep(self.conn)
        attempt = store.get_factory_attempt(self.conn, self.conn.execute("SELECT repair_attempt_id FROM factory_independent_reviews").fetchone()[0])
        job = store.get_job(self.conn, attempt["job_id"])
        store.transition_job(self.conn, job["job_id"], state="running")
        claim = store.claim_factory_execution(self.conn, job["job_id"], owner="fixture-executor")["lease"]
        self.config["enabled"] = False
        self.assertFalse(store.authorize_factory_effect(self.conn, job["job_id"], claim))

    def test_outbox_docker_repair_health_uses_database_and_ingress_http(self):
        from hatchet_hermes_bridge import operator
        row = self.reserve(); self.complete(row, "changes_requested")
        response = MagicMock(); response.__enter__.return_value.status = 200
        with patch.dict(os.environ, {"HATCHET_FACTORY_OPERATOR_TEST_HEALTH": "", "HATCHET_FACTORY_OPERATOR_RUNTIME": "docker",
            "HATCHET_EVENT_INGRESS_URL": "http://event-ingress:19878"}), patch.object(operator, "urlopen", return_value=response) as health:
            self.assertEqual(reviews.sweep(self.conn)["repairs"], 1)
            health.assert_called_once_with("http://event-ingress:19878/health", timeout=3)

    def test_missing_requirements_is_durable_operator_state_without_dispatch(self):
        self.conn.execute("UPDATE bridge_jobs SET payload_json=json_remove(payload_json,'$.instructions') WHERE job_id=?", (self.job["job_id"],)); self.conn.commit()
        row = self.reserve()
        self.assertEqual(row["state"], "needs_operator")
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))

    def test_schema_upgrade_preserves_reserved_pass_and_result(self):
        row = self.reserve(); self.complete(row)
        before = dict(self.conn.execute("SELECT * FROM factory_independent_reviews").fetchone())
        self.conn.execute("UPDATE bridge_schema_meta SET schema_version=8"); self.conn.commit()
        store.init_db(self.conn)
        self.assertEqual(dict(self.conn.execute("SELECT * FROM factory_independent_reviews").fetchone()), before)
        self.assertEqual(self.reserve()["review_id"], row["review_id"])

    def test_requirement_digest_is_utf8_canonical_and_claim_respects_manual_control(self):
        value = {"title": "Fix café 🧪", "description": "Check the Unicode input"}
        request = store.get_job(self.conn, self.reserve()["job_id"])["payload"]
        request["review"]["requirements"] = value
        request["review"]["requirements_digest"] = reviews.requirements_digest(value)
        self.assertEqual(reviews.validate_request(request)["requirements"], value)
        row = self.conn.execute("SELECT * FROM factory_independent_reviews").fetchone()
        self.conn.execute("INSERT INTO factory_issue_controls (work_item_key,held,revision,actor,reason,updated_at) VALUES ('ENG-8',1,1,'root','manual',?)", (store.utc_now(),)); self.conn.commit()
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))

    def test_gateway_native_authority_rejects_unreserved_or_unclaimed_job(self):
        with self.assertRaises(store.BridgeValidationError): reviews.authorization(self.conn, "fake-job", "fake-capability")
        row = self.reserve()
        self.assertFalse(reviews.authorization(self.conn, row["job_id"], row["capability"])["invocation_allowed"])
        reviews.claim(self.conn, row["job_id"], row["capability"])
        self.assertTrue(reviews.authorization(self.conn, row["job_id"], row["capability"])["invocation_allowed"])
        reviews.complete(self.conn, row["job_id"], row["capability"], self.receipt(row))
        authority = reviews.authorization(self.conn, row["job_id"], row["capability"])
        self.assertTrue(authority["authorized"])
        self.assertFalse(authority["invocation_allowed"])

    def test_http_review_endpoints_require_scoped_inspection_token(self):
        import test_server
        row = self.reserve()
        with patch.dict(os.environ, {"HATCHET_HERMES_INSPECTION_TOKEN": "scoped-inspection-fixture"}):
            for action in ("claim", "complete", "authorization"):
                handler = test_server._PostHandler(self.conn, {"review_capability": row["capability"], "result": self.receipt(row)},
                    f"/jobs/{row['job_id']}/independent-review/{action}", headers={"Authorization": "Bearer ordinary-bridge-fixture"})
                handler.do_POST()
                self.assertEqual(handler.response[0], 403)
            self.assertEqual(self.conn.execute("SELECT state FROM factory_independent_reviews").fetchone()[0], "pending")

    def test_changed_original_requirements_cannot_consume_evidence_as_approval(self):
        self.conn.execute("UPDATE bridge_jobs SET payload_json=json_set(payload_json,'$.instructions','Changed task') WHERE job_id=?", (self.job["job_id"],)); self.conn.commit()
        row = self.reserve()
        self.assertEqual((row["state"], row["reason"]), ("needs_operator", "review_inconclusive"))


if __name__ == "__main__":
    unittest.main()
