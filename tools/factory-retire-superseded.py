#!/usr/bin/env python3
"""Root maintenance retirement of an absence-superseded orphan execution.

Containment is trusted operator evidence, not inferred from database expiry.
This script never migrates a database or changes issue/attempt ownership.
"""
from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hatchet/bridge"))
from hatchet_hermes_bridge import interrupted_resolution as recovery, store

OPERATION = "factory-superseded-retirement-v1"


def _target(conn, attempt_id, successor_attempt_id):
    attempt = store.get_factory_attempt(conn, attempt_id)
    successor = store.get_factory_attempt(conn, successor_attempt_id)
    job = store.get_job(conn, attempt["job_id"])
    successor_job = store.get_job(conn, successor["job_id"])
    lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt_id,)).fetchone()
    if (attempt["mode"] != "real" or attempt["action"] != "create_draft_pr" or attempt["status"] != "superseded"
            or attempt["prior_pr_url"] or attempt["supersede_pr_url"]
            or job["state"] != "running" or job["requested_action"] != "create_draft_pr"
            or job["branch"] != attempt["branch"] or not lease or not lease["effect_authorized_at"]
            or lease["released_at"] or lease["cancel_requested_at"] or recovery._age(lease["expires_at"]) <= 0):
        raise store.BridgeConflict("requires an absence-superseded running real job with expired authorized orphan lease")
    if (job["pr_url"] or job["commit_sha"] or job["verification_receipt"] or lease["effect_observed_at"]
            or conn.execute("SELECT 1 FROM factory_attempt_events WHERE attempt_id=? AND "
                            "(event_type='execution_effect_commit_failed' OR (event_type='execution_receipt_observed' "
                            "AND json_extract(details_json,'$.state')='completed'))", (attempt_id,)).fetchone()):
        raise store.BridgeConflict("observed receipts require receipt recovery")
    if conn.execute("SELECT 1 FROM factory_recovery_requests WHERE attempt_id=?", (attempt_id,)).fetchone():
        raise store.BridgeConflict("attempt already has a recovery decision")
    latest = conn.execute("SELECT MAX(generation) FROM factory_attempts WHERE work_item_key=?", (attempt["work_item_key"],)).fetchone()[0]
    if (successor["prior_attempt_id"] != attempt_id or successor["work_item_key"] != attempt["work_item_key"]
            or successor["generation"] != attempt["generation"] + 1 or latest != successor["generation"]
            or successor["mode"] != "real" or successor["action"] != "create_draft_pr"
            or successor["status"] != "verified_ready" or successor_job["state"] != "ready_for_human_review"
            or successor_job["requested_action"] != "create_draft_pr"
            or successor_job["payload"].get("repo") != job["payload"].get("repo")):
        raise store.BridgeConflict("requires the exact latest verified successor of this superseded attempt")
    fence = json.loads(successor["artifact_fence_json"] or "{}")
    expected = {"kind": "github_branch_pr_absence_v1", "prior_attempt_id": attempt_id,
                "prior_job_id": job["job_id"], "repository": job["payload"].get("repo"),
                "head_branch": job["branch"], "lease_id": lease["lease_id"], "lease_epoch": lease["epoch"],
                "effect_authorized_at": lease["effect_authorized_at"], "lease_expires_at": lease["expires_at"],
                "branch_exists": False, "pull_request_count": 0}
    supersession = conn.execute("SELECT * FROM factory_attempt_events WHERE attempt_id=? AND "
                               "event_type='absence_superseded_unknown_attempt'", (attempt_id,)).fetchall()
    if (not isinstance(fence, dict) or any(fence.get(key) != value for key, value in expected.items())
            or len(supersession) != 1):
        raise store.BridgeConflict("successor lacks matching durable absence supersession evidence")
    details = json.loads(supersession[0]["details_json"])
    if (details.get("by_request_id") != successor["request_id"]
            or details.get("evidence_digest") != fence.get("evidence_digest")
            or details.get("observed_at") != fence.get("observed_at")):
        raise store.BridgeConflict("supersession event does not identify the successor absence decision")
    control = store.get_factory_issue_control(conn, attempt["work_item_key"])
    if any(item.get("attempt_id") != attempt_id for item in control["blockers"]):
        raise store.BridgeConflict("another execution is unresolved on this issue")
    return attempt, job, lease, successor, successor_job, supersession[0], control


def preview(conn, attempt_id, *, successor_attempt_id, attestation, absence_evidence, reason, actor):
    principal = recovery._principal(actor)
    evidence = recovery._attestation(attestation)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320 or store.sanitize_detail(reason) != reason:
        raise store.BridgeValidationError("a bounded sanitized retirement reason is required")
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        attempt, job, lease, successor, successor_job, supersession, control = _target(conn, attempt_id, successor_attempt_id)
        if any(evidence[key] != value for key, value in {
            "attempt_id": attempt_id, "job_id": job["job_id"], "lease_id": lease["lease_id"], "lease_epoch": lease["epoch"],
        }.items()):
            raise store.BridgeConflict("attestation does not identify the exact orphan execution")
        now = datetime.now(timezone.utc)
        age = recovery._age(evidence["observed_at"], now=now)
        if not 0 <= age <= 300 or age > recovery._age(supersession["created_at"], now=now):
            raise store.BridgeConflict("containment review is stale or predates supersession")
        absence = store._validate_absence_evidence(absence_evidence, repository=job["payload"]["repo"], branch=job["branch"])
        if not 0 <= recovery._age(absence["observed_at"], now=now) <= min(120, age):
            raise store.BridgeConflict("requires fresh absence inspection after containment review")
        # Hash complete rows so changed identity/state cannot reuse a preview;
        # only hashes leave this process, never payloads or lease fence tokens.
        snapshot = {}
        for table, key, identity in (("factory_attempts", "attempt_id", attempt_id),
                                     ("bridge_jobs", "job_id", job["job_id"]),
                                     ("factory_execution_leases", "attempt_id", attempt_id),
                                     ("factory_attempts", "attempt_id", successor_attempt_id),
                                     ("bridge_jobs", "job_id", successor_job["job_id"])):
            row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone()
            snapshot[f"{table}:{identity}"] = store.payload_hash(dict(row))
        for table in ("factory_attempt_events", "bridge_events", "factory_attempt_outbox"):
            key, identity = ("job_id", job["job_id"]) if table == "bridge_events" else ("attempt_id", attempt_id)
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY rowid", (identity,))]
            snapshot[table] = store.payload_hash(rows)
        context = {"operation": OPERATION, "principal": principal, "attempt_id": attempt_id,
                   "successor_attempt_id": successor_attempt_id, "reason": reason, "attestation": evidence,
                   "absence_evidence": absence, "snapshot": snapshot, "control_digest": control["snapshot_digest"]}
        return {"eligible": True, "snapshot_digest": store.payload_hash(context), "context": context,
                "containment_basis": "trusted_root_attestation_not_machine_verified",
                "effect_summary": "Fail only the orphan job with unknown outcome, release its lease and suppress its delivery; preserve superseded attempt, verified successor and ownership."}


def confirm(conn, attempt_id, *, successor_attempt_id, attestation, absence_evidence, reason, actor, request_id, snapshot_digest):
    principal = recovery._principal(actor)
    evidence = recovery._attestation(attestation)
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{8,128}", request_id)
            or not isinstance(snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest)):
        raise store.BridgeValidationError("retirement requires a request ID and preview digest")
    intent = {"operation": OPERATION, "attempt_id": attempt_id, "successor_attempt_id": successor_attempt_id,
              "attestation": evidence, "absence_evidence": absence_evidence, "reason": reason,
              "principal": principal, "snapshot_digest": snapshot_digest}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT * FROM factory_recovery_requests WHERE request_key=?", (request_id,)).fetchone()
        if prior:
            record = json.loads(prior["evidence_json"])
            if (prior["state"] != recovery.EVENT or prior["attempt_id"] != attempt_id or record.get("intent") != intent
                    or prior["evidence_digest"] != store.payload_hash(record)):
                raise store.BridgeConflict("retirement request ID conflicts with existing intent")
            return {**json.loads(prior["result_json"]), "created": False}
        approved = preview(conn, attempt_id, successor_attempt_id=successor_attempt_id, attestation=evidence,
                           absence_evidence=absence_evidence, reason=reason, actor=actor)
        if approved["snapshot_digest"] != snapshot_digest:
            raise store.BridgeConflict("retirement preview changed; inspect and preview again")
        record = {"intent": intent, "preview": approved["context"]}
        digest, now = store.payload_hash(record), store.utc_now()
        job_id = evidence["job_id"]
        conn.execute("UPDATE factory_execution_leases SET released_at=? WHERE attempt_id=?", (now, attempt_id))
        conn.execute("UPDATE bridge_jobs SET state='failed',reason_code='operator_interrupted_outcome_unknown',updated_at=? WHERE job_id=?", (now, job_id))
        conn.execute("UPDATE factory_attempt_outbox SET state='suppressed',acknowledged_at=COALESCE(acknowledged_at,?),delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE attempt_id=?", (now, now, attempt_id))
        audit = {"operation": OPERATION, "request_id": request_id, "actor": actor, "reason": reason,
                 "effect_outcome": "unknown", "successor_attempt_id": successor_attempt_id,
                 "containment_basis": approved["containment_basis"], "evidence_digest": digest,
                 "snapshot_digest": snapshot_digest, "inspection_digest": store.payload_hash(absence_evidence)}
        store._append_factory_attempt_event(conn, attempt_id, recovery.EVENT, audit)
        store._append_event(conn, job_id=job_id, event_type=recovery.EVENT, state="failed", details=audit)
        result = {"created": True, "attempt_id": attempt_id, "job_id": job_id, "successor_attempt_id": successor_attempt_id,
                  "attempt_status": "superseded", "effect_outcome": "unknown", "resolution": recovery.EVENT}
        conn.execute("INSERT INTO factory_recovery_requests (request_key,attempt_id,job_id,evidence_json,evidence_digest,result_json,state,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (request_id, attempt_id, job_id, store.canonical_json(record), digest, store.canonical_json(result), recovery.EVENT, actor, now, now))
        return result


def private_file(path, *, evidence=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise store.BridgeValidationError("requires an absolute path without symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise store.BridgeValidationError("requires a root-owned regular file without other write permission")
        parent = path.parent.stat()
        if not evidence:
            if parent.st_uid != 0 or parent.st_mode & 0o022:
                raise store.BridgeValidationError("database requires a root-owned directory without other write permission")
            return path
        if (stat.S_IMODE(info.st_mode) != 0o600 or parent.st_uid != 0
                or stat.S_IMODE(parent.st_mode) != 0o700 or info.st_size > 4096):
            raise store.BridgeValidationError("evidence requires root-owned 0600 file in 0700 directory, at most 4096 bytes")
        contents = source.read(4097)
        if len(contents) > 4096:
            raise store.BridgeValidationError("evidence exceeds 4096 bytes")
        return json.loads(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "confirm"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--successor-attempt-id", required=True)
    parser.add_argument("--attestation", required=True)
    parser.add_argument("--absence-evidence", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--snapshot-digest")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise store.BridgeValidationError("superseded retirement requires root")
        if args.action == "confirm" and (not args.yes or not args.request_id or not args.snapshot_digest):
            raise store.BridgeValidationError("confirmation requires --yes, --request-id and --snapshot-digest")
        db = private_file(args.database)
        evidence = private_file(args.attestation, evidence=True)
        absence = private_file(args.absence_evidence, evidence=True)
        with closing(sqlite3.connect(db.as_uri() + ("?mode=ro" if args.action == "preview" else "?mode=rw"), uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            store._require_runtime_schema(conn)
            values = {"successor_attempt_id": args.successor_attempt_id, "attestation": evidence,
                      "absence_evidence": absence, "reason": args.reason, "actor": f"uid:0;pid:{os.getpid()};account:root"}
            if args.action == "confirm":
                values.update(request_id=args.request_id, snapshot_digest=args.snapshot_digest)
            result = (preview if args.action == "preview" else confirm)(conn, args.attempt_id, **values)
            print(json.dumps(result, sort_keys=True))
        return 0
    except (store.BridgeError, OSError, ValueError, sqlite3.Error) as exc:
        # SQLite/filesystem errors may contain paths; no payload or credential output.
        print(f"Retirement refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
