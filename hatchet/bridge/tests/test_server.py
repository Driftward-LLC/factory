import tempfile
import unittest
import os
import hashlib
import json
import threading
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path

from hatchet_hermes_bridge.server import BridgeHandler, _bounded_limit, _error_response, _finding_batch
from hatchet_hermes_bridge.store import BridgeValidationError, acquire_verification_lease, claim_verification_lease, list_events, submit_job, transition_job
from test_support import connect_migrate as connect


class BridgeServerTests(unittest.TestCase):
    def test_factory_execution_analytics_endpoint_is_fenced_and_idempotent(self):
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            from hatchet_hermes_bridge.store import reserve_factory_retry, claim_factory_execution, authorize_factory_effect
            attempt, job, _ = reserve_factory_retry(
                conn, work_item_key="ENG-40", mode="real", request_id="analytics-http",
                reason="test", actor="uid:0", payload={"repo": "Driftward-LLC/capykit"},
            )
            transition_job(conn, job["job_id"], state="running")
            lease = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
            self.assertTrue(authorize_factory_effect(conn, job["job_id"], lease))
            details = {
                "analytics_schema_version": 1,
                "invocation_id": "dispatch-http",
                "execution_backend": "hermes",
                "execution_transport": "grpc_uds",
                "backend_version": None,
                "configured_auth_mode": "unknown",
            }
            body = {
                **{key: lease[key] for key in ("lease_id", "owner", "epoch", "fence_token")},
                "event_type": "execution_backend_invoked",
                "details": details,
            }
            path = f"/jobs/{job['job_id']}/factory-execution/analytics"
            created = _PostHandler(conn, body, path); created.do_POST()
            self.assertEqual(created.response, (201, {"recorded": True}))
            replay = _PostHandler(conn, body, path); replay.do_POST()
            self.assertEqual(replay.response, (200, {"recorded": False}))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_backend_invoked'",
                (attempt["attempt_id"],),
            ).fetchone()[0], 1)

            completed = {
                **body,
                "event_type": "execution_backend_completed",
                "details": {**details, "backend_outcome": "created", "elapsed_ms": 100},
            }
            completed_handler = _PostHandler(conn, completed, path); completed_handler.do_POST()
            self.assertEqual(completed_handler.response, (201, {"recorded": True}))
            commit_failure = {
                **{key: lease[key] for key in ("lease_id", "owner", "epoch", "fence_token")},
                "effect_observed": False,
                "failure_event_type": "execution_effect_commit_failed",
                "failure_details": {
                    **details,
                    "failure_domain": "factory",
                    "failure_stage": "effect_commit",
                    "reason_code": "factory_effect_commit_failed",
                    "retryable": None,
                    "failure_disposition": "recovery_required",
                    "operator_intervention_required": True,
                    "effect_outcome": "observed",
                    "elapsed_ms": 110,
                    "observed_pr_url": "https://github.com/Driftward-LLC/capykit/pull/40",
                    "observed_commit_sha": None,
                    "observed_branch": job["branch"],
                    "observed_hermes_session_id": None,
                    "backend_outcome": "created",
                    "verification_context": None,
                },
            }
            settled = _PostHandler(
                conn, commit_failure, f"/jobs/{job['job_id']}/factory-execution/settle"
            ); settled.do_POST()
            self.assertEqual(settled.response, (200, {"ok": True}))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_effect_commit_failed'",
                (attempt["attempt_id"],),
            ).fetchone()[0], 1)

    def test_factory_intake_reserve_endpoint_creates_attempt_and_job_lineage(self):
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            body = {
                "work_item_key": "ENG-9",
                "mode": "real",
                "request_id": "linear:issue:ENG-9:update:delivery",
                "reason": "Linear Ready intake reserved a factory attempt",
                "hatchet_workflow_run_id": "workflow-1",
                "hatchet_step_run_id": "step-1",
                "payload": {"repo": "Driftward-LLC/capykit", "linear_issue": "ENG-9", "dry_run": False, "source_event_key": "linear:issue:ENG-9:update:delivery"},
            }
            handler = _PostHandler(conn, body, "/factory/intake/reserve")
            handler.do_POST()
            self.assertEqual(handler.response[0], 201)
            self.assertEqual(handler.response[1]["attempt"]["job_id"], handler.response[1]["job"]["job_id"])
            self.assertEqual(handler.response[1]["job"]["payload"]["factory_retry"]["attempt_id"], handler.response[1]["attempt"]["attempt_id"])
            self.assertEqual(handler.response[1]["job"]["hatchet_workflow_run_id"], "workflow-1")

    def test_lineaged_dry_run_completes_through_the_http_transition_api(self):
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            body = {
                "work_item_key": "ENG-10",
                "mode": "dry-run",
                "request_id": "linear:issue:ENG-10:update:delivery",
                "reason": "Linear Ready intake reserved a factory attempt",
                "payload": {"repo": "Driftward-LLC/capykit", "linear_issue": "ENG-10", "dry_run": True, "source_event_key": "linear:issue:ENG-10:update:delivery"},
            }
            reserved = _PostHandler(conn, body, "/factory/intake/reserve")
            reserved.do_POST()
            self.assertEqual(reserved.response[0], 201)
            job_id = reserved.response[1]["job"]["job_id"]
            attempt_id = reserved.response[1]["attempt"]["attempt_id"]

            for state, details in (
                ("running", {}),
                ("effect_committed", {"pr_url": f"dry-run://github/pr/{job_id}"}),
                ("completed", {"reason_code": "dry_run_completed"}),
            ):
                transition = _PostHandler(conn, {"state": state, "details": details}, f"/jobs/{job_id}/transitions")
                transition.do_POST()
                self.assertEqual(transition.response[0], 200)

            self.assertEqual(conn.execute("SELECT state FROM bridge_jobs WHERE job_id=?", (job_id,)).fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT status FROM factory_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_outbox").fetchone()[0], 0)

    def test_loopback_http_cannot_terminalize_committed_factory_job(self):
        saved = os.environ.get("HATCHET_HERMES_BRIDGE_TOKEN")
        self.addCleanup(lambda: os.environ.pop("HATCHET_HERMES_BRIDGE_TOKEN", None) if saved is None else os.environ.__setitem__("HATCHET_HERMES_BRIDGE_TOKEN", saved))
        os.environ["HATCHET_HERMES_BRIDGE_TOKEN"] = "general"
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            from hatchet_hermes_bridge.store import reserve_factory_retry, claim_factory_execution, authorize_factory_effect, commit_factory_execution, get_factory_attempt
            attempt, job, _ = reserve_factory_retry(conn, work_item_key="ENG-62", mode="real", request_id="http-post-commit", reason="test", actor="uid:0", payload={"repo":"Driftward-LLC/capykit"})
            transition_job(conn, job["job_id"], state="running", details={})
            claim = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
            self.assertTrue(authorize_factory_effect(conn, job["job_id"], claim))
            commit_factory_execution(conn, job["job_id"], claim, {"commit_sha":"abcdef1", "pr_url":"https://github.com/Driftward-LLC/capykit/pull/99", "branch":job["branch"]})
            server = HTTPServer(("127.0.0.1", 0), BridgeHandler); server.conn = conn
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
            endpoint = f"http://127.0.0.1:{server.server_port}/jobs/{job['job_id']}/transitions"
            for state in ("failed", "completed", "canceled"):
                with self.subTest(state=state), self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(endpoint, data=json.dumps({"state":state, "details":{}}).encode(), headers={"Authorization":"Bearer general"}), timeout=2)
                self.assertEqual(rejected.exception.code, 400)
            self.assertEqual(conn.execute("SELECT state FROM bridge_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], "effect_committed")
            self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "effect_committed")

    def test_active_factory_execution_rejects_generic_terminal_before_authorize(self):
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            from hatchet_hermes_bridge.store import reserve_factory_retry, claim_factory_execution, authorize_factory_effect
            attempt, job, _ = reserve_factory_retry(conn, work_item_key="ENG-62", mode="real", request_id="http-race", reason="test", actor="uid:0", payload={"repo":"Driftward-LLC/capykit"})
            transition_job(conn, job["job_id"], state="running", details={})
            lease = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
            failed = _PostHandler(conn, {"state":"failed", "details":{"reason_code":"forged"}}, f"/jobs/{job['job_id']}/transitions")
            failed.do_POST()
            self.assertEqual(failed.response[0], 400)
            self.assertTrue(authorize_factory_effect(conn, job["job_id"], lease))
            self.assertEqual(conn.execute("SELECT state FROM bridge_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], "running")
            self.assertEqual(conn.execute("SELECT status FROM factory_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0], "effect_authorized")
    def test_loopback_http_inspection_capability_boundary(self):
        saved = {key: os.environ.get(key) for key in ("HATCHET_HERMES_BRIDGE_TOKEN", "HATCHET_HERMES_INSPECTION_TOKEN")}
        self.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value) for key, value in saved.items()])
        os.environ["HATCHET_HERMES_BRIDGE_TOKEN"] = "general"; os.environ["HATCHET_HERMES_INSPECTION_TOKEN"] = "inspect"
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            from hatchet_hermes_bridge.store import _queue_pr_inspection
            _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "http-a"); conn.commit()
            request = conn.execute("SELECT * FROM factory_inspection_requests").fetchone()
            server = HTTPServer(("127.0.0.1", 0), BridgeHandler); server.conn = conn
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
            url = f"http://127.0.0.1:{server.server_port}/jobs/{request['job_id']}/inspection-result"
            result = {"repository":"Driftward-LLC/capykit","number":20,"url":"https://github.com/Driftward-LLC/capykit/pull/20","state":"closed","merged":False,"draft":True,"head_branch":"codex/hatchet-hermes-abc","head_sha":"abcdef1","base_ref":"main","reason":"ok"}
            body = json.dumps({"inspection_request_key":request["request_key"], "inspection_capability":request["capability"], "result":result}).encode()
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(url, data=body, headers={"Authorization":"Bearer general"}), timeout=2)
            self.assertEqual(denied.exception.code, 403)
            with urlopen(Request(url, data=body, headers={"Authorization":"Bearer general", "X-Hatchet-Inspection-Token":"inspect"}), timeout=2) as response:
                self.assertEqual(response.status, 200)
    def test_inspection_completion_requires_dedicated_token_and_bound_capability(self):
        saved = os.environ.get("HATCHET_HERMES_INSPECTION_TOKEN")
        self.addCleanup(lambda: os.environ.pop("HATCHET_HERMES_INSPECTION_TOKEN", None) if saved is None else os.environ.__setitem__("HATCHET_HERMES_INSPECTION_TOKEN", saved))
        os.environ["HATCHET_HERMES_INSPECTION_TOKEN"] = "inspection-only"
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            from hatchet_hermes_bridge.store import _queue_pr_inspection, get_job
            _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "legacy-a")
            conn.commit()
            request = conn.execute("SELECT * FROM factory_inspection_requests").fetchone()
            result = {"repository":"Driftward-LLC/capykit","number":20,"url":"https://github.com/Driftward-LLC/capykit/pull/20","state":"closed","merged":False,"draft":True,"head_branch":"codex/hatchet-hermes-abcdef","head_sha":"abcdef1","base_ref":"main","reason":"ok"}
            path = f"/jobs/{request['job_id']}/inspection-result"
            denied = _PostHandler(conn, {"inspection_request_key":request["request_key"], "inspection_capability":request["capability"], "result":result}, path, {"Authorization":"Bearer general"}); denied.do_POST()
            self.assertEqual(denied.response[0], 403)
            wrong = _PostHandler(conn, {"inspection_request_key":request["request_key"], "inspection_capability":"forged", "result":result}, path, {"X-Hatchet-Inspection-Token":"inspection-only"}); wrong.do_POST()
            self.assertEqual(wrong.response[0], 400)
            accepted = _PostHandler(conn, {"inspection_request_key":request["request_key"], "inspection_capability":request["capability"], "result":result}, path, {"X-Hatchet-Inspection-Token":"inspection-only"}); accepted.do_POST()
            self.assertEqual(accepted.response[0], 200)
            replay = _PostHandler(conn, {"inspection_request_key":request["request_key"], "inspection_capability":request["capability"], "result":result}, path, {"X-Hatchet-Inspection-Token":"inspection-only"}); replay.do_POST()
            self.assertEqual(replay.response[0], 200)
            self.assertEqual(get_job(conn, request["job_id"])["state"], "completed")
    def test_inspection_and_recovery_require_separate_credentials(self):
        saved = {key: os.environ.get(key) for key in ("HATCHET_HERMES_INSPECTION_TOKEN", "HATCHET_HERMES_RECOVERY_TOKEN")}
        self.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value) for key, value in saved.items()])
        os.environ["HATCHET_HERMES_INSPECTION_TOKEN"] = "inspect"
        os.environ["HATCHET_HERMES_RECOVERY_TOKEN"] = "recover"
        self.assertFalse(_ScopedHandler({"Authorization":"Bearer general"})._require_inspection_auth())
        self.assertTrue(_ScopedHandler({"X-Hatchet-Inspection-Token":"inspect"})._require_inspection_auth())
        self.assertFalse(_ScopedHandler({"X-Hatchet-Inspection-Token":"inspect"})._require_recovery_auth())
        self.assertTrue(_ScopedHandler({"X-Hatchet-Recovery-Token":"recover"})._require_recovery_auth())
    def test_verifier_authority_requires_a_dedicated_token(self):
        old = os.environ.get("HATCHET_HERMES_VERIFIER_TOKEN")
        self.addCleanup(lambda: os.environ.pop("HATCHET_HERMES_VERIFIER_TOKEN", None) if old is None else os.environ.__setitem__("HATCHET_HERMES_VERIFIER_TOKEN", old))
        os.environ["HATCHET_HERMES_VERIFIER_TOKEN"] = "verifier-test-only"
        denied = _VerifierHandler({"Authorization": "Bearer general-bridge-token"})
        self.assertFalse(denied._require_verifier_auth())
        self.assertEqual(denied.response, (403, {"error": "verifier authorization required"}))
        wrong = _VerifierHandler({"X-Hatchet-Verifier-Token": "wrong"})
        self.assertFalse(wrong._require_verifier_auth())
        accepted = _VerifierHandler({"X-Hatchet-Verifier-Token": "verifier-test-only"})
        self.assertTrue(accepted._require_verifier_auth())

    def test_verifier_token_still_requires_valid_durable_receipt(self):
        old_token = os.environ.get("HATCHET_HERMES_VERIFIER_TOKEN")
        old_policy = os.environ.get("HATCHET_VERIFIER_POLICY_PATH")
        self.addCleanup(lambda: os.environ.pop("HATCHET_HERMES_VERIFIER_TOKEN", None) if old_token is None else os.environ.__setitem__("HATCHET_HERMES_VERIFIER_TOKEN", old_token))
        self.addCleanup(lambda: os.environ.pop("HATCHET_VERIFIER_POLICY_PATH", None) if old_policy is None else os.environ.__setitem__("HATCHET_VERIFIER_POLICY_PATH", old_policy))
        os.environ["HATCHET_HERMES_VERIFIER_TOKEN"] = "verifier-test-only"
        os.environ["HATCHET_VERIFIER_POLICY_PATH"] = str(Path(__file__).resolve().parents[2] / "workers" / "verifier-policy.json")
        catalog = json.loads(Path(os.environ["HATCHET_VERIFIER_POLICY_PATH"]).read_text())
        policy = next(item for item in catalog["policies"] if item["repo"] == "Driftward-LLC/driftward-host")
        payload = {"repo": policy["repo"], "dry_run": False, "verification_policy": {"catalog_version": catalog["version"], "digest": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), **policy}}
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name)
            self.addCleanup(conn.close)
            job, _ = submit_job(conn, {"idempotency_key": "hatchet:server:receipt", "requested_action": "create_draft_pr", "payload": payload})
            from hatchet_hermes_bridge.store import transition_job
            transition_job(conn, job["job_id"], state="running")
            transition_job(conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/1"})
            transition_job(conn, job["job_id"], state="verification_pending")
            handler = _PostHandler(conn, {"state": "ready_for_human_review", "details": {"reason_code": "draft_pr_verified"}}, f"/jobs/{job['job_id']}/transitions", {"X-Hatchet-Verifier-Token": "verifier-test-only"})
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
            self.assertIn("verification completion", handler.response[1]["error"])

    def test_verification_failure_completion_requires_verifier_token_and_closes_once(self):
        old = os.environ.get("HATCHET_HERMES_VERIFIER_TOKEN")
        self.addCleanup(lambda: os.environ.pop("HATCHET_HERMES_VERIFIER_TOKEN", None) if old is None else os.environ.__setitem__("HATCHET_HERMES_VERIFIER_TOKEN", old))
        os.environ["HATCHET_HERMES_VERIFIER_TOKEN"] = "verifier-test-only"
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name); self.addCleanup(conn.close)
            job, _ = submit_job(conn, {"idempotency_key": "hatchet:server:terminal-failure", "requested_action": "create_draft_pr", "payload": {"repo": "Driftward-LLC/driftward-host", "dry_run": False}})
            transition_job(conn, job["job_id"], state="running")
            transition_job(conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/2"})
            pending = transition_job(conn, job["job_id"], state="verification_pending", details={"verification_context": {"local_commands": [{"command": "x", "exit_code": 0}], "secret_check": {"command": "y", "exit_code": 0}}})
            replacement = {"local_commands": [{"command": "replace", "exit_code": 0}], "secret_check": {"command": "replace", "exit_code": 0}}
            replay = _PostHandler(conn, {"state": "verification_pending", "details": {"verification_context": replacement}}, f"/jobs/{job['job_id']}/transitions"); replay.do_POST()
            self.assertEqual(replay.response[0], 409)
            overwrite = _PostHandler(conn, {"state": "failed", "details": {"verification_context": replacement}}, f"/jobs/{job['job_id']}/transitions"); overwrite.do_POST()
            self.assertEqual(overwrite.response[0], 400)
            self.assertEqual(conn.execute("SELECT verification_original_context_json FROM bridge_jobs WHERE job_id = ?", (job["job_id"],)).fetchone()[0], json.dumps(pending["verification_context"], sort_keys=True, separators=(",", ":")))
            self.assertTrue(acquire_verification_lease(conn, pending["job_id"], attempt_id="red-ci") ["acquired"])
            self.assertTrue(claim_verification_lease(conn, pending["job_id"], attempt_id="red-ci"))
            body = {"state": "failed", "details": {"reason_code": "github_checks_failed", "verification_attempt_id": "red-ci", "error": "sanitized"}}
            path = f"/jobs/{job['job_id']}/verification/completion"
            for headers in ({}, {"X-Hatchet-Verifier-Token": "wrong"}):
                handler = _PostHandler(conn, body, path, headers); handler.do_POST()
                self.assertEqual(handler.response[0], 403)
                self.assertEqual(conn.execute("SELECT state FROM bridge_jobs WHERE job_id = ?", (job["job_id"],)).fetchone()[0], "verification_pending")
            handler = _PostHandler(conn, body, path, {"X-Hatchet-Verifier-Token": "verifier-test-only"}); handler.do_POST()
            self.assertEqual(handler.response[0], 200)
            self.assertEqual(handler.response[1]["job"]["state"], "failed")
            self.assertEqual([event["event_type"] for event in list_events(conn, job["job_id"])].count("transition:failed"), 1)
    def test_event_limit_validation(self):
        self.assertEqual(_bounded_limit({}, default=100, label="event limit"), 100)
        self.assertEqual(_bounded_limit({"limit": ["2"]}, default=100, label="event limit"), 2)
        for value in (["0"], ["251"], ["not-a-number"], ["1", "2"]):
            with self.assertRaises(BridgeValidationError):
                _bounded_limit({"limit": value}, default=100, label="event limit")

    def test_reconciliation_body_rejects_unknown_top_level_fields(self):
        self.assertEqual(_finding_batch({"findings": []}), [])
        for body in ({}, {"findings": [], "raw_payload": {}}, {"findings": "not-a-list"}):
            with self.assertRaises(BridgeValidationError):
                _finding_batch(body)

    def test_validation_errors_have_a_clean_client_response(self):
        status, payload = _error_response(BridgeValidationError("severity must be a string"))
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "severity must be a string"})

    def test_non_string_finding_severity_returns_400_without_ledger_mutation(self):
        with tempfile.NamedTemporaryFile() as database:
            conn = connect(database.name)
            self.addCleanup(conn.close)
            job, _ = submit_job(conn, {
                "idempotency_key": "hatchet:server:severity-validation",
                "requested_action": "create_draft_pr",
                "payload": {"repo": "Driftward-LLC/driftward-host"},
            })

            for severity in ([], {}, None, 0, 1.5):
                with self.subTest(severity=severity):
                    body = {"findings": [{
                        "finding_key": "reconciliation:v1:github_pr_branch_mismatch:server",
                        "job_id": job["job_id"],
                        "subject": f"bridge:{job['job_id']}",
                        "kind": "github_pr_branch_mismatch",
                        "severity": severity,
                        "details": {"expected": {"branch": "codex/a"}, "observed": {"headRefName": "codex/b"}},
                    }]}
                    handler = _PostHandler(conn, body)

                    handler.do_POST()

                    self.assertEqual(handler.response, (400, {"error": "reconciliation finding severity must be warning or critical"}))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0], 0)


class _PostHandler:
    """Exercise BridgeHandler.do_POST without a socket or authenticated secrets."""

    do_POST = BridgeHandler.do_POST
    _handle_error = BridgeHandler._handle_error

    def __init__(self, conn, body, path="/reconciliation/findings", headers=None):
        self.conn = conn
        self.path = path
        self.headers = headers or {}
        self._body = body
        self.response = None

    def _require_auth(self):
        return True

    _require_verifier_auth = BridgeHandler._require_verifier_auth
    _require_inspection_auth = BridgeHandler._require_inspection_auth
    _require_recovery_auth = BridgeHandler._require_recovery_auth

    def _read_json(self):
        return self._body

    def _write_json(self, status, payload):
        self.response = (status, payload)


class _VerifierHandler:
    _require_verifier_auth = BridgeHandler._require_verifier_auth

    def __init__(self, headers):
        self.headers = headers
        self.response = None

    def _write_json(self, status, payload):
        self.response = (status, payload)


class _ScopedHandler(_VerifierHandler):
    _require_inspection_auth = BridgeHandler._require_inspection_auth
    _require_recovery_auth = BridgeHandler._require_recovery_auth
