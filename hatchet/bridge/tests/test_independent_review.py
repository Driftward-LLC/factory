import copy
import json
import sqlite3
import unittest
import os
import shutil
import subprocess
import uuid
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
        self.config = {"schema": "factory-independent-review-policy-v1", "enabled": True,
                       "automatic_repairs_allowed": True, "max_passes": 2, "repositories": [REPO]}
        self.config["digest"] = store.payload_hash(self.config)
        self.policy_patch = patch.object(reviews, "policy", return_value=self.config)
        self.policy_patch.start(); self.addCleanup(self.policy_patch.stop)
        payload = fixtures.verifier_payload(REPO)
        payload["instructions"] = "Reject stale observations and keep the draft PR unchanged."
        with patch.object(fixtures, "verifier_payload", return_value=payload):
            fixtures.ReviewRepairTests.setUp(self)
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
        self.assertIsNone(self.job["hermes_session_id"])
        row = self.reserve()
        request = store.get_job(self.conn, row["job_id"])["payload"]["review"]
        self.assertEqual(request["generator_execution_id"], f"factory-execution:{self.attempt['attempt_id']}:{self.job['job_id']}")
        self.assertNotIn("generator_session_id", request)
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

    def legacy_review(self, session="generator-session"):
        review_id = str(uuid.uuid4())
        requirements = {"title": "", "description": self.job["payload"]["instructions"]}
        request = {"review_id": review_id, "repository": REPO, "pr_url": fixtures.PR,
            "branch": self.job["branch"], "head_sha": "a" * 40, "pass_number": 1,
            "generator_session_id": session, "policy_digest": self.config["digest"], "expected_base": "main",
            "requirements": requirements, "requirements_digest": reviews.requirements_digest(requirements)}
        capability, now = str(uuid.uuid4()), store.utc_now()
        job, _ = store.submit_job(self.conn, {"idempotency_key": f"legacy-review:{review_id}", "requested_action": "read_only_review",
            "payload": {"repo": REPO, "review": request, "review_capability": capability, "dry_run": False}}, internal_review=True)
        with self.conn:
            self.conn.execute("INSERT INTO factory_independent_reviews (review_id,lineage_key,pass_number,source_attempt_id,source_job_id,job_id,repository,pr_url,head_sha,policy_digest,capability,state,created_at,updated_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,'pending',?,?)",
                (review_id, store.payload_hash({"repository": REPO, "pr_url": fixtures.PR}), self.attempt["attempt_id"], self.job["job_id"],
                 job["job_id"], REPO, fixtures.PR, "a" * 40, self.config["digest"], capability, now, now))
        return dict(self.conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (review_id,)).fetchone())

    def test_legacy_request_and_result_keep_session_isolation_semantics(self):
        row = self.legacy_review()
        review_job = store.get_job(self.conn, row["job_id"])
        request = reviews.validate_request(review_job["payload"])
        self.assertEqual(request["generator_session_id"], "generator-session")
        with self.assertRaises(store.BridgeValidationError):
            reviews._receipt({**self.receipt(row), "reviewer_session_id": "generator-session"}, request)
        self.complete(row)
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "review_passed")
        self.assertEqual(store.get_job(self.conn, row["job_id"])["payload"], review_job["payload"])
        self.assertIsNone(store.get_job(self.conn, self.job["job_id"])["hermes_session_id"])

    def test_generator_request_variants_are_mutually_exclusive_and_bounded(self):
        row = self.reserve()
        payload = store.get_job(self.conn, row["job_id"])["payload"]
        request = payload["review"]
        self.assertEqual(reviews.validate_request(payload), request)
        missing = {k: v for k, v in request.items() if k != "generator_execution_id"}
        for changed in (missing, {**request, "generator_session_id": "legacy"},
                        *({**request, "generator_execution_id": value} for value in (
                            "", None, "factory-execution:job", request["generator_execution_id"] + "\n", "a" * 321))):
            with self.subTest(request=changed), self.assertRaises(store.BridgeValidationError):
                reviews.validate_request({**payload, "review": changed})
        for changed in ({**missing, "generator_session_id": ""}, {**missing, "generator_session_id": None}):
            with self.assertRaises(store.BridgeValidationError): reviews.validate_request({**payload, "review": changed})
        with self.assertRaises(store.BridgeValidationError): reviews._receipt(self.receipt(row), None)

    def test_existing_stopped_legacy_review_and_shared_pass_budget_are_unchanged(self):
        row = self.legacy_review("unavailable-generator-session")
        with self.conn: reviews._stop(self.conn, row, "review_inconclusive")
        before = dict(self.conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (row["review_id"],)).fetchone())
        job_before = store.get_job(self.conn, row["job_id"])
        source_before = store.get_job(self.conn, self.job["job_id"])
        attempt_before = store.get_factory_attempt(self.conn, self.attempt["attempt_id"])
        self.assertEqual(self.reserve(), before)
        for _ in range(2): self.assertEqual(reviews.sweep(self.conn), {"reserved": 0, "repairs": 0})
        self.assertFalse(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.assertEqual(dict(self.conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (row["review_id"],)).fetchone()), before)
        self.assertEqual(store.get_job(self.conn, row["job_id"]), job_before)
        self.assertEqual(store.get_job(self.conn, self.job["job_id"]), source_before)
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"]), attempt_before)
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["passes_used"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 1)

    def test_bounded_executor_receipt_with_null_session_reaches_claimable_review(self):
        compiled = fixtures.ROOT / "hatchet/workers/bridge-executor-worker/dist/hermes"
        if not shutil.which("node") or not (compiled / "bounded-evidence.js").is_file():
            self.skipTest("built executor required for cross-language receipt boundary")
        payload = fixtures.verifier_payload(REPO)
        payload["instructions"] = "Keep each effect bound to its Factory execution."
        attempt, job, _ = store.reserve_factory_intake(self.conn, work_item_key="ENG-9", mode="real", request_id="bounded", reason="fixture", actor="intake-worker", payload=payload)
        raw = {"status": "created", "prUrl": fixtures.PR, "commitSha": "b" * 40, "branch": job["branch"],
               "hermesSessionId": "untrusted-model-session", "validation": fixtures.original_context(job)}
        code = "const fs=require('node:fs'); const v=JSON.parse(fs.readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(require(process.argv[1]).boundedHermesDraftPrReceipt(v.job,v.raw)));"
        result = subprocess.run(["node", "-e", code, str(compiled / "bounded-evidence.js")], input=json.dumps({"job": job, "raw": raw}), text=True, capture_output=True, timeout=10, check=True)
        receipt = json.loads(result.stdout)
        self.assertIsNone(receipt["hermesSessionId"])
        self.finish(job, sha="b" * 40, executor_receipt=receipt)
        fixtures.review_repair.inspect(self.conn, issue="ENG-9", source_attempt_id=attempt["attempt_id"], actor=fixtures.ACTOR)
        inspection = self.conn.execute("SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=?", (attempt["attempt_id"],)).fetchone()
        store.complete_pr_inspection(self.conn, inspection["job_id"], {"repository": REPO, "number": 77, "url": fixtures.PR,
            "state": "open", "merged": False, "draft": True, "head_branch": job["branch"], "head_sha": "b" * 40, "base_ref": "main", "reason": "ok"},
            request_key=inspection["request_key"], capability=inspection["capability"])
        source = store.get_job(self.conn, job["job_id"])
        original_attempt = store.get_factory_attempt(self.conn, attempt["attempt_id"])
        self.assertIsNone(source["hermes_session_id"])
        row = reviews.reserve(self.conn, job["job_id"])
        self.assertEqual(row["state"], "pending")
        review_job = store.get_job(self.conn, row["job_id"])
        self.assertEqual(review_job["payload"]["review"]["generator_execution_id"], f"factory-execution:{attempt['attempt_id']}:{job['job_id']}")
        code = "const fs=require('node:fs'); const v=JSON.parse(fs.readFileSync(0,'utf8')); const m=require(process.argv[1]); const r=m.reviewIntent(v.job); if(!m.boundedIndependentReview(r,v.receipt))process.exit(1);"
        subprocess.run(["node", "-e", code, str(compiled / "independent-review.js")], input=json.dumps({"job": review_job, "receipt": self.receipt(row)}), text=True, capture_output=True, timeout=10, check=True)
        self.assertTrue(reviews.claim(self.conn, row["job_id"], row["capability"]))
        self.assertTrue(reviews.authorization(self.conn, row["job_id"], row["capability"])["invocation_allowed"])
        self.assertEqual(store.get_job(self.conn, job["job_id"]), source)
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"]), original_attempt)

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

    def test_wrong_capability_model_or_head_never_pass(self):
        row = self.reserve()
        with self.assertRaises(store.BridgeValidationError): reviews.claim(self.conn, row["job_id"], "not-the-reserved-capability")
        for changes in ({"reviewer_model": "gpt-6.1-sol"}, {"head_sha": "b" * 40}):
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

    def test_schema8_upgrade_and_schema9_reentry_preserve_all_historical_rows(self):
        self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2020-01-01T00:00:00.000000Z' WHERE state='completed'")
        self.conn.execute("DROP TABLE factory_independent_reviews")
        self.conn.execute("UPDATE bridge_schema_meta SET schema_version=8")
        self.conn.commit()

        def contents(include_metadata=False):
            tables = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {name: [tuple(row) for row in self.conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                    for name in tables if include_metadata or name != "bridge_schema_meta"}

        before = contents()
        self.assertGreater(len(before["factory_inspection_requests"]), 0)
        store.init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], 9)
        self.assertEqual(contents(), {**before, "factory_independent_reviews": []})
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        after = contents(include_metadata=True)
        store.init_db(self.conn)
        self.assertEqual(contents(include_metadata=True), after)

    def test_older_schema_still_backfills_stale_inspections(self):
        self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2020-01-01T00:00:00.000000Z' WHERE state='completed'")
        self.conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
        self.conn.commit()
        before = self.conn.execute("SELECT count(*) FROM factory_inspection_requests").fetchone()[0]
        store.init_db(self.conn)
        self.assertGreater(self.conn.execute("SELECT count(*) FROM factory_inspection_requests").fetchone()[0], before)

    def test_future_or_malformed_schema_version_is_rejected_before_ddl(self):
        self.conn.execute("DROP TABLE factory_independent_reviews")
        for version in (10, "future", -1, 8.5):
            with self.subTest(version=version):
                self.conn.execute("UPDATE bridge_schema_meta SET schema_version=?", (version,))
                self.conn.commit()
                with self.assertRaisesRegex(store.BridgeValidationError, "unsupported bridge schema version"):
                    store.init_db(self.conn)
                self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], version)
                self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='factory_independent_reviews'").fetchone())

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

    def test_launch_inspection_expiry_retains_completion_without_granting_pass(self):
        row = self.reserve(); reviews.claim(self.conn, row["job_id"], row["capability"])
        self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2020-01-01T00:00:00.000000Z' WHERE state='completed'"); self.conn.commit()
        authority = reviews.authorization(self.conn, row["job_id"], row["capability"])
        self.assertFalse(authority["invocation_allowed"])
        self.assertTrue(authority["completion_allowed"])
        self.assertEqual(reviews.complete(self.conn, row["job_id"], row["capability"], self.receipt(row))["state"], "review_pending")
        self.assertFalse(reviews._settle_receipt(self.conn, row["review_id"]))
        self.inspect(); reviews._settle_receipt(self.conn, row["review_id"])
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "review_passed")

    def test_unchanged_repair_head_requires_operator_without_second_pass_or_dispatch_failure(self):
        row = self.reserve(); self.complete(row, "changes_requested"); reviews.sweep(self.conn)
        repair_id = self.conn.execute("SELECT repair_attempt_id FROM factory_independent_reviews").fetchone()[0]
        repair = store.get_factory_attempt(self.conn, repair_id)
        job = store.get_job(self.conn, repair["job_id"])
        self.finish(job, sha="a" * 40)
        self.intent.update(source_attempt_id=repair_id); self.inspect()
        for _ in range(2):
            self.assertEqual(reviews.sweep(self.conn), {"reserved": 0, "repairs": 0})
        projected = reviews.projection(self.conn, job["job_id"])
        self.assertEqual((projected["state"], projected["passes_used"]), ("needs_operator", 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 1)
        self.assertEqual(len(store.list_factory_outbox(self.conn)), 2)

    def test_completion_authority_still_rejects_policy_revocation_and_manual_control(self):
        row = self.reserve(); reviews.claim(self.conn, row["job_id"], row["capability"])
        self.config["enabled"] = False
        self.assertFalse(reviews.authorization(self.conn, row["job_id"], row["capability"])["completion_allowed"])
        self.config["enabled"] = True
        self.conn.execute("INSERT INTO factory_issue_controls (work_item_key,held,revision,actor,reason,updated_at) VALUES ('ENG-8',1,1,'root','manual',?)", (store.utc_now(),)); self.conn.commit()
        self.assertFalse(reviews.authorization(self.conn, row["job_id"], row["capability"])["completion_allowed"])

    def test_unenrolled_newer_attempts_cannot_starve_an_eligible_older_attempt(self):
        payload = fixtures.verifier_payload(REPO)
        payload["instructions"] = "Another task"
        for index in range(3):
            attempt, job, _ = store.reserve_factory_intake(self.conn, work_item_key=f"ENG-{90 + index}", mode="real",
                request_id=f"newer-{index}", reason="fixture", actor="intake-worker", payload=payload)
            unenrolled = dict(job["payload"]); unenrolled.pop("independent_review_enrollment", None)
            self.conn.execute("UPDATE bridge_jobs SET payload_json=?,state='ready_for_human_review' WHERE job_id=?", (store.canonical_json(unenrolled), job["job_id"]))
            self.conn.execute("UPDATE factory_attempts SET status='verified_ready' WHERE attempt_id=?", (attempt["attempt_id"],)); self.conn.commit()
        self.assertEqual(reviews.sweep(self.conn, limit=1)["reserved"], 1)
        self.assertEqual(self.conn.execute("SELECT source_job_id FROM factory_independent_reviews").fetchone()[0], self.job["job_id"])


if __name__ == "__main__":
    unittest.main()
