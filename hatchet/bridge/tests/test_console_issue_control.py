import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from hatchet_hermes_bridge import store
from hatchet_hermes_bridge.console import make_console_server
from hatchet_hermes_bridge.console_control import IssueControl, _origin
from hatchet_hermes_bridge.operator import OperatorError
from test_operator import observation, ROUTES, POLICY
from test_support import connect_migrate


ROOT = Path(__file__).resolve().parents[3]
ACTOR = "operator@driftward.test"
ORIGIN = "https://factory.example.ts.net:19881"


class IssueControlFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "bridge.db")
        self.ingress = str(Path(self.temp.name) / "ingress.db")
        connect_migrate(self.db).close()
        with sqlite3.connect(self.ingress) as conn:
            conn.execute("CREATE TABLE ingress_events (event_id TEXT, source TEXT, subject TEXT, normalized_json TEXT, provider_updated_at_us INTEGER)")
            conn.execute("INSERT INTO ingress_events VALUES ('one','linear','ENG-8',?,1)", (json.dumps(observation(updatedAt="2026-10-01T01:00:00Z")),))
        env = patch.dict(os.environ, {
            "FACTORY_CONSOLE_OPERATOR_LOGINS": ACTOR,
            "FACTORY_CONSOLE_PUBLIC_ORIGIN": ORIGIN,
            "FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED": "",
            "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": ROUTES,
            "HATCHET_VERIFIER_POLICY_PATH": POLICY,
            "HATCHET_FACTORY_OPERATOR_TEST_HEALTH": "healthy",
            "HATCHET_INTAKE_DRY_RUN": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        self.service = IssueControl(self.db, self.ingress)

    def approval(self, action="takeover", **changes):
        body = {"action": action, "reason": "Manual implementation"}
        if action == "return":
            body["intent"] = "fresh_attempt"
        body.update(changes)
        preview = self.service.preview("ENG-8", ACTOR, body)
        return {**body, "preview_token": preview["preview_token"], "snapshot_digest": preview["snapshot_digest"], "request_id": f"request-{action}"}

    def take(self):
        return self.service.confirm("ENG-8", ACTOR, self.approval())


class ConsoleControlServiceTests(IssueControlFixture):
    def test_preview_is_read_only_take_and_return_are_atomic_and_audited(self):
        command = self.approval()
        self.assertEqual(self.service.read("ENG-8")["revision"], 0)
        taken = self.service.confirm("ENG-8", ACTOR, command)["control"]
        self.assertEqual(taken["status"], "manual")
        self.assertTrue(taken["safe_to_edit"])
        self.assertEqual(taken["actor"], ACTOR)
        returned = self.service.confirm("ENG-8", ACTOR, self.approval("return"))
        self.assertEqual(returned["control"]["status"], "factory")
        self.assertFalse(returned["control"]["safe_to_edit"])
        self.assertTrue(returned["control"]["automatic_intake_blocked"])
        with closing(store.connect_readonly(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0], 1)
            history = store.get_factory_issue_control(conn, "ENG-8")["history"]
            self.assertEqual([item["action"] for item in history], ["return_control", "take_control"])
            job = store.get_job(conn, store.get_factory_attempt(conn, returned["attempt_id"])["job_id"])
            self.assertEqual(job["payload"]["repo"], "Driftward-LLC/capykit")
            self.assertNotIn("actor", job["payload"])

    def test_committed_command_recovers_after_expiry_restart_and_linear_churn(self):
        self.take()
        command = self.approval("return")
        original = self.service.confirm("ENG-8", ACTOR, command)
        with sqlite3.connect(self.ingress) as conn:
            conn.execute("DELETE FROM ingress_events")
        restarted = IssueControl(self.db, self.ingress)
        with patch("hatchet_hermes_bridge.console_control.time.time", return_value=9999999999):
            replay = restarted.confirm("ENG-8", ACTOR, command)
        self.assertEqual(replay["attempt_id"], original["attempt_id"])
        self.assertEqual(len(replay["control"]["history"]), 2)
        with self.assertRaises(store.BridgeValidationError):
            restarted.confirm("ENG-8", "other@driftward.test", command)
        with self.assertRaises(store.BridgeValidationError):
            restarted.confirm("ENG-8", ACTOR, {**command, "reason": "Changed reason"})

    def test_reused_request_id_with_another_approval_conflicts(self):
        first = self.approval()
        self.service.confirm("ENG-8", ACTOR, first)
        changed = {**self.approval("return"), "request_id": first["request_id"]}
        with self.assertRaises(store.BridgeConflict):
            self.service.confirm("ENG-8", ACTOR, changed)
        self.assertEqual(self.service.read("ENG-8")["mode"], "manual")

    def test_uncommitted_expired_and_restart_tokens_fail_closed(self):
        command = self.approval()
        with patch("hatchet_hermes_bridge.console_control.time.time", return_value=9999999999):
            with self.assertRaises(HTTPException) as raised:
                self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(raised.exception.status_code, 410)
        with self.assertRaises(store.BridgeConflict):
            IssueControl(self.db, self.ingress).confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.service.read("ENG-8")["revision"], 0)

    def test_changed_store_snapshot_rejects_takeover(self):
        command = self.approval()
        conn = store.connect_runtime(self.db)
        store.reserve_factory_attempt(conn, work_item_key="ENG-8", mode="real", request_id="new-work", reason="test", actor="test")
        conn.close()
        with self.assertRaises(store.BridgeConflict):
            self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.service.read("ENG-8")["mode"], "factory")

    def test_runtime_mode_change_invalidates_approval(self):
        command = self.approval()
        with patch.dict(os.environ, {"HATCHET_INTAKE_DRY_RUN": "true"}):
            with self.assertRaises(store.BridgeConflict):
                self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.service.read("ENG-8")["revision"], 0)

    def test_replay_reports_current_ownership_not_old_safe_label(self):
        takeover = self.approval()
        self.service.confirm("ENG-8", ACTOR, takeover)
        self.service.confirm("ENG-8", ACTOR, self.approval("return"))
        replay = self.service.confirm("ENG-8", ACTOR, takeover)
        self.assertEqual(replay["control"]["status"], "factory")
        self.assertFalse(replay["control"]["safe_to_edit"])

    def test_open_or_uninspected_recorded_pr_cannot_be_returned_as_fresh_work(self):
        self.take()
        first = self.service.confirm("ENG-8", ACTOR, self.approval("return"))
        takeover = {**self.approval(), "request_id": "another-takeover"}
        self.service.confirm("ENG-8", ACTOR, takeover)
        with closing(store.connect_runtime(self.db)) as conn, conn:
            conn.execute("UPDATE factory_attempts SET prior_pr_url=? WHERE attempt_id=?", ("https://github.com/Driftward-LLC/capykit/pull/20", first["attempt_id"]))
            prior = store.get_factory_attempt(conn, first["attempt_id"])
        with self.assertRaisesRegex(OperatorError, "inspection_pending"):
            self.approval("return")
        result = {"url": prior["prior_pr_url"], "state": "open", "merged": False, "draft": True, "head_branch": prior["branch"], "head_sha": "a" * 40, "base_ref": "main"}
        inspected = {"state": "completed", "result": result, "request_key": "inspection-fixture", "job_id": "inspection-job", "result_digest": "b" * 64, "observed_at": store.utc_now()}
        with patch("hatchet_hermes_bridge.operator.get_pr_inspection", return_value=inspected):
            with self.assertRaisesRegex(OperatorError, "prior_draft_pr_blocks_retry"):
                self.approval("return")
        self.assertEqual(self.service.read("ENG-8")["mode"], "manual")

    def test_newest_factory_linear_observation_is_not_skipped(self):
        self.take()
        with sqlite3.connect(self.ingress) as conn:
            conn.execute("INSERT INTO ingress_events VALUES ('two','linear','ENG-8',?,2)", (json.dumps(observation(status="Done", dispatchEligible=False, reasonCode="linear_issue_factory_lifecycle_actor")),))
        with self.assertRaises(store.BridgeConflict):
            self.approval("return")
        self.assertEqual(self.service.read("ENG-8")["mode"], "manual")

    def test_return_rechecks_linear_requirements_even_without_timestamp_change(self):
        self.take()
        command = self.approval("return")
        with sqlite3.connect(self.ingress) as conn:
            conn.execute("UPDATE ingress_events SET normalized_json=?", (json.dumps(observation(title="Changed requirements", updatedAt="2026-10-01T01:00:00Z")),))
        with self.assertRaises(store.BridgeConflict):
            self.service.confirm("ENG-8", ACTOR, command)
        self.assertEqual(self.service.read("ENG-8")["mode"], "manual")

    def test_failed_return_does_not_unlock_or_enqueue(self):
        self.take()
        command = self.approval("return")
        with patch("hatchet_hermes_bridge.store.reserve_factory_retry", side_effect=store.BridgeConflict("reservation failed")):
            with self.assertRaises(store.BridgeConflict):
                self.service.confirm("ENG-8", ACTOR, command)
        control = self.service.read("ENG-8")
        self.assertEqual(control["mode"], "manual")
        self.assertEqual(control["revision"], 1)

    def test_preview_rejects_unsupported_operations_and_client_actor(self):
        for value in ({"action": []}, {"action": "verify"}, {"action": "return", "intent": "continue"}, {"action": "takeover", "actor": "root"}, {"action": "takeover", "reason": "x" * 321}):
            with self.assertRaises(store.BridgeValidationError):
                self.service.preview("ENG-8", ACTOR, {"reason": "test", **value})


class ConsoleControlHTTPTests(IssueControlFixture):
    def setUp(self):
        super().setUp()
        self.server = make_console_server(self.db, "127.0.0.1", 0, ROOT / "factory/console", ingress_db_path=self.ingress)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="preview", *, headers=None, body=None, method="POST", raw=None):
        defaults = {"Tailscale-User-Login": ACTOR, "Origin": ORIGIN, "X-Factory-Action": "issue-control", "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
        defaults.update(headers or {})
        content = raw if raw is not None else json.dumps(body or {"action": "takeover", "reason": "manual"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.putrequest(method, "/api/issues/ENG-8/control" + ("/" + path if path else ""))
            for key, values in defaults.items():
                for value in values if isinstance(values, list) else [values]:
                    if value is not None:
                        conn.putheader(key, value)
            conn.putheader("Content-Length", len(content))
            conn.endheaders(content)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_browser_takeover_preview_confirm_and_read(self):
        status, preview = self.request()
        self.assertEqual(status, 200, preview)
        status, result = self.request("confirm", body={"action": "takeover", "reason": "manual", "request_id": "http-request", "preview_token": preview["preview_token"], "snapshot_digest": preview["snapshot_digest"]})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["control"]["safe_to_edit"])
        self.assertEqual(self.request("", method="GET")[1]["actor"], ACTOR)

    def test_default_denial_unlisted_users_and_development_never_mutate(self):
        for env in ({"FACTORY_CONSOLE_OPERATOR_LOGINS": ""}, {"FACTORY_CONSOLE_PUBLIC_ORIGIN": ""}, {"FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED": "1"}):
            with patch.dict(os.environ, env):
                self.assertEqual(self.request()[0], 403)
                self.assertFalse(self.request("", method="GET")[1]["actions_enabled"])
        self.assertEqual(self.request(headers={"Tailscale-User-Login": "reader@driftward.test"})[0], 403)
        self.assertEqual(self.service.read("ENG-8")["revision"], 0)

    def test_foreign_missing_duplicate_headers_and_spoofed_actor_rejected(self):
        for headers in ({"Origin": "https://evil.test"}, {"Origin": None}, {"Origin": [ORIGIN, ORIGIN]}, {"Tailscale-User-Login": [ACTOR, ACTOR]}, {"X-Factory-Action": None}, {"Content-Type": "text/plain"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request(headers=headers)[0], 403, headers)
        self.assertEqual(self.request(body={"action": "takeover", "reason": "manual", "actor": "root"})[0], 400)

    def test_json_size_duplicate_fields_and_non_objects_rejected(self):
        for raw, expected in ((b'{"action":"takeover","action":"return","reason":"manual"}', 400), (b"[]", 400), (b"{}" * 9000, 413), (b"not json", 400)):
            self.assertEqual(self.request(raw=raw)[0], expected)
        self.assertEqual(self.service.read("ENG-8")["revision"], 0)

    def test_origin_configuration_is_an_exact_https_origin(self):
        for origin in ("http://factory.test", ORIGIN + "/", ORIGIN + "/path", "https://user@factory.test", "https://factory.test?x=y", "https://bad origin", "https://factory.test:999999"):
            with patch.dict(os.environ, {"FACTORY_CONSOLE_PUBLIC_ORIGIN": origin}):
                self.assertIsNone(_origin())
                self.assertEqual(self.request()[0], 403)


if __name__ == "__main__":
    unittest.main()
