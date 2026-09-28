from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .store import (
    BridgeConflict,
    BridgeError,
    BridgeNotFound,
    BridgeValidationError,
    connect_runtime,
    count_events,
    count_jobs,
    get_job,
    list_events,
    list_lifecycle_events,
    list_jobs,
    reconciliation_scan,
    acknowledge_reconciliation_scan,
    reconciliation_correlations,
    reconciliation_findings_status,
    record_reconciliation_findings,
    record_job_correlation,
    reserve_factory_intake,
    submit_job,
    transition_job,
    acquire_verification_lease,
    claim_verification_lease,
    record_verification_observation,
    get_factory_attempt,
    get_factory_attempt_for_job,
    list_factory_attempts,
    list_factory_attempt_events,
    claim_factory_execution,
    heartbeat_factory_execution,
    authorize_factory_effect,
    commit_factory_execution,
    execution_may_effect,
    settle_factory_execution,
    recover_factory_execution,
    record_factory_execution_analytics,
    record_factory_execution_route,
    list_factory_outbox,
    claim_factory_outbox_delivery,
    acknowledge_factory_outbox,
    record_pr_inspection,
    SCHEMA_VERSION,
)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "HatchetHermesBridge/0.1"

    def _require_auth(self) -> bool:
        expected = os.environ.get("HATCHET_HERMES_BRIDGE_TOKEN")
        if not expected:
            return True

        got = self.headers.get("Authorization", "")
        if got == f"Bearer {expected}":
            return True

        self._write_json(401, {"error": "unauthorized"})
        return False

    def _require_verifier_auth(self) -> bool:
        expected = os.environ.get("HATCHET_HERMES_VERIFIER_TOKEN")
        if not expected or self.headers.get("X-Hatchet-Verifier-Token", "") != expected:
            self._write_json(403, {"error": "verifier authorization required"})
            return False
        return True

    def _require_inspection_auth(self) -> bool:
        expected = os.environ.get("HATCHET_HERMES_INSPECTION_TOKEN")
        if not expected or self.headers.get("X-Hatchet-Inspection-Token", "") != expected:
            self._write_json(403, {"error": "inspection authorization required"})
            return False
        return True

    def _require_recovery_auth(self) -> bool:
        expected = os.environ.get("HATCHET_HERMES_RECOVERY_TOKEN")
        if not expected or self.headers.get("X-Hatchet-Recovery-Token", "") != expected:
            self._write_json(403, {"error": "recovery authorization required"})
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BridgeValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BridgeValidationError("body must be a JSON object")
        return data

    def _write_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_error(self, exc: Exception) -> None:
        status, payload = _error_response(exc)
        self._write_json(status, payload)

    @property
    def conn(self):
        return self.server.conn

    def do_GET(self) -> None:
        if not self._require_auth():
            return

        try:
            parsed = urlparse(self.path)
            path = parsed.path
            parts = [unquote(part) for part in path.split("/") if part]

            if path == "/health":
                self._write_json(200, {"ok": True, "schema_version": SCHEMA_VERSION, "release_commit": _release_commit()})
                return

            if parts == ["jobs"]:
                query = parse_qs(parsed.query)
                limit = 50
                if "limit" in query:
                    try:
                        limit = int(query["limit"][0])
                    except (TypeError, ValueError) as exc:
                        raise BridgeValidationError("limit must be an integer") from exc
                    if limit < 1 or limit > 250:
                        raise BridgeValidationError("limit must be between 1 and 250")
                self._write_json(200, {"jobs": list_jobs(self.conn, limit), "total": count_jobs(self.conn)})
                return

            if parts == ["lifecycle", "events"]:
                query = parse_qs(parsed.query)
                limit = _bounded_limit(query, default=250, label="event limit")
                after_created = _single(query, "after_created_at")
                after_event = _single(query, "after_event_id")
                before_created = _single(query, "before_created_at")
                before_event = _single(query, "before_event_id")
                self._write_json(200, {"events": list_lifecycle_events(self.conn, after_created_at=after_created, after_event_id=after_event, before_created_at=before_created, before_event_id=before_event, limit=limit)})
                return

            if len(parts) == 2 and parts[0] == "attempts":
                self._write_json(200, get_factory_attempt(self.conn, parts[1]))
                return

            if len(parts) == 3 and parts[0] == "attempts" and parts[1] == "work-item":
                self._write_json(200, {"attempts": list_factory_attempts(self.conn, parts[2])})
                return

            if len(parts) == 3 and parts[0] == "attempts" and parts[1] == "job":
                attempt = get_factory_attempt_for_job(self.conn, parts[2])
                self._write_json(200, {"attempt": attempt} if attempt else {"attempt": None})
                return

            if len(parts) == 3 and parts[0] == "attempts" and parts[2] == "events":
                query = parse_qs(parsed.query)
                self._write_json(200, {"events": list_factory_attempt_events(self.conn, parts[1], _bounded_limit(query, default=100, label="event limit"))})
                return

            if parts == ["factory", "outbox"]:
                self._write_json(200, {"outbox": list_factory_outbox(self.conn, 250)})
                return

            if parts == ["reconciliation", "findings"]:
                query = parse_qs(parsed.query)
                active_minutes = _bounded_limit(query, default=30, label="active_within_minutes", key="active_within_minutes", maximum=1440)
                self._write_json(200, {"findings": reconciliation_findings_status(self.conn, active_within_minutes=active_minutes)})
                return

            if len(parts) == 2 and parts[0] == "jobs":
                self._write_json(200, get_job(self.conn, parts[1]))
                return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "events":
                query = parse_qs(parsed.query)
                limit = _bounded_limit(query, default=100, label="event limit")
                self._write_json(200, {"events": list_events(self.conn, parts[1], limit), "total": count_events(self.conn, parts[1])})
                return

            self._write_json(404, {"error": "not found"})
        except Exception as exc:
            self._handle_error(exc)


    def do_POST(self) -> None:
        if not self._require_auth():
            return

        try:
            path = urlparse(self.path).path
            parts = [unquote(part) for part in path.split("/") if part]
            body = self._read_json()

            if parts == ["reconciliation", "scan"]:
                if set(body) != {"cohort", "limit"}:
                    raise BridgeValidationError("reconciliation scan requires only cohort and limit")
                self._write_json(200, reconciliation_scan(self.conn, cohort=body["cohort"], limit=body["limit"]))
                return
            if parts == ["reconciliation", "scan", "ack"]:
                if set(body) != {"cohort", "token"}:
                    raise BridgeValidationError("reconciliation scan acknowledgement requires only cohort and token")
                acknowledge_reconciliation_scan(self.conn, cohort=body["cohort"], token=body["token"])
                self._write_json(200, {"ok": True})
                return
            if parts == ["reconciliation", "correlations"]:
                self._write_json(200, reconciliation_correlations(self.conn, body))
                return

            if parts == ["jobs"]:
                job, created = submit_job(self.conn, body)
                self._write_json(201 if created else 200, {"created": created, "job": job})
                return

            if parts == ["factory", "intake", "reserve"]:
                attempt, job, created = reserve_factory_intake(
                    self.conn,
                    work_item_key=body.get("work_item_key"),
                    mode=body.get("mode"),
                    request_id=body.get("request_id"),
                    reason=body.get("reason"),
                    actor="intake-worker",
                    payload=body.get("payload"),
                    hatchet_workflow_run_id=body.get("hatchet_workflow_run_id"),
                    hatchet_step_run_id=body.get("hatchet_step_run_id"),
                )
                self._write_json(201 if created else 200, {"created": created, "attempt": attempt, "job": job, "accepted_event_enqueued": created})
                return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "transitions":
                state = body.get("state")
                if not isinstance(state, str):
                    raise BridgeValidationError("state is required")
                if state == "ready_for_human_review" or (state == "failed" and isinstance(body.get("details"), dict) and "verification_attempt_id" in body["details"]):
                    raise BridgeValidationError("verification completion requires the verifier completion endpoint")
                attempt = get_factory_attempt_for_job(self.conn, parts[1])
                if state == "effect_committed" and attempt and attempt.get("mode") == "real":
                    raise BridgeValidationError("factory effect completion requires the fenced execution endpoint")
                if attempt and attempt.get("mode") == "real" and state in {"failed", "completed", "ready_for_human_review"}:
                    lease = self.conn.execute("SELECT effect_authorized_at FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL", (attempt["attempt_id"],)).fetchone()
                    if lease:
                        raise BridgeValidationError("factory execution terminal transition requires the dedicated execution, cancel, or verifier endpoint")
                details = body.get("details", {})
                job = transition_job(self.conn, parts[1], state=state, details=details)
                self._write_json(200, {"job": job})
                return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "correlations":
                job = record_job_correlation(self.conn, parts[1], body)
                self._write_json(200, {"job": job})
                return

            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "verification" and parts[3] == "lease":
                self._write_json(200, acquire_verification_lease(self.conn, parts[1], attempt_id=body.get("attempt_id"), ttl_seconds=body.get("ttl_seconds", 300)))
                return

            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "claim":
                self._write_json(200, claim_factory_execution(self.conn, parts[1], owner=body.get("owner"), ttl_seconds=body.get("ttl_seconds", 900)))
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "may-effect":
                self._write_json(200, {"allowed": execution_may_effect(self.conn, parts[1], body)})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "heartbeat":
                self._write_json(200, {"renewed": heartbeat_factory_execution(self.conn, parts[1], body)})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "authorize":
                self._write_json(200, {"authorized": authorize_factory_effect(self.conn, parts[1], body)})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "settle":
                claim = {key: body.get(key) for key in ("lease_id", "owner", "epoch", "fence_token")}
                settle_factory_execution(
                    self.conn,
                    parts[1],
                    claim,
                    effect_observed=body.get("effect_observed") is True,
                    failure_details=body.get("failure_details"),
                    failure_event_type=body.get("failure_event_type", "execution_backend_failed"),
                )
                self._write_json(200, {"ok": True})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "route":
                if set(body) != {"details"}:
                    raise BridgeValidationError("factory execution route body is invalid")
                recorded = record_factory_execution_route(self.conn, parts[1], body.get("details"))
                self._write_json(201 if recorded else 200, {"recorded": recorded})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "analytics":
                if set(body) != {"lease_id", "owner", "epoch", "fence_token", "event_type", "details"}:
                    raise BridgeValidationError("factory execution analytics body is invalid")
                claim = {key: body.get(key) for key in ("lease_id", "owner", "epoch", "fence_token")}
                event_type = body.get("event_type")
                if event_type not in {"execution_backend_invoked", "execution_backend_completed"}:
                    raise BridgeValidationError("factory execution analytics endpoint accepts invocation and completion only")
                recorded = record_factory_execution_analytics(
                    self.conn,
                    parts[1],
                    claim,
                    event_type=event_type,
                    details=body.get("details"),
                )
                self._write_json(201 if recorded else 200, {"recorded": recorded})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "commit":
                claim = {key: body.get(key) for key in ("lease_id", "owner", "epoch", "fence_token")}
                self._write_json(200, {"job": commit_factory_execution(self.conn, parts[1], claim, body.get("details"))})
                return
            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "factory-execution" and parts[3] == "recover":
                if not self._require_recovery_auth(): return
                self._write_json(200, {"attempt": recover_factory_execution(self.conn, parts[1], request_key=body.get("request_key"), evidence=body.get("evidence"), actor="recovery-token")})
                return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "inspection-result":
                if not self._require_inspection_auth(): return
                self._write_json(200, {"inspection": record_pr_inspection(self.conn, parts[1], body.get("result"), request_key=body.get("inspection_request_key"), capability=body.get("inspection_capability"))})
                return

            if len(parts) == 3 and parts[0] == "factory" and parts[1] == "outbox" and parts[2] == "claim":
                item = claim_factory_outbox_delivery(self.conn, body.get("outbox_key"))
                self._write_json(200, {"outbox": item})
                return
            if len(parts) == 4 and parts[0] == "factory" and parts[1] == "outbox" and parts[3] == "ack":
                acknowledge_factory_outbox(self.conn, parts[2], body.get("delivery_lease_id")); self._write_json(200, {"ok": True}); return

            if len(parts) == 5 and parts[0] == "jobs" and parts[2] == "verification" and parts[3] == "lease" and parts[4] == "claim":
                if not self._require_verifier_auth():
                    return
                self._write_json(200, {"claimed": claim_verification_lease(self.conn, parts[1], attempt_id=body.get("attempt_id"))})
                return

            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "verification" and parts[3] == "observation":
                if not self._require_verifier_auth():
                    return
                job = record_verification_observation(self.conn, parts[1], attempt_id=body.get("verification_attempt_id"), receipt=body.get("verification_receipt"))
                self._write_json(200, {"job": job})
                return

            if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "verification" and parts[3] == "completion":
                if not self._require_verifier_auth():
                    return
                state = body.get("state")
                if state not in {"ready_for_human_review", "failed"} or not isinstance(body.get("details"), dict):
                    raise BridgeValidationError("verification completion requires a terminal verification state and details")
                job = transition_job(
                    self.conn,
                    parts[1],
                    state=state,
                    details=body["details"],
                    _factory_verifier_completion=True,
                )
                self._write_json(200, {"job": job})
                return

            if parts == ["reconciliation", "findings"]:
                created, existing = record_reconciliation_findings(self.conn, _finding_batch(body))
                self._write_json(200, {"created_finding_keys": created, "existing_finding_keys": existing})
                return

            self._write_json(404, {"error": "not found"})
        except Exception as exc:
            self._handle_error(exc)


def _bounded_limit(query: dict[str, list[str]], *, default: int, label: str, key: str = "limit", maximum: int = 250) -> int:
    values = query.get(key, [])
    if not values:
        return default
    if len(values) != 1:
        raise BridgeValidationError(f"{label} must be specified at most once")
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise BridgeValidationError(f"{label} must be an integer") from exc
    if value < 1 or value > maximum:
        raise BridgeValidationError(f"{label} must be between 1 and {maximum}")
    return value


def _release_commit() -> str:
    """Return non-secret immutable release provenance for health validation."""
    configured = os.environ.get("DRIFTWARD_BRIDGE_RELEASE_VERSION")
    metadata = Path(__file__).resolve().parents[1] / "RELEASE_METADATA.json"
    try:
        value = json.loads(metadata.read_text()).get("commit")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return configured if configured else "development-unversioned"


def _single(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key, [])
    if not values:
        return None
    if len(values) != 1 or not values[0]:
        raise BridgeValidationError(f"{key} must be specified at most once")
    return values[0]


def _finding_batch(body: dict[str, Any]) -> list[dict[str, Any]]:
    if set(body) != {"findings"} or not isinstance(body.get("findings"), list):
        raise BridgeValidationError("reconciliation findings body must contain only findings")
    return body["findings"]


def _error_response(exc: Exception) -> tuple[int, dict[str, str]]:
    if isinstance(exc, BridgeNotFound):
        return 404, {"error": str(exc)}
    if isinstance(exc, BridgeConflict):
        return 409, {"error": str(exc)}
    if isinstance(exc, BridgeValidationError):
        return 400, {"error": str(exc)}
    if isinstance(exc, BridgeError):
        return 500, {"error": str(exc)}
    return 500, {"error": "internal server error"}


def serve(db_path: str, host: str, port: int) -> None:
    conn = connect_runtime(db_path, check_same_thread=False)
    httpd = HTTPServer((host, port), BridgeHandler)
    httpd.conn = conn
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()
