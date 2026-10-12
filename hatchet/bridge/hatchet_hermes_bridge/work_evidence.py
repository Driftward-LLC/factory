"""Content-only contracts for Factory work evidence; no execution authority.

Builders accept durable bridge records, never arbitrary checkpoint submissions.
Only hashes, bounded references and explicit observations enter these documents.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reference(value: Any, pattern: str, maximum: int = 200) -> str | None:
    return value if isinstance(value, str) and len(value) <= maximum and re.fullmatch(pattern, value) else None


def sha256(value: Any) -> str | None:
    return reference(value, r"[0-9a-f]{64}")


def admission_manifest(attempt: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    source = next((key for key in ("implementation_prompt", "instructions") if isinstance(payload.get(key), str) and payload[key].strip()), None)
    policy = payload.get("verification_policy")
    policy = policy if isinstance(policy, dict) else {}
    return {
        "schema": "factory-execution-manifest-v1",
        "phase": "admission",
        "producer": "factory-bridge",
        "producer_version": "work-evidence-v1",
        "attempt_id": attempt["attempt_id"],
        "job_id": job["job_id"],
        "mode": attempt["mode"],
        "action": job["requested_action"],
        "generation": attempt["generation"],
        "prior_attempt_id": attempt.get("prior_attempt_id"),
        "repository": reference(payload.get("repo"), r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
        "work_item_key": reference(attempt["work_item_key"], r"[A-Z][A-Z0-9]*-[1-9][0-9]*"),
        # This is the stored effect-intent hash; webhook redeliveries cannot alter it.
        "input_digest": job["payload_hash"],
        "requirements_source": source or "unavailable",
        "requirements_digest": digest({"source": source, "text": payload[source]}) if source else None,
        "policy_snapshot_digest": digest(policy) if policy else None,
        "submitted_verifier_policy_digest": sha256(policy.get("digest")),
        "expected_head_sha": reference(payload.get("expected_head_sha"), r"[0-9a-f]{40}"),
        # An expected repair head or a branch name is not a measured checkout SHA.
        "base_commit_sha": None,
        "runtime_bundle_digest": None,
        "environment_image_digest": None,
        "input_artifact_ref": None,
        "route": None,
        "revision": {
            "source_attempt_id": payload["factory_revision"].get("source_attempt_id"),
            "feedback_digest": payload["factory_revision"].get("feedback_digest"),
            "reviewed_head_sha": payload["factory_revision"].get("reviewed_head_sha"),
            "review_url_digest": digest(payload["factory_revision"]["review_url"]) if payload["factory_revision"].get("review_url") else None,
            "design_url_digest": digest(payload["factory_revision"]["design_url"]) if payload["factory_revision"].get("design_url") else None,
            "approval_kind": "operator_attestation",
            "independent_review_result": "unknown",
        } if isinstance(payload.get("factory_revision"), dict) else None,
    }


def execution_manifest(admission: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    # The caller has validated the complete route envelope before persistence.
    return {**admission, "phase": "execution", "route": dict(route)}


def specification_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "factory-specification-snapshot-v1",
        "result": "recorded" if manifest["requirements_digest"] else "unavailable",
        "requirements_digest": manifest["requirements_digest"],
        "requirements_source": manifest["requirements_source"],
        "approval": "unknown",
        "revision": manifest.get("revision"),
    }


def implementation_receipt(job: dict[str, Any]) -> dict[str, Any]:
    repository = reference(job["payload"].get("repo"), r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    pr_url = reference(job.get("pr_url"), r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", 300)
    if pr_url and (repository is None or not pr_url.startswith(f"https://github.com/{repository}/pull/")):
        pr_url = None
    commit = reference(job.get("commit_sha"), r"[0-9a-f]{40}")
    return {
        "schema": "factory-implementation-receipt-v1",
        "result": "recorded" if pr_url and commit else "unavailable",
        "repository": repository,
        "pr_url": pr_url,
        "commit_sha": commit,
        "receipt_digest": digest({key: job.get(key) for key in ("pr_url", "branch", "commit_sha")}),
        "verification": "pending",
    }


def verification_receipt(job: dict[str, Any], *, passed: bool) -> dict[str, Any]:
    receipt = job.get("verification_receipt") if passed else None
    return {
        "schema": "factory-verification-receipt-v1",
        "result": "passed" if passed else "failed",
        "reason_code": reference(job.get("reason_code"), r"[a-z][a-z0-9_]{0,119}"),
        "commit_sha": reference(job.get("commit_sha"), r"[0-9a-f]{40}"),
        "receipt_digest": job.get("verification_digest") if passed else None,
        "policy_digest": receipt["policy_digest"] if receipt else None,
        "verifier_version": reference(receipt.get("verifier_version"), r"[A-Za-z0-9][A-Za-z0-9._+-]{0,119}") if receipt else None,
        "checks_passed": receipt["checks"]["total_count"] if receipt else None,
        "local_commands_passed": len(receipt["local_policy"]["results"]) if receipt else None,
        # Preserve the distinction between reported local checks and independent tests.
        "local_evidence_source": "executor_reported" if receipt else "unavailable",
        "review_approval": "unknown",
    }


def replay_gaps(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return ["admission_manifest"]
    missing = [key for key in (
        "requirements_digest", "policy_snapshot_digest", "base_commit_sha",
        "runtime_bundle_digest", "environment_image_digest", "input_artifact_ref",
    ) if manifest.get(key) is None]
    route = manifest.get("route")
    if route is None:
        missing.append("execution_route")
    else:
        missing.extend(f"route.{key}" for key in ("backend_version", "factory_release_sha", "verifier_policy_digest") if route.get(key) is None)
        missing.extend(f"route.{key}" for key in ("configured_provider", "configured_model") if route.get(key) == "unknown")
    return missing
