from organization_fixtures import configure
setUpModule, tearDownModule = configure()
import json
import os
import sqlite3
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from hatchet_hermes_bridge import console, independent_review as reviews, operator, review_enrollment as enrollment, store
import test_independent_review as fixtures
import test_review_repair as repair


class EnrollmentRecoveryTests(unittest.TestCase):
    observe = repair.ReviewRepairTests.observe
    finish = repair.ReviewRepairTests.finish
    inspect = repair.ReviewRepairTests.inspect
    receipt = fixtures.IndependentReviewTests.receipt
    complete = fixtures.IndependentReviewTests.complete

    def setUp(self):
        # Simulate the historical admission path before enrollment was sealed.
        with patch.object(reviews, "seal_admission", side_effect=lambda payload, mode: dict(payload)):
            fixtures.IndependentReviewTests.setUp(self)
        self.intent.pop("feedback")
        self.original_job = self.job
        self.original_attempt = store.get_factory_attempt(self.conn, self.attempt["attempt_id"])

    def preview(self, **changes):
        return enrollment.preview(**self.paths, **(self.intent | changes))

    def confirm(self, digest, request_id="enrollment-1", **changes):
        return enrollment.confirm(**self.paths, **(self.intent | changes), request_id=request_id, snapshot_digest=digest)

    def test_existing_verified_enrollment_receipt_and_console_without_reexecution(self):
        self.assertEqual(reviews.projection(self.conn, self.job["job_id"])["state"], "unknown")
        self.assertEqual(console.lifecycle(self.db, "ENG-8")["attempts"][0]["independent_review"]["status"], "unknown")
        preview = self.preview()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_review_enrollments").fetchone()[0], 0)
        result = self.confirm(preview["snapshot_digest"])
        self.assertTrue(result["created"])
        self.assertFalse(self.confirm(preview["snapshot_digest"])["created"])
        self.assertEqual(store.get_job(self.conn, self.job["job_id"]), self.original_job)
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"]), self.original_attempt)
        row = dict(self.conn.execute("SELECT * FROM factory_independent_reviews").fetchone())
        self.complete(row)
        self.assertEqual(console.lifecycle(self.db, "ENG-8")["attempts"][0]["independent_review"]["status"], "passed")
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "verified_ready")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0], 1)

    def test_recovered_enrollment_follows_repair_and_preserves_two_pass_budget(self):
        self.confirm(self.preview()["snapshot_digest"])
        row = dict(self.conn.execute("SELECT * FROM factory_independent_reviews").fetchone())
        self.complete(row, "changes_requested")
        self.assertEqual(reviews.sweep(self.conn)["repairs"], 1)
        repaired = store.get_factory_attempt(self.conn, self.conn.execute("SELECT repair_attempt_id FROM factory_independent_reviews").fetchone()[0])
        job = store.get_job(self.conn, repaired["job_id"])
        self.assertEqual(job["payload"]["independent_review_enrollment"], {"policy_digest": self.config["digest"]})
        self.finish(job, sha="b" * 40)
        self.intent.update(source_attempt_id=repaired["attempt_id"], reviewed_head_sha="b" * 40)
        self.inspect()
        second = reviews.reserve(self.conn, job["job_id"])
        self.assertEqual(second["pass_number"], 2)
        self.complete(second, "changes_requested")
        self.assertEqual(reviews.projection(self.conn, job["job_id"])["reason"], "review_budget_exhausted")
        self.assertEqual(store.get_job(self.conn, self.original_job["job_id"]), self.original_job)
        with self.assertRaises(store.BridgeConflict): self.preview()

    def test_head_policy_actor_hold_and_active_work_fail_closed(self):
        preview = self.preview()
        self.inspect(head_sha="b" * 40)
        with self.assertRaises(store.BridgeConflict): self.confirm(preview["snapshot_digest"])
        self.inspect()
        changed = {**self.config, "automatic_repairs_allowed": False, "digest": "b" * 64}
        with patch.object(reviews, "policy", return_value=changed), self.assertRaisesRegex(store.BridgeConflict, "snapshot"):
            self.confirm(preview["snapshot_digest"])
        with patch.object(reviews, "policy", return_value={**self.config, "enabled": False}), self.assertRaises(store.BridgeConflict): self.preview()
        with self.assertRaises(store.BridgeValidationError): enrollment.preview(**(self.paths | {"actor": "uid:1;pid:1;account:user"}), **self.intent)
        control = store.get_factory_issue_control(self.conn, "ENG-8")
        store.take_factory_issue_control(self.conn, work_item_key="ENG-8", request_id="hold", actor=repair.ACTOR,
            reason="manual fixture", expected_revision=control["revision"], expected_snapshot_digest=control["snapshot_digest"])
        with self.assertRaises(store.BridgeConflict): self.preview()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_review_enrollments").fetchone()[0], 0)

    def test_concurrent_confirm_and_scheduling_restart_are_one_reservation(self):
        digest = self.preview()["snapshot_digest"]
        def apply(_): return self.confirm(digest)
        def schedule():
            conn = store.connect_runtime(self.db)
            try: return reviews.sweep(conn)
            finally: conn.close()
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = [pool.submit(apply, index) for index in range(2)]
            sweep = pool.submit(schedule)
            results = [future.result() for future in pending]
            self.assertEqual(sweep.result()["reserved"], 0)
        self.assertEqual(sum(r["created"] for r in results), 1)
        conn = store.connect_runtime(self.db)
        try:
            self.assertEqual(reviews.sweep(conn)["reserved"], 0)
            self.assertEqual(reviews.reserve(conn, self.job["job_id"])["review_id"], results[0]["review_id"])
        finally: conn.close()
        with self.assertRaises(store.BridgeConflict): self.confirm(digest, request_id="different")
        with self.assertRaises(store.BridgeConflict): self.confirm(digest, reason="different")

    def test_newer_active_source_cannot_be_recovered(self):
        store.reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real", request_id="newer", reason="fixture", actor="fixture")
        with self.assertRaisesRegex(store.BridgeConflict, "latest"):
            self.preview()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_review_enrollments").fetchone()[0], 0)

    def test_atomic_failure_and_append_only_audit(self):
        digest = self.preview()["snapshot_digest"]
        with patch.object(reviews, "reserve", side_effect=store.BridgeConflict("injected failure")), self.assertRaises(store.BridgeConflict): self.confirm(digest)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_review_enrollments").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 0)
        self.confirm(digest)
        for sql in ("DELETE FROM factory_review_enrollments", "UPDATE factory_review_enrollments SET policy_digest='forged'", "INSERT OR REPLACE INTO factory_review_enrollments SELECT * FROM factory_review_enrollments"):
            with self.assertRaises(sqlite3.IntegrityError): self.conn.execute(sql)
            self.conn.rollback()

    def test_schema10_explicit_atomic_upgrade_no_backfill_old_image_refusal(self):
        with self.conn:
            self.conn.execute("DROP TABLE factory_review_enrollments")
            self.conn.execute("UPDATE bridge_schema_meta SET schema_version=10")
        for connect in (store.connect_runtime, store.connect_readonly):
            with self.assertRaisesRegex(store.BridgeValidationError, "migration"): connect(self.db)
        with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "before-version-update"}), self.assertRaises(store.BridgeError): store.init_db(self.conn)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='factory_review_enrollments'").fetchone())
        store.init_db(self.conn)
        before = self.conn.total_changes
        store.init_db(self.conn)
        self.assertEqual(before, self.conn.total_changes)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_review_enrollments").fetchone()[0], 0)
        with patch.object(store, "SCHEMA_VERSION", 10), self.assertRaises(store.BridgeValidationError): store.connect_runtime(self.db)
        self.assertEqual(store.get_job(self.conn, self.job["job_id"]), self.original_job)

    @unittest.skipUnless(os.geteuid() == 0, "root Unix operator fixture")
    def test_public_operator_role_client_auth_inspect_preview_confirm(self):
        socket_path = str(self.root / "operator.sock")
        server = operator._UnixHTTPServer(socket_path, operator.OperatorHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(server.shutdown)
        env = {**os.environ, "HATCHET_FACTORY_OPERATOR_TOKEN": "fixture-local", "FACTORY_OPERATOR_SOCKET": socket_path}
        prefix = [__import__("sys").executable, "-m", "hatchet_hermes_bridge.review_enrollment_client", "ENG-8"]
        def run(phase, *flags):
            result = subprocess.run(prefix + [phase, "--source-attempt", self.attempt["attempt_id"], *flags], capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with patch.dict(os.environ, env):
            if __import__("shutil").which("jq") and __import__("shutil").which("curl"):
                credentials = self.root / "operator.env"
                credentials.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture-local\n"); credentials.chmod(0o600)
                source = subprocess.run([str(repair.ROOT / "factory/factory"), "review-enrollment", "ENG-8", "inspect", "--source-attempt", self.attempt["attempt_id"], "--yes"], capture_output=True, text=True,
                    env={**env, "FACTORY_OPERATOR_ENV": str(credentials)}, timeout=10)
                self.assertEqual(source.returncode, 0, source.stderr)
            run("inspect", "--yes"); self.inspect()
            flags = ["--repo", repair.REPO, "--pr", repair.PR, "--branch", self.job["branch"], "--reviewed-head", "a" * 40, "--reason", self.intent["reason"]]
            preview = run("preview", *flags)
            self.assertTrue(run("confirm", *flags, "--request-id", "public-cli", "--snapshot-digest", preview["snapshot_digest"], "--yes")["created"])
            self.assertRegex(self.conn.execute("SELECT actor FROM factory_review_enrollments").fetchone()[0], r"^uid:0;pid:[0-9]+;account:root$")
        self.assertEqual(store.get_job(self.conn, self.job["job_id"]), self.original_job)


class AdmissionEnrollmentTests(unittest.TestCase):
    def policy(self, **changes):
        value = {"schema": "factory-independent-review-policy-v1", "enabled": True,
                 "automatic_repairs_allowed": True, "max_passes": 2, "repositories": [repair.REPO], **changes}
        return {**value, "digest": store.payload_hash(value)}

    def test_intake_and_retry_admissions_strip_forgery_and_preserve_duplicate_policy(self):
        import tempfile
        from pathlib import Path
        for admission in (store.reserve_factory_intake, store.reserve_factory_retry):
            for mode, policy_changes, reason in (("real", {}, "eligible"), ("real", {"enabled": False}, "policy_disabled"),
                  ("real", {"repositories": []}, "repository_excluded"), ("dry-run", {}, "dry_run")):
                with self.subTest(admission=admission.__name__, mode=mode, reason=reason), tempfile.TemporaryDirectory() as directory:
                    from test_support import connect_migrate
                    conn = connect_migrate(str(Path(directory) / "bridge.db"))
                    self.addCleanup(conn.close)
                    payload = repair.verifier_payload(repair.REPO)
                    payload.update(independent_review_enrollment={"policy_digest": "forged"}, independent_review_decision={"reason": "eligible"})
                    kwargs = dict(work_item_key="ENG-8", mode=mode, request_id="new", reason="fixture", actor="fixture", payload=payload)
                    policy = self.policy(**policy_changes)
                    with patch.object(reviews, "policy", return_value=policy):
                        attempt, job, created = admission(conn, **kwargs)
                    self.assertTrue(created)
                    self.assertEqual(job["payload"]["independent_review_decision"], {"schema": "factory-review-admission-v1", "reason": reason, "policy_digest": policy["digest"]})
                    self.assertEqual(job["payload"].get("independent_review_enrollment"), {"policy_digest": policy["digest"]} if reason == "eligible" else None)
                    with patch.object(reviews, "policy", side_effect=AssertionError("duplicates must not read policy")):
                        self.assertEqual(admission(conn, **kwargs), (attempt, job, False))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0], 1)
                    projected = reviews.projection(conn, job["job_id"])
                    if reason != "eligible":
                        self.assertEqual((projected["state"], projected["reason"]), ("not_required", reason))

    def test_retry_verified_receipt_console_path(self):
        case = EnrollmentRecoveryTests(methodName="test_existing_verified_enrollment_receipt_and_console_without_reexecution")
        try:
            case.setUp()
            # Separate work item admits through the actual retry path.
            payload = repair.verifier_payload(repair.REPO)
            payload["instructions"] = "Keep the requested draft behavior."
            attempt, job, _ = store.reserve_factory_retry(case.conn, work_item_key="ENG-9", mode="real", request_id="retry", reason="return to Factory", actor=repair.ACTOR, payload=payload)
            case.finish(job)
            # Trusted source observation follows the new issue identity.
            c = sqlite3.connect(case.ingress)
            original = json.loads(c.execute("SELECT normalized_json FROM ingress_events").fetchone()[0]); original["identifier"] = "ENG-9"
            c.execute("INSERT INTO ingress_events VALUES ('new','linear','ENG-9',?,2)", (json.dumps(original),)); c.commit(); c.close()
            case.intent.update(issue="ENG-9", source_attempt_id=attempt["attempt_id"], branch=job["branch"])
            from hatchet_hermes_bridge import review_repair
            review_repair.inspect(case.conn, issue="ENG-9", source_attempt_id=attempt["attempt_id"], actor=repair.ACTOR)
            request = case.conn.execute("SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? ORDER BY created_at DESC LIMIT 1", (attempt["attempt_id"],)).fetchone()
            store.complete_pr_inspection(case.conn, request["job_id"], {"repository": repair.REPO, "number": 77, "url": repair.PR, "state": "open", "merged": False, "draft": True, "head_branch": job["branch"], "head_sha": "a"*40, "base_ref": "main", "reason": "ok"}, request_key=request["request_key"], capability=request["capability"])
            row = reviews.reserve(case.conn, job["job_id"])
            self.assertTrue(reviews.claim(case.conn, row["job_id"], row["capability"]))
            reviews.complete(case.conn, row["job_id"], row["capability"], case.receipt(row))
            # Final observation must postdate retained receipt.
            review_repair.inspect(case.conn, issue="ENG-9", source_attempt_id=attempt["attempt_id"], actor=repair.ACTOR)
            request = case.conn.execute("SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? ORDER BY created_at DESC LIMIT 1", (attempt["attempt_id"],)).fetchone()
            store.complete_pr_inspection(case.conn, request["job_id"], {"repository": repair.REPO, "number": 77, "url": repair.PR, "state": "open", "merged": False, "draft": True, "head_branch": job["branch"], "head_sha": "a"*40, "base_ref": "main", "reason": "ok"}, request_key=request["request_key"], capability=request["capability"])
            self.assertTrue(reviews._settle_receipt(case.conn, row["review_id"]))
            self.assertEqual(console.lifecycle(case.db, "ENG-9")["attempts"][0]["independent_review"]["status"], "passed")
        finally: case.doCleanups()

    def test_sealing_failure_rolls_back_retry_and_intake_atomically(self):
        import tempfile
        from pathlib import Path
        from test_support import connect_migrate
        for admission in (store.reserve_factory_intake, store.reserve_factory_retry):
            with tempfile.TemporaryDirectory() as directory:
                conn = connect_migrate(str(Path(directory) / "bridge.db"))
                try:
                    with patch.object(reviews, "policy", side_effect=store.BridgeValidationError("policy unavailable")), self.assertRaises(store.BridgeValidationError):
                        admission(conn, work_item_key="ENG-8", mode="real", request_id="new", reason="fixture", actor="fixture", payload=repair.verifier_payload(repair.REPO))
                    for table in ("factory_attempts", "bridge_jobs", "factory_attempt_outbox", "factory_execution_manifests"):
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                finally: conn.close()

    def test_absence_retry_policy_and_forgery_use_shared_admission(self):
        import test_operator as operator_fixtures
        for changes, reason in (({}, "eligible"), ({"enabled": False}, "policy_disabled"), ({"repositories": []}, "repository_excluded")):
            case = operator_fixtures.FactoryOperatorTests()
            try:
                case.setUp()
                with patch.object(reviews, "policy", return_value=self.policy(**changes)):
                    first, _ = case.unknown_effect_attempt()
                    evidence = case.absence_evidence(first["attempt"]["branch"])
                    preview = operator.preview_force_absence_retry(**case.kwargs(), attempt_id=first["attempt"]["attempt_id"], absence_evidence=evidence)
                    reserve = store.reserve_factory_absence_retry
                    def forged(conn, **kwargs):
                        kwargs["payload"] = {**kwargs["payload"], "independent_review_enrollment": {"policy_digest": "forged"}}
                        return reserve(conn, **kwargs)
                    with patch.object(operator, "reserve_factory_absence_retry", side_effect=forged):
                        result = operator.reserve_force_absence_retry(**case.kwargs(), attempt_id=first["attempt"]["attempt_id"], request_id="absence", reason="absence confirmed", actor=repair.ACTOR, snapshot_digest=preview["snapshot_digest"], absence_evidence=evidence)
                    with patch.object(reviews, "policy", side_effect=AssertionError("duplicates must preserve admission")):
                        replay = operator.reserve_force_absence_retry(**case.kwargs(), attempt_id=first["attempt"]["attempt_id"], request_id="absence", reason="absence confirmed", actor=repair.ACTOR, snapshot_digest=preview["snapshot_digest"], absence_evidence=evidence)
                    self.assertFalse(replay["created"])
                    self.assertEqual(replay["job"], result["job"])
                    self.assertEqual(result["job"]["payload"]["independent_review_decision"]["reason"], reason)
                    self.assertEqual(result["job"]["payload"].get("independent_review_enrollment"), {"policy_digest": self.policy(**changes)["digest"]} if reason == "eligible" else None)
            finally: case.doCleanups()
