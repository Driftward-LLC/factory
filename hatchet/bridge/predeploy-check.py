#!/usr/bin/env python3
"""Sanitized predeploy assertions for verifier rollout; never prints row data."""
from __future__ import annotations

import argparse
import sqlite3
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hatchet_hermes_bridge.legacy_artifacts import (  # noqa: E402
    json_object,
    reconcile_legacy_active_row,
    routing_repositories,
)
from hatchet_hermes_bridge.store import classify_factory_import_conflicts  # noqa: E402

KEY_RE = re.compile(r"[A-Z][A-Z0-9]*-[1-9][0-9]*$")


def _inspection_sources(conn: sqlite3.Connection, tables: set[str], work_item: str) -> list[tuple[str, dict[str, object]]]:
    if "factory_inspection_requests" not in tables:
        return []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(factory_inspection_requests)")}
    required = {"work_item_key", "url", "result_json"}
    count = int(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests").fetchone()[0])
    # An empty unrelated table is provably ignored.  Any populated unknown or
    # partial schema might be migrated/imported, so stop before install writes.
    if count and not required <= columns:
        raise ValueError("legacy inspection schema is unsupported")
    if not required <= columns:
        return []
    sources: list[tuple[str, dict[str, object]]] = []
    for row in conn.execute("SELECT url,result_json FROM factory_inspection_requests WHERE work_item_key=?", (work_item,)):
        sources.append(("inspection", {"url": row["url"]}))
        if row["result_json"] is not None:
            sources.append(("inspection", json_object(row["result_json"])))
    return sources


def _rows(conn: sqlite3.Connection, tables: set[str], table: str, query: str, args: tuple[object, ...]) -> list[dict[str, object]]:
    if table not in tables:
        return []
    return [dict(row) for row in conn.execute(query, args)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--require-zero-legacy-ready", action="store_true")
    parser.add_argument("--require-zero-import-conflicts", action="store_true")
    parser.add_argument("--validate-legacy-import", action="store_true")
    parser.add_argument("--require-current-attestations", action="store_true")
    parser.add_argument("--routing-registry", default=str(Path(__file__).resolve().parents[1] / "workers" / "routing-registry.json"))
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print("legacy ready receipts: 0")
        if args.require_zero_import_conflicts or args.validate_legacy_import:
            print("legacy import conflicts: 0")
            print("legacy conflict attestations: attested=0 required=0")
        return 0
    try:
        routed_repositories, enabled_repositories = routing_repositories(Path(args.routing_registry).read_text()) if args.validate_legacy_import else (set(), set())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print("legacy ready receipts: 0")
        print("legacy import conflicts: 0")
        print("legacy conflict attestations: attested=0 required=0")
        print("legacy import validation errors: 1")
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bridge_jobs)")}
        if "verification_original_context_json" not in columns:
            count = int(conn.execute("SELECT COUNT(*) FROM bridge_jobs WHERE state = 'ready_for_human_review'").fetchone()[0])
        else:
            count = int(conn.execute("SELECT COUNT(*) FROM bridge_jobs WHERE state = 'ready_for_human_review' AND verification_original_context_json IS NULL").fetchone()[0])
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conflict_counts = classify_factory_import_conflicts(conn, routing_registry=Path(args.routing_registry), legacy_only=True) if "factory_import_conflicts" in tables else {"original": 0, "resolvable": 0, "unresolved": 0, "attested": 0, "attestation_required": 0}
        legacy_errors = 0
        if args.validate_legacy_import:
            if "factory_inspection_requests" in tables:
                inspection_columns = {row[1] for row in conn.execute("PRAGMA table_info(factory_inspection_requests)")}
                inspection_count = int(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests").fetchone()[0])
                if inspection_count and not {"work_item_key", "url", "result_json"} <= inspection_columns:
                    legacy_errors += 1
            seen: set[tuple[str, str]] = set()
            urls: dict[str, str] = {}
            branches: dict[str, str] = {}
            action_column = "requested_action" if "requested_action" in columns else "'create_draft_pr' AS requested_action"
            for row in conn.execute(f"SELECT job_id,idempotency_key,payload_json,branch,pr_url,state,{action_column} FROM bridge_jobs WHERE idempotency_key LIKE 'hatchet:work-item:linear:%:create_draft_pr:%:active'"):
                try:
                    source = dict(row)
                    payload = json_object(source["payload_json"])
                    work_item = payload.get("linear_issue") or payload.get("work_item_id")
                    if not isinstance(work_item, str) or not KEY_RE.fullmatch(work_item): raise ValueError
                    mode = "dry-run" if payload.get("dry_run") is True else "real"
                    attempts = _rows(conn, tables, "factory_attempts", "SELECT * FROM factory_attempts WHERE job_id=? OR work_item_key=?", (source["job_id"], work_item))
                    attempt_ids = [item["attempt_id"] for item in attempts if isinstance(item.get("attempt_id"), str)]
                    leases = _rows(conn, tables, "factory_execution_leases", f"SELECT * FROM factory_execution_leases WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})", tuple(attempt_ids)) if attempt_ids else []
                    attempt_events = _rows(conn, tables, "factory_attempt_events", f"SELECT * FROM factory_attempt_events WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})", tuple(attempt_ids)) if attempt_ids else []
                    attempt_outbox = _rows(conn, tables, "factory_attempt_outbox", f"SELECT * FROM factory_attempt_outbox WHERE job_id=? OR attempt_id IN ({','.join('?' for _ in attempt_ids)})", (source["job_id"], *attempt_ids)) if attempt_ids else _rows(conn, tables, "factory_attempt_outbox", "SELECT * FROM factory_attempt_outbox WHERE job_id=?", (source["job_id"],))

                    def artifacts() -> object:
                        yield ("bridge", {"pr_url": source["pr_url"], "branch": source["branch"]})
                        if "bridge_events" not in tables:
                            raise ValueError("bridge event history missing")
                        for event in conn.execute("SELECT details_json FROM bridge_events WHERE job_id=?", (source["job_id"],)):
                            yield ("event", json_object(event["details_json"]))
                        yield from _inspection_sources(conn, tables, work_item)

                    decision = reconcile_legacy_active_row(
                        source,
                        artifact_sources=artifacts(),
                        attempts=attempts,
                        import_conflicts=(),
                        execution_leases=leases,
                        attempt_events=attempt_events,
                        attempt_outbox=attempt_outbox,
                        routed_repositories=routed_repositories,
                        enabled_repositories=enabled_repositories,
                    )
                    if decision.action == "not_legacy":
                        continue
                    if (work_item, mode) in seen:
                        raise ValueError("duplicate legacy work item")
                    seen.add((work_item, mode))
                    if decision.prior_pr_url:
                        if decision.prior_pr_url in urls and urls[decision.prior_pr_url] != source["job_id"]:
                            raise ValueError("duplicate legacy artifact reference")
                        urls[decision.prior_pr_url] = source["job_id"]
                    if decision.branch in branches and branches[decision.branch] != source["job_id"]:
                        raise ValueError("duplicate legacy artifact reference")
                    branches[decision.branch] = source["job_id"]
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_errors += 1
    finally:
        conn.close()
    print(f"legacy ready receipts: {count}")
    if args.require_zero_import_conflicts or args.validate_legacy_import:
        print("legacy import conflicts: original={original} resolvable={resolvable} unresolved={unresolved}".format(**conflict_counts))
        print("legacy conflict attestations: attested={attested} required={attestation_required}".format(**conflict_counts))
    if args.validate_legacy_import:
        print(f"legacy import validation errors: {legacy_errors}")
    return 1 if ((args.require_zero_legacy_ready and count) or (args.require_zero_import_conflicts and conflict_counts["unresolved"]) or (args.require_current_attestations and conflict_counts["attestation_required"]) or (args.validate_legacy_import and (conflict_counts["unresolved"] or legacy_errors))) else 0


if __name__ == "__main__":
    raise SystemExit(main())
