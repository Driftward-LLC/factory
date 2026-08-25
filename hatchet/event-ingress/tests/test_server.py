from __future__ import annotations

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

    def _disconnect_after_request(self, request: bytes) -> None:
        client = socket.create_connection(self.server.server_address)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.sendall(request)
        client.close()
