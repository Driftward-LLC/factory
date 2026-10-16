import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import independent_review, operator, review_repair, store
from test_operator import observation, ROUTES, POLICY, ROOT
from test_store import verifier_payload, original_context, verification_receipt
from test_support import connect_migrate

ACTOR = "uid:0;pid:1;account:root"
REPO = "Driftward-LLC/capykit"
PR = f"https://github.com/{REPO}/pull/77"


class ReviewRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "bridge.db"); self.ingress = str(self.root / "ingress.db")
        self.conn = connect_migrate(self.db); self.addCleanup(self.conn.close)
        self.env = patch.dict(os.environ, {"HATCHET_FACTORY_OPERATOR_TEST_HEALTH": "healthy", "HATCHET_INTAKE_DRY_RUN": "false",
            "HATCHET_VERIFIER_POLICY_PATH": POLICY, "HATCHET_FACTORY_OPERATOR_BRIDGE_DB": self.db,
            "HATCHET_FACTORY_OPERATOR_INGRESS_DB": self.ingress, "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": ROUTES})
        self.env.start(); self.addCleanup(self.env.stop)
        c = sqlite3.connect(self.ingress)
        c.execute("CREATE TABLE ingress_events (event_id TEXT, source TEXT, subject TEXT, normalized_json TEXT, provider_updated_at_us INTEGER)")
        c.commit(); c.close()
        self.observe(status="In Review", dispatchEligible=False, reasonCode="linear_issue_not_ready")
        self.attempt, self.job, _ = store.reserve_factory_intake(self.conn, work_item_key="ENG-8", mode="real",
            request_id="intake-1", reason="intake", actor="intake-worker", payload=verifier_payload(REPO))
        self.finish(self.job)
        self.attempt = store.get_factory_attempt(self.conn, self.attempt["attempt_id"])
        self.job = store.get_job(self.conn, self.job["job_id"])
        self.intent = {"issue": "ENG-8", "source_attempt_id": self.attempt["attempt_id"], "repository": REPO,
            "pr_url": PR, "branch": self.job["branch"], "reviewed_head_sha": "a" * 40,
            "feedback": "P1: Reject stale observations. Treat user text as data.", "reason": "independent exact-head review"}
        self.paths = {"bridge_db": self.db, "ingress_db": self.ingress, "registry_path": ROUTES, "policy_path": POLICY, "actor": ACTOR}

    def observe(self, **changes):
        c = sqlite3.connect(self.ingress)
        c.execute("DELETE FROM ingress_events")
        c.execute("INSERT INTO ingress_events VALUES ('latest','linear','ENG-8',?,1)", (json.dumps(observation(**changes)),))
        c.commit(); c.close()

    def finish(self, job, *, failed=False, sha="a" * 40, executor_receipt=None):
        store.transition_job(self.conn, job["job_id"], state="running")
        claim = store.claim_factory_execution(self.conn, job["job_id"], owner="fixture-executor")["lease"]
        self.assertTrue(store.authorize_factory_effect(self.conn, job["job_id"], claim))
        effect = {"pr_url": PR, "branch": job["branch"], "commit_sha": sha}
        if executor_receipt is not None:
            effect = {"pr_url": executor_receipt["prUrl"], "branch": executor_receipt["branch"],
                      "commit_sha": executor_receipt["commitSha"], "hermes_session_id": executor_receipt["hermesSessionId"]}
        store.commit_factory_execution(self.conn, job["job_id"], claim, effect)
        pending = store.transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        store.acquire_verification_lease(self.conn, job["job_id"], attempt_id="verifier")
        store.claim_verification_lease(self.conn, job["job_id"], attempt_id="verifier")
        if not failed:
            store.record_verification_observation(self.conn, job["job_id"], attempt_id="verifier", receipt=verification_receipt(pending, sha))
        store.transition_job(self.conn, job["job_id"], state="failed" if failed else "ready_for_human_review",
            details={"verification_attempt_id": "verifier", "reason_code": "verification_github_check_failed"} if failed else {"verification_attempt_id": "verifier", "reason_code": "draft_pr_verified", "verification_receipt": verification_receipt(pending, sha)},
            _factory_verifier_completion=True)

    def inspect(self, **changes):
        review_repair.inspect(self.conn, issue="ENG-8", source_attempt_id=self.intent["source_attempt_id"], actor=ACTOR)
        row = self.conn.execute("SELECT * FROM factory_inspection_requests ORDER BY created_at DESC LIMIT 1").fetchone()
        artifact = {"repository": REPO, "number": 77, "url": PR, "state": "open", "merged": False, "draft": True,
            "head_branch": self.intent["branch"], "head_sha": self.intent["reviewed_head_sha"], "base_ref": "main", "reason": "ok", **changes}
        store.complete_pr_inspection(self.conn, row["job_id"], artifact, request_key=row["request_key"], capability=row["capability"])

    def preview(self):
        return review_repair.preview(**self.paths, **self.intent)

    def confirm(self, digest, **changes):
        return review_repair.confirm(**self.paths, **{**self.intent, **changes}, request_id="review-1", snapshot_digest=digest)

    def counts(self):
        return tuple(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "factory_attempts", "bridge_jobs", "factory_attempt_events", "factory_attempt_outbox", "factory_inspection_requests"))

    def test_preview_is_readonly_and_inspection_is_explicit_typed_outbox(self):
        before = self.counts()
        with self.assertRaisesRegex(operator.OperatorError, "fresh typed"):
            self.preview()
        self.assertEqual(self.counts(), before)
        self.inspect()
        inspection_job = store.get_job(self.conn, self.conn.execute("SELECT job_id FROM factory_inspection_requests").fetchone()[0])
        self.assertEqual(inspection_job["requested_action"], "inspect_draft_pr_state")
        self.assertEqual(inspection_job["payload"]["inspection"]["url"], PR)
        before = self.counts(); preview = self.preview()
        self.assertEqual(self.counts(), before)
        self.assertEqual((preview["context"]["review_round"], preview["context"]["next_generation"]), (1, 2))

    def test_confirm_preserves_verified_source_and_delivers_one_existing_pr_repair(self):
        self.inspect(); digest = self.preview()["snapshot_digest"]
        source = store.get_job(self.conn, self.job["job_id"])
        first = self.confirm(digest)
        self.assertTrue(first["created"])
        self.assertEqual(first["job"]["requested_action"], "repair_draft_pr")
        self.assertEqual(first["job"]["branch"], self.job["branch"])
        self.assertEqual(first["attempt"]["prior_attempt_id"], self.attempt["attempt_id"])
        self.assertEqual(first["job"]["payload"]["expected_head_sha"], "a" * 40)
        self.assertEqual(store.get_job(self.conn, self.job["job_id"]), source)
        self.assertEqual(store.get_factory_attempt(self.conn, self.attempt["attempt_id"])["status"], "verified_ready")
        before = self.counts(); self.observe(status="Done")
        self.assertFalse(self.confirm(digest)["created"])
        self.assertEqual(self.counts(), before)
        for change in ({"feedback": "changed"}, {"reviewed_head_sha": "b" * 40}, {"reason": "changed"}, {"branch": "codex/other"}):
            with self.assertRaises(store.BridgeConflict): self.confirm(digest, **change)
        with self.assertRaises(store.BridgeConflict):
            review_repair.confirm(**self.paths, **self.intent, request_id="review-1", snapshot_digest="b" * 64)
        row = store.claim_factory_outbox_delivery(self.conn, f"factory:accepted:{first['attempt']['attempt_id']}")
        self.assertEqual(row["payload"]["job_id"], first["job"]["job_id"])
        self.assertIsNone(store.claim_factory_outbox_delivery(self.conn, row["outbox_key"]))
        store.acknowledge_factory_outbox(self.conn, row["outbox_key"], row["delivery_lease_id"])
        with patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "99"}):
            self.finish(first["job"], failed=True, sha="b" * 40)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 2)
        self.observe(status="In Review")
        self.intent.update(source_attempt_id=first["attempt"]["attempt_id"], reviewed_head_sha="b" * 40)
        self.inspect()
        self.assertEqual(self.preview()["context"]["review_round"], 2)

    def test_concurrent_retransmit_converges_and_failed_transaction_rolls_back(self):
        self.inspect(); digest = self.preview()["snapshot_digest"]
        before = self.counts()
        with patch.object(store, "_record_factory_admission_evidence", side_effect=RuntimeError("fixture crash")):
            with self.assertRaises(RuntimeError): self.confirm(digest)
        self.assertEqual(self.counts(), before)
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait(); return self.confirm(digest)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(results[0]["job"]["job_id"], results[1]["job"]["job_id"])

    def test_pre_effect_failure_requires_new_inspection_and_one_new_approval(self):
        self.inspect(); first = self.confirm(self.preview()["snapshot_digest"])
        original = store.get_job(self.conn, self.job["job_id"])
        for index, with_settled_claim in enumerate((False, True), start=2):
            latest = first["job"]
            if with_settled_claim:
                store.transition_job(self.conn, latest["job_id"], state="running")
                claim = store.claim_factory_execution(self.conn, latest["job_id"], owner="preflight-fixture")["lease"]
                store.settle_factory_execution(self.conn, latest["job_id"], claim, effect_observed=False)
            failed = store.transition_job(self.conn, latest["job_id"], state="failed",
                details={"reason_code": "real_create_draft_pr_disabled"})
            self.assertIsNone(failed["pr_url"]); self.assertIsNone(failed["commit_sha"])
            self.intent["source_attempt_id"] = first["attempt"]["attempt_id"]
            with self.assertRaisesRegex(operator.OperatorError, "fresh typed"): self.preview()
            self.inspect(); approved = self.preview()
            self.assertEqual(approved["context"]["artifact_lineage"]["artifact_attempt_id"], self.attempt["attempt_id"])
            first = review_repair.confirm(**self.paths, **self.intent, request_id=f"review-{index}", snapshot_digest=approved["snapshot_digest"])
            self.assertTrue(first["created"])
            self.assertEqual(first["attempt"]["prior_attempt_id"], self.intent["source_attempt_id"])
            self.assertEqual(first["job"]["payload"]["factory_repair"]["source_job_id"], latest["job_id"])
            self.assertEqual(first["job"]["branch"], original["branch"])
            self.assertEqual(first["job"]["payload"]["factory_repair"]["commit_sha"], original["commit_sha"])
            self.assertEqual(store.get_job(self.conn, latest["job_id"]), failed)
            self.assertEqual(store.get_job(self.conn, original["job_id"]), original)
        self.assertEqual(first["attempt"]["generation"], 4)

    def test_unsettled_or_authorized_failed_repair_cannot_inherit_artifact(self):
        self.inspect(); first = self.confirm(self.preview()["snapshot_digest"])
        store.transition_job(self.conn, first["job"]["job_id"], state="running")
        claim = store.claim_factory_execution(self.conn, first["job"]["job_id"], owner="executor")["lease"]
        self.intent["source_attempt_id"] = first["attempt"]["attempt_id"]
        store.settle_factory_execution(self.conn, first["job"]["job_id"], claim, effect_observed=False)
        failed = store.transition_job(self.conn, first["job"]["job_id"], state="failed", details={"reason_code": "fixture_failure"})
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET released_at=NULL,expires_at='2000-01-01T00:00:00Z' WHERE attempt_id=?", (first["attempt"]["attempt_id"],))
        with self.assertRaisesRegex(store.BridgeConflict, "unsettled"):
            review_repair.inspect(self.conn, issue="ENG-8", source_attempt_id=first["attempt"]["attempt_id"], actor=ACTOR)
        store.settle_factory_execution(self.conn, first["job"]["job_id"], claim, effect_observed=False)
        # Exercise the absorbing grant evidence with an intentionally inconsistent
        # synthetic released row: even terminal status cannot erase authorization.
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET effect_authorized_at=? WHERE attempt_id=?", (store.utc_now(), first["attempt"]["attempt_id"]))
        with self.assertRaisesRegex(store.BridgeConflict, "conclusively pre-effect"): self.preview()
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET effect_authorized_at=NULL WHERE attempt_id=?", (first["attempt"]["attempt_id"],))
            store._append_factory_attempt_event(self.conn, first["attempt"]["attempt_id"], "effect_authorized", {"fixture": True})
        with self.assertRaisesRegex(store.BridgeConflict, "conclusively pre-effect"): self.preview()
        self.assertEqual(store.get_job(self.conn, first["job"]["job_id"]), failed)

    def test_authorized_unknown_repair_keeps_the_absorbing_execution_guard(self):
        self.inspect(); first = self.confirm(self.preview()["snapshot_digest"])
        store.transition_job(self.conn, first["job"]["job_id"], state="running")
        claim = store.claim_factory_execution(self.conn, first["job"]["job_id"], owner="executor")["lease"]
        self.assertTrue(store.authorize_factory_effect(self.conn, first["job"]["job_id"], claim))
        store.settle_factory_execution(self.conn, first["job"]["job_id"], claim, effect_observed=False)
        self.assertEqual(store.get_factory_attempt(self.conn, first["attempt"]["attempt_id"])["status"], "effect_outcome_unknown")
        self.intent["source_attempt_id"] = first["attempt"]["attempt_id"]
        with self.assertRaisesRegex(store.BridgeConflict, "unsettled"):
            review_repair.inspect(self.conn, issue="ENG-8", source_attempt_id=first["attempt"]["attempt_id"], actor=ACTOR)

    def test_non_bmp_feedback_confirmed_in_python_passes_the_executor_fence(self):
        compiled = ROOT / "hatchet/workers/bridge-executor-worker/dist/workflows/factory-artifact-fence.js"
        if not shutil.which("node") or not compiled.is_file(): self.skipTest("built executor required for cross-language boundary")
        self.inspect()
        self.intent["feedback"] = "Bug. " * 399 + "Fix." + "😀"
        self.assertEqual(len(self.intent["feedback"]), 2000)
        approved = self.preview(); result = self.confirm(approved["snapshot_digest"])
        code = """
const { validateFactoryArtifactFence } = require(process.argv[1]);
const job = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fence = job.payload.factory_artifact_fence;
validateFactoryArtifactFence(job, { inspectDraftPrState: async () => ({
  repository: job.payload.repo, number: 77, url: fence.prior_pr_url, state: 'open',
  merged: false, draft: true, head_branch: job.branch, head_sha: fence.head_sha, base_ref: fence.base_ref, reason: 'ok',
}) }).then(result => process.stdout.write(JSON.stringify(result))).catch(() => process.exit(1));
"""
        checked = subprocess.run(["node", "-e", code, str(compiled)], input=json.dumps(result["job"]), capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), {"ok": True})
        with self.assertRaises(operator.OperatorError):
            review_repair.preview(**self.paths, **{**self.intent, "feedback": self.intent["feedback"] + "😀"})

    def test_stale_non_draft_closed_merged_head_base_and_branch_guards(self):
        for changes in ({"state": "closed"}, {"merged": True}, {"draft": False}, {"head_sha": "b" * 40},
                        {"head_branch": "codex/other"}, {"base_ref": "other"}, {"reason": "inspect_unavailable"}):
            with self.subTest(changes=changes):
                self.inspect(**changes)
                with self.assertRaises(store.BridgeConflict): self.preview()
        self.inspect()
        with self.conn:
            self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(operator.OperatorError, "fresh typed"): self.preview()

    def test_snapshot_ingress_policy_mode_control_and_lineage_guards(self):
        self.inspect(); digest = self.preview()["snapshot_digest"]
        for changes in ({"status": "Blocked"}, {"status": "Done"}, {"blockedBy": ["issue"]}, {"delegate": "Driftward Factory"}):
            self.observe(**changes)
            with self.assertRaises(operator.OperatorError): self.confirm(digest)
        self.observe(status="In Review", title="new intent")
        with self.assertRaisesRegex(store.BridgeConflict, "snapshot changed"): self.confirm(digest)
        self.observe(status="In Review", dispatchEligible=False, reasonCode="linear_issue_not_ready")
        with patch.dict(os.environ, {"HATCHET_INTAKE_DRY_RUN": "true"}):
            with self.assertRaises(operator.OperatorError): self.preview()
        for field, value in (("pr_url", f"https://github.com/{REPO}/pull/78"), ("repository", "Driftward-LLC/factory"),
                             ("source_attempt_id", "missing"), ("reviewed_head_sha", "abc123")):
            with self.assertRaises(store.BridgeError): review_repair.preview(**self.paths, **{**self.intent, field: value})
        with self.assertRaises(store.BridgeValidationError):
            review_repair.preview(**{**self.paths, "actor": "uid:1000;pid:1;account:user"}, **self.intent)
        control = store.get_factory_issue_control(self.conn, "ENG-8")
        store.take_factory_issue_control(self.conn, work_item_key="ENG-8", request_id="hold", reason="manual", actor=ACTOR,
            expected_revision=control["revision"], expected_snapshot_digest=control["snapshot_digest"])
        with self.assertRaisesRegex(store.BridgeConflict, "held"): self.preview()

    def test_disabled_route_changed_policy_and_unreleased_leases_fail_closed(self):
        self.inspect()
        catalog = json.loads(Path(ROUTES).read_text())
        catalog["routes"] = [{**route, "execution_enabled": False} if route["repo"] == REPO else route for route in catalog["routes"]]
        routes = self.root / "routes.json"; routes.write_text(json.dumps(catalog))
        with self.assertRaises(operator.OperatorError):
            review_repair.preview(**{**self.paths, "registry_path": str(routes)}, **self.intent)
        catalog = json.loads(Path(POLICY).read_text())
        for policy in catalog["policies"]:
            if policy["repo"] == REPO: policy["required_local_commands"].append("new-required-command")
        policies = self.root / "policy.json"; policies.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(store.BridgeConflict, "policy changed"):
            review_repair.preview(**{**self.paths, "policy_path": str(policies)}, **self.intent)
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET released_at=NULL,expires_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(store.BridgeConflict, "unsettled"): self.preview()
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET released_at=?", (store.utc_now(),))
            self.conn.execute("UPDATE verification_leases SET closed_at=NULL,expires_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(store.BridgeConflict, "verifier lease"): self.preview()

    def test_feedback_is_bounded_and_sanitized_before_reservation(self):
        self.inspect()
        with self.assertRaises(operator.OperatorError):
            review_repair.preview(**self.paths, **{**self.intent, "feedback": "x" * 2001})
        self.intent["feedback"] = "Fix input checks; Bearer fixture-sensitive-token-for-redaction"
        approved = self.preview()
        self.assertNotIn("fixture-sensitive-token-for-redaction", approved["context"]["feedback"])
        result = self.confirm(approved["snapshot_digest"])
        self.assertNotIn("fixture-sensitive-token-for-redaction", json.dumps(result))

    def test_stale_source_and_corrupt_lineage_do_not_reserve(self):
        self.inspect()
        source_payload = dict(self.job["payload"])
        source_payload["factory_retry"] = {**source_payload["factory_retry"], "attempt_id": "wrong"}
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET payload_json=? WHERE job_id=?", (json.dumps(source_payload), self.job["job_id"]))
        with self.assertRaisesRegex(store.BridgeConflict, "lineage"): self.preview()
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET payload_json=? WHERE job_id=?", (json.dumps(self.job["payload"]), self.job["job_id"]))
        digest = self.preview()["snapshot_digest"]
        self.confirm(digest)
        with self.assertRaisesRegex(store.BridgeConflict, "latest"): self.preview()

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("jq") and shutil.which("curl"), "root Unix CLI fixture")
    def test_real_cli_unix_operator_preview_confirm_and_peer_actor(self):
        env_file = self.root / "operator.env"; env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture-local\n"); env_file.chmod(0o600)
        socket_path = str(self.root / "operator.sock")
        server = operator._UnixHTTPServer(socket_path, operator.OperatorHandler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {"HATCHET_FACTORY_OPERATOR_TOKEN": "fixture-local", "FACTORY_OPERATOR_ENV": str(env_file),
                                    "FACTORY_OPERATOR_SOCKET": socket_path}):
            prefix = [str(ROOT / "factory/factory"), "review-repair", "ENG-8"]
            def run(phase, *extra):
                result = subprocess.run(prefix + [phase, "--source-attempt", self.attempt["attempt_id"], *extra], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            run("inspect", "--yes")
            row = self.conn.execute("SELECT * FROM factory_inspection_requests ORDER BY created_at DESC LIMIT 1").fetchone()
            store.complete_pr_inspection(self.conn, row["job_id"], {"repository": REPO, "number": 77, "url": PR, "state": "open",
                "merged": False, "draft": True, "head_branch": self.job["branch"], "head_sha": "a" * 40, "base_ref": "main", "reason": "ok"},
                request_key=row["request_key"], capability=row["capability"])
            self.intent["feedback"] = "😀" * 2000
            flags = ["--repo", REPO, "--pr", PR, "--branch", self.job["branch"], "--reviewed-head", "a" * 40,
                     "--feedback", self.intent["feedback"], "--reason", self.intent["reason"]]
            if self.intent.get("policy_reauthorization"):
                flags += ["--from-policy-digest", self.intent["policy_reauthorization"]["source_digest"],
                          "--to-policy-digest", self.intent["policy_reauthorization"]["target_digest"]]
            before = self.counts(); preview = run("preview", *flags); self.assertEqual(self.counts(), before)
            # JSON escaping takes 12 ASCII bytes per supplementary code point.
            escaped = subprocess.run(["curl", "--silent", "--show-error", "--fail", "--unix-socket", socket_path,
                "-H", "Authorization: Bearer fixture-local", "-H", "Content-Type: application/json", "--data-binary", "@-",
                "http://localhost/v1/review-repair/preview"], input=json.dumps(self.intent), capture_output=True, text=True, timeout=10)
            self.assertEqual(escaped.returncode, 0, escaped.stderr)
            self.assertEqual(json.loads(escaped.stdout)["snapshot_digest"], preview["snapshot_digest"])
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5); client.connect(socket_path)
                client.sendall(b"POST /v1/review-repair/preview HTTP/1.0\r\nAuthorization: Bearer fixture-local\r\nContent-Length: 32769\r\n\r\n")
                response = b""
                while chunk := client.recv(4096): response += chunk
            self.assertIn(b"400 Bad Request", response)
            self.assertIn(b"bounded limit", response)
            self.assertEqual(self.counts(), before)
            result = run("confirm", *flags, "--request-id", "cli-review", "--snapshot-digest", preview["snapshot_digest"], "--yes")
            self.assertTrue(result["created"])
            self.assertEqual(result["job"]["payload"]["factory_review_repair"]["feedback"], self.intent["feedback"])
            self.assertRegex(result["attempt"]["actor"], r"^uid:0;pid:[0-9]+;account:root$")


class PolicyReauthorizationTests(unittest.TestCase):
    observe = ReviewRepairTests.observe
    finish = ReviewRepairTests.finish
    inspect = ReviewRepairTests.inspect
    preview = ReviewRepairTests.preview
    confirm = ReviewRepairTests.confirm
    counts = ReviewRepairTests.counts

    def setUp(self):
        old_tmp = tempfile.TemporaryDirectory(); self.addCleanup(old_tmp.cleanup)
        catalog = json.loads(Path(POLICY).read_text())
        old = next(policy for policy in catalog["policies"] if policy["repo"] == REPO)
        old["allowed_changed_paths"].remove(".gitleaksignore")
        old_path = Path(old_tmp.name) / "policy.json"; old_path.write_text(json.dumps(catalog))
        self.old_policy_path = str(old_path)
        self.old_policy = operator._policy(REPO, str(old_path))
        with patch(f"{__name__}.POLICY", str(old_path)), patch("test_store.canonical_verifier_policy", return_value=self.old_policy):
            payload = verifier_payload(REPO)
            payload["implementation_prompt"] = "Repair only the reviewed false-positive secret fingerprints."
            with patch(f"{__name__}.verifier_payload", return_value=payload):
                ReviewRepairTests.setUp(self)
        self.paths["policy_path"] = POLICY
        os.environ["HATCHET_VERIFIER_POLICY_PATH"] = POLICY
        self.current_policy = operator._policy(REPO, POLICY)
        self.transition = {"source_digest": self.old_policy["digest"], "target_digest": self.current_policy["digest"]}

    def authorize_transition(self):
        self.intent["policy_reauthorization"] = dict(self.transition)
        self.inspect()
        return self.preview()

    def test_explicit_transition_preserves_source_and_uses_current_enforcement(self):
        self.assertEqual(self.old_policy["digest"], "0ce5a999e7c58b67904da5b57fa8855e6780a29a03fa193c55baf525323e2494")
        self.inspect()
        with self.assertRaisesRegex(store.BridgeConflict, "explicit compatible"): self.preview()
        source = store.get_job(self.conn, self.job["job_id"])
        attempt = store.get_factory_attempt(self.conn, self.attempt["attempt_id"])
        approved = self.authorize_transition()
        self.assertEqual(approved["context"]["source_policy"], self.old_policy)
        result = self.confirm(approved["snapshot_digest"])
        payload = result["job"]["payload"]
        store._validate_payload_policy(result["job"])
        self.assertEqual(payload["verification_policy"], self.current_policy)
        self.assertEqual(payload["independent_review_enrollment"], source["payload"]["independent_review_enrollment"])
        self.assertEqual(payload["factory_review_repair"]["approval"]["policy_reauthorization"], self.transition)
        self.assertEqual(store.get_job(self.conn, source["job_id"]), source)
        self.assertEqual(store.get_factory_attempt(self.conn, attempt["attempt_id"]), attempt)
        event = self.conn.execute("SELECT details_json FROM factory_attempt_events WHERE attempt_id=? AND event_type='operator_review_repair_authorized'", (result["attempt"]["attempt_id"],)).fetchone()
        self.assertEqual(json.loads(event[0])["policy_reauthorization"], self.transition)
        self.assertFalse(self.confirm(approved["snapshot_digest"])["created"])
        with self.assertRaises(store.BridgeConflict): self.confirm(approved["snapshot_digest"], policy_reauthorization={**self.transition, "target_digest": "f" * 64})

    def test_wrong_digests_changed_policy_and_nonroot_fail_without_writes(self):
        self.inspect(); before = self.counts()
        for transition in ({**self.transition, "source_digest": "f" * 64}, {**self.transition, "target_digest": "f" * 64}):
            with self.assertRaises(store.BridgeConflict): review_repair.preview(**self.paths, **self.intent, policy_reauthorization=transition)
        for mutation in ({"expected_base": "other"}, {"id": "new-id"}, {"catalog_version": 2},
                         {"required_local_commands": ["true"]}, {"forbidden_changed_paths": ["elsewhere"]},
                         {"secret_check_command": "true"}, {"allowed_changed_paths": self.current_policy["allowed_changed_paths"] + [".gitleaks.toml"]}):
            target = {**self.current_policy, **mutation}
            target["digest"] = store.payload_hash({k: v for k, v in target.items() if k not in {"digest", "catalog_version"}})
            intent = {**self.intent, "policy_reauthorization": {**self.transition, "target_digest": target["digest"]}}
            with self.subTest(mutation=mutation), self.assertRaises(store.BridgeConflict):
                review_repair._reauthorize_policy(self.old_policy, target, intent)
        with self.assertRaises(store.BridgeValidationError):
            review_repair.preview(**{**self.paths, "actor": "uid:1000;pid:1;account:user"}, **self.intent, policy_reauthorization=self.transition)
        for transition in ({"source_digest": "f" * 64}, {"source_digest": 3, "target_digest": "f" * 64}, None):
            with self.assertRaises(operator.OperatorError): review_repair.preview(**self.paths, **self.intent, policy_reauthorization=transition)
        self.assertEqual(self.counts(), before)

    def test_confirmation_rechecks_policy_and_exact_artifact(self):
        approved = self.authorize_transition(); before = self.counts()
        catalog = json.loads(Path(POLICY).read_text())
        next(p for p in catalog["policies"] if p["repo"] == REPO)["required_local_commands"].append("new-required-command")
        changed = self.root / "changed.json"; changed.write_text(json.dumps(catalog))
        with self.assertRaises(store.BridgeConflict):
            review_repair.confirm(**{**self.paths, "policy_path": str(changed)}, **self.intent, request_id="changed", snapshot_digest=approved["snapshot_digest"])
        with self.conn:
            self.conn.execute("UPDATE factory_inspection_requests SET observed_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(operator.OperatorError, "fresh typed"): self.confirm(approved["snapshot_digest"])
        self.assertEqual(self.counts(), before)
        self.inspect(head_sha="b" * 40)
        with self.assertRaisesRegex(store.BridgeConflict, "exact draft"): self.confirm(approved["snapshot_digest"])

    def test_failed_pre_effect_repair_can_be_reauthorized_without_rewriting_history(self):
        with patch.dict(os.environ, {"HATCHET_VERIFIER_POLICY_PATH": self.old_policy_path}):
            old_paths = {**self.paths, "policy_path": self.old_policy_path}
            self.inspect()
            approved = review_repair.preview(**old_paths, **self.intent)
            previous = review_repair.confirm(**old_paths, **self.intent, request_id="old-repair", snapshot_digest=approved["snapshot_digest"])
            failed = store.transition_job(self.conn, previous["job"]["job_id"], state="failed", details={"reason_code": "real_create_draft_pr_disabled"})
        self.intent["source_attempt_id"] = previous["attempt"]["attempt_id"]
        approved = self.authorize_transition()
        self.assertEqual(approved["context"]["artifact_lineage"]["artifact_attempt_id"], self.attempt["attempt_id"])
        result = self.confirm(approved["snapshot_digest"])
        self.assertEqual(result["attempt"]["prior_attempt_id"], previous["attempt"]["attempt_id"])
        self.assertEqual(result["job"]["payload"]["verification_policy"], self.current_policy)
        self.assertEqual(store.get_job(self.conn, failed["job_id"]), failed)
        self.assertIsNone(failed["pr_url"])
        self.assertIsNone(failed["commit_sha"])

    def test_new_intake_and_reauthorized_repair_keep_review_enrollment_and_budget(self):
        payload = verifier_payload(REPO)
        fresh, job, _ = store.reserve_factory_intake(self.conn, work_item_key="ENG-9", mode="real", request_id="fresh", reason="fresh", actor="intake-worker", payload=payload)
        self.assertEqual(job["payload"]["verification_policy"], self.current_policy)
        self.assertEqual(job["payload"]["independent_review_enrollment"], {"policy_digest": independent_review.policy()["digest"]})
        approved = self.authorize_transition(); result = self.confirm(approved["snapshot_digest"])
        self.finish(result["job"], sha="b" * 40)
        self.intent.update(source_attempt_id=result["attempt"]["attempt_id"], reviewed_head_sha="b" * 40)
        self.inspect()
        row = independent_review.reserve(self.conn, result["job"]["job_id"])
        self.assertEqual(row["pass_number"], 1)
        self.assertEqual(row["head_sha"], "b" * 40)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(independent_review.reserve(self.conn, result["job"]["job_id"])["review_id"], row["review_id"])
        self.assertTrue(independent_review.claim(self.conn, row["job_id"], row["capability"]))
        self.assertTrue(independent_review.authorization(self.conn, row["job_id"], row["capability"])["invocation_allowed"])
        # Another root authorization cannot reset the existing PR's review lineage.
        self.intent.pop("policy_reauthorization")
        second = review_repair.confirm(**self.paths, **self.intent, request_id="next", snapshot_digest=self.preview()["snapshot_digest"])
        self.finish(second["job"], sha="c" * 40)
        self.intent.update(source_attempt_id=second["attempt"]["attempt_id"], reviewed_head_sha="c" * 40)
        self.inspect()
        with self.assertRaisesRegex(store.BridgeConflict, "continuation requires"):
            independent_review.reserve(self.conn, second["job"]["job_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_independent_reviews").fetchone()[0], 1)

    def test_reauthorized_job_passes_real_gateway_and_worker_policy_boundaries(self):
        compiled = ROOT / "hatchet/workers/bridge-executor-worker/dist"
        if not shutil.which("node") or not (compiled / "hermes/create-draft-pr.js").is_file():
            self.skipTest("built executor required for cross-language boundary")
        approved = self.authorize_transition(); result = self.confirm(approved["snapshot_digest"])
        code = """
const root = process.argv[1];
const { validateRealCreateDraftPrJob } = require(root + '/hermes/create-draft-pr.js');
const { validateFactoryArtifactFence } = require(root + '/workflows/factory-artifact-fence.js');
const job = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
validateRealCreateDraftPrJob(job);
const fence = job.payload.factory_artifact_fence;
validateFactoryArtifactFence(job, { inspectDraftPrState: async () => ({
  repository: job.payload.repo, number: 77, url: fence.prior_pr_url, state: 'open',
  merged: false, draft: true, head_branch: job.branch, head_sha: fence.head_sha, base_ref: fence.base_ref, reason: 'ok',
}) }).then(result => {
  if (!result.ok) process.exit(2);
  job.payload.verification_policy.allowed_changed_paths.push('.gitleaks.toml');
  try { validateRealCreateDraftPrJob(job); process.exit(3); } catch { process.stdout.write('accepted current; rejected mutation'); }
}).catch(() => process.exit(1));
"""
        checked = subprocess.run(["node", "-e", code, str(compiled)], input=json.dumps(result["job"]), capture_output=True, text=True,
            env={**os.environ, "DRIFTWARD_HERMES_ALLOWED_REPOS": REPO}, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout, "accepted current; rejected mutation")

    def test_real_cli_reauthorization_preview_confirm(self):
        self.intent["policy_reauthorization"] = dict(self.transition)
        ReviewRepairTests.test_real_cli_unix_operator_preview_confirm_and_peer_actor(self)
