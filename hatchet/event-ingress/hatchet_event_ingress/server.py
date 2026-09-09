from __future__ import annotations

import hashlib
import hmac
import json
import os
from errno import ECONNABORTED, ECONNRESET, EPIPE
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .normalizer import IngressValidationError, normalize_github_pull_request, normalize_linear_issue
from .publisher import PublishError, publish_hatchet_event
from .store import IngressConflict, IngressNotFound, claim_pending_publication, connect, count_reconciliation_observations, get_event, latest_observation, list_events, list_lifecycle_observations, list_reconciliation_observations, mark_publication_failed, mark_published, publication_status, record_event


def _single_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key, [])
    if not values:
        return None
    if len(values) != 1 or not values[0]:
        raise IngressValidationError(f"{key} must be specified at most once")
    return values[0]


class IngressHandler(BaseHTTPRequestHandler):
    server_version = "HatchetEventIngress/0.1"

    @property
    def conn(self):
        return self.server.conn

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b"{}"

    def _read_json(self, raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise IngressValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise IngressValidationError("body must be a JSON object")
        return data

    def _write_json(self, status: int, payload: dict[str, Any] | list[Any]) -> bool:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            if exc.errno not in {EPIPE, ECONNRESET, ECONNABORTED}:
                raise
            self.close_connection = True
            return False
        return True

    def _require_github_signature(self, raw: bytes) -> bool:
        secret = os.environ.get("HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            return True

        got = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(got, expected):
            return True
        self._write_json(401, {"error": "invalid signature"})
        return False

    def _require_linear_signature(self, raw: bytes, body: dict[str, Any]) -> bool:
        secret = os.environ.get("HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET", "")
        if not secret:
            return True

        got = self.headers.get("Linear-Signature", "")
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, expected):
            self._write_json(401, {"error": "invalid signature"})
            return False

        tolerance_ms = int(os.environ.get("HATCHET_EVENT_INGRESS_LINEAR_TIMESTAMP_TOLERANCE_MS", "60000"))
        header_timestamp = self.headers.get("Linear-Timestamp")
        timestamp = header_timestamp or body.get("webhookTimestamp")
        if timestamp is None:
            self._write_json(401, {"error": "missing timestamp"})
            return False
        try:
            timestamp_ms = int(timestamp)
        except (TypeError, ValueError):
            self._write_json(401, {"error": "invalid timestamp"})
            return False

        import time

        now_ms = int(time.time() * 1000)
        if abs(now_ms - timestamp_ms) > tolerance_ms:
            self._write_json(401, {"error": "stale timestamp"})
            return False
        return True

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, IngressNotFound):
            self._write_json(404, {"error": str(exc)})
        elif isinstance(exc, IngressConflict):
            self._write_json(409, {"error": str(exc)})
        elif isinstance(exc, (IngressValidationError, ValueError)):
            self._write_json(400, {"error": str(exc)})
        elif isinstance(exc, PublishError):
            self._write_json(502, {"error": str(exc)})
        else:
            self.log_error("ingress request failed: %s", type(exc).__name__)
            self._write_json(500, {"error": "internal server error"})

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            parts = [unquote(part) for part in path.split("/") if part]

            if path == "/health":
                status = publication_status(self.conn)
                ok = status["unpublished"] == 0
                self._write_json(200 if ok else 503, {"ok": ok, "publication": status})
                return

            if path == "/ready":
                self._write_json(200, {"ok": True})
                return

            if parts == ["events"]:
                query = parse_qs(parsed.query)
                limit = 50
                if "limit" in query:
                    try:
                        limit = int(query["limit"][0])
                    except (TypeError, ValueError) as exc:
                        raise IngressValidationError("limit must be an integer") from exc
                    if limit < 1 or limit > 250:
                        raise IngressValidationError("limit must be between 1 and 250")
                self._write_json(200, {"events": list_events(self.conn, limit)})
                return

            if parts == ["observations", "latest"]:
                query = parse_qs(parsed.query)
                sources = query.get("source", [])
                subjects = query.get("subject", [])
                if len(sources) != 1 or len(subjects) != 1:
                    raise IngressValidationError("source and subject are required exactly once")
                observation = latest_observation(self.conn, source=sources[0], subject=subjects[0])
                self._write_json(200, {"status": "matched", "observation": observation} if observation else {"status": "not_found"})
                return

            if parts == ["reconciliation", "observations"]:
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                self._write_json(200, {"observations": list_reconciliation_observations(self.conn, limit), "total": count_reconciliation_observations(self.conn)})
                return

            if parts == ["lifecycle", "observations"]:
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["250"])[0])
                after_created = _single_query_value(query, "after_created_at")
                after_event = _single_query_value(query, "after_event_id")
                before_created = _single_query_value(query, "before_created_at")
                before_event = _single_query_value(query, "before_event_id")
                self._write_json(200, {"observations": list_lifecycle_observations(self.conn, limit, after_created_at=after_created, after_event_id=after_event, before_created_at=before_created, before_event_id=before_event)})
                return

            if len(parts) == 2 and parts[0] == "events":
                self._write_json(200, get_event(self.conn, parts[1]))
                return

            self._write_json(404, {"error": "not found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        raw = self._read_body()

        try:
            path = urlparse(self.path).path
            parts = [unquote(part) for part in path.split("/") if part]
            body = self._read_json(raw)

            if parts in (["events", "github", "pull_request"], ["events", "github", "pull-request"]):
                if not self._require_github_signature(raw):
                    return
                delivery_id = self.headers.get("X-GitHub-Delivery") or body.get("delivery_id")
                delivery = str(delivery_id).strip() if delivery_id else None
                normalized = normalize_github_pull_request(body, delivery_id=delivery)
                event, created = record_event(self.conn, normalized=normalized, raw_payload=body)
                if not event["hatchet_published"]:
                    event = _publish_event(self.conn, event)
                self._write_json(201 if created else 200, {"created": created, "event": event})
                return

            if parts == ["events", "linear", "issue"]:
                if not self._require_linear_signature(raw, body):
                    return
                delivery_id = self.headers.get("Linear-Delivery") or body.get("delivery_id")
                delivery = str(delivery_id).strip() if delivery_id else None
                normalized = normalize_linear_issue(body, delivery_id=delivery)
                event, created = record_event(self.conn, normalized=normalized, raw_payload=body)
                if not event["hatchet_published"]:
                    event = _publish_event(self.conn, event)
                self._write_json(201 if created else 200, {"created": created, "event": event})
                return

            self._write_json(404, {"error": "not found"})
        except Exception as exc:
            self._handle_error(exc)


def _publish_event(conn, event: dict[str, Any]) -> dict[str, Any]:
    claimed = claim_pending_publication(conn, event["event_key"])
    if not claimed:
        return get_event(conn, event["event_key"])
    lease_id = claimed["publish_lease_id"]
    try:
        event_id = publish_hatchet_event(claimed["hatchet_event_name"], claimed["normalized"])
    except PublishError:
        mark_publication_failed(conn, claimed["event_key"], lease_id)
        raise
    return mark_published(conn, claimed["event_key"], event_id, lease_id=lease_id)


def serve(db_path: str, host: str, port: int) -> None:
    conn = connect(db_path)
    httpd = HTTPServer((host, port), IngressHandler)
    httpd.conn = conn
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()
