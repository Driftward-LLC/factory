from __future__ import annotations

import argparse
import json
import sys
import os
import subprocess

from .server import serve
from .operator import serve_operator
from .reconciler import reconcile_jobs
from .store import (
    DEFAULT_DB_PATH,
    BridgeError,
    connect_migrate,
    connect_runtime,
    get_job,
    list_events,
    list_jobs,
    submit_job,
    transition_job,
    list_factory_outbox,
    claim_factory_outbox_delivery,
    acknowledge_factory_outbox,
    fail_factory_outbox_delivery,
    list_factory_inspection_outbox,
    claim_factory_inspection_outbox,
    acknowledge_factory_inspection_outbox,
    fail_factory_inspection_outbox_delivery,
)


def print_json(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_json_object(raw: str) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hatchet Hermes bridge")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Explicitly migrate/backfill the bridge database")

    status = sub.add_parser("status", help="List recent jobs")
    status.add_argument("--limit", type=int, default=20)

    serve_parser = sub.add_parser("serve", help="Serve local HTTP API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=19877)

    operator = sub.add_parser("serve-operator", help="Serve root-only factory retry operator socket")
    operator.add_argument("--socket", default="/run/driftward-factory-operator/operator.sock")

    submit = sub.add_parser("submit", help="Submit or deduplicate a job")
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--action", required=True)
    submit.add_argument("--payload-json", default="{}")
    submit.add_argument("--hatchet-workflow-run-id")
    submit.add_argument("--hatchet-step-run-id")

    get = sub.add_parser("get", help="Get a job by job ID or idempotency key")
    get.add_argument("ref")

    events = sub.add_parser("events", help="List events for a job")
    events.add_argument("ref")

    transition = sub.add_parser("transition", help="Transition a job")
    transition.add_argument("ref")
    transition.add_argument("--state", required=True)
    transition.add_argument("--details-json", default="{}")

    reconcile = sub.add_parser("reconcile", help="Reconcile stale bridge jobs against GitHub")
    reconcile.add_argument("--stale-minutes", type=float, default=30.0)
    reconcile.add_argument("--limit", type=int, default=50)
    reconcile.add_argument("--dry-run", action="store_true")
    dispatch = sub.add_parser("dispatch-factory-outbox", help="Deliver fixed accepted events from the retry outbox")
    dispatch.add_argument("--limit", type=int, default=50)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            serve(args.db, args.host, args.port)
            return 0
        if args.command == "serve-operator":
            serve_operator(args.socket)
            return 0

        conn = connect_migrate(args.db) if args.command == "init" else connect_runtime(args.db)
        try:
            if args.command == "init":
                print_json({"ok": True, "db": args.db})
            elif args.command == "status":
                print_json({"jobs": list_jobs(conn, args.limit)})
            elif args.command == "submit":
                payload = parse_json_object(args.payload_json)
                job, created = submit_job(
                    conn,
                    {
                        "idempotency_key": args.idempotency_key,
                        "requested_action": args.action,
                        "payload": payload,
                        "hatchet_workflow_run_id": args.hatchet_workflow_run_id,
                        "hatchet_step_run_id": args.hatchet_step_run_id,
                    },
                )
                print_json({"created": created, "job": job})
            elif args.command == "get":
                print_json(get_job(conn, args.ref))
            elif args.command == "events":
                print_json({"events": list_events(conn, args.ref)})
            elif args.command == "transition":
                details = parse_json_object(args.details_json)
                print_json(transition_job(conn, args.ref, state=args.state, details=details))
            elif args.command == "reconcile":
                from datetime import timedelta

                results = reconcile_jobs(
                    conn,
                    stale_after=timedelta(minutes=args.stale_minutes),
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
                print_json({"dry_run": args.dry_run, "results": results})
            elif args.command == "dispatch-factory-outbox":
                delivered = failed = 0
                release_root = os.environ.get("DRIFTWARD_BRIDGE_RELEASE_ROOT")
                if not release_root:
                    raise BridgeError("immutable bridge release root is required for outbox dispatch")
                deliveries = [(row, claim_factory_outbox_delivery, acknowledge_factory_outbox, fail_factory_outbox_delivery) for row in list_factory_outbox(conn, max(1, min(args.limit, 250)))]
                deliveries += [(row, claim_factory_inspection_outbox, acknowledge_factory_inspection_outbox, fail_factory_inspection_outbox_delivery) for row in list_factory_inspection_outbox(conn, max(1, min(args.limit, 250)))]
                for row, claim, acknowledge, fail in deliveries:
                    claimed = claim(conn, row["outbox_key"])
                    if not claimed or (claimed.get("event_type") and claimed["event_type"] != "hermes_job_accepted"):
                        continue
                    payload = claimed["payload"]
                    env = {key: value for key, value in os.environ.items() if key.startswith("HATCHET_CLIENT_")}
                    env.update({"BRIDGE_JOB_ID": str(payload["job_id"]), "BRIDGE_IDEMPOTENCY_KEY": str(payload["idempotency_key"])})
                    try:
                        result = subprocess.run(["/usr/bin/node", f"{release_root}/worker-triggers/push-accepted-event.js"], env=env, capture_output=True, timeout=15, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        result = None
                    if result is not None and result.returncode == 0:
                        acknowledge(conn, claimed["outbox_key"]); delivered += 1
                    else:
                        fail(conn, claimed["outbox_key"], claimed["delivery_lease_id"]); failed += 1
                        print(f"factory outbox delivery failed: {claimed['outbox_key']}", file=sys.stderr)
                print_json({"delivered": delivered, "failed": failed})
                if failed:
                    return 1
            else:
                parser.error(f"unknown command {args.command}")
        finally:
            conn.close()
    except (BridgeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
