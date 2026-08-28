from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from errno import ECONNABORTED, ECONNRESET, EPIPE
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_TARGET_URL = "http://127.0.0.1:19878/events/linear/issue"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FORWARDED_HEADERS = {
    "content-type",
    "linear-delivery",
    "linear-event",
    "linear-signature",
    "linear-timestamp",
    "user-agent",
}


class PublicLinearWebhookProxyHandler(BaseHTTPRequestHandler):
    server_version = "HatchetLinearWebhookProxy/0.1"

    def _write_response(self, status: int, body: bytes, content_type: str) -> bool:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            if exc.errno not in {EPIPE, ECONNRESET, ECONNABORTED}:
                raise
            self.close_connection = True
            return False
        return True

    def _write_json(self, status: int, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return self._write_response(status, body, "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _target_url(self) -> str:
        return os.environ.get("HATCHET_PUBLIC_LINEAR_WEBHOOK_TARGET_URL", DEFAULT_TARGET_URL)

    def _target_health_url(self, target_url: str) -> str:
        parsed = urlsplit(target_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("invalid target URL")
        return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))

    def _target_health(self, target_url: str) -> tuple[bool, str]:
        try:
            request = urllib.request.Request(self._target_health_url(target_url), method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 300, f"http={response.status}"
        except urllib.error.HTTPError as exc:
            return False, f"http={exc.code}"
        except (OSError, ValueError, urllib.error.URLError):
            return False, "unavailable"

    def _write_ready(self) -> None:
        secret_configured = bool(os.environ.get("HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET"))
        target_url = self._target_url()
        target_healthy, target_detail = self._target_health(target_url)
        errors = []
        if not secret_configured:
            errors.append("linear webhook secret is not configured")
        if not target_healthy:
            errors.append("linear ingress target unavailable")
        self._write_json(
            200 if not errors else 503,
            {
                "ok": not errors,
                "linearWebhookSecretConfigured": secret_configured,
                "targetHealthy": target_healthy,
                "targetHealth": target_detail,
                "errors": errors,
            },
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(200, {"ok": True})
            return
        if self.path == "/ready":
            self._write_ready()
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/events/linear/issue":
            self._write_json(404, {"error": "not found"})
            return

        if not os.environ.get("HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET"):
            self._write_json(503, {"error": "linear webhook secret is not configured"})
            return

        raw = self._read_body()
        target_url = self._target_url()
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in FORWARDED_HEADERS and key.lower() not in HOP_BY_HOP_HEADERS
        }
        request = urllib.request.Request(
            target_url,
            data=raw,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
        except OSError:
            self._write_json(502, {"error": "linear ingress target unavailable"})
            return

        self._write_response(status, body, content_type)

    def do_PUT(self) -> None:
        self._write_json(405, {"error": "method not allowed"})

    def do_PATCH(self) -> None:
        self._write_json(405, {"error": "method not allowed"})

    def do_DELETE(self) -> None:
        self._write_json(405, {"error": "method not allowed"})


def serve_public_proxy(host: str, port: int) -> None:
    httpd = HTTPServer((host, port), PublicLinearWebhookProxyHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
