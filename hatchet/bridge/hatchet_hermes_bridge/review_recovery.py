"""Root-approved delivery of the one legacy review stopped before claim.

The reserved review/pass and failed job remain durable. This never resets a
native journal: an existing different or ambiguous intent still fails closed.
"""
from __future__ import annotations

import json
import re
from contextlib import nullcontext

from . import independent_review as reviews, interrupted_resolution, review_repair, store, work_evidence

FIELDS = {"issue", "review_id", "source_attempt_id", "repository", "pr_url", "branch", "reviewed_head_sha", "reason"}


def _intent(value):
    # Reuse the exact artifact and bounded operator text validation.
    if set(value) != FIELDS:
        raise store.BridgeValidationError("review recovery requires exact review, source, artifact and reason fields")
    intent = review_repair._intent({k: v for k, v in value.items() if k != "review_id"} | {"feedback": "legacy unstarted review delivery"})
    intent.pop("feedback")
    if not isinstance(value["review_id"], str) or not re.fullmatch(reviews.UUID_PATTERN, value["review_id"]):
        raise store.BridgeValidationError("review recovery requires a review UUID")
    return {**intent, "review_id": value["review_id"]}


def _requirements(conn, attempt, job):
    seen = set()
    while True:
        if attempt["attempt_id"] in seen:
            raise store.BridgeConflict("review requirements ancestry is cyclic")
        seen.add(attempt["attempt_id"])
        manifest = conn.execute("SELECT document_json,document_digest FROM factory_execution_manifests WHERE attempt_id=? AND phase='admission'", (attempt["attempt_id"],)).fetchone()
        try:
            document = json.loads(manifest["document_json"]) if manifest else None
        except (TypeError, ValueError):
            raise store.BridgeConflict("review recovery admission manifest is malformed") from None
        if (not manifest or work_evidence.digest(document) != manifest["document_digest"]
                or document != work_evidence.admission_manifest(attempt, job)):
            raise store.BridgeConflict("review recovery requires intact admission requirements manifests")
        if not attempt.get("prior_attempt_id"):
            return {"title": job["payload"].get("title", ""),
                    "description": job["payload"].get("implementation_prompt") or job["payload"].get("instructions", "")}
        parent = store.get_factory_attempt(conn, attempt["prior_attempt_id"])
        if parent["generation"] >= attempt["generation"] or parent["work_item_key"] != attempt["work_item_key"] or parent["mode"] != "real":
            raise store.BridgeConflict("review requirements ancestry is invalid")
        attempt, job = parent, store.get_job(conn, parent["job_id"])


def _target(conn, issue, review_id, source_attempt_id):
    row = conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (review_id,)).fetchone()
    if (not row or row["source_attempt_id"] != source_attempt_id or row["state"] != "needs_operator"
            or row["reason"] != "review_inconclusive" or any(row[k] is not None for k in
                ("started_at", "result_json", "result_digest", "result_recorded_at", "repair_attempt_id"))
            or conn.execute("SELECT 1 FROM factory_independent_review_deliveries WHERE review_id=?", (review_id,)).fetchone()):
        raise store.BridgeConflict("review recovery requires an unrecovered legacy preflight stop without invocation or receipt")
    attempt, source, _ = review_repair._source(conn, issue, source_attempt_id)
    old = store.get_job(conn, row["job_id"])
    if (attempt["status"] != "verified_ready" or source["state"] != "ready_for_human_review"
            or source["job_id"] != row["source_job_id"] or source.get("hermes_session_id") is not None
            or old["state"] != "failed" or old["reason_code"] != "review_inconclusive"
            or old["requested_action"] != "read_only_review" or old.get("hermes_session_id") is not None
            or old["payload_hash"] != store.payload_hash(old["payload"])
            or conn.execute("SELECT 1 FROM bridge_events WHERE job_id=? AND (state NOT IN ('accepted','failed') OR event_type NOT IN ('accepted','transition:failed','submit_deduplicated'))", (old["job_id"],)).fetchone()):
        raise store.BridgeConflict("review recovery has started, ambiguous or inconsistent job evidence")
    rows = conn.execute("SELECT review_id,pass_number FROM factory_independent_reviews WHERE lineage_key=? ORDER BY pass_number", (row["lineage_key"],)).fetchall()
    if ([r["pass_number"] for r in rows] != list(range(1, len(rows) + 1)) or len(rows) > 2
            or rows[-1]["review_id"] != review_id):
        raise store.BridgeConflict("review recovery requires the latest reserved pass within its durable budget")
    requirements = _requirements(conn, attempt, source)
    expected = {"review_id": review_id, "repository": row["repository"], "pr_url": row["pr_url"],
        "branch": source["branch"], "head_sha": row["head_sha"], "generator_session_id": "unavailable-generator-session",
        "pass_number": row["pass_number"], "policy_digest": row["policy_digest"],
        "expected_base": source["payload"]["expected_base"], "requirements": requirements,
        "requirements_digest": reviews.requirements_digest(requirements)}
    if (old["payload"] != {"repo": row["repository"], "review": expected, "review_capability": row["capability"], "dry_run": False}
            or not requirements["description"] or source["commit_sha"] != row["head_sha"]
            or row["lineage_key"] != store.payload_hash({"repository": row["repository"], "pr_url": row["pr_url"]})):
        raise store.BridgeConflict("review recovery does not match the legacy missing-session preflight")
    return row, attempt, source, old, expected


def inspect(conn, *, issue, review_id, source_attempt_id, actor):
    interrupted_resolution._principal(actor)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row, _, _, _, _ = _target(conn, issue, review_id, source_attempt_id)
        created = store._queue_pr_inspection(conn, issue, row["pr_url"], source_attempt_id, force_refresh=True)
        store._append_factory_attempt_event(conn, source_attempt_id, "independent_review_recovery_inspection_requested", {"actor": actor, "review_id": review_id})
        return {"created": created, "review_id": review_id}


def _preview(conn, *, ingress_db, registry_path, policy_path, actor, intent):
    principal = interrupted_resolution._principal(actor)
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        row, attempt, source, old, request = _target(conn, intent["issue"], intent["review_id"], intent["source_attempt_id"])
        # Reuse the trusted ingress, route, verification policy and exact draft gates.
        artifact = review_repair._preview(conn, ingress_db=ingress_db, registry_path=registry_path, policy_path=policy_path,
            actor=actor, intent={k: v for k, v in intent.items() if k != "review_id"} | {"feedback": "legacy unstarted review delivery"})
        current = reviews.policy()
        if (not current["enabled"] or current["digest"] != row["policy_digest"] or row["repository"] not in current["repositories"]
                or source["payload"].get("independent_review_enrollment") != {"policy_digest": current["digest"]}):
            raise store.BridgeConflict("review recovery policy or enrollment changed")
        request = {k: v for k, v in request.items() if k != "generator_session_id"} | {
            "generator_execution_id": f"factory-execution:{attempt['attempt_id']}:{source['job_id']}"}
        reviews.validate_request({"repo": row["repository"], "review": request, "review_capability": row["capability"], "dry_run": False})
        evidence = {"review": dict(row), "job": old,
            "events": [dict(r) for r in conn.execute("SELECT * FROM bridge_events WHERE job_id=? ORDER BY event_id", (old["job_id"],))],
            "outbox": [dict(r) for r in conn.execute("SELECT * FROM factory_inspection_outbox WHERE job_id=? ORDER BY outbox_key", (old["job_id"],))]}
        context = {"intent": intent, "principal": principal, "artifact_digest": artifact["snapshot_digest"],
                   "review_policy": current, "old_evidence_digest": store.payload_hash(evidence), "request": request}
        return {"eligible": True, "action": "recover_unstarted_review", "review_id": row["review_id"],
                "pass_number": row["pass_number"], "passes_used": row["pass_number"], "max_passes": 2,
                "snapshot_digest": store.payload_hash(context), "context": context}, evidence


def preview(*, bridge_db, ingress_db, registry_path, policy_path, actor, **value):
    conn = store.connect_readonly(bridge_db)
    try:
        return _preview(conn, ingress_db=ingress_db, registry_path=registry_path, policy_path=policy_path, actor=actor, intent=_intent(value))[0]
    finally:
        conn.close()


def confirm(*, bridge_db, ingress_db, registry_path, policy_path, actor, request_id, snapshot_digest, **value):
    from . import operator
    intent = _intent(value)
    approval = {**intent, "principal": interrupted_resolution._principal(actor), "snapshot_digest": snapshot_digest}
    request_id = operator._bounded_text(request_id, name="request_id", limit=160)
    if not isinstance(snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest):
        raise store.BridgeValidationError("review recovery requires a snapshot digest")
    conn = store.connect_runtime(bridge_db)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM factory_independent_review_deliveries WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if json.loads(existing["approval_json"]) != approval:
                    raise store.BridgeConflict("review recovery request_id conflicts with existing intent")
                return {"created": False, "review_id": existing["review_id"], "job": store.get_job(conn, existing["job_id"])}
            current, evidence = _preview(conn, ingress_db=ingress_db, registry_path=registry_path, policy_path=policy_path, actor=actor, intent=intent)
            if current["snapshot_digest"] != snapshot_digest:
                raise store.BridgeConflict("review recovery snapshot changed; preview again")
            row = evidence["review"]
            job, _ = store.submit_job(conn, {"idempotency_key": f"factory:review-recovery:{row['review_id']}", "requested_action": "read_only_review",
                "payload": {"repo": row["repository"], "review": current["context"]["request"], "review_capability": row["capability"], "dry_run": False}}, internal_review=True)
            now = store.utc_now()
            conn.execute("INSERT INTO factory_independent_review_deliveries (review_id,job_id,request_id,approval_json,evidence_json,actor,created_at) VALUES (?,?,?,?,?,?,?)",
                (row["review_id"], job["job_id"], request_id, store.canonical_json(approval), store.canonical_json(evidence), actor, now))
            conn.execute("UPDATE factory_independent_reviews SET state='pending',reason=NULL,updated_at=? WHERE review_id=?", (now, row["review_id"]))
            key = f"factory:review-recovery:{row['review_id']}"
            conn.execute("INSERT INTO factory_inspection_outbox (outbox_key,request_key,job_id,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,'pending',?,?)",
                (f"{key}:accepted", key, job["job_id"], store.canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), now, now))
            store._append_factory_attempt_event(conn, row["source_attempt_id"], "independent_review_delivery_recovered",
                {"actor": actor, "reason": intent["reason"], "review_id": row["review_id"], "pass_number": row["pass_number"],
                 "old_job_id": row["job_id"], "job_id": job["job_id"], "request_id": request_id, "snapshot_digest": snapshot_digest,
                 "old_evidence_digest": current["context"]["old_evidence_digest"]})
            return {"created": True, "review_id": row["review_id"], "job": job}
    finally:
        conn.close()
