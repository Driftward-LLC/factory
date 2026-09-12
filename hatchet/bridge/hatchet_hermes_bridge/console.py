"""Read-only FastAPI console for sanitized Factory lifecycle data."""
from __future__ import annotations

import os
import re
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .store import (
    BridgeError,
    BridgeNotFound,
    BridgeValidationError,
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


def _mode() -> str:
    return "real" if os.environ.get("HATCHET_INTAKE_DRY_RUN", "true").strip().lower() == "false" else "dry-run"


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


def overview(db_path: str, viewer: str, lifecycle_db_path: str | None = None) -> dict[str, Any]:
    conn = connect_readonly(db_path)
    try:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "viewer": viewer,
            "mode": _mode(),
            "active_attempts": list_active_factory_attempts(conn),
            "recent_attempts": list_recent_factory_attempts(conn),
            "status_counts": _status_counts(conn),
            "reconciliation": reconciliation_findings_status(conn),
            "lifecycle_outbox": _lifecycle_status(lifecycle_db_path),
            "links": {
                "hatchet": os.environ.get("FACTORY_CONSOLE_HATCHET_URL", "http://127.0.0.1:8888/"),
            },
        }
    finally:
        conn.close()


def lifecycle(db_path: str, issue: str) -> dict[str, Any]:
    if not ISSUE_RE.fullmatch(issue):
        raise BridgeValidationError("issue must be an ENG identifier")
    conn = connect_readonly(db_path)
    try:
        return get_factory_lifecycle(conn, issue)
    finally:
        conn.close()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def create_console_app(db_path: str, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None) -> FastAPI:
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
        return overview(db_path, request.state.viewer, lifecycle_db_path)

    @app.get("/api/lifecycle/{issue}")
    def api_lifecycle(issue: str) -> dict[str, Any]:
        return lifecycle(db_path, issue)

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


def make_console_server(db_path: str, host: str, port: int, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None) -> ConsoleServer:
    return ConsoleServer(create_console_app(db_path, asset_root, lifecycle_db_path), host, port)


def serve_console(db_path: str, host: str, port: int, asset_root: str | Path | None = None, lifecycle_db_path: str | None = None) -> None:
    server = make_console_server(db_path, host, port, asset_root, lifecycle_db_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
