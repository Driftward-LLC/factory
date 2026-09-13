from __future__ import annotations

import json
import hashlib
import hmac
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from hatchet_event_ingress.server import IngressHandler
from hatchet_event_ingress.store import connect


class RecordingHTTPServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors: list[tuple] = []

    def handle_error(self, request, client_address) -> None:
        self.errors.append((request, client_address))


class DelayedIngressHandler(IngressHandler):
    messages: list[str] = []

    def _write_json(self, status, payload):
        time.sleep(0.05)
        return super()._write_json(status, payload)

    def log_message(self, format, *args) -> None:
        type(self).messages.append(format % args)


class IngressServerTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = RecordingHTTPServer(("127.0.0.1", 0), DelayedIngressHandler)
        self.server.conn = connect(str(Path(self.tempdir.name) / "ingress.db"))
        self.server.timeout = 1
        DelayedIngressHandler.messages = []

    def tearDown(self) -> None:
        self.server.server_close()
        self.server.conn.close()
        self.tempdir.cleanup()

    def test_disconnected_clients_do_not_emit_server_errors(self) -> None:
        for request in (
            b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
            b"GET /observations/latest?source=linear&subject=ENG-39 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
            b"POST /events/linear/issue HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1\r\nConnection: close\r\n\r\n{",
        ):
            self._disconnect_after_request(request)
            self.server.handle_request()

        result: dict[str, object] = {}

        def healthy_request() -> None:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/health", timeout=1) as response:
                result["status"] = response.status

        thread = threading.Thread(target=healthy_request)
        thread.start()
        self.server.handle_request()
        thread.join(timeout=1)

        self.assertEqual(result.get("status"), 200)
        self.assertEqual(self.server.errors, [])
        self.assertFalse(any("500" in message for message in DelayedIngressHandler.messages))

    def test_application_errors_are_logged_and_return_sanitized_500(self) -> None:
        result: dict[str, object] = {}

        def failing_request() -> None:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.server.server_port}/reconciliation/observations?limit=10",
                    timeout=1,
                )
            except urllib.error.HTTPError as error:
                result["status"] = error.code
                result["body"] = error.read().decode("utf-8")

        with patch("hatchet_event_ingress.server.list_reconciliation_observations", side_effect=RuntimeError("database boom")):
            thread = threading.Thread(target=failing_request)
            thread.start()
            self.server.handle_request()
            thread.join(timeout=1)

        self.assertEqual(result.get("status"), 500)
        self.assertEqual(result.get("body"), '{"error": "internal server error"}')
        self.assertEqual(self.server.errors, [])
        self.assertTrue(any("RuntimeError" in message for message in DelayedIngressHandler.messages))

    def test_github_checks_and_reviews_are_published_through_dedicated_paths(self) -> None:
        cases = (
            (
                "/events/github/check_suite",
                {
                    "action": "completed",
                    "repository": {"full_name": "Driftward-LLC/factory"},
                    "check_suite": {
                        "id": 901,
                        "head_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success",
                        "latest_check_runs_count": 2,
                        "updated_at": "2026-09-13T08:00:00Z",
                        "pull_requests": [{"number": 28}],
                    },
                },
                "driftward.github.checks.completed",
            ),
            (
                "/events/github/pull_request_review",
                {
                    "action": "submitted",
                    "repository": {"full_name": "Driftward-LLC/factory"},
                    "pull_request": {
                        "number": 28,
                        "html_url": "https://github.com/Driftward-LLC/factory/pull/28",
                        "title": "ENG-40: Factory analytics",
                        "body": "Refs ENG-40",
                        "state": "open",
                        "updated_at": "2026-09-13T08:05:00Z",
                        "head": {"ref": "codex/eng-40-factory-analytics", "sha": "abc123"},
                        "base": {"ref": "main"},
                    },
                    "review": {
                        "id": 902,
                        "state": "approved",
                        "commit_id": "abc123",
                        "submitted_at": "2026-09-13T08:05:00Z",
                    },
                    "sender": {"type": "User", "login": "not-persisted-in-normalized-event"},
                },
                "driftward.github.review.submitted",
            ),
        )

        for index, (path, payload, expected_event) in enumerate(cases, 1):
            with self.subTest(path=path):
                result: dict[str, object] = {}
                raw = json.dumps(payload).encode("utf-8")
                secret = "test-github-signing-secret"
                signature = "sha256=" + hmac.new(
                    secret.encode("utf-8"), raw, hashlib.sha256
                ).hexdigest()

                def send_request() -> None:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{self.server.server_port}{path}",
                        data=raw,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitHub-Delivery": f"github-delivery-{index}",
                            "X-Hub-Signature-256": signature,
                        },
                    )
                    with urllib.request.urlopen(request, timeout=1) as response:
                        result["status"] = response.status
                        result["body"] = json.load(response)

                with patch.dict(
                    "os.environ",
                    {"HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET": secret},
                ), patch("hatchet_event_ingress.server.publish_hatchet_event", return_value=f"hatchet-{index}"):
                    thread = threading.Thread(target=send_request)
                    thread.start()
                    self.server.handle_request()
                    thread.join(timeout=1)

                self.assertEqual(result.get("status"), 201)
                event = result["body"]["event"]
                self.assertEqual(event["hatchet_event_name"], expected_event)
                self.assertTrue(event["hatchet_published"])
                self.assertIsNotNone(event["published_at"])
                self.assertNotIn("raw_payload", event)
                receipt = self.server.conn.execute(
                    "SELECT signature_verified FROM ingress_receipts "
                    "WHERE delivery_id = ?",
                    (f"github-delivery-{index}",),
                ).fetchone()
                self.assertEqual(receipt["signature_verified"], 1)
                if path.endswith("pull_request_review"):
                    self.assertNotIn("body", event["normalized"])
                    self.assertNotIn("login", json.dumps(event["normalized"]))

    def test_signed_reopened_pull_request_is_published_as_open_unmerged(self) -> None:
        payload = {
            "action": "reopened",
            "repository": {"full_name": "Driftward-LLC/factory"},
            "pull_request": {
                "number": 28,
                "html_url": "https://github.com/Driftward-LLC/factory/pull/28",
                "title": "ENG-40: Factory analytics",
                "body": "Refs ENG-40",
                "state": "open",
                "merged": False,
                "created_at": "2026-09-13T08:00:00Z",
                "updated_at": "2026-09-13T09:00:00Z",
                "head": {"ref": "codex/eng-40-factory-analytics", "sha": "abc123"},
                "base": {"ref": "main"},
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        secret = "test-github-signing-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        result: dict[str, object] = {}

        def send_request() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.server.server_port}/events/github/pull_request",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": "github-delivery-reopened",
                    "X-Hub-Signature-256": signature,
                },
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                result["status"] = response.status
                result["body"] = json.load(response)

        with patch.dict(
            "os.environ",
            {"HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET": secret},
        ), patch(
            "hatchet_event_ingress.server.publish_hatchet_event",
            return_value="hatchet-reopened",
        ):
            thread = threading.Thread(target=send_request)
            thread.start()
            self.server.handle_request()
            thread.join(timeout=1)

        self.assertEqual(result.get("status"), 201)
        event = result["body"]["event"]
        self.assertEqual(event["hatchet_event_name"], "driftward.github.pr.reopened")
        self.assertEqual(event["normalized"]["action"], "reopened")
        self.assertEqual(event["normalized"]["state"], "open")
        self.assertFalse(event["normalized"]["merged"])
        receipt = self.server.conn.execute(
            "SELECT signature_verified FROM ingress_receipts WHERE delivery_id = ?",
            ("github-delivery-reopened",),
        ).fetchone()
        self.assertEqual(receipt["signature_verified"], 1)

    def test_signed_github_body_replay_with_new_delivery_header_is_duplicate(self) -> None:
        payload = {
            "action": "completed",
            "repository": {"full_name": "Driftward-LLC/factory"},
            "check_suite": {
                "id": 903,
                "head_sha": "abc123",
                "status": "completed",
                "conclusion": "success",
                "updated_at": "2026-09-13T08:00:00Z",
                "pull_requests": [{"number": 28}],
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        secret = "test-github-signing-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()

        def deliver(delivery_id: str) -> tuple[int, dict[str, object]]:
            result: dict[str, object] = {}

            def send_request() -> None:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}/events/github/check_suite",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": delivery_id,
                        "X-Hub-Signature-256": signature,
                    },
                )
                with urllib.request.urlopen(request, timeout=1) as response:
                    result["status"] = response.status
                    result["body"] = json.load(response)

            thread = threading.Thread(target=send_request)
            thread.start()
            self.server.handle_request()
            thread.join(timeout=1)
            return int(result["status"]), result["body"]  # type: ignore[return-value]

        with patch.dict(
            "os.environ",
            {"HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET": secret},
        ), patch(
            "hatchet_event_ingress.server.publish_hatchet_event",
            return_value="hatchet-replay",
        ) as publish:
            first_status, first = deliver("github-delivery-original")
            replay_status, replay = deliver("github-delivery-attacker-selected")

        self.assertEqual(first_status, 201)
        self.assertEqual(replay_status, 200)
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["event"]["event_key"], replay["event"]["event_key"])
        self.assertEqual(publish.call_count, 1)
        receipts = self.server.conn.execute(
            "SELECT delivery_id,signature_verified,disposition FROM ingress_receipts "
            "ORDER BY received_at,receipt_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in receipts],
            [
                ("github-delivery-original", 1, "accepted"),
                ("github-delivery-attacker-selected", 1, "duplicate"),
            ],
        )

    def test_linear_freshness_ignores_unsigned_timestamp_header(self) -> None:
        payload = {"webhookTimestamp": 0}
        raw = json.dumps(payload).encode("utf-8")
        secret = "test-linear-signing-secret"
        signature = hmac.new(
            secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        result: dict[str, object] = {}

        def send_request() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.server.server_port}/events/linear/issue",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "Linear-Signature": signature,
                    "Linear-Timestamp": str(int(time.time() * 1000)),
                    "Linear-Delivery": "attacker-selected-delivery",
                },
            )
            try:
                urllib.request.urlopen(request, timeout=1)
            except urllib.error.HTTPError as error:
                result["status"] = error.code
                result["body"] = json.load(error)

        with patch.dict(
            "os.environ",
            {"HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET": secret},
        ):
            thread = threading.Thread(target=send_request)
            thread.start()
            self.server.handle_request()
            thread.join(timeout=1)

        self.assertEqual(result.get("status"), 401)
        self.assertEqual(result.get("body"), {"error": "stale timestamp"})
        self.assertEqual(
            self.server.conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0],
            0,
        )

    def _disconnect_after_request(self, request: bytes) -> None:
        client = socket.create_connection(self.server.server_address)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.sendall(request)
        client.close()
