"""Allowlisted, read-only verification evidence for one console attempt.

These observations never confer review, merge, or execution authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from . import independent_review, work_evidence
from .store import BridgeError, get_factory_attempt, get_factory_work_evidence, get_job, get_pr_inspection, sanitize_detail


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc) else None
    except ValueError:
        return None


def _reference(value: Any, pattern: str, maximum: int = 200) -> str | None:
    ref = work_evidence.reference(value, pattern, maximum)
    return ref if sanitize_detail(ref) == ref else None


def _checkpoint(rows: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    row = next((row for row in rows if row["checkpoint_type"] == kind), None)
    result: dict[str, Any] = {"result": "unavailable", "recorded_at": None}
    if not row:
        return result
    document = row["document"]
    schema = "factory-" + kind.replace("_", "-") + "-v1"
    if (row["contract_version"] != 1 or not isinstance(document, dict)
            or document.get("schema") != schema or work_evidence.digest(document) != row["document_digest"]
            or not _time(row["created_at"])):
        return {**result, "result": "invalid"}
    allowed = ("passed", "failed") if kind == "verification_receipt" else ("recorded", "unavailable")
    result.update(result=document.get("result") if document.get("result") in allowed else "invalid", recorded_at=row["created_at"])
    if kind in {"implementation_receipt", "verification_receipt"}:
        result["commit_sha"] = _reference(document.get("commit_sha"), r"[0-9a-f]{40}")
    if kind == "verification_receipt":
        result.update(
            policy_digest=work_evidence.sha256(document.get("policy_digest")),
            receipt_digest=work_evidence.sha256(document.get("receipt_digest")),
            checks_passed=document.get("checks_passed") if type(document.get("checks_passed")) is int and 1 <= document["checks_passed"] <= 512 else None,
            local_commands_passed=document.get("local_commands_passed") if type(document.get("local_commands_passed")) is int and 1 <= document["local_commands_passed"] <= 32 else None,
            local_evidence_source="executor_reported" if document.get("local_evidence_source") == "executor_reported" else "unavailable",
        )
        if result["result"] == "passed" and any(result[key] is None for key in ("commit_sha", "policy_digest", "receipt_digest", "checks_passed", "local_commands_passed")):
            result["result"] = "invalid"
    return result


def project_review(conn: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Exact-head historical review evidence, never a live approval signal."""
    attempt, job = record["attempt"], record.get("job") or {}
    report: dict[str, Any] = {
        "schema": "factory-console-independent-review-v1", "attempt_id": attempt["attempt_id"],
        "status": "unavailable", "pass_count": None, "max_passes": 2,
        "reviewed_sha": None, "current_sha": None, "recorded_at": None,
        "reviewer_model": None, "reviewer_session_id": None, "reason_code": None, "findings": [],
        "historical_status": None, "superseded_by_attempt_id": None, "inherited_from_attempt_id": None,
    }
    try:
        row = conn.execute("SELECT * FROM factory_independent_reviews WHERE source_attempt_id=? AND source_job_id=?",
                           (attempt["attempt_id"], job.get("job_id"))).fetchone()
        inherited = False
        if not row:
            row = conn.execute(
                "SELECT r.* FROM factory_independent_reviews r JOIN factory_attempts a ON a.attempt_id=r.repair_attempt_id "
                "WHERE r.repair_attempt_id=? AND a.job_id=? AND r.state='needs_operator' AND r.reason='review_operator_required' AND r.head_sha=?",
                (attempt["attempt_id"], job.get("job_id"), job.get("commit_sha")),
            ).fetchone()
            inherited = row is not None
        if not row:
            return {**report, "status": "not_requested", "pass_count": 0}
        sha = _reference(row["head_sha"], r"[0-9a-f]{40}")
        created = _time(row["created_at"])
        recorded = _time(row["updated_at"])
        repo = _reference(row["repository"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        url = _reference(row["pr_url"], r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", 300)
        passes = conn.execute("SELECT COUNT(*) FROM factory_independent_reviews WHERE lineage_key=?", (row["lineage_key"],)).fetchone()[0]
        if (not sha or not created or not recorded or not repo or not url or not url.startswith(f"https://github.com/{repo}/pull/")
                or repo != job.get("payload", {}).get("repo") or url != (job.get("pr_url") or attempt.get("prior_pr_url"))
                or type(passes) is not int or not 1 <= passes <= 2
                or row["state"] not in {"pending", "running", "awaiting_inspection", "passed", "changes_requested", "needs_operator"}):
            return report
        report.update(status="pending" if row["state"] == "awaiting_inspection" else row["state"],
                      pass_count=passes, reviewed_sha=sha, recorded_at=row["updated_at"],
                      reason_code=row["reason"] if row["reason"] in independent_review.REASONS else None)
        if inherited:
            report["inherited_from_attempt_id"] = _reference(row["source_attempt_id"], r"[A-Za-z0-9_.:-]+")
        if row["result_json"]:
            receipt = json.loads(row["result_json"])
            if work_evidence.digest(receipt) != row["result_digest"]:
                return {**report, "status": "unavailable"}
            request = get_job(conn, independent_review._delivery_job_id(conn, row))["payload"]["review"]
            if (not isinstance(request, dict)
                    or any(request.get(key) != row[key] for key in ("review_id", "repository", "pr_url", "head_sha", "policy_digest", "pass_number"))
                    or ("generator_execution_id" in request and request["generator_execution_id"] != f"factory-execution:{row['source_attempt_id']}:{row['source_job_id']}")):
                return {**report, "status": "unavailable"}
            independent_review._receipt(receipt, request)
            if row["state"] in {"passed", "changes_requested"} and receipt["verdict"] != row["state"]:
                return {**report, "status": "unavailable"}
            report.update(reviewer_model=receipt["reviewer_model"],
                          reviewer_session_id=_reference(receipt["reviewer_session_id"], r"[A-Za-z0-9_.:-]+"),
                          findings=[{key: sanitize_detail(finding[key], 1200) if isinstance(finding[key], str) else finding[key]
                                     for key in ("priority", "file", "line", "title", "body")} for finding in receipt["findings"]])
        elif row["state"] in {"passed", "changes_requested"}:
            report["status"] = "unavailable"
        committed = _reference(job.get("commit_sha"), r"[0-9a-f]{40}")
        report["current_sha"] = committed
        inspection = get_pr_inspection(conn, repo, url)
        if inspection and inspection["state"] == "completed":
            result, observed = inspection.get("result"), _time(inspection.get("observed_at"))
            if (isinstance(result, dict) and observed and observed >= created
                    and result.get("repository") == repo and result.get("url") == url
                    and result.get("number") == int(url.rsplit("/", 1)[1])
                    and result.get("state") in {"open", "closed"}
                    and type(result.get("merged")) is bool and type(result.get("draft")) is bool):
                report["current_sha"] = _reference(result.get("head_sha"), r"[0-9a-f]{40}") or committed
        if report["current_sha"] and report["current_sha"] != sha:
            report["status"] = "stale"
        if not inherited and row["repair_attempt_id"]:
            repair = get_factory_attempt(conn, row["repair_attempt_id"])
            if (repair["work_item_key"] == attempt["work_item_key"]
                    and (row["state"] == "changes_requested" or (row["state"] == "needs_operator" and row["reason"] == "review_operator_required" and repair["status"] == "verified_ready"))
                    and repair["status"] in {"reserved", "accepted", "running", "effect_authorized", "effect_committed", "verification_pending",
                                             "cancellation_requested", "effect_outcome_unknown", "verified_ready"}):
                report.update(status="superseded", historical_status=row["state"],
                              superseded_by_attempt_id=_reference(repair["attempt_id"], r"[A-Za-z0-9_.:-]+"))
        return report
    except (BridgeError, sqlite3.Error, ValueError, TypeError, KeyError):
        return {**report, "status": "unavailable", "findings": [], "reviewer_model": None, "reviewer_session_id": None}


def attempt_evidence(conn: Any, record: dict[str, Any]) -> dict[str, Any]:
    attempt = record["attempt"]
    job = record.get("job") or {}
    empty_inspection = {"status": "unavailable", "observed_at": None, "head_sha": None, "pr_state": "unknown", "merged": None, "draft": None}
    report: dict[str, Any] = {
        "schema": "factory-console-verification-v1", "attempt_id": attempt["attempt_id"],
        "mode": attempt["mode"], "available": False,
        "specification": {"result": "unavailable", "recorded_at": None},
        "implementation": {"result": "unavailable", "recorded_at": None},
        "verification": {"result": "unavailable", "recorded_at": None},
        "inspection": empty_inspection, "head_comparison": "unavailable",
        "independent_review": record.get("independent_review") or project_review(conn, record),
    }
    evidence = get_factory_work_evidence(conn, attempt["attempt_id"])
    report["available"] = evidence["available"]
    for key, kind in (("specification", "specification_snapshot"), ("implementation", "implementation_receipt"), ("verification", "verification_receipt")):
        report[key] = _checkpoint(evidence["checkpoints"], kind)
    # Simulated artifacts cannot establish real verification or PR-head evidence.
    if attempt["mode"] != "real":
        report["implementation"] = {"result": "not_applicable", "recorded_at": None}
        report["verification"] = {"result": "not_applicable", "recorded_at": None}
        return report
    repo = _reference(job.get("payload", {}).get("repo"), r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    url = _reference(job.get("pr_url") or attempt.get("prior_pr_url"), r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", 300)
    if not repo or not url or not url.startswith(f"https://github.com/{repo}/pull/"):
        return report
    try:
        inspection = get_pr_inspection(conn, repo, url)
        if not inspection:
            return report
        if inspection["state"] != "completed":
            report["inspection"] = {**empty_inspection, "status": inspection["state"] if inspection["state"] in {"pending", "expired"} else "unavailable"}
            return report
        result = inspection.get("result")
        observed = _time(inspection.get("observed_at"))
        if (not isinstance(result, dict) or not observed
                or result.get("repository") != repo or result.get("url") != url
                or result.get("number") != int(url.rsplit("/", 1)[1])
                or result.get("state") not in {"open", "closed", "unknown"}
                or type(result.get("merged")) is not bool or type(result.get("draft")) is not bool):
            raise ValueError("invalid inspection")
        report["inspection"] = {
            "status": "recorded" if inspection["fresh"] else "stale",
            "observed_at": inspection["observed_at"],
            "head_sha": _reference(result.get("head_sha"), r"[0-9a-f]{40}"),
            "pr_state": result["state"], "merged": result["merged"], "draft": result["draft"],
        }
    except (BridgeError, ValueError, TypeError, KeyError):
        report["inspection"] = {**empty_inspection, "status": "invalid"}
        return report
    verified = report["verification"]
    if verified["result"] == "passed" and report["inspection"]["head_sha"] and result["state"] != "unknown":
        if observed < _time(verified["recorded_at"]):
            report["head_comparison"] = "inspection_predates_verification"
        else:
            report["head_comparison"] = "same_head" if verified["commit_sha"] == report["inspection"]["head_sha"] else "head_changed"
    return report
