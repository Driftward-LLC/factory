import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hatchet_hermes_bridge.console import make_console_server
from hatchet_hermes_bridge.store import reserve_factory_attempt
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
        self.server = make_console_server(self.db, "127.0.0.1", 0, ROOT / "factory/console")
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
        self.assertIn(b"loadOverview", raw)

        status, _, raw = self.get("/api/openapi.json")
        self.assertEqual(status, 200)
        schema = json.loads(raw)
        self.assertIn("/api/overview", schema["paths"])
        self.assertIn("/api/lifecycle/{issue}", schema["paths"])

    def test_invalid_issue_is_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            self.get("/api/lifecycle/not-an-issue")
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
