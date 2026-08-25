from __future__ import annotations

import argparse
import json
import os
import threading
import time
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .outbox import Outbox
from .writer import Config, LifecycleWriter, SourceError


def build_writer() -> tuple[LifecycleWriter, Outbox]:
    outbox = Outbox(os.environ.get("FACTORY_LIFECYCLE_DB", "/var/lib/driftward-factory-lifecycle-writer/outbox.db"))
    return LifecycleWriter(Config.from_env(), outbox), outbox


def scan_and_deliver(writer: LifecycleWriter) -> dict:
    writer.verify()
    scan = writer.scan()
    delivery = writer.deliver()  # Must drain even when a source is unavailable.
    return {"scan": scan, "delivery": delivery}


def status_health(source_errors: dict, delivery: dict, outbox_status: dict) -> tuple[bool, str | None]:
    pending_age = outbox_status.get("oldest_queue_age_seconds") or 0
    unhealthy = bool(source_errors or delivery.get("retried") or pending_age > 600)
    return (not unhealthy, "source_or_outbox_degraded" if unhealthy else None)


def lifecycle_bind_host() -> str:
    return os.environ.get("FACTORY_LIFECYCLE_HOST", "127.0.0.1")


def serve(interval: int) -> int:
    outbox = Outbox(os.environ.get("FACTORY_LIFECYCLE_DB", "/var/lib/driftward-factory-lifecycle-writer/outbox.db"))
    state = {"ok": False, "last_error": "starting", "last_scan": None, "source_errors": {}}
    def loop() -> None:
        while True:
            try:
                writer = LifecycleWriter(Config.from_env(), outbox)
                result = scan_and_deliver(writer)
                status = outbox.status()
                ok, last_error = status_health(writer.source_errors, result["delivery"], status)
                state.update(ok=ok, last_error=last_error, last_scan=time.time(), source_errors=writer.source_errors)
            except Exception as exc:  # details stay in local journal, never Linear
                logging.getLogger(__name__).warning("factory lifecycle cycle failed: %s", type(exc).__name__)
                state.update(ok=False, last_error=type(exc).__name__, last_scan=time.time())
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/health", "/status"}:
                self.send_error(404); return
            payload = json.dumps({"ok": state["ok"], "last_error": state["last_error"], "source_errors": state["source_errors"], "outbox": outbox.status()}, sort_keys=True).encode()
            self.send_response(200 if state["ok"] else 503); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self, format, *args):
            return
    ThreadingHTTPServer((lifecycle_bind_host(), int(os.environ.get("FACTORY_LIFECYCLE_PORT", "19880"))), Handler).serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory lifecycle writer")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    canary = commands.add_parser("canary")
    canary.add_argument("--issue-id", required=True); canary.add_argument("--identifier", required=True); canary.add_argument("--issue-url", required=True)
    serve_parser = commands.add_parser("serve"); serve_parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)
    writer, outbox = build_writer()
    try:
        if args.command == "serve":
            outbox.close(); return serve(max(10, min(args.interval, 300)))
        writer.verify()
        if args.command == "canary":
            key = writer.canary(issue_id=args.issue_id, identifier=args.identifier, issue_url=args.issue_url)
            result = {"canary_key": key, "delivery": writer.deliver()}
        else:
            result = {"scan": writer.scan(), "delivery": writer.deliver(), "outbox": outbox.status()}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (SourceError, ValueError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True))
        return 1
    finally:
        outbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
