from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

from .store import DRAFT_PR_EFFECT_ACTIONS, list_jobs_by_states, transition_job


@dataclass(frozen=True)
class GitHubBranch:
    name: str
    commit_sha: str | None


@dataclass(frozen=True)
class GitHubPullRequest:
    number: int
    url: str
    state: str
    is_draft: bool
    head_ref_name: str


class GitHubInspector(Protocol):
    def find_pr_for_branch(self, repo: str, branch: str) -> GitHubPullRequest | None:
        ...

    def find_branch(self, repo: str, branch: str) -> GitHubBranch | None:
        ...


class GhCliGitHubInspector:
    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    def find_pr_for_branch(self, repo: str, branch: str) -> GitHubPullRequest | None:
        result = self._run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,state,isDraft,headRefName",
                "--limit",
                "10",
            ]
        )
        prs = json.loads(result.stdout)
        if not isinstance(prs, list) or not prs:
            return None

        pr = sorted(prs, key=lambda item: int(item.get("number", 0)), reverse=True)[0]
        return GitHubPullRequest(
            number=int(pr["number"]),
            url=str(pr["url"]),
            state=str(pr["state"]),
            is_draft=bool(pr.get("isDraft")),
            head_ref_name=str(pr.get("headRefName") or branch),
        )

    def find_branch(self, repo: str, branch: str) -> GitHubBranch | None:
        result = self._run(
            [
                "api",
                f"repos/{repo}/branches/{quote(branch, safe='')}",
                "--jq",
                "{name:.name,commit_sha:.commit.sha}",
            ],
            allow_404=True,
        )
        if result.returncode == 1 and "HTTP 404" in result.stderr:
            return None
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        payload = json.loads(result.stdout)
        return GitHubBranch(
            name=str(payload.get("name") or branch),
            commit_sha=payload.get("commit_sha"),
        )

    def _run(self, args: list[str], *, allow_404: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode != 0 and not allow_404:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result


def reconcile_jobs(
    conn,
    *,
    inspector: GitHubInspector | None = None,
    stale_after: timedelta = timedelta(minutes=30),
    limit: int = 50,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    inspector = inspector or GhCliGitHubInspector()
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    for job in list_jobs_by_states(conn, {"running", "effect_committed", "verification_pending"}, limit=limit):
        if not _is_stale(job, now, stale_after):
            results.append(_result(job, "skipped", "not_stale"))
            continue

        if job["requested_action"] not in DRAFT_PR_EFFECT_ACTIONS:
            results.append(_result(job, "skipped", "unsupported_action"))
            continue

        if job["state"] in {"effect_committed", "verification_pending"}:
            results.append(_reconcile_effect_committed(conn, job, dry_run=dry_run))
            continue

        results.append(_reconcile_running(conn, job, inspector, dry_run=dry_run))

    return results


def _reconcile_effect_committed(conn, job: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    if not job.get("pr_url"):
        return _result(job, "blocked", "effect_committed_without_pr_url")

    if job["state"] == "verification_pending":
        return _result(job, "verification_pending", "draft_pr_verification_pending")

    details = {"reason_code": "draft_pr_verification_pending"}
    if dry_run:
        return _result(job, "would_mark_verification_pending", details["reason_code"], details=details)

    pending = transition_job(conn, job["job_id"], state="verification_pending", details=details)
    return _result(pending, "verification_pending", details["reason_code"], details=details)


def _reconcile_running(
    conn,
    job: dict[str, Any],
    inspector: GitHubInspector,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    repo = _payload_string(job, "repo")
    branch = job.get("branch")
    if not repo or not branch:
        return _fail_or_dry_run(
            conn,
            job,
            dry_run=dry_run,
            reason_code="reconciled_missing_repo_or_branch",
            error="running create_draft_pr job is missing repo or deterministic branch",
        )

    pr = inspector.find_pr_for_branch(repo, branch)
    if pr:
        branch_ref = inspector.find_branch(repo, branch)
        commit_sha = branch_ref.commit_sha if branch_ref else job.get("commit_sha")
        committed_details = {
            "commit_sha": commit_sha,
            "pr_url": pr.url,
            "reason_code": "reconciled_pr_found",
            "summary": f"Reconciler found PR #{pr.number} for deterministic branch {branch}.",
        }
        pending_details = {"reason_code": "draft_pr_verification_pending"}
        if dry_run:
            return _result(
                job,
                "would_mark_verification_pending",
                pending_details["reason_code"],
                details={
                    "effect_committed": committed_details,
                    "verification_pending": pending_details,
                },
            )

        committed = transition_job(conn, job["job_id"], state="effect_committed", details=committed_details)
        pending = transition_job(conn, committed["job_id"], state="verification_pending", details=pending_details)
        return _result(pending, "verification_pending", pending_details["reason_code"], details=committed_details)

    branch_ref = inspector.find_branch(repo, branch)
    if branch_ref:
        details = {
            "commit_sha": branch_ref.commit_sha,
            "reason_code": "reconciled_branch_found_without_pr",
            "error": f"deterministic branch {branch} exists without a pull request",
        }
        if dry_run:
            return _result(job, "would_mark_effect_committed", details["reason_code"], details=details)

        committed = transition_job(conn, job["job_id"], state="effect_committed", details=details)
        return _result(committed, "effect_committed", details["reason_code"], details=details)

    return _fail_or_dry_run(
        conn,
        job,
        dry_run=dry_run,
        reason_code="reconciled_no_side_effect_after_timeout",
        error=f"no PR or branch found for deterministic branch {branch}",
    )


def _fail_or_dry_run(
    conn,
    job: dict[str, Any],
    *,
    dry_run: bool,
    reason_code: str,
    error: str,
) -> dict[str, Any]:
    details = {"reason_code": reason_code, "error": error}
    if dry_run:
        return _result(job, "would_fail", reason_code, details=details)

    failed = transition_job(conn, job["job_id"], state="failed", details=details)
    return _result(failed, "failed", reason_code, details=details)


def _is_stale(job: dict[str, Any], now: datetime, stale_after: timedelta) -> bool:
    updated_at = _parse_utc(job["updated_at"])
    return updated_at + stale_after <= now


def _parse_utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _payload_string(job: dict[str, Any], key: str) -> str | None:
    value = job.get("payload", {}).get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _result(
    job: dict[str, Any],
    action: str,
    reason_code: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "idempotency_key": job["idempotency_key"],
        "state": job["state"],
        "branch": job.get("branch"),
        "action": action,
        "reason_code": reason_code,
        "details": details or {},
    }
