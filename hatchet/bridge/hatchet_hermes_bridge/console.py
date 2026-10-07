"""Sanitized Factory console with opt-in, narrowly scoped issue approvals."""
from __future__ import annotations

import os
import re
import socket
import sqlite3
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .analytics import factory_analytics
from .console_evidence import _time, attempt_evidence
from .console_intake import observed_intake
from .console_control import install_issue_control_routes
from .store import (
    BridgeError,
    BridgeConflict,
    BridgeNotFound,
    BridgeValidationError,
    EXECUTION_FAILURE_DOMAINS,
    EXECUTION_FAILURE_STAGES,
    connect_readonly,
    get_factory_lifecycle,
    list_active_factory_attempts,
    list_recent_factory_attempts,
    reconciliation_findings_status,
)


ISSUE_RE = re.compile(r"^ENG-[1-9][0-9]*$")
DEFAULT_ASSET_ROOT = Path(__file__).resolve().parents[3] / "factory" / "console"
ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/styles.css": ("styles.css", "text/css"),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
# Legacy report dimensions may contain free text; browser labels must be known.
ANALYTICS_FAILURE_REASONS = frozenset({
    "artifact_fence_changed", "artifact_fence_invalid",
    "factory_execution_recovery_required", "factory_receipt_job_load_failed",
    "factory_receipt_analytics_setup_failed", "hermes_process_timeout",
    "hermes_process_exit_nonzero", "hermes_process_output_limit_exceeded",
    "hermes_grpc_client_setup_failed", "hermes_grpc_response_malformed",
    "hermes_grpc_gateway_unavailable", "hermes_grpc_gateway_timeout",
    "hermes_grpc_gateway_unauthenticated", "hermes_grpc_gateway_forbidden",
    "verification_pr_base_mismatch", "verification_pr_author_mismatch",
    "verification_pr_linear_link_missing", "verification_changed_file_scope_invalid",
    "verification_changed_file_forbidden", "verification_github_checks_missing",
    "verification_github_checks_pending", "verification_github_checks_timeout",
    "verification_github_required_check_missing", "verification_github_check_failed",
    "verification_local_command_evidence_missing", "verification_local_command_failed",
    "verification_local_command_policy_mismatch", "verification_secret_check_evidence_missing",
    "verification_secret_check_failed", "verification_secret_check_policy_mismatch",
    "verification_policy_unavailable", "verification_policy_missing", "verification_policy_invalid",
    "verification_commit_mismatch", "verification_evidence_malformed",
    "verification_evidence_unavailable", "verification_recovery_context_unavailable",
})


def _mode() -> str:
    return "real" if os.environ.get("HATCHET_INTAKE_DRY_RUN", "true").strip().lower() == "false" else "dry-run"


def _with_agent_trace(record: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    """Navigation only, not proof of ingestion or execution completion."""
    attempt = record["attempt"]
    trace = {"attempt_id": attempt["attempt_id"], "status": "unavailable", "url": None}
    project = os.environ.get("FACTORY_CONSOLE_POSTHOG_PROJECT_URL", "")
    lineage = ((record.get("job") or {}).get("payload") or {}).get("factory_retry")
    if attempt.get("mode") == "dry-run":
        trace["status"] = "not_applicable"
    elif (attempt.get("mode") == "real"
          and (record.get("job") or {}).get("reason_code") == "factory_receipt_analytics_setup_failed"):
        trace["status"] = "setup_failed"
    elif not re.fullmatch(r"https://(?:us|eu|app)\.posthog\.com/project/[1-9][0-9]*/?", project):
        trace["status"] = "not_configured"
    elif (attempt.get("mode") == "real" and isinstance(lineage, dict)
          and lineage.get("attempt_id") == attempt["attempt_id"]
          and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", attempt["attempt_id"])):
        # Matches the Hermes observer's attempt-scoped UUIDv5 contract.
        trace_id = uuid.uuid5(uuid.NAMESPACE_URL, f"driftward-hermes:trace:attempt:{attempt['attempt_id']}")
        started = conn.execute(
            "SELECT created_at FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type IN ('execution_backend_invoked','attempt_started') "
            "ORDER BY CASE event_type WHEN 'execution_backend_invoked' THEN 0 ELSE 1 END,created_at,event_id LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        anchor = _time(started[0] if started else attempt.get("created_at"))
        if anchor:
            try:
                timestamp = anchor.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            except OverflowError:
                pass  # An unrepresentable UTC timestamp cannot provide a safe lookup.
            else:
                trace.update(status="unverified", url=f"{project.rstrip('/')}/ai-observability/traces/{trace_id}?{urlencode({'timestamp': timestamp})}")
    return {**record, "agent_trace": trace}


def _allow_unauthenticated() -> bool:
    return os.environ.get("FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED", "").strip().lower() in {"1", "true", "yes"}


def _identity(headers: Any) -> str | None:
    value = headers.get("Tailscale-User-Login")
    if not isinstance(value, str):
        return "local-development" if _allow_unauthenticated() else None
    value = value.strip()
    if not value or len(value) > 320 or any(ord(char) < 32 for char in value):
        return None
    return value


def _status_counts(conn: Any) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM factory_attempts GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _lifecycle_status(path: str | None) -> dict[str, Any]:
    unavailable = {"available": False, "dead_letters": 0, "dead_letter_reasons": {}, "archived": 0}
    if not path:
        return unavailable
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            states = {str(row[0]): int(row[1]) for row in conn.execute("SELECT delivery_state, COUNT(*) FROM lifecycle_outbox GROUP BY delivery_state")}
            reasons = {
                str(row[0] or "unknown"): int(row[1])
                for row in conn.execute("SELECT reason_code, COUNT(*) FROM lifecycle_outbox WHERE delivery_state = 'dead_letter' GROUP BY reason_code")
            }
        finally:
            conn.close()
        return {"available": True, "dead_letters": states.get("dead_letter", 0), "dead_letter_reasons": reasons, "archived": states.get("archived", 0)}
    except (OSError, sqlite3.Error):
        return unavailable


def overview(db_path: str, viewer: str, lifecycle_db_path: str | None = None, ingress_db_path: str | None = None) -> dict[str, Any]:
    conn = connect_readonly(db_path)
    try:
        # Intake exclusion and attempt lists must agree on one ledger snapshot.
        conn.execute("BEGIN")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "viewer": viewer,
            "mode": _mode(),
            "intake": observed_intake(conn, ingress_db_path),
            "active_attempts": [_with_agent_trace(row, conn) for row in list_active_factory_attempts(conn)],
            "recent_attempts": [_with_agent_trace(row, conn) for row in list_recent_factory_attempts(conn)],
            "status_counts": _status_counts(conn),
            "reconciliation": reconciliation_findings_status(conn),
            "lifecycle_outbox": _lifecycle_status(lifecycle_db_path),
            "links": {
                "hatchet": os.environ.get("FACTORY_CONSOLE_HATCHET_URL", "http://127.0.0.1:8888/"),
            },
        }
    finally:
        conn.close()


def lifecycle(db_path: str, issue: str, ingress_db_path: str | None = None) -> dict[str, Any]:
    if not ISSUE_RE.fullmatch(issue):
        raise BridgeValidationError("issue must be an ENG identifier")
    conn = connect_readonly(db_path)
    try:
        # Keep the attempt, immutable checkpoints and inspection in one read snapshot.
        with conn:
            conn.execute("BEGIN")
            report = get_factory_lifecycle(conn, issue)
            report["attempts"] = [_with_agent_trace(row, conn) for row in report["attempts"]]
            report["verification_evidence"] = attempt_evidence(conn, report["attempts"][0]) if report["attempts"] else None
            report["intake"] = observed_intake(conn, ingress_db_path, issue)
            return report
    finally:
        conn.close()


def _fields(value: dict[str, Any] | None, *keys: str) -> dict[str, Any] | None:
    return {key: value.get(key) for key in keys} if value is not None else None


def analytics(db_path: str, ingress_db_path: str | None, lifecycle_db_path: str | None, days: int) -> dict[str, Any]:
    report = factory_analytics(db_path, ingress_db_path or "", lifecycle_db_path or "", days=days)
    github = report["github"]
    classifications: Counter[tuple[str, str, str]] = Counter()
    for row in report["failures"]["by_classification"]:
        key = tuple(
            row[field] if row[field] in allowed else "unknown"
            for field, allowed in (
                ("failure_domain", EXECUTION_FAILURE_DOMAINS),
                ("failure_stage", EXECUTION_FAILURE_STAGES),
                ("reason_code", ANALYTICS_FAILURE_REASONS),
            )
        )
        classifications[key] += row["count"]
    return {
        "schema": "factory-console-analytics-v1",
        "generated_at": report["generated_at"],
        "window": _fields(report["window"], "days", "started_at", "ended_at"),
        "coverage": _fields(report["coverage"], "cohort_attempts", "cohort_work_items", "dry_run_attempts_excluded", "legacy_attempts_excluded", "bridge_available", "ingress_available"),
        "time_series": {
            **_fields(report["time_series"], "timezone", "basis", "started_at", "ended_at"),
            "points": [
                _fields(point, "date", "partial", "prs_recorded", "verified_attempts", "repair_attempts", "failed_attempts")
                for point in report["time_series"]["points"]
            ],
            "coverage": {
                metric: _fields(report["time_series"]["coverage"][metric], "observed", "eligible", "rate", "complete")
                for metric in ("prs_recorded", "verified_attempts", "repair_attempts", "failed_attempts")
            },
        },
        "funnel": {"stages": {
            stage: _fields(report["funnel"]["stages"][stage], "count", "rate_from_attempted")
            for stage in ("attempted", "executor_started", "effect_committed", "draft_pr_available", "verified_ready")
        }},
        "github": {
            **_fields(github, "merged_prs", "closed_unmerged_prs", "open_prs", "unobserved_prs"),
            "coverage": _fields(github.get("coverage"), "observed", "eligible", "rate", "complete"),
            **{
                key: {
                    **_fields(github.get(key) or {}, *fields),
                    "available": (github.get(key) or {}).get("available", False),
                    "coverage": _fields((github.get(key) or {}).get("coverage"), "observed", "eligible", "rate", "complete"),
                }
                for key, fields in (
                    ("check_suite_observations", ("available", "observations", "successful_observations")),
                    ("reviews", ("available", "changes_requested_prs", "changes_requested_rate")),
                )
            },
            "first_pass_checks": _fields(github.get("first_pass_checks") or {"available": False}, "available", "success_rate"),
        },
        "recovery": {
            "by_attempt_kind": {
                kind: _fields(report["recovery"]["by_attempt_kind"][kind], "attempts", "verified")
                for kind in ("initial", "retry", "absence_retry", "repair", "unknown")
            },
            "repair_budgets_exhausted": report["recovery"]["repair_budgets_exhausted"],
        },
        "failures": {
            "currently_failed_attempts": report["failures"]["currently_failed_attempts"],
            "by_classification": [
                {"failure_domain": key[0], "failure_stage": key[1], "reason_code": key[2], "count": count}
                for key, count in sorted(classifications.items(), key=lambda item: (-item[1], item[0]))
            ],
        },
        "latency_ms": {
            stage: _fields(report["latency_ms"][stage], "samples", "p50", "p95", "eligible", "coverage_rate")
            for stage in ("attempt_to_executor_started", "executor_started_to_effect_committed", "effect_committed_to_verified_ready", "attempt_to_terminal")
        },
    }


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def create_console_app(db_path: str, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None, ingress_db_path: str | None = None) -> FastAPI:
    assets = Path(asset_root or DEFAULT_ASSET_ROOT)
    app = FastAPI(
        title="Driftward Factory Console API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def console_boundary(request: Request, call_next: Any) -> Any:
        if request.url.path != "/health":
            viewer = _identity(request.headers)
            if viewer is None:
                response = _error(401, "Factory Console requires Tailscale Serve authentication")
            else:
                request.state.viewer = viewer
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(BridgeNotFound)
    async def not_found(_: Request, __: BridgeNotFound) -> JSONResponse:
        return _error(404, "not found")

    @app.exception_handler(BridgeError)
    async def bridge_error(_: Request, exc: BridgeError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(BridgeConflict)
    async def conflict(_: Request, exc: BridgeConflict) -> JSONResponse:
        return _error(409, str(exc))

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, bool]:
        try:
            conn = connect_readonly(db_path)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            if not all((assets / name).is_file() for name, _ in ASSETS.values()):
                raise OSError("console assets unavailable")
            if lifecycle_db_path and not _lifecycle_status(lifecycle_db_path)["available"]:
                raise OSError("lifecycle outbox unavailable")
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="console unavailable") from exc

    @app.get("/api/overview")
    def api_overview(request: Request) -> dict[str, Any]:
        return overview(db_path, request.state.viewer, lifecycle_db_path, ingress_db_path)

    install_issue_control_routes(app, db_path, ingress_db_path)

    @app.get("/api/lifecycle/{issue}")
    def api_lifecycle(issue: str) -> dict[str, Any]:
        return lifecycle(db_path, issue, ingress_db_path)

    @app.get("/api/analytics")
    def api_analytics(request: Request) -> dict[str, Any]:
        query = request.query_params
        if set(query) - {"days"}:
            raise HTTPException(status_code=400, detail="analytics has unsupported query parameters")
        values = query.getlist("days") if "days" in query else ["30"]
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,3}", values[0]) or not 1 <= int(values[0]) <= 365:
            raise HTTPException(status_code=400, detail="analytics days must be an integer between 1 and 365")
        try:
            return analytics(db_path, ingress_db_path, lifecycle_db_path, int(values[0]))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="analytics unavailable") from exc

    def asset_handler(name: str, media_type: str) -> Any:
        def asset() -> FileResponse:
            path = assets / name
            if not path.is_file():
                raise HTTPException(status_code=500, detail="Console asset unavailable")
            return FileResponse(path, media_type=media_type)

        return asset

    for route, (name, media_type) in ASSETS.items():
        app.add_api_route(route, asset_handler(name, media_type), methods=["GET"], include_in_schema=False)

    return app


class ConsoleServer:
    """Small synchronous wrapper used beside the Unix-socket operator server."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self._socket = socket.create_server((host, port))
        self._stopped = threading.Event()
        self.server_port = int(self._socket.getsockname()[1])
        config = uvicorn.Config(app, access_log=False, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)

    def serve_forever(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._stopped.wait(timeout=5)

    def server_close(self) -> None:
        self._socket.close()


def make_console_server(db_path: str, host: str, port: int, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None, ingress_db_path: str | None = None) -> ConsoleServer:
    return ConsoleServer(create_console_app(db_path, asset_root, lifecycle_db_path, ingress_db_path), host, port)


def serve_console(db_path: str, host: str, port: int, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None, ingress_db_path: str | None = None) -> None:
    server = make_console_server(db_path, host, port, asset_root, lifecycle_db_path, ingress_db_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
