"""Policy-controlled review decisions; evidence never grants merge/deploy authority."""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import review_repair, store, work_evidence

_SOURCE_POLICY = Path(__file__).resolve().parents[2] / "workers" / "independent-review-policy.json"
POLICY_PATH = _SOURCE_POLICY if _SOURCE_POLICY.is_file() else Path(__file__).resolve().parents[1] / "independent-review-policy.json"
REASONS = {"ok", "review_backend_unavailable", "review_isolation_unavailable", "review_inconclusive",
           "review_malformed", "review_head_changed", "review_timeout", "review_budget_exhausted", "review_policy_changed", "review_operator_required"}
REQUEST_FIELDS = {"review_id", "repository", "pr_url", "branch", "head_sha", "pass_number", "policy_digest", "expected_base", "requirements", "requirements_digest"}
GENERATOR_FIELDS = {"generator_session_id", "generator_execution_id"}
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
EXECUTION_ID_RE = re.compile(f"factory-execution:{UUID_PATTERN}:{UUID_PATTERN}")
RECEIPT_FIELDS = {"schema", "review_id", "repository", "pr_url", "head_sha", "verdict", "reviewer_model",
                  "reviewer_session_id", "backend", "isolation", "findings", "reason"}


def requirements_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _generator_identity_valid(review: dict[str, Any]) -> bool:
    if not isinstance(review, dict):
        return False
    fields = set(review) & GENERATOR_FIELDS
    if fields == {"generator_execution_id"}:
        value = review["generator_execution_id"]
        return isinstance(value, str) and EXECUTION_ID_RE.fullmatch(value) is not None
    if fields == {"generator_session_id"}:
        value = review["generator_session_id"]
        return isinstance(value, str) and 1 <= len(value) <= 320
    return False


def policy() -> dict[str, Any]:
    try:
        if not POLICY_PATH.exists():
            value = {"schema": "factory-independent-review-policy-v1", "enabled": False,
                     "automatic_repairs_allowed": False, "max_passes": 2, "repositories": []}
            return {**value, "digest": store.payload_hash(value)}
        value = json.loads(POLICY_PATH.read_text())
        if (set(value) != {"schema", "enabled", "automatic_repairs_allowed", "max_passes", "repositories"}
                or value["schema"] != "factory-independent-review-policy-v1"
                or type(value["enabled"]) is not bool or type(value["automatic_repairs_allowed"]) is not bool
                or type(value["max_passes"]) is not int or value["max_passes"] != 2
                or not isinstance(value["repositories"], list) or len(set(value["repositories"])) != len(value["repositories"])
                or any(not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in value["repositories"])):
            raise ValueError("invalid policy")
        return {**value, "digest": store.payload_hash(value)}
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise store.BridgeValidationError("independent review policy is unavailable or malformed") from exc


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"repo", "review", "review_capability", "dry_run"}:
        raise store.BridgeValidationError("review request is malformed")
    review = payload["review"]
    if (payload["dry_run"] is not False or not isinstance(review, dict) or set(review) - GENERATOR_FIELDS != REQUEST_FIELDS
            or not _generator_identity_valid(review)
            or review["repository"] != payload["repo"] or not store._canonical_enabled_repo(payload["repo"])
            or type(review["pass_number"]) is not int or review["pass_number"] not in {1, 2}
            or not isinstance(payload["review_capability"], str) or len(payload["review_capability"]) < 20):
        raise store.BridgeValidationError("review request is malformed")
    for key in ("review_id", "repository", "pr_url", "branch"):
        if not isinstance(review[key], str) or not review[key] or len(review[key]) > 320:
            raise store.BridgeValidationError("review request is malformed")
    if not re.fullmatch(r"[0-9a-f]{40}", review["head_sha"]) or not re.fullmatch(r"[0-9a-f]{64}", review["policy_digest"]):
        raise store.BridgeValidationError("review request identity is malformed")
    store._parse_pr_reference(payload["repo"], review["pr_url"])
    requirements = review["requirements"]
    if (not isinstance(requirements, dict) or set(requirements) != {"title", "description"}
            or not isinstance(requirements["title"], str) or len(requirements["title"]) > 320
            or not isinstance(requirements["description"], str) or len(requirements["description"]) > 12000
            or any(store._secret_shaped(text) for text in requirements.values())
            or review["requirements_digest"] != requirements_digest(requirements)
            or not isinstance(review["expected_base"], str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", review["expected_base"])):
        raise store.BridgeValidationError("review requirements are missing or malformed")
    return review


def projection(conn, source_job_id: str) -> dict[str, Any]:
    """Safe read-only status; callers retain legacy verified_ready semantics."""
    row = conn.execute("SELECT * FROM factory_independent_reviews WHERE source_job_id=?", (source_job_id,)).fetchone()
    if not row:
        row = conn.execute("SELECT r.* FROM factory_independent_reviews r JOIN factory_attempts a ON a.attempt_id=r.repair_attempt_id WHERE a.job_id=? AND r.state='needs_operator'", (source_job_id,)).fetchone()
    if not row:
        return {"state": "checks_passed", "passes_used": 0, "max_passes": 2, "reason": None}
    used = conn.execute("SELECT COUNT(*) FROM factory_independent_reviews WHERE lineage_key=?", (row["lineage_key"],)).fetchone()[0]
    return {"state": {"pending": "review_pending", "running": "review_pending", "awaiting_inspection": "review_pending", "passed": "review_passed"}.get(row["state"], row["state"]),
            "passes_used": used, "max_passes": 2, "reviewed_head_sha": row["head_sha"],
            "reason": row["reason"], "repair_attempt_id": row["repair_attempt_id"]}


def _fresh_artifact(conn, attempt, job, *, require_fresh: bool = True) -> dict[str, Any]:
    from . import operator
    recorded = store.get_pr_inspection(conn, job["payload"]["repo"], job["pr_url"], prior_attempt_id=attempt["attempt_id"])
    if (not recorded or recorded["state"] != "completed" or not isinstance(recorded.get("result"), dict)
            or (require_fresh and (not recorded["fresh"] or not operator._inspection_fresh(recorded)))):
        raise store.BridgeConflict("review requires a fresh typed artifact inspection")
    result = recorded["result"]
    if (result.get("reason") != "ok" or result.get("state") != "open" or result.get("draft") is not True
            or result.get("merged") is not False or result.get("head_sha") != job["commit_sha"]
            or result.get("head_branch") != job["branch"] or result.get("base_ref") != job["payload"]["expected_base"]):
        raise store.BridgeConflict("review exact checked draft head changed")
    return recorded


def reserve(conn, source_job_id: str) -> dict[str, Any] | None:
    """Count a pass before dispatch, atomically with the existing accepted outbox."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM factory_independent_reviews WHERE source_job_id=?", (source_job_id,)).fetchone()
        if existing:
            return dict(existing)
        job = store.get_job(conn, source_job_id)
        current = policy()
        if (not current["enabled"] or job["payload"].get("repo") not in current["repositories"]
                or job["payload"].get("independent_review_enrollment") != {"policy_digest": current["digest"]}):
            return None
        attempt = store.get_factory_attempt_for_job(conn, source_job_id)
        if (not attempt or attempt["mode"] != "real" or attempt["status"] != "verified_ready"
                or job["state"] != "ready_for_human_review" or job["requested_action"] not in store.DRAFT_PR_EFFECT_ACTIONS):
            raise store.BridgeConflict("review requires a verified real generator execution")
        review_repair._source(conn, attempt["work_item_key"], attempt["attempt_id"])
        preflight_reason = None
        recorded = store.get_pr_inspection(conn, job["payload"]["repo"], job["pr_url"], prior_attempt_id=attempt["attempt_id"])
        if not recorded or not recorded["fresh"]:
            raise store.BridgeConflict("review requires a fresh typed artifact inspection")
        try:
            _fresh_artifact(conn, attempt, job)
        except store.BridgeConflict:
            preflight_reason = "review_head_changed"
        lineage = store.payload_hash({"repository": job["payload"]["repo"], "pr_url": job["pr_url"]})
        rows = conn.execute("SELECT * FROM factory_independent_reviews WHERE lineage_key=? ORDER BY pass_number", (lineage,)).fetchall()
        if rows and rows[-1]["repair_attempt_id"] == attempt["attempt_id"] and rows[-1]["head_sha"] == job["commit_sha"]:
            # An unchanged artifact is not another review opportunity or a new budget.
            if rows[-1]["state"] == "changes_requested":
                _stop(conn, rows[-1], "review_operator_required")
                store._append_factory_attempt_event_once(conn, attempt["attempt_id"], "independent_review_unchanged_head", rows[-1]["review_id"])
            return None
        if len(rows) >= 2:
            raise store.BridgeConflict("independent review budget exhausted; operator required")
        if rows and (rows[-1]["state"] != "changes_requested" or rows[-1]["repair_attempt_id"] != attempt["attempt_id"]):
            raise store.BridgeConflict("independent review continuation requires its one reserved repair")
        number, review_id, capability, now = len(rows) + 1, str(uuid.uuid4()), str(uuid.uuid4()), store.utc_now()
        requirements = {"title": job["payload"].get("title", ""),
                        "description": job["payload"].get("implementation_prompt") or job["payload"].get("instructions", "")}
        # Repair instructions are not the original task; follow immutable ancestry.
        ancestor, requirements_job, seen = attempt, job, {attempt["attempt_id"]}
        while ancestor.get("prior_attempt_id"):
            parent = store.get_factory_attempt(conn, ancestor["prior_attempt_id"])
            if parent["attempt_id"] in seen or parent["generation"] >= ancestor["generation"] or parent["work_item_key"] != attempt["work_item_key"]:
                raise store.BridgeConflict("review requirements ancestry is invalid")
            seen.add(parent["attempt_id"])
            ancestor = parent
            ancestor_job = store.get_job(conn, ancestor["job_id"])
            requirements_job = ancestor_job
            requirements = {"title": ancestor_job["payload"].get("title", ""),
                            "description": ancestor_job["payload"].get("implementation_prompt") or ancestor_job["payload"].get("instructions", "")}
        manifest = conn.execute("SELECT document_json,document_digest FROM factory_execution_manifests WHERE attempt_id=? AND phase='admission'", (ancestor["attempt_id"],)).fetchone()
        if manifest:
            try:
                document = json.loads(manifest["document_json"])
                expected = work_evidence.admission_manifest(ancestor, requirements_job)
                if (work_evidence.digest(document) != manifest["document_digest"]
                        or any(document.get(key) != expected[key] for key in ("requirements_source", "requirements_digest"))):
                    preflight_reason = "review_inconclusive"
            except (TypeError, ValueError, AttributeError):
                preflight_reason = "review_inconclusive"
        if (not isinstance(requirements["description"], str) or not 1 <= len(requirements["description"]) <= 12000
                or store._secret_shaped(requirements["description"])):
            requirements["description"] = ""
            preflight_reason = "review_inconclusive"
        if not isinstance(requirements["title"], str) or len(requirements["title"]) > 320 or store._secret_shaped(requirements["title"]):
            requirements["title"] = ""
            preflight_reason = "review_inconclusive"
        request = {"review_id": review_id, "repository": job["payload"]["repo"], "pr_url": job["pr_url"],
                   "branch": job["branch"], "head_sha": job["commit_sha"],
                   "generator_execution_id": f"factory-execution:{attempt['attempt_id']}:{job['job_id']}",
                   "pass_number": number, "policy_digest": current["digest"], "expected_base": job["payload"]["expected_base"],
                   "requirements": requirements, "requirements_digest": requirements_digest(requirements)}
        review_job, _ = store.submit_job(conn, {"idempotency_key": f"factory:independent-review:{lineage}:{number}",
            "requested_action": "read_only_review", "payload": {"repo": request["repository"], "review": request,
                "review_capability": capability, "dry_run": False}}, internal_review=True)
        conn.execute("INSERT INTO factory_independent_reviews (review_id,lineage_key,pass_number,source_attempt_id,source_job_id,job_id,repository,pr_url,head_sha,policy_digest,capability,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (review_id, lineage, number, attempt["attempt_id"], source_job_id, review_job["job_id"], request["repository"], request["pr_url"], request["head_sha"], current["digest"], capability, now, now))
        conn.execute("INSERT INTO factory_inspection_outbox (outbox_key,request_key,job_id,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"factory:review:{review_id}:accepted", f"factory:review:{review_id}", review_job["job_id"], store.canonical_json({"job_id": review_job["job_id"], "idempotency_key": review_job["idempotency_key"]}), "pending", now, now))
        store._append_factory_attempt_event(conn, attempt["attempt_id"], "independent_review_reserved", {"review_id": review_id, "pass_number": number, "head_sha": request["head_sha"]})
        if preflight_reason:
            _stop(conn, conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (review_id,)).fetchone(), preflight_reason)
        return dict(conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (review_id,)).fetchone())


def _row(conn, job_id: str, capability: str):
    row = conn.execute("SELECT * FROM factory_independent_reviews WHERE job_id=?", (job_id,)).fetchone()
    if not row or not isinstance(capability, str) or capability != row["capability"]:
        raise store.BridgeValidationError("review requires its reserved capability")
    return row


def authorization(conn, job_id: str, capability: str) -> dict[str, Any]:
    """Native gateway checks a real reservation; this endpoint never grants a pass."""
    row = _row(conn, job_id, capability)
    request = store.get_job(conn, job_id)["payload"]["review"]
    allowed = completion_allowed = row["state"] == "running"
    if allowed:
        try:
            current = policy()
            source = store.get_job(conn, row["source_job_id"])
            attempt, _, _ = review_repair._source(conn, store.get_factory_attempt(conn, row["source_attempt_id"])["work_item_key"], row["source_attempt_id"])
            completion_allowed = current["enabled"] and current["digest"] == row["policy_digest"] and row["repository"] in current["repositories"]
            # The bridge still obtains a fresh final inspection before any pass.
            _fresh_artifact(conn, attempt, source, require_fresh=False)
            allowed = completion_allowed
            if allowed:
                try: _fresh_artifact(conn, attempt, source)
                except store.BridgeError: allowed = False
        except store.BridgeError:
            allowed = completion_allowed = False
    return {"authorized": True, "invocation_allowed": bool(allowed), "completion_allowed": bool(completion_allowed),
            "job_id": job_id, "state": row["state"], "review": request}


def claim(conn, job_id: str, capability: str) -> bool:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, job_id, capability)
        if row["state"] != "pending":
            return False
        if not store.get_job(conn, job_id)["payload"]["review"]["requirements"]["description"]:
            _stop(conn, row, "review_inconclusive")
            return False
        current = policy()
        if not current["enabled"] or current["digest"] != row["policy_digest"]:
            _stop(conn, row, "review_policy_changed")
            return False
        try:
            source = store.get_job(conn, row["source_job_id"])
            attempt, _, _ = review_repair._source(conn, store.get_factory_attempt(conn, row["source_attempt_id"])["work_item_key"], row["source_attempt_id"])
            _fresh_artifact(conn, attempt, source)
        except store.BridgeError:
            _stop(conn, row, "review_head_changed")
            return False
        now = store.utc_now()
        conn.execute("UPDATE factory_independent_reviews SET state='running',started_at=?,updated_at=? WHERE review_id=?", (now, now, row["review_id"]))
        store.transition_job(conn, job_id, state="running")
        return True


def repair_authorized(conn, job: dict[str, Any], attempt_id: str) -> bool:
    """Repeat policy/ledger authority at the irrevocable effect grant."""
    review = job["payload"].get("factory_review_repair")
    if not isinstance(review, dict) or not review.get("policy_authority"):
        return True
    authority = review["policy_authority"]
    current = policy()
    row = conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (authority.get("review_id"),)).fetchone()
    return bool(row and current["enabled"] and current["automatic_repairs_allowed"]
        and authority == {"kind": "independent_review_policy_v1", "review_id": row["review_id"], "policy_digest": current["digest"]}
        and row["policy_digest"] == current["digest"] and row["pass_number"] == 1
        and row["state"] == "changes_requested" and row["repair_attempt_id"] == attempt_id
        and row["source_attempt_id"] == review.get("source_attempt_id")
        and row["head_sha"] == job["payload"].get("expected_head_sha")
        and row["repository"] in current["repositories"])


def _receipt(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    if (not _generator_identity_valid(request) or not isinstance(value, dict)
            or set(value) != RECEIPT_FIELDS or len(store.canonical_json(value)) > 20000):
        raise store.BridgeValidationError("review receipt is malformed")
    if (value["schema"] != "factory-independent-review-v1" or any(value[k] != request[k] for k in ("review_id", "repository", "pr_url", "head_sha"))
            or value["verdict"] not in {"passed", "changes_requested", "inconclusive"} or value["reason"] not in REASONS
            or value["reviewer_model"] != "gpt-6-astra" or value["backend"] not in {"isolated_codex", "isolated_hermes"}
            or not isinstance(value["reviewer_session_id"], str) or not 1 <= len(value["reviewer_session_id"]) <= 200
            or value["reviewer_session_id"] == request.get("generator_session_id")):
        raise store.BridgeValidationError("review receipt identity is malformed")
    isolation = value["isolation"]
    if (not isinstance(isolation, dict) or set(isolation) != {"separate_session", "isolated_checkout", "isolated_browser", "write_credentials"}
            or any(type(v) is not bool for v in isolation.values())):
        raise store.BridgeValidationError("review isolation is malformed")
    if value["verdict"] != "inconclusive" and isolation != {"separate_session": True, "isolated_checkout": True, "isolated_browser": True, "write_credentials": False}:
        raise store.BridgeValidationError("review isolation is unavailable")
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 20:
        raise store.BridgeValidationError("review findings are malformed")
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"priority", "file", "line", "title", "body"}
                or type(finding["priority"]) is not int or finding["priority"] not in range(4)
                or type(finding["line"]) is not int or not 1 <= finding["line"] <= 1000000
                or any(not isinstance(finding[k], str) or not 1 <= len(finding[k]) <= limit or store._secret_shaped(finding[k]) for k, limit in (("file", 300), ("title", 160), ("body", 1200)))
                or finding["file"].startswith("/") or ".." in finding["file"].split("/")):
            raise store.BridgeValidationError("review findings are malformed")
    if (value["verdict"] == "passed" and findings) or (value["verdict"] == "changes_requested" and not findings):
        raise store.BridgeValidationError("review verdict contradicts findings")
    if value["verdict"] != "inconclusive" and value["reason"] != "ok":
        raise store.BridgeValidationError("review verdict is inconclusive")
    return value


def _stop(conn, row, reason: str) -> None:
    conn.execute("UPDATE factory_independent_reviews SET state='needs_operator',reason=?,updated_at=? WHERE review_id=?", (reason, store.utc_now(), row["review_id"]))
    job = store.get_job(conn, row["job_id"])
    if job["state"] in {"accepted", "running"}:
        store.transition_job(conn, job["job_id"], state="failed", details={"reason_code": reason})


def complete(conn, job_id: str, capability: str, result: Any) -> dict[str, Any]:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, job_id, capability)
        if row["state"] != "running":
            if row["result_json"] and store.payload_hash(result) == row["result_digest"]:
                return projection(conn, row["source_job_id"])
            raise store.BridgeConflict("review result is already settled or was not claimed")
        request = store.get_job(conn, job_id)["payload"]["review"]
        try:
            receipt = _receipt(result, request)
        except (store.BridgeValidationError, TypeError, ValueError):
            _stop(conn, row, "review_malformed")
            return projection(conn, row["source_job_id"])
        now = store.utc_now()
        conn.execute("UPDATE factory_independent_reviews SET state='awaiting_inspection',result_json=?,result_digest=?,result_recorded_at=?,updated_at=? WHERE review_id=?",
            (store.canonical_json(receipt), store.payload_hash(receipt), now, now, row["review_id"]))
        store._queue_pr_inspection(conn, store.get_factory_attempt(conn, row["source_attempt_id"])["work_item_key"], row["pr_url"], row["source_attempt_id"], force_refresh=True)
        return projection(conn, row["source_job_id"])


def _settle_receipt(conn, review_id: str) -> bool:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM factory_independent_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not row or row["state"] != "awaiting_inspection":
            return False
        receipt = json.loads(row["result_json"])
        current = policy()
        state, reason = receipt["verdict"], receipt["reason"]
        if state == "inconclusive":
            state, reason = "needs_operator", reason if reason != "ok" else "review_inconclusive"
        if not current["enabled"] or current["digest"] != row["policy_digest"]:
            state, reason = "needs_operator", "review_policy_changed"
        try:
            source = store.get_job(conn, row["source_job_id"])
            attempt, _, _ = review_repair._source(conn, store.get_factory_attempt(conn, row["source_attempt_id"])["work_item_key"], row["source_attempt_id"])
            recorded = store.get_pr_inspection(conn, row["repository"], row["pr_url"], prior_attempt_id=row["source_attempt_id"])
            if state != "needs_operator" and (not recorded or not recorded["observed_at"] or recorded["observed_at"] < row["result_recorded_at"]):
                store._queue_pr_inspection(conn, attempt["work_item_key"], row["pr_url"], row["source_attempt_id"])
                return False
            if state != "needs_operator":
                _fresh_artifact(conn, attempt, source)
        except store.BridgeError:
            state, reason = "needs_operator", "review_head_changed"
        if state == "changes_requested" and (row["pass_number"] == 2 or not current["automatic_repairs_allowed"]):
            reason = "review_budget_exhausted" if row["pass_number"] == 2 else "review_operator_required"
        conn.execute("UPDATE factory_independent_reviews SET state=?,reason=?,updated_at=? WHERE review_id=?",
            (state, reason, store.utc_now(), row["review_id"]))
        if state == "needs_operator":
            store.transition_job(conn, row["job_id"], state="failed", details={"reason_code": reason})
        else:
            conn.execute("UPDATE bridge_jobs SET state='completed',reason_code=?,updated_at=? WHERE job_id=? AND state='running'", (reason, store.utc_now(), row["job_id"]))
            store._append_event(conn, job_id=row["job_id"], event_type="independent_review_completed", state="completed", details={"reason_code": reason})
        store._append_factory_attempt_event(conn, row["source_attempt_id"], "independent_review_completed", {"review_id": row["review_id"], "state": state, "reason": reason, "pass_number": row["pass_number"]})
        return True


def sweep(conn, limit: int = 50) -> dict[str, int]:
    """Existing dispatcher owns progress; retries never allocate a fresh budget."""
    current = policy()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with conn:
        for row in conn.execute("SELECT * FROM factory_independent_reviews WHERE state IN ('pending','running','awaiting_inspection') AND created_at<? LIMIT ?", (cutoff, limit)).fetchall():
            _stop(conn, row, "review_timeout")
    for row in conn.execute("SELECT review_id FROM factory_independent_reviews WHERE state='awaiting_inspection' LIMIT ?", (limit,)).fetchall():
        _settle_receipt(conn, row["review_id"])
    if not current["enabled"]:
        return {"reserved": 0, "repairs": 0}
    repairs = 0
    for row in conn.execute("SELECT * FROM factory_independent_reviews WHERE state='changes_requested' AND pass_number=1 AND reason='ok' AND repair_attempt_id IS NULL LIMIT ?", (limit,)).fetchall():
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                repairs += bool(review_repair.reserve_policy_repair(conn, dict(row), current))
        except store.BridgeError:
            with conn:
                _stop(conn, row, "review_head_changed")
    reserved = 0
    repositories = current["repositories"]
    rows = conn.execute(f"""SELECT a.attempt_id,a.job_id,a.work_item_key FROM factory_attempts a
        JOIN bridge_jobs j ON j.job_id=a.job_id
        WHERE a.mode='real' AND a.status='verified_ready'
        AND json_extract(j.payload_json,'$.repo') IN ({','.join('?' for _ in repositories)})
        AND json_extract(j.payload_json,'$.independent_review_enrollment.policy_digest')=?
        AND NOT EXISTS (SELECT 1 FROM factory_independent_reviews r WHERE r.source_job_id=a.job_id
            OR (r.repair_attempt_id=a.attempt_id AND r.state='needs_operator'))
        ORDER BY a.updated_at DESC LIMIT ?""", (*repositories, current["digest"], limit)).fetchall()
    for row in rows:
        job = store.get_job(conn, row["job_id"])
        if job["payload"].get("repo") not in current["repositories"]:
            continue
        if job["payload"].get("independent_review_enrollment") != {"policy_digest": current["digest"]}:
            continue
        with conn:
            store._queue_pr_inspection(conn, row["work_item_key"], job["pr_url"], row["attempt_id"])
        try:
            reserved += bool(reserve(conn, row["job_id"]))
        except store.BridgeError:
            # Preconditions may still be awaiting the durable typed inspection.
            continue
    return {"reserved": reserved, "repairs": repairs}
