import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hatchet_hermes_bridge.analytics import factory_analytics
from hatchet_hermes_bridge.cli import main
from hatchet_hermes_bridge.console import make_console_server
from hatchet_hermes_bridge.store import reserve_factory_attempt
from factory_lifecycle_writer.outbox import Outbox
from test_support import connect_migrate as connect


ROOT = Path(__file__).resolve().parents[3]


class FactoryConsoleTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = str(Path(self.work.name) / "bridge.db")
        conn = connect(self.db)
        reserve_factory_attempt(
            conn,
            work_item_key="ENG-119",
            mode="real",
            request_id="console-test",
            reason="console fixture",
            actor="test",
        )
        conn.close()
        self.lifecycle_db = str(Path(self.work.name) / "lifecycle.db")
        outbox = Outbox(self.lifecycle_db)
        outbox.record({
            "outbox_key": "lifecycle:v2:console", "phase": "unresolved", "delivery_state": "dead_letter",
            "issue_id": None, "identifier": None, "issue_url": None, "job_id": "job-inspect",
            "idempotency_key": "inspect:console", "source_event_key": None, "source_delivery_id": None,
            "repo": "Driftward-LLC/capykit", "pr_url": None, "reason_code": "linear_issue_identity_invalid",
            "execution_mode": "real", "source_kind": "bridge_event", "source_recorded_at": "2026-01-01T00:00:00.000000Z",
            "marker": "private", "body": "private", "next_attempt_at": None,
            "delivered_at": None, "suppressed_at": None, "last_error_code": None,
        })
        outbox.close()
        self.ingress_db = str(Path(self.work.name) / "ingress.db")
        self.server = make_console_server(self.db, "127.0.0.1", 0, ROOT / "factory/console", self.lifecycle_db, self.ingress_db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path, *, identity=True):
        headers = {"Tailscale-User-Login": "operator@driftward.test"} if identity else {}
        with urlopen(Request(self.base + path, headers=headers), timeout=2) as response:
            return response.status, response.headers, response.read()

    def test_health_is_public_but_console_requires_tailscale_identity(self):
        self.assertEqual(self.get("/health", identity=False)[0], 200)
        with patch.dict(os.environ, {"FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED": ""}):
            with self.assertRaises(HTTPError) as raised:
                self.get("/", identity=False)
        self.assertEqual(raised.exception.code, 401)

    def test_overview_returns_sanitized_attempts_and_security_headers(self):
        with patch.dict(os.environ, {"HATCHET_INTAKE_DRY_RUN": "false"}):
            status, headers, raw = self.get("/api/overview")
        value = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(value["viewer"], "operator@driftward.test")
        self.assertEqual(value["mode"], "real")
        self.assertEqual(value["active_attempts"][0]["attempt"]["work_item_key"], "ENG-119")
        self.assertEqual(value["status_counts"], {"reserved": 1})
        self.assertEqual(value["reconciliation"]["actionable"], 0)
        self.assertEqual(value["reconciliation"]["scope"]["terminal_work"], 0)
        self.assertEqual(value["lifecycle_outbox"]["dead_letters"], 1)
        self.assertEqual(value["lifecycle_outbox"]["dead_letter_reasons"], {"linear_issue_identity_invalid": 1})
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_issue_lifecycle_and_static_assets_are_available(self):
        status, _, raw = self.get("/api/lifecycle/ENG-119")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["work_item_key"], "ENG-119")

        status, headers, raw = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/html")
        self.assertIn(b"Factory Console", raw)

        status, headers, raw = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/javascript")
        self.assertIn(b"/api/overview", raw)
        self.assertIn(b"/api/lifecycle/", raw)

        status, _, raw = self.get("/api/openapi.json")
        self.assertEqual(status, 200)
        schema = json.loads(raw)
        self.assertIn("/api/overview", schema["paths"])
        self.assertIn("/api/lifecycle/{issue}", schema["paths"])
        self.assertIn("/api/analytics", schema["paths"])

    def test_invalid_issue_is_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            self.get("/api/lifecycle/not-an-issue")
        self.assertEqual(raised.exception.code, 400)

    def test_analytics_is_authenticated_readonly_and_preserves_missing_evidence(self):
        with patch.dict(os.environ, {"FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED": ""}):
            with self.assertRaises(HTTPError) as raised:
                self.get("/api/analytics", identity=False)
        self.assertEqual(raised.exception.code, 401)
        before = {path: Path(path).read_bytes() for path in (self.db, self.lifecycle_db)}
        with patch("sqlite3.connect", wraps=sqlite3.connect) as opened:
            status, headers, raw = self.get("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(len(opened.call_args_list), 2)
        for call in opened.call_args_list:
            self.assertTrue(call.args[0].endswith("?mode=ro"))
            self.assertTrue(call.kwargs["uri"])
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertFalse(Path(self.ingress_db).exists())
        value = json.loads(raw)
        self.assertEqual(value["schema"], "factory-console-analytics-v1")
        self.assertEqual(value["window"]["days"], 30)
        self.assertEqual(value["coverage"]["cohort_work_items"], 1)
        self.assertFalse(value["coverage"]["ingress_available"])
        self.assertEqual(value["funnel"]["stages"]["attempted"]["count"], 1)
        self.assertIsNone(value["github"]["coverage"])
        self.assertIsNone(value["github"]["merged_prs"])
        self.assertFalse(value["github"]["reviews"]["available"])
        self.assertIsNone(value["github"]["reviews"]["changes_requested_rate"])
        self.assertFalse(value["github"]["first_pass_checks"]["available"])
        self.assertEqual(value["time_series"]["timezone"], "UTC")
        self.assertEqual(len(value["time_series"]["points"]), 31)
        self.assertTrue(value["time_series"]["coverage"]["prs_recorded"]["complete"])

    def test_analytics_rejects_invalid_duplicate_and_unknown_parameters(self):
        for query in ("days=0", "days=366", "days=-1", "days=1.5", "days=", "days=true", "days=7&days=30", "days=7&source=private", "other="):
            with self.subTest(query=query), self.assertRaises(HTTPError) as raised:
                self.get("/api/analytics?" + query)
            self.assertEqual(raised.exception.code, 400)
        for days in (1, 7, 90, 365):
            with self.subTest(days=days):
                self.assertEqual(json.loads(self.get(f"/api/analytics?days={days}")[2])["window"]["days"], days)
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(self.base + "/api/analytics", data=b"{}", headers={"Tailscale-User-Login": "operator@driftward.test"}), timeout=2)
        self.assertEqual(raised.exception.code, 405)

    def test_analytics_uses_optional_ingress_and_preserves_unverified_coverage(self):
        conn = connect(self.db)
        conn.execute("UPDATE factory_attempts SET prior_pr_url = ?", ("https://github.com/Driftward-LLC/capykit/pull/119",))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.ingress_db)
        conn.execute("CREATE TABLE ingress_events (event_id TEXT, source TEXT, subject TEXT, normalized_json TEXT, decision TEXT, reason_code TEXT, hatchet_published INTEGER, created_at TEXT, provider_updated_at_us INTEGER)")
        conn.commit()
        conn.close()
        value = json.loads(self.get("/api/analytics")[2])
        self.assertTrue(value["coverage"]["ingress_available"])
        self.assertIsNone(value["github"]["coverage"])
        self.assertFalse(value["github"]["reviews"]["available"])
        self.assertIsNone(value["github"]["merged_prs"])

    def test_analytics_allowlist_excludes_private_fields_and_unknown_classifications(self):
        report = factory_analytics(self.db, self.ingress_db, self.lifecycle_db)
        private = "private-operator-label-and-path"
        report["sources"] = {"bridge": {"path": private}}
        report["backends"] = [{"configured_auth_mode": private}]
        report["coverage"]["extra"] = private
        report["funnel"]["stages"]["attempted"]["extra"] = private
        report["time_series"]["extra"] = private
        report["time_series"]["points"][0]["extra"] = private
        report["time_series"]["coverage"]["prs_recorded"]["extra"] = private
        report["github"]["coverage"] = {"observed": 0, "eligible": 18, "rate": 0, "complete": False, "extra": private}
        report["github"]["reviews"] = {"available": True, "coverage": report["github"]["coverage"], "changes_requested_prs": 0, "changes_requested_rate": None, "actor": private}
        report["failures"]["by_classification"] = [
            {"failure_domain": "verification", "failure_stage": "verification_checks", "reason_code": "verification_github_check_failed", "count": 2, "error": private},
            {"failure_domain": private, "failure_stage": private, "reason_code": private, "count": 3},
            {"failure_domain": "unknown", "failure_stage": "unknown", "reason_code": "unknown", "count": 1},
        ]
        with patch("hatchet_hermes_bridge.console.factory_analytics", return_value=report):
            value = json.loads(self.get("/api/analytics")[2])
        self.assertNotIn(private, json.dumps(value))
        self.assertEqual(set(value), {"schema", "generated_at", "window", "coverage", "time_series", "funnel", "github", "recovery", "failures", "latency_ms"})
        self.assertEqual(value["github"]["coverage"], {"observed": 0, "eligible": 18, "rate": 0, "complete": False})
        self.assertIsNone(value["github"]["reviews"]["changes_requested_rate"])
        self.assertEqual(value["failures"]["by_classification"], [
            {"failure_domain": "unknown", "failure_stage": "unknown", "reason_code": "unknown", "count": 4},
            {"failure_domain": "verification", "failure_stage": "verification_checks", "reason_code": "verification_github_check_failed", "count": 2},
        ])

    def test_analytics_data_failure_returns_sanitized_service_unavailable(self):
        with patch("hatchet_hermes_bridge.console.factory_analytics", side_effect=RuntimeError("private database path")):
            with self.assertRaises(HTTPError) as raised:
                self.get("/api/analytics")
        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(json.loads(raised.exception.read()), {"error": "analytics unavailable"})

    def test_console_entry_points_pass_operator_analytics_sources(self):
        env = {"HATCHET_FACTORY_OPERATOR_INGRESS_DB": self.ingress_db, "HATCHET_FACTORY_OPERATOR_LIFECYCLE_DB": self.lifecycle_db}
        with patch.dict(os.environ, env), patch("hatchet_hermes_bridge.console.serve_console") as serve:
            self.assertEqual(main(["--db", self.db, "serve-console"]), 0)
        self.assertEqual(serve.call_args.args[-2:], (self.lifecycle_db, self.ingress_db))
        with patch.dict(os.environ, env), patch("hatchet_hermes_bridge.console.make_console_server") as make, patch("hatchet_hermes_bridge.cli.serve_operator"), patch("hatchet_hermes_bridge.cli.threading.Thread"):
            self.assertEqual(main(["--db", self.db, "serve-operator", "--console-host", "127.0.0.1", "--console-port", "19881"]), 0)
        self.assertEqual(make.call_args.args[-2:], (self.lifecycle_db, self.ingress_db))


if __name__ == "__main__":
    unittest.main()
