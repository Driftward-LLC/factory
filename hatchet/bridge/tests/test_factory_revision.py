import hashlib
import http.client
import json
import os
import sqlite3
import threading
import subprocess
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from hatchet_hermes_bridge import operator, store
from hatchet_hermes_bridge.console_control import IssueControl
from test_console_issue_control import ACTOR, ORIGIN, ROOT, IssueControlFixture


class RevisionTests(IssueControlFixture):
    def setUp(self):
        super().setUp()
        self.paths = {"bridge_db": self.db, "ingress_db": self.ingress, "registry_path": os.environ["HATCHET_INTAKE_ROUTING_REGISTRY_PATH"],
                      "policy_path": os.environ["HATCHET_VERIFIER_POLICY_PATH"], "issue": "ENG-8"}
        preview = operator.preview_retry(**self.paths, mode="real")
        self.first = operator.reserve_retry(**self.paths, mode="real", request_id="implementation", reason="fixture", actor=ACTOR, snapshot_digest=preview["snapshot_digest"])
        self.pr_url = "https://github.com/Driftward-LLC/capykit/pull/80"
        self.head = "a" * 40
        self.result = {"repository": "Driftward-LLC/capykit", "number": 80, "url": self.pr_url, "state": "open", "merged": False, "draft": True,
                       "head_branch": self.first["job"]["branch"], "head_sha": self.head, "base_ref": "main", "reason": "ok"}
        with closing(store.connect_runtime(self.db)) as conn, conn:
            conn.execute("UPDATE bridge_jobs SET state='ready_for_human_review',pr_url=?,commit_sha=? WHERE job_id=?", (self.pr_url, self.head, self.first["job"]["job_id"]))
            conn.execute("UPDATE factory_attempts SET status='verified_ready',prior_pr_url=? WHERE attempt_id=?", (self.pr_url, self.first["attempt"]["attempt_id"]))
            conn.execute("INSERT INTO factory_inspection_requests(request_key,repository,number,url,work_item_key,prior_attempt_id,state,result_json,result_digest,observed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("revision-observation", self.result["repository"], 80, self.pr_url, "ENG-8", self.first["attempt"]["attempt_id"], "completed",
                          store.canonical_json(self.result), hashlib.sha256(store.canonical_json(self.result).encode()).hexdigest(), store.utc_now(), store.utc_now(), store.utc_now()))
        self.feedback = {"feedback": "Prevent duplicate grant submissions and rerun the focused tests."}

    def command(self):
        return self.approval("revision", reason="Address approved feedback", revision=self.feedback)

    def alter_inspection(self, **changes):
        self.result.update(changes)
        with closing(store.connect_runtime(self.db)) as conn, conn:
            conn.execute("UPDATE factory_inspection_requests SET result_json=?,result_digest=? WHERE request_key='revision-observation'",
                         (store.canonical_json(self.result), hashlib.sha256(store.canonical_json(self.result).encode()).hexdigest()))

    def counts(self):
        with closing(store.connect_readonly(self.db)) as conn:
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("factory_attempts", "factory_attempt_outbox", "factory_execution_manifests"))

    def test_preview_read_only_and_new_attempt_preserves_old_history_and_pr(self):
        with closing(store.connect_readonly(self.db)) as conn:
            old = store.get_factory_attempt(conn, self.first["attempt"]["attempt_id"])
            events = store.list_factory_attempt_events(conn, old["attempt_id"])
        before = self.counts()
        command = self.command()
        self.assertEqual(self.counts(), before)
        result = self.service.confirm("ENG-8", ACTOR, command)
        with closing(store.connect_readonly(self.db)) as conn:
            attempt = store.get_factory_attempt(conn, result["attempt_id"])
            job = store.get_job(conn, attempt["job_id"])
            self.assertEqual(store.get_factory_attempt(conn, old["attempt_id"]), old)
            self.assertEqual(store.list_factory_attempt_events(conn, old["attempt_id"]), events)
            self.assertEqual(job["branch"], self.first["job"]["branch"])
            self.assertEqual(attempt["prior_attempt_id"], old["attempt_id"])
            self.assertEqual(attempt["generation"], 2)
            self.assertEqual(job["requested_action"], "repair_draft_pr")
            self.assertEqual(job["payload"]["factory_repair"]["trigger"], "review_feedback")
            self.assertNotIn("reason_code", job["payload"]["factory_repair"])
            self.assertEqual(job["payload"]["factory_repair"]["pr_url"], self.pr_url)
            revision = job["payload"]["factory_revision"]
            self.assertEqual(revision["feedback"], self.feedback["feedback"])
            self.assertEqual(revision["reviewed_head_sha"], self.head)
            self.assertEqual(revision["approval_kind"], "operator_attestation")
            self.assertIsNone(revision["review_url"])
            evidence = store.get_factory_work_evidence(conn, attempt["attempt_id"])
            self.assertEqual(evidence["manifests"][0]["document"]["revision"]["independent_review_result"], "unknown")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_issue_control_commands").fetchone()[0], 0)
        self.assertEqual(self.counts(), tuple(value + 1 for value in before))
        from hatchet_hermes_bridge.analytics import factory_analytics
        analytics = factory_analytics(self.db, self.ingress, str(Path(self.temp.name) / "absent-lifecycle.db"))
        self.assertEqual(analytics["recovery"]["by_attempt_kind"]["revision"]["attempts"], 1)
        self.assertEqual(analytics["recovery"]["by_attempt_kind"]["repair"]["attempts"], 0)

    def test_commit_before_response_replays_after_restart_expiry_mode_and_ingress_churn(self):
        command = self.command()
        original = self.service.confirm("ENG-8", ACTOR, command)
        with sqlite3.connect(self.ingress) as conn:
            conn.execute("DELETE FROM ingress_events")
        before = self.counts()
        restarted = IssueControl(self.db, self.ingress)
        with patch("hatchet_hermes_bridge.console_control.time.time", return_value=9999999999), patch.dict(os.environ, {"HATCHET_INTAKE_DRY_RUN": "true"}):
            replay = restarted.confirm("ENG-8", ACTOR, command)
        self.assertEqual(replay["attempt_id"], original["attempt_id"])
        self.assertEqual(self.counts(), before)
        with self.assertRaises(store.BridgeConflict):
            restarted.confirm("ENG-8", ACTOR, {**command, "revision": {"feedback": "Different feedback"}})

    def test_double_confirmation_converges_atomically(self):
        command = self.command()
        barrier = threading.Barrier(2)
        def confirm():
            barrier.wait()
            return self.service.confirm("ENG-8", ACTOR, command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual(results[0]["attempt_id"], results[1]["attempt_id"])
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_atomic_rollback_leaves_no_job_attempt_or_outbox(self):
        command = self.command()
        with patch("hatchet_hermes_bridge.store._record_factory_admission_evidence", side_effect=RuntimeError("injected evidence failure")):
            with self.assertRaises(RuntimeError):
                self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_manual_takeover_between_preview_confirm_blocks_revision(self):
        command = self.command()
        self.take()
        with self.assertRaises(store.BridgeConflict):
            self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_changed_head_and_closed_merged_non_draft_unknown_fail_closed(self):
        command = self.command()
        for changes in ({"head_sha": "b" * 40}, {"head_branch": "codex/other"}, {"state": "closed"}, {"merged": True}, {"draft": False}, {"reason": "inspect_unavailable"}):
            with self.subTest(changes=changes):
                original = dict(self.result)
                self.alter_inspection(**changes)
                with self.assertRaises((operator.OperatorError, store.BridgeConflict)):
                    self.service.confirm("ENG-8", ACTOR, command)
                self.alter_inspection(**original)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_latest_lifecycle_status_allowed_but_terminal_or_blocked_denied(self):
        from test_operator import observation
        for index, status in enumerate(("In Review", "Needs Operator", "Ready", "Done", "Canceled", "Blocked"), 2):
            with sqlite3.connect(self.ingress) as conn:
                conn.execute("INSERT INTO ingress_events VALUES (?,'linear','ENG-8',?,?)", (str(index), json.dumps(observation(status=status, reasonCode="linear_issue_factory_lifecycle_actor")), index))
            if status in {"In Review", "Needs Operator", "Ready"}:
                self.assertTrue(operator.preview_revision(**self.paths, revision=self.feedback)["eligible"])
            else:
                with self.assertRaises(operator.OperatorError):
                    operator.preview_revision(**self.paths, revision=self.feedback)

    def test_dry_run_rejects_new_revision_and_stale_inspection_needs_explicit_refresh(self):
        command = self.command()
        with patch.dict(os.environ, {"HATCHET_INTAKE_DRY_RUN": "true"}):
            with self.assertRaises((store.BridgeConflict, operator.OperatorError)):
                self.service.confirm("ENG-8", ACTOR, command)
        with closing(store.connect_runtime(self.db)) as conn, conn:
            conn.execute("UPDATE factory_inspection_requests SET observed_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(operator.OperatorError, "inspection_pending"):
            operator.preview_revision(**self.paths, revision=self.feedback)
        before = self.counts()
        result = operator.inspect_revision(**self.paths)
        self.assertTrue(result["created"])
        self.assertEqual(self.counts(), before)
        with closing(store.connect_readonly(self.db)) as conn:
            jobs = list(conn.execute("SELECT requested_action FROM bridge_jobs ORDER BY created_at"))
            self.assertEqual([row[0] for row in jobs], ["create_draft_pr", "inspect_draft_pr_state"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_outbox").fetchone()[0], 1)

    def test_root_operator_replay_binds_actor_feedback_references_and_head(self):
        revision = {**self.feedback, "review_url": "https://github.com/Driftward-LLC/capykit/pull/80#review", "reviewer": "independent reviewer", "review_model": "gpt-6-astra", "design_url": "https://example.test/spec"}
        preview = operator.preview_revision(**self.paths, revision=revision)
        kwargs = {**self.paths, "revision": revision, "request_id": "root-revision", "reason": "review feedback", "actor": "uid:0", "snapshot_digest": preview["snapshot_digest"]}
        first = operator.reserve_revision(**kwargs)
        self.assertFalse(operator.reserve_revision(**kwargs)["created"])
        self.assertEqual(first["attempt"]["generation"], 2)
        for changes in ({"actor": "other"}, {"revision": {"feedback": "different"}}, {"snapshot_digest": "b" * 64}):
            with self.assertRaises(store.BridgeConflict):
                operator.reserve_revision(**{**kwargs, **changes})

    def test_revision_verifier_repair_rebuilds_head_and_does_not_inherit_approval(self):
        result = self.service.confirm("ENG-8", ACTOR, self.command())
        with closing(store.connect_runtime(self.db)) as conn, conn:
            attempt = store.get_factory_attempt(conn, result["attempt_id"])
            conn.execute("UPDATE bridge_jobs SET state='failed',pr_url=?,commit_sha=?,reason_code='verification_github_check_failed' WHERE job_id=?", (self.pr_url, "b" * 40, attempt["job_id"]))
            conn.execute("UPDATE factory_attempts SET status='failed' WHERE attempt_id=?", (attempt["attempt_id"],))
            failed = store.get_job(conn, attempt["job_id"])
            with patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "10"}):
                repair = store._reserve_factory_verification_repair(conn, failed_job=failed, factory_attempt=store.get_factory_attempt(conn, attempt["attempt_id"]), details={"error": "checks failed"})
            self.assertIsNotNone(repair)
            repaired_attempt, job, created = repair
            self.assertTrue(created)
            self.assertEqual(repaired_attempt["generation"], 3)
            self.assertNotIn("factory_revision", job["payload"])
            self.assertEqual(job["payload"]["factory_artifact_fence"]["head_sha"], "b" * 40)
            self.assertEqual(job["payload"]["factory_artifact_fence"]["prior_attempt_id"], attempt["attempt_id"])
            self.assertEqual(job["payload"]["factory_repair"]["reason_code"], "verification_github_check_failed")
            self.assertEqual(job["branch"], self.first["job"]["branch"])

    def test_revision_input_bounds_and_optional_reviewed_head(self):
        preview = operator.preview_revision(**self.paths, revision=self.feedback)
        self.assertEqual(preview["revision"]["reviewed_head_sha"], self.head)
        for value in ({}, {"feedback": "x" * 6001}, {"feedback": "ok", "review_url": "file:///root/secrets"}, {"feedback": "ok", "reviewed_head_sha": "b" * 40}):
            with self.assertRaises((store.BridgeValidationError, store.BridgeConflict)):
                operator.preview_revision(**self.paths, revision=value)

    def test_real_browser_api_revision_auth_preview_confirm_and_inspection(self):
        from hatchet_hermes_bridge.console import make_console_server
        server = make_console_server(self.db, "127.0.0.1", 0, ROOT / "factory/console", ingress_db_path=self.ingress)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body, origin=ORIGIN, actor=ACTOR):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("POST", "/api/issues/ENG-8/" + path, json.dumps(body), {"Tailscale-User-Login": actor, "Origin": origin,
                    "X-Factory-Action": "issue-control", "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            body = {"action": "revision", "reason": "approved feedback", "revision": self.feedback}
            self.assertEqual(request("control/preview", body, origin="https://evil.test")[0], 403)
            self.assertEqual(request("revision/inspect", {}, actor="reader@test")[0], 403)
            self.assertEqual(request("revision/inspect", {"command": "run agent"})[0], 400)
            self.assertEqual(request("revision/inspect", {})[0], 200)
            status, preview = request("control/preview", body)
            self.assertEqual(status, 200, preview)
            self.assertEqual(preview["artifact"]["head_sha"], self.head)
            status, result = request("control/confirm", {**body, "snapshot_digest": preview["snapshot_digest"], "preview_token": preview["preview_token"], "request_id": "api-revision"})
            self.assertEqual(status, 200, result)
            self.assertTrue(result["attempt_id"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(os.geteuid() == 0, "root operator Unix peer credentials require root")
    def test_real_cli_preview_reserve_and_replay_through_unix_operator(self):
        socket_path = str(Path(self.temp.name) / "operator.sock")
        token_file = Path(self.temp.name) / "operator.env"
        token_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=isolated-test-token\n")
        token_file.chmod(0o600)
        server = operator._UnixHTTPServer(socket_path, operator.OperatorHandler)
        os.chmod(socket_path, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "HATCHET_FACTORY_OPERATOR_TOKEN": "isolated-test-token", "HATCHET_FACTORY_OPERATOR_BRIDGE_DB": self.db,
               "HATCHET_FACTORY_OPERATOR_INGRESS_DB": self.ingress, "FACTORY_OPERATOR_ENV": str(token_file), "FACTORY_OPERATOR_SOCKET": socket_path}
        def run(*args):
            with patch.dict(os.environ, env):
                return subprocess.run([str(ROOT / "factory/factory"), "revise", "ENG-8", *args], env=env, capture_output=True, text=True, timeout=10)
        try:
            preview = run("preview", "--feedback", self.feedback["feedback"])
            self.assertEqual(preview.returncode, 0, preview.stderr)
            digest = json.loads(preview.stdout)["snapshot_digest"]
            flags = ["--feedback", self.feedback["feedback"], "--reason", "approved feedback", "--request-id", "cli-revision", "--snapshot-digest", digest]
            self.assertNotEqual(run(*flags).returncode, 0)
            reserved = run(*flags, "--yes")
            self.assertEqual(reserved.returncode, 0, reserved.stderr)
            self.assertTrue(json.loads(reserved.stdout)["created"])
            replay = run(*flags, "--yes")
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertFalse(json.loads(replay.stdout)["created"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
