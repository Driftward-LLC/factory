"""Root-attested interrupted execution resolution, not an executor receipt.

The operator cannot inspect executor processes or revoke provider credentials.
Containment is an explicit trusted-root attestation, never inferred from a TTL,
process-list snapshot, or unchanged PR. See docs/interrupted-execution.md.
"""
from __future__ import annotations

import json
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from . import store

SCHEMA = "factory-interrupted-resolution-v1"
EVENT = "execution_interrupted_resolved"
CHECKS = {"executor_and_descendants_stopped", "restart_and_replay_blocked",
          "external_writes_settled", "workspace_preserved"}


def _principal(actor: str) -> str:
    # Only the Unix operator obtains this identity, from SO_PEERCRED. PID is
    # audit context, not identity: a retry from another root CLI process is safe.
    if not isinstance(actor, str) or not re.fullmatch(r"uid:0;pid:[0-9]+;account:[A-Za-z0-9_-]+", actor):
        raise store.BridgeValidationError("interrupted resolution requires a root Unix peer")
    return actor.split(";pid:")[0]


def _age(value: Any, *, now: datetime | None = None) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return ((now or datetime.now(timezone.utc)) - parsed).total_seconds()
    except (AttributeError, TypeError, ValueError):
        raise store.BridgeValidationError("resolution evidence requires an aware timestamp") from None


def _attestation(value: Any) -> dict[str, Any]:
    fields = {"schema", "attempt_id", "job_id", "lease_id", "lease_epoch", "observed_at",
              "runtime_ref", "containment_method", "evidence_sha256", "workspace_snapshot_sha256", "checks"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != SCHEMA:
        raise store.BridgeValidationError("invalid interrupted resolution attestation")
    for key in ("attempt_id", "job_id", "lease_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value[key]):
            raise store.BridgeValidationError("invalid resolution identity")
    if type(value["lease_epoch"]) is not int or value["lease_epoch"] < 1:
        raise store.BridgeValidationError("invalid resolution epoch")
    if value["containment_method"] not in ("isolated_runtime_retired", "write_authority_revoked"):
        raise store.BridgeValidationError("resolution requires durable external containment")
    if not isinstance(value["runtime_ref"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,159}", value["runtime_ref"]):
        raise store.BridgeValidationError("invalid resolution runtime reference")
    for key in ("evidence_sha256", "workspace_snapshot_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise store.BridgeValidationError("resolution requires preserved evidence digests")
    if not isinstance(value["checks"], dict) or set(value["checks"]) != CHECKS or any(value["checks"][key] is not True for key in CHECKS):
        raise store.BridgeValidationError("all containment checks must be explicitly attested")
    if store.sanitize_detail(value) != value:
        raise store.BridgeValidationError("resolution evidence contains disallowed material")
    return value


def _target(conn, attempt_id: str):
    attempt = store.get_factory_attempt(conn, attempt_id)
    control = store.get_factory_issue_control(conn, attempt["work_item_key"])
    job = store.get_job(conn, attempt["job_id"])
    lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt_id,)).fetchone()
    if (not control["held"] or attempt["mode"] != "real" or attempt["status"] != "cancellation_requested"
            or job["state"] != "running" or job["requested_action"] not in store.DRAFT_PR_EFFECT_ACTIONS
            or not lease or not lease["effect_authorized_at"] or not lease["cancel_requested_at"] or lease["released_at"]
            or _age(lease["expires_at"]) <= 0):
        raise store.BridgeConflict("resolution requires a held, canceled, unresolved authorized execution")
    if job["pr_url"] or job["commit_sha"] or lease["effect_observed_at"] or conn.execute(
        "SELECT 1 FROM factory_attempt_events WHERE attempt_id=? AND (event_type='execution_effect_commit_failed' "
        "OR (event_type='execution_receipt_observed' AND json_extract(details_json,'$.state')='completed'))", (attempt_id,),
    ).fetchone():
        raise store.BridgeConflict("observed execution receipts must use receipt recovery")
    if conn.execute("SELECT 1 FROM factory_recovery_requests WHERE attempt_id=?", (attempt_id,)).fetchone():
        raise store.BridgeConflict("attempt already has a recovery decision")
    if not attempt["prior_pr_url"] and job["requested_action"] != "create_draft_pr":
        raise store.BridgeConflict("repair resolution requires a known prior PR")
    return attempt, job, lease, control


def inspect(conn, attempt_id: str, *, actor: str) -> dict[str, Any]:
    _principal(actor)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt, job, _, _ = _target(conn, attempt_id)
        if not attempt["prior_pr_url"]:
            return {"created": False, "absence_evidence_target": {
                "repository": job["payload"]["repo"], "head_branch": job["branch"]}}
        created = store._queue_pr_inspection(conn, attempt["work_item_key"], attempt["prior_pr_url"], attempt_id, force_refresh=True)
        observed = store.get_pr_inspection(conn, job["payload"]["repo"], attempt["prior_pr_url"], prior_attempt_id=attempt_id)
        if created:
            store._append_factory_attempt_event(conn, attempt_id, "interrupted_resolution_inspection_requested", {"actor": actor})
        return {"created": created, "inspection_request_key": observed["request_key"], "state": observed["state"]}


def preview(conn, attempt_id: str, *, attestation: dict[str, Any], reason: str, actor: str,
            absence_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    principal = _principal(actor)
    evidence = _attestation(attestation)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320 or store.sanitize_detail(reason) != reason:
        raise store.BridgeValidationError("a bounded, sanitized resolution reason is required")
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        attempt, job, lease, control = _target(conn, attempt_id)
        if any(evidence[key] != expected for key, expected in {
            "attempt_id": attempt_id, "job_id": job["job_id"], "lease_id": lease["lease_id"], "lease_epoch": lease["epoch"],
        }.items()):
            raise store.BridgeConflict("attestation does not bind the current execution")
        now = datetime.now(timezone.utc)
        age = _age(evidence["observed_at"], now=now)
        if not 0 <= age <= 300 or age > _age(lease["cancel_requested_at"], now=now):
            raise store.BridgeConflict("containment attestation is stale or predates cancellation")
        if attempt["prior_pr_url"]:
            if absence_evidence is not None:
                raise store.BridgeValidationError("a known prior PR requires artifact inspection, not absence evidence")
            observed = store.get_pr_inspection(conn, job["payload"]["repo"], attempt["prior_pr_url"], prior_attempt_id=attempt_id)
            if not observed or not observed["fresh"] or observed["state"] != "completed" or _age(observed["observed_at"], now=now) > age:
                raise store.BridgeConflict("a fresh artifact inspection after containment attestation is required")
            result = observed["result"]
            if (result.get("reason") != "ok" or result.get("state") not in {"open", "closed"}
                    or result.get("head_branch") != job["branch"] or not store.RELEASE_SHA_RE.fullmatch(result.get("head_sha") or "")):
                raise store.BridgeConflict("artifact inspection does not identify this execution's branch and head")
            inspection = {key: observed[key] for key in ("request_key", "result_digest", "observed_at", "result")}
        else:
            absence = store._validate_absence_evidence(absence_evidence, repository=job["payload"]["repo"], branch=job["branch"])
            if not 0 <= _age(absence["observed_at"], now=now) <= min(120, age):
                raise store.BridgeConflict("fresh branch and PR absence evidence after containment attestation is required")
            inspection = {"source": "trusted_root_absence_attestation", "result_digest": store.payload_hash(absence),
                          "observed_at": absence["observed_at"], "result": absence}
        context = {"schema": SCHEMA, "principal": principal, "reason": reason, "attestation": evidence,
                   "control_digest": control["snapshot_digest"], "revision": control["revision"],
                   "inspection": inspection}
        return {"eligible": True, "snapshot_digest": store.payload_hash(context), "context": context,
                "effect_summary": "Retire this execution as failed with unknown effect outcome; retain manual hold. No success, retry, PR mutation or Linear update.",
                "containment_basis": "trusted_root_attestation_not_machine_verified"}


def resolve(conn, attempt_id: str, *, attestation: dict[str, Any], reason: str, actor: str,
            request_id: str, snapshot_digest: str, absence_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    principal = _principal(actor)
    evidence = _attestation(attestation)
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{8,128}", request_id)
            or not isinstance(snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest)):
        raise store.BridgeValidationError("resolution requires a request ID and preview digest")
    intent = {"attempt_id": attempt_id, "attestation": evidence, "reason": reason,
              "principal": principal, "snapshot_digest": snapshot_digest}
    if absence_evidence is not None:
        intent["absence_evidence"] = absence_evidence
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT * FROM factory_recovery_requests WHERE request_key=?", (request_id,)).fetchone()
        if prior:
            stored = json.loads(prior["evidence_json"])
            if (prior["state"] != EVENT or prior["attempt_id"] != attempt_id or stored.get("intent") != intent
                    or prior["evidence_digest"] != store.payload_hash(stored)):
                raise store.BridgeConflict("resolution request ID conflicts with existing intent")
            return {**json.loads(prior["result_json"]), "created": False}
        approved = preview(conn, attempt_id, attestation=evidence, reason=reason, actor=actor, absence_evidence=absence_evidence)
        if approved["snapshot_digest"] != snapshot_digest:
            raise store.BridgeConflict("resolution preview changed; inspect and preview again")
        record = {"intent": intent, "inspection": approved["context"]["inspection"]}
        digest = store.payload_hash(record)
        attempt = store.get_factory_attempt(conn, attempt_id)
        job_id, issue, now = attempt["job_id"], attempt["work_item_key"], store.utc_now()
        # Dedicated operator transition, deliberately not an executor success
        # receipt or ordinary transition bypass. All guards ran under this lock.
        conn.execute("UPDATE factory_execution_leases SET released_at=? WHERE attempt_id=?", (now, attempt_id))
        conn.execute("UPDATE factory_attempts SET status='failed',updated_at=? WHERE attempt_id=?", (now, attempt_id))
        conn.execute("UPDATE bridge_jobs SET state='failed',reason_code='operator_interrupted_outcome_unknown',updated_at=? WHERE job_id=?", (now, job_id))
        conn.execute("UPDATE factory_attempt_outbox SET state='suppressed',acknowledged_at=COALESCE(acknowledged_at,?),delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE attempt_id=?", (now, now, attempt_id))
        conn.execute("UPDATE factory_issue_controls SET revision=revision+1,actor=?,reason=?,updated_at=? WHERE work_item_key=? AND held=1", (actor, reason, now, issue))
        audit = {"request_id": request_id, "actor": actor, "reason": reason, "effect_outcome": "unknown",
                 "containment_basis": approved["containment_basis"], "evidence_digest": digest, "snapshot_digest": snapshot_digest,
                 "inspection_digest": approved["context"]["inspection"]["result_digest"]}
        store._append_factory_attempt_event(conn, attempt_id, EVENT, audit)
        store._append_event(conn, job_id=job_id, event_type=EVENT, state="failed", details=audit)
        result = {"created": True, "attempt_id": attempt_id, "job_id": job_id, "effect_outcome": "unknown",
                  "resolution": EVENT, "control": store.get_factory_issue_control(conn, issue)}
        conn.execute("INSERT INTO factory_recovery_requests (request_key,attempt_id,job_id,evidence_json,evidence_digest,result_json,state,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (request_id, attempt_id, job_id, store.canonical_json(record), digest,
                      store.canonical_json(result), EVENT, actor, now, now))
        return result
