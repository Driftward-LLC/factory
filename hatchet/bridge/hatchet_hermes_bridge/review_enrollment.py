"""Explicit enrollment of an existing verified draft; original admission stays immutable."""
from __future__ import annotations

import json
import re
from contextlib import nullcontext

from . import independent_review as reviews, interrupted_resolution, review_recovery, review_repair, store

FIELDS = review_repair.FIELDS - {"feedback"}


def _intent(value):
    if set(value) != FIELDS:
        raise store.BridgeValidationError("review enrollment requires exact source, artifact and reason fields")
    result = review_repair._intent({**value, "feedback": "enroll existing verified draft for independent review"})
    result.pop("feedback")
    return result


def _target(conn, issue, source_attempt_id):
    attempt, job, _ = review_repair._source(conn, issue, source_attempt_id)
    if attempt["status"] != "verified_ready" or job["state"] != "ready_for_human_review":
        raise store.BridgeConflict("review enrollment requires a verified real source")
    lineage = store.payload_hash({"repository": job["payload"]["repo"], "pr_url": job["pr_url"]})
    rows = conn.execute("SELECT review_id,pass_number,state FROM factory_independent_reviews WHERE lineage_key=? ORDER BY pass_number", (lineage,)).fetchall()
    # Recovery cannot provide a fresh budget or override review/repair history.
    if rows or reviews.enrollment(conn, job):
        raise store.BridgeConflict("review enrollment already exists or its lineage has consumed review budget")
    review_recovery._requirements(conn, attempt, job)
    return attempt, job, lineage


def inspect(conn, *, issue, source_attempt_id, actor):
    interrupted_resolution._principal(actor)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt, job, _ = _target(conn, issue, source_attempt_id)
        created = store._queue_pr_inspection(conn, issue, job["pr_url"], attempt["attempt_id"], force_refresh=True)
        store._append_factory_attempt_event(conn, source_attempt_id, "independent_review_enrollment_inspection_requested", {"actor": actor})
        return {"created": created, "source_attempt_id": source_attempt_id}


def _preview(conn, *, ingress_db, registry_path, policy_path, actor, intent):
    principal = interrupted_resolution._principal(actor)
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        attempt, job, lineage = _target(conn, intent["issue"], intent["source_attempt_id"])
        artifact = review_repair._preview(conn, ingress_db=ingress_db, registry_path=registry_path,
            policy_path=policy_path, actor=actor, intent={**intent, "feedback": "enroll existing verified draft for independent review"})
        current = reviews.policy()
        if not current["enabled"] or job["payload"]["repo"] not in current["repositories"]:
            raise store.BridgeConflict("review enrollment is excluded by current policy")
        reviews._fresh_artifact(conn, attempt, job)
        context = {"intent": intent, "principal": principal, "source_job_id": job["job_id"],
                   "source_payload_hash": job["payload_hash"], "artifact_digest": artifact["snapshot_digest"],
                   "lineage_key": lineage, "review_policy": current, "passes_used": 0, "max_passes": 2}
        return {"eligible": True, "action": "enroll_existing_review", "snapshot_digest": store.payload_hash(context), "context": context}


def preview(*, bridge_db, ingress_db, registry_path, policy_path, actor, **value):
    conn = store.connect_readonly(bridge_db)
    try:
        return _preview(conn, ingress_db=ingress_db, registry_path=registry_path, policy_path=policy_path, actor=actor, intent=_intent(value))
    finally:
        conn.close()


def confirm(*, bridge_db, ingress_db, registry_path, policy_path, actor, request_id, snapshot_digest, **value):
    from . import operator
    intent = _intent(value)
    approval = {**intent, "principal": interrupted_resolution._principal(actor), "snapshot_digest": snapshot_digest}
    request_id = operator._bounded_text(request_id, name="request_id", limit=160)
    if not isinstance(snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest):
        raise store.BridgeValidationError("review enrollment requires a snapshot digest")
    conn = store.connect_runtime(bridge_db)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM factory_review_enrollments WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if json.loads(existing["approval_json"]) != approval:
                    raise store.BridgeConflict("review enrollment request_id conflicts with existing intent")
                row = conn.execute("SELECT review_id,job_id FROM factory_independent_reviews WHERE source_job_id=?", (existing["source_job_id"],)).fetchone()
                return {"created": False, "review_id": row["review_id"], "job": store.get_job(conn, row["job_id"])}
            current = _preview(conn, ingress_db=ingress_db, registry_path=registry_path, policy_path=policy_path, actor=actor, intent=intent)
            if current["snapshot_digest"] != snapshot_digest:
                raise store.BridgeConflict("review enrollment snapshot changed; preview again")
            context = current["context"]
            conn.execute("INSERT INTO factory_review_enrollments (source_job_id,source_attempt_id,request_id,policy_digest,approval_json,evidence_json,actor,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (context["source_job_id"], intent["source_attempt_id"], request_id, context["review_policy"]["digest"],
                 store.canonical_json(approval), store.canonical_json(context), actor, store.utc_now()))
            row = reviews.reserve(conn, context["source_job_id"])
            if not row or row["state"] != "pending":
                raise store.BridgeConflict("review enrollment policy or preflight changed; preview again")
            store._append_factory_attempt_event(conn, intent["source_attempt_id"], "independent_review_enrolled",
                {"actor": actor, "reason": intent["reason"], "request_id": request_id, "snapshot_digest": snapshot_digest, "review_id": row["review_id"]})
            return {"created": True, "review_id": row["review_id"], "job": store.get_job(conn, row["job_id"])}
    finally:
        conn.close()
