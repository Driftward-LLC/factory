import json
import os
import shutil
import sqlite3
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from hatchet_hermes_bridge import console, independent_review as reviews, operator, review_recovery as recovery, store
import test_independent_review as review_fixtures
import test_review_repair as repair_fixtures
from test_review_repair import PR, REPO, ROOT


class ReviewRecoveryTests(unittest.TestCase):
    observe = repair_fixtures.ReviewRepairTests.observe
    finish = repair_fixtures.ReviewRepairTests.finish
    inspect = repair_fixtures.ReviewRepairTests.inspect
    legacy_review = review_fixtures.IndependentReviewTests.legacy_review
    receipt = review_fixtures.IndependentReviewTests.receipt

    def setUp(self):
        review_fixtures.IndependentReviewTests.setUp(self)
        self.row = self.legacy_review("unavailable-generator-session")
        with self.conn:
            reviews._stop(self.conn, self.row, "review_inconclusive")
            self.conn.execute("UPDATE factory_independent_reviews SET created_at='2020-01-01T00:00:00.000000Z' WHERE review_id=?", (self.row["review_id"],))
        self.intent.pop("feedback")
        self.intent["review_id"] = self.row["review_id"]
        self.old = self.history()

    def history(self):
        job_id = self.row["job_id"]
        return {"job": store.get_job(self.conn, job_id),
                "source": store.get_job(self.conn, self.job["job_id"]),
                "attempt": store.get_factory_attempt(self.conn, self.attempt["attempt_id"]),
                "events": [tuple(r) for r in self.conn.execute("SELECT * FROM bridge_events WHERE job_id=? ORDER BY event_id", (job_id,))],
                "outbox": [tuple(r) for r in self.conn.execute("SELECT * FROM factory_inspection_outbox WHERE job_id=? ORDER BY outbox_key", (job_id,))]}

    def preview(self, **changes):
        return recovery.preview(**self.paths, **(self.intent | changes))

    def confirm(self, digest, request_id="recovery-1", **changes):
        return recovery.confirm(**self.paths, **(self.intent | changes), request_id=request_id, snapshot_digest=digest)

    def test_delivery_preserves_original_evidence_and_uses_same_pass_through_completion(self):
        before = self.conn.total_changes
        preview = self.preview()
        self.assertEqual(self.conn.total_changes, before)
        result = self.confirm(preview["snapshot_digest"])
        job = result["job"]
        self.assertTrue(result["created"])
        self.assertNotEqual(job["job_id"], self.row["job_id"])
        request = reviews.validate_request(job["payload"])
        self.assertEqual((request["review_id"], request["pass_number"]), (self.row["review_id"], 1))
        self.assertEqual(request["generator_execution_id"], f"factory-execution:{self.attempt['attempt_id']}:{self.job['job_id']}")
        self.assertEqual(self.history(), self.old)
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["passes_used"], 1)
        self.assertEqual(reviews.sweep(self.conn), {"reserved": 0, "repairs": 0})
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "review_pending")
        for operation in (reviews.claim, reviews.authorization):
            with self.assertRaises(store.BridgeValidationError): operation(self.conn, self.row["job_id"], self.row["capability"])
        with self.assertRaises(store.BridgeValidationError):
            reviews.complete(self.conn, self.row["job_id"], self.row["capability"], self.receipt(self.row))
        outbox = self.conn.execute("SELECT * FROM factory_inspection_outbox WHERE job_id=?", (job["job_id"],)).fetchone()
        delivery = store.claim_factory_inspection_outbox(self.conn, outbox["outbox_key"])
        self.assertEqual(delivery["payload"]["job_id"], job["job_id"])
        self.assertTrue(reviews.claim(self.conn, job["job_id"], self.row["capability"]))
        self.assertTrue(reviews.authorization(self.conn, job["job_id"], self.row["capability"])["invocation_allowed"])
        self.assertFalse(reviews.claim(self.conn, job["job_id"], self.row["capability"]))
        reviews.complete(self.conn, job["job_id"], self.row["capability"], self.receipt(self.row))
        self.inspect()
        self.assertTrue(reviews._settle_receipt(self.conn, self.row["review_id"]))
        self.assertEqual(store.get_job(self.conn, job["job_id"])["state"], "completed")
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "review_passed")
        projected = console.lifecycle(self.db, "ENG-8")["verification_evidence"]["independent_review"]
        self.assertEqual((projected["status"], projected["pass_count"]), ("passed", 1))
        self.assertEqual(projected["reviewer_model"], "gpt-6-astra")
        self.assertEqual(self.history(), self.old)
        self.assertFalse(self.confirm(preview["snapshot_digest"])["created"])

    def test_concurrent_confirmation_and_intent_conflicts(self):
        digest = self.preview()["snapshot_digest"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.confirm(digest), range(2)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(results[0]["job"]["job_id"], results[1]["job"]["job_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 1)
        for changes in ({"reason": "changed"}, {"reviewed_head_sha": "b" * 40}, {"source_attempt_id": "other"}):
            with self.assertRaises(store.BridgeConflict): self.confirm(digest, **changes)
        with self.assertRaises(store.BridgeConflict): self.confirm("b" * 64)
        with self.assertRaises(store.BridgeConflict): self.confirm(digest, request_id="other")

    def test_started_receipt_and_running_event_evidence_are_denied(self):
        for column, value in (("started_at", store.utc_now()), ("result_recorded_at", store.utc_now()), ("repair_attempt_id", "repair")):
            with self.subTest(column=column), self.conn:
                self.conn.execute(f"UPDATE factory_independent_reviews SET {column}=? WHERE review_id=?", (value, self.row["review_id"]))
            with self.assertRaises(store.BridgeConflict): self.preview()
            with self.conn: self.conn.execute(f"UPDATE factory_independent_reviews SET {column}=NULL WHERE review_id=?", (self.row["review_id"],))
        with self.conn: store._append_event(self.conn, job_id=self.row["job_id"], event_type="transition:running", state="running", details={})
        with self.assertRaisesRegex(store.BridgeConflict, "evidence"): self.preview()

    def test_head_policy_enrollment_and_unreleased_lease_denials(self):
        digest = self.preview()["snapshot_digest"]
        self.inspect(head_sha="b" * 40)
        with self.assertRaises(store.BridgeConflict): self.confirm(digest)
        self.inspect()
        self.config["enabled"] = False
        with self.assertRaises(store.BridgeConflict): self.preview()
        self.config["enabled"] = True
        with self.conn: self.conn.execute("UPDATE factory_execution_leases SET released_at=NULL WHERE attempt_id=?", (self.attempt["attempt_id"],))
        with self.assertRaises(store.BridgeConflict): self.preview()
        with self.conn: self.conn.execute("UPDATE factory_execution_leases SET released_at=? WHERE attempt_id=?", (store.utc_now(), self.attempt["attempt_id"]))
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET payload_json=json_remove(payload_json,'$.independent_review_enrollment') WHERE job_id=?", (self.job["job_id"],))
        with self.assertRaises(store.BridgeConflict): self.preview()

    def test_explicit_hold_stale_inspection_and_required_manifest_denials(self):
        with self.conn:
            self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2020-01-01T00:00:00.000000Z'")
        with self.assertRaisesRegex(operator.OperatorError, "fresh typed"): self.preview()
        self.inspect()
        with patch.object(recovery.work_evidence, "admission_manifest", return_value={}):
            with self.assertRaisesRegex(store.BridgeConflict, "requirements manifests"): self.preview()
        control = store.get_factory_issue_control(self.conn, "ENG-8")
        store.take_factory_issue_control(self.conn, work_item_key="ENG-8", request_id="hold", actor=repair_fixtures.ACTOR,
            reason="manual fixture", expected_revision=control["revision"], expected_snapshot_digest=control["snapshot_digest"])
        with self.assertRaisesRegex(store.BridgeConflict, "held"): self.preview()

    def test_nonroot_wrong_legacy_identity_and_source_session_denials(self):
        with self.assertRaises(store.BridgeValidationError):
            recovery.preview(**(self.paths | {"actor": "uid:1;pid:1;account:user"}), **self.intent)
        with self.conn: self.conn.execute("UPDATE bridge_jobs SET hermes_session_id='real-generator-session' WHERE job_id=?", (self.job["job_id"],))
        with self.assertRaises(store.BridgeConflict): self.preview()
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET hermes_session_id=NULL WHERE job_id=?", (self.job["job_id"],))
            old = store.get_job(self.conn, self.row["job_id"])
            old["payload"]["review"]["generator_session_id"] = "different-legacy-session"
            self.conn.execute("UPDATE bridge_jobs SET payload_json=?,payload_hash=? WHERE job_id=?",
                (store.canonical_json(old["payload"]), store.payload_hash(old["payload"]), self.row["job_id"]))
        with self.assertRaisesRegex(store.BridgeConflict, "missing-session"): self.preview()

    def test_newer_real_attempt_revokes_recovery(self):
        self.observe(status="Ready", dispatchEligible=True, reasonCode="ok")
        store.reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real",
            request_id="newer", reason="fixture", actor="fixture")
        with self.assertRaisesRegex(store.BridgeConflict, "latest"): self.preview()

    def test_recovery_does_not_reset_two_pass_budget(self):
        # A later reservation is durable even when this earlier review never ran.
        other, _ = store.reserve_factory_attempt(self.conn, work_item_key="ENG-99", mode="real",
            request_id="other", reason="fixture", actor="fixture")
        with self.conn:
            self.conn.execute("INSERT INTO factory_independent_reviews (review_id,lineage_key,pass_number,source_attempt_id,source_job_id,job_id,repository,pr_url,head_sha,policy_digest,capability,state,created_at,updated_at) VALUES ('other',?,2,?,?,?,?,?,?,?,?,'needs_operator',?,?)",
                (self.row["lineage_key"], other["attempt_id"], "other-source", self.job["job_id"], REPO, PR,
                 "b" * 40, self.config["digest"], self.row["capability"], store.utc_now(), store.utc_now()))
        with self.assertRaisesRegex(store.BridgeConflict, "budget"): self.preview()

    def test_atomic_rollback_and_append_only_delivery(self):
        digest = self.preview()["snapshot_digest"]
        with patch.object(store, "_append_factory_attempt_event", side_effect=RuntimeError("fixture crash")):
            with self.assertRaises(RuntimeError): self.confirm(digest)
        self.assertEqual(self.history(), self.old)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_review_deliveries").fetchone()[0], 0)
        self.confirm(digest)
        for sql in ("DELETE FROM factory_independent_review_deliveries", "UPDATE factory_independent_review_deliveries SET actor='other'",
                    "INSERT OR REPLACE INTO factory_independent_review_deliveries SELECT * FROM factory_independent_review_deliveries"):
            with self.assertRaises(sqlite3.IntegrityError): self.conn.execute(sql)
            self.conn.rollback()
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_generation_two_recovery_uses_original_requirements_manifest(self):
        case = repair_fixtures.ReviewRepairTests()
        payload = repair_fixtures.verifier_payload(REPO)
        payload["instructions"] = "Keep the original task requirements through review repair."
        with patch.object(repair_fixtures, "verifier_payload", return_value=payload):
            case.setUp()
        self.addCleanup(case.doCleanups)
        case.config = self.config
        original_requirements = {"title": case.job["payload"].get("title", ""), "description": case.job["payload"]["instructions"]}
        case.inspect()
        approved = repair_fixtures.review_repair.preview(**case.paths, **case.intent)
        repair = repair_fixtures.review_repair.confirm(**case.paths, **case.intent, request_id="repair", snapshot_digest=approved["snapshot_digest"])
        case.finish(repair["job"])
        case.attempt = store.get_factory_attempt(case.conn, repair["attempt"]["attempt_id"])
        case.job = store.get_job(case.conn, repair["job"]["job_id"])
        case.intent["source_attempt_id"] = case.attempt["attempt_id"]
        case.inspect()
        row = review_fixtures.IndependentReviewTests.legacy_review(case, "unavailable-generator-session")
        with case.conn:
            old = store.get_job(case.conn, row["job_id"])
            old["payload"]["review"]["requirements"] = original_requirements
            old["payload"]["review"]["requirements_digest"] = reviews.requirements_digest(original_requirements)
            case.conn.execute("UPDATE bridge_jobs SET payload_json=?,payload_hash=? WHERE job_id=?",
                (store.canonical_json(old["payload"]), store.payload_hash(old["payload"]), row["job_id"]))
            reviews._stop(case.conn, row, "review_inconclusive")
        intent = {k: v for k, v in case.intent.items() if k != "feedback"} | {"review_id": row["review_id"]}
        preview = recovery.preview(**case.paths, **intent)
        self.assertEqual(preview["context"]["request"]["requirements"], original_requirements)
        result = recovery.confirm(**case.paths, **intent, request_id="generation-two", snapshot_digest=preview["snapshot_digest"])
        self.assertTrue(result["created"])
        self.assertEqual(result["job"]["payload"]["review"]["generator_execution_id"], f"factory-execution:{case.attempt['attempt_id']}:{case.job['job_id']}")

    def test_schema9_requires_explicit_atomic_idempotent_migration(self):
        original_review = tuple(self.conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (self.row["review_id"],)).fetchone())
        with self.conn:
            self.conn.execute("DROP TABLE factory_independent_review_deliveries")
            self.conn.execute("UPDATE bridge_schema_meta SET schema_version=9")
        with self.assertRaisesRegex(store.BridgeValidationError, "migration"):
            store.connect_runtime(self.db)
        with self.assertRaisesRegex(store.BridgeValidationError, "migration"):
            store.connect_readonly(self.db)
        with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "before-version-update"}):
            with self.assertRaises(store.BridgeError): store.init_db(self.conn)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='factory_independent_review_deliveries'").fetchone())
        self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], 9)
        store.init_db(self.conn)
        self.assertEqual(self.history(), self.old)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (self.row["review_id"],)).fetchone()), original_review)
        before = self.conn.total_changes
        store.init_db(self.conn)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.history(), self.old)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("jq") and shutil.which("curl"), "root Unix CLI fixture")
    def test_real_cli_inspect_preview_confirm(self):
        env_file = self.root / "operator.env"
        env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture-local\n"); env_file.chmod(0o600)
        socket_path = str(self.root / "operator.sock")
        server = operator._UnixHTTPServer(socket_path, operator.OperatorHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {"HATCHET_FACTORY_OPERATOR_TOKEN": "fixture-local", "FACTORY_OPERATOR_ENV": str(env_file), "FACTORY_OPERATOR_SOCKET": socket_path}):
            prefix = [str(ROOT / "factory/factory"), "review-recovery", "ENG-8"]
            def run(phase, *flags):
                output = subprocess.run(prefix + [phase, "--review", self.row["review_id"], "--source-attempt", self.attempt["attempt_id"], *flags], capture_output=True, text=True, timeout=10)
                self.assertEqual(output.returncode, 0, output.stderr)
                return json.loads(output.stdout)
            run("inspect", "--yes")
            self.inspect()
            flags = ["--repo", REPO, "--pr", PR, "--branch", self.job["branch"], "--reviewed-head", "a" * 40, "--reason", self.intent["reason"]]
            preview = run("preview", *flags)
            result = run("confirm", *flags, "--request-id", "cli-recovery", "--snapshot-digest", preview["snapshot_digest"], "--yes")
            self.assertTrue(result["created"])
            stored = self.conn.execute("SELECT actor FROM factory_independent_review_deliveries").fetchone()[0]
            self.assertRegex(stored, r"^uid:0;pid:[0-9]+;account:root$")
            self.assertEqual(self.history(), self.old)


if __name__ == "__main__":
    unittest.main()
