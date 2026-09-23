"""Allowlisted, read-only verification evidence for one console attempt.

These observations never confer review, merge, or execution authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import work_evidence
from .store import BridgeError, get_factory_work_evidence, get_pr_inspection, sanitize_detail


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
        "independent_review": "unavailable",
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
