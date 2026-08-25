from __future__ import annotations

import os
import socket
import struct
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from hatchet_event_ingress.public_proxy import PublicLinearWebhookProxyHandler


class RecordingHTTPServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors: list[tuple] = []

    def handle_error(self, request, client_address) -> None:
        self.errors.append((request, client_address))


class ForwardTargetHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).requests += 1
        body = b'{"accepted":true}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


class PublicProxyTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        ForwardTargetHandler.requests = 0
        self.target = HTTPServer(("127.0.0.1", 0), ForwardTargetHandler)
        self.target_thread = threading.Thread(target=self.target.serve_forever)
        self.target_thread.start()
        self.proxy = RecordingHTTPServer(
            ("127.0.0.1", 0),
            PublicLinearWebhookProxyHandler,
        )
        self.proxy.timeout = 1
        self.env = patch.dict(
            os.environ,
            {
                "HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET": "test-secret",
                "HATCHET_PUBLIC_LINEAR_WEBHOOK_TARGET_URL": (
                    f"http://127.0.0.1:{self.target.server_port}/events/linear/issue"
                ),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.proxy.server_close()
        self.target.shutdown()
        self.target.server_close()
        self.target_thread.join(timeout=1)

    def test_disconnected_forwarded_webhook_does_not_emit_a_server_error(self) -> None:
        client = socket.create_connection(self.proxy.server_address)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.sendall(
            b"POST /events/linear/issue HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n{}"
        )
        client.close()
        self.proxy.handle_request()

        result: dict[str, object] = {}

        def healthy_request() -> None:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.proxy.server_port}/health",
                timeout=1,
            ) as response:
                result["status"] = response.status

        thread = threading.Thread(target=healthy_request)
        thread.start()
        self.proxy.handle_request()
        thread.join(timeout=1)

        self.assertEqual(ForwardTargetHandler.requests, 1)
        self.assertEqual(result.get("status"), 200)
        self.assertEqual(self.proxy.errors, [])


if __name__ == "__main__":
    unittest.main()
