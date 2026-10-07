"""Pure, fail-closed legacy artifact parsing shared by import and predeploy."""
from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

PR_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^codex/hatchet-hermes-[0-9a-f]{16}$")
LINEAR_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
DRY_RUN_SENTINEL_RE = re.compile(
    r"^dry-run://github/pr/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Artifact evidence is deliberately path-scoped.  Historical bridge payloads
# contain arbitrary provider metadata, so a recursive "url"/"branch" walk is
# not evidence of a PR effect.
DIRECT_PR_FIELDS = frozenset({"pr_url"})
EVENT_PR_FIELDS = frozenset({"pr_url", "pull_url", "html_url"})
EVENT_BRANCH_FIELDS = frozenset({"branch", "head_branch", "headRefName"})
PENDING_ATTEMPT_STATUSES = frozenset({
    "reserved", "accepted", "running", "effect_authorized", "effect_committed",
    "verification_pending", "cancellation_requested", "effect_outcome_unknown",
})
LEGACY_SOURCE_TERMINAL_STATES = frozenset({"completed"})
LEGACY_RECEIPT_STATUSES = frozenset({"legacy_completed"})
LEGACY_IMPORTED_EVENT = "legacy_imported"
LEGACY_IMPORT_CONFLICT_REASONS = frozenset({
    "duplicate_legacy_artifact_reference",
    "duplicate_legacy_work_item",
    "legacy_artifact_reference_conflict",
    "legacy_payload_malformed",
})


@dataclass(frozen=True)
class LegacyImportDecision:
    """One fail-closed reconciliation decision for an active-key legacy row."""

    action: str
    work_item_key: str
    mode: str
    repository: str
    branch: str
    prior_pr_url: str | None
    attempt_id: str | None = None


@dataclass(frozen=True)
class LegacyArtifactEvidence:
    """Canonical historical artifact evidence, including dry-run sentinels."""

    urls: frozenset[str]
    branches: frozenset[str]
    dry_run_sentinels: frozenset[str]


def canonical_repository(value: Any) -> str:
    if not isinstance(value, str) or not REPOSITORY_RE.fullmatch(value):
        raise ValueError("repository is not canonical")
    return value


def parse_pr_url(value: Any) -> tuple[str, int, str]:
    match = PR_URL_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("PR URL is invalid")
    repository = f"{match.group(1)}/{match.group(2)}"
    return repository, int(match.group(3)), value


def json_object(raw: Any) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("JSON must be an object")
    return value


def deterministic_branch(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    return f"codex/hatchet-hermes-{digest}"


def legacy_active_key(work_item_key: str, mode: str) -> str:
    if not LINEAR_KEY_RE.fullmatch(work_item_key) or mode not in {"real", "dry-run"}:
        raise ValueError("legacy work item or mode is invalid")
    return f"hatchet:work-item:linear:{work_item_key}:create_draft_pr:{mode}:active"


def routing_repositories(raw: Any) -> tuple[set[str], set[str]]:
    """Return canonical and execution-enabled repositories from one registry."""
    registry = json_object(raw)
    if not isinstance(registry.get("version"), int) or not isinstance(registry.get("routes"), list):
        raise ValueError("routing registry is invalid")
    routed: set[str] = set()
    enabled: set[str] = set()
    for route in registry["routes"]:
        if not isinstance(route, dict) or not isinstance(route.get("execution_enabled"), bool):
            raise ValueError("routing registry is invalid")
        repository = canonical_repository(route.get("repo"))
        if repository in routed:
            raise ValueError("routing registry has duplicate repository")
        routed.add(repository)
        if route["execution_enabled"]:
            enabled.add(repository)
    retired = registry.get("retired_repositories", [])
    if not isinstance(retired, list) or len(retired) > 32:
        raise ValueError("retired repositories are invalid")
    for value in retired:
        repository = canonical_repository(value)
        if repository in routed:
            raise ValueError("retired repository is duplicated or still routed")
        routed.add(repository)
    if not routed or not enabled:
        raise ValueError("routing registry is invalid")
    return routed, enabled


def _optional_pr_url(value: Any, *, repository: str) -> str | None:
    if value is None or value == "":
        return None
    observed, _number, url = parse_pr_url(value)
    if observed != repository:
        raise ValueError("PR reference repository mismatch")
    return url


def _legacy_dry_run_sentinel(value: Any) -> bool:
    return isinstance(value, str) and DRY_RUN_SENTINEL_RE.fullmatch(value) is not None


def _field_refs(value: Any, *, pr_fields: frozenset[str], branch_fields: frozenset[str], key: str) -> list[tuple[str, str]]:
    """Collect strict artifact fields; present malformed values are conflicts.

    Legacy JSON contains unrelated URLs, so only the historical artifact field
    aliases are interpreted.  Absence, NULL, and an empty string are optional;
    every other value under a recognized key is evidence and must be valid.
    """
    refs: list[tuple[str, str]] = []
    if key in pr_fields:
        if value is None or value == "":
            return refs
        if not isinstance(value, str):
            raise ValueError(f"artifact PR field {key} is not a string")
        parse_pr_url(value)
        return [("pr", value)]
    if key in branch_fields:
        if value is None or value == "":
            return refs
        if not isinstance(value, str):
            raise ValueError(f"artifact branch field {key} is not a string")
        if not BRANCH_RE.fullmatch(value):
            raise ValueError(f"artifact branch field {key} is invalid")
        return [("branch", value)]
    return refs


def collect_legacy_artifact_evidence(source: Any, *, mode: str) -> LegacyArtifactEvidence:
    """Collect only documented, path-aware historical artifact evidence.

    A source is ``(kind, object)``.  ``bridge`` is the durable bridge-job
    row, ``event`` is a documented bridge event details object, and
    ``inspection`` is one exact inspection request/result object.  Untyped or
    unknown sources intentionally contribute no evidence; they cannot turn a
    nested provider URL into a durable receipt.
    """
    if mode not in {"real", "dry-run"}:
        raise ValueError("legacy mode is invalid")
    if not isinstance(source, tuple) or len(source) != 2:
        return LegacyArtifactEvidence(frozenset(), frozenset(), frozenset())
    kind, value = source
    if not isinstance(value, Mapping):
        raise ValueError("authoritative artifact source is not an object")
    if kind == "bridge":
        pr_fields, branch_fields = DIRECT_PR_FIELDS, frozenset({"branch"})
        objects = (value,)
    elif kind == "event":
        # Known result fields are permitted only at the event root or its
        # documented effect/result object, never in arbitrary nested metadata.
        pr_fields, branch_fields = EVENT_PR_FIELDS, EVENT_BRANCH_FIELDS
        objects = (value,) + tuple(item for key, item in value.items() if key in {"effect", "result", "pull_request"} and isinstance(item, Mapping))
    elif kind == "inspection":
        pr_fields, branch_fields = frozenset({"url", "pr_url"}), EVENT_BRANCH_FIELDS
        objects = (value,)
    else:
        return LegacyArtifactEvidence(frozenset(), frozenset(), frozenset())
    urls: set[str] = set(); branches: set[str] = set(); sentinels: set[str] = set()
    for obj in objects:
        for key in pr_fields:
            if key not in obj:
                continue
            candidate = obj[key]
            if mode == "dry-run" and _legacy_dry_run_sentinel(candidate):
                sentinels.add(candidate); continue
            urls.update(item for field, item in _field_refs(candidate, pr_fields=pr_fields, branch_fields=branch_fields, key=key) if field == "pr")
        for key in branch_fields:
            if key in obj:
                branches.update(item for field, item in _field_refs(obj[key], pr_fields=pr_fields, branch_fields=branch_fields, key=key) if field == "branch")
    return LegacyArtifactEvidence(frozenset(urls), frozenset(branches), frozenset(sentinels))


def collect_legacy_artifact_refs(value: Any, *, mode: str) -> list[tuple[str, str]]:
    """Compatibility wrapper for callers that only need external artifacts."""
    evidence = collect_legacy_artifact_evidence(value, mode=mode)
    return [("pr", url) for url in evidence.urls] + [("branch", branch) for branch in evidence.branches]


def validate_artifact_refs(repository: Any, refs: list[tuple[str, str]]) -> tuple[set[str], set[str]]:
    """Reject cross-repository refs and distinct artifacts within one lineage."""
    expected = canonical_repository(repository)
    urls: set[str] = set()
    branches: set[str] = set()
    for kind, value in refs:
        if kind == "pr":
            observed, _, url = parse_pr_url(value)
            if observed != expected:
                raise ValueError("PR reference repository mismatch")
            urls.add(url)
        elif kind == "branch":
            if not BRANCH_RE.fullmatch(value):
                raise ValueError("deterministic branch is invalid")
            branches.add(value)
        else:
            raise ValueError("artifact reference kind is invalid")
    if len(urls) > 1 or len(branches) > 1:
        raise ValueError("conflicting artifact references")
    return urls, branches


def _legacy_failed_source_is_side_effect_free(
    sources: Iterable[Any],
    *,
    mode: str,
    expected_branch: str,
) -> bool:
    for source in sources:
        if not isinstance(source, tuple) or len(source) != 2:
            return False
        kind, value = source
        if kind == "bridge":
            if not isinstance(value, Mapping):
                return False
            if value.get("pr_url") not in {None, ""}:
                return False
            if value.get("branch") not in {None, "", expected_branch}:
                return False
            continue
        evidence = collect_legacy_artifact_evidence(source, mode=mode)
        if evidence.urls or evidence.dry_run_sentinels:
            return False
        if any(branch != expected_branch for branch in evidence.branches):
            return False
    return True


def reconcile_legacy_active_row(
    row: Mapping[str, Any],
    *,
    artifact_sources: Iterable[Any],
    attempts: Iterable[Mapping[str, Any]],
    import_conflicts: Iterable[Mapping[str, Any]],
    execution_leases: Iterable[Mapping[str, Any]],
    attempt_events: Iterable[Mapping[str, Any]] = (),
    attempt_outbox: Iterable[Mapping[str, Any]] = (),
    routed_repositories: set[str],
    enabled_repositories: set[str],
) -> LegacyImportDecision:
    """Reconcile one historical active-key row without weakening retry gates.

    This function is deliberately SQLite-free.  The write-time importer and
    read-only predeploy checker both pass their local rows into this same
    decision boundary so historical receipt reconciliation cannot drift from
    rollout validation.
    """
    payload = json_object(row.get("payload_json"))
    aliases = [payload.get(field) for field in ("linear_issue", "work_item_id") if field in payload]
    if not aliases or any(not isinstance(value, str) for value in aliases) or len(set(aliases)) != 1:
        raise ValueError("legacy work item aliases are invalid")
    work_item = aliases[0]
    if not isinstance(work_item, str) or not LINEAR_KEY_RE.fullmatch(work_item):
        raise ValueError("legacy work item is invalid")
    if not isinstance(payload.get("dry_run"), bool):
        raise ValueError("legacy mode is invalid")
    mode = "dry-run" if payload["dry_run"] else "real"
    repository = canonical_repository(payload.get("repo"))
    key = legacy_active_key(work_item, mode)
    job_id = row.get("job_id")
    if not isinstance(job_id, str) or not job_id or row.get("idempotency_key") != key:
        raise ValueError("legacy idempotency lineage is invalid")
    expected_branch = deterministic_branch(key)
    if row.get("requested_action") not in {None, "create_draft_pr"}:
        raise ValueError("legacy action lineage is invalid")
    raw_branch = row.get("branch")
    if raw_branch != expected_branch:
        raise ValueError("legacy branch lineage is invalid")
    if repository not in routed_repositories:
        raise ValueError("legacy repository is not canonically routed")

    sources = tuple(artifact_sources)
    related_attempts = list(attempts)
    conflicts = list(import_conflicts)
    leases = list(execution_leases)
    events = list(attempt_events)
    outbox_rows = list(attempt_outbox)
    source_attempt_ids = {
        attempt.get("attempt_id")
        for attempt in related_attempts
        if attempt.get("job_id") == job_id
        or (
            attempt.get("action") == "create_draft_pr"
            and attempt.get("work_item_key") == work_item
            and attempt.get("mode") == mode
            and attempt.get("idempotency_key") == key
        )
    }
    source_attempt_ids.discard(None)
    source_attempts = [attempt for attempt in related_attempts if attempt.get("attempt_id") in source_attempt_ids]
    source_leases = [lease for lease in leases if lease.get("attempt_id") in source_attempt_ids]
    source_events = [event for event in events if event.get("attempt_id") in source_attempt_ids]
    source_outbox_rows = [
        outbox
        for outbox in outbox_rows
        if outbox.get("job_id") == job_id or outbox.get("attempt_id") in source_attempt_ids
    ]

    # A generation-one retry created by the current operator reuses the old
    # active key.  It is not a historical source row and is governed by the
    # retry ledger itself, never by legacy receipt import/reconciliation.
    if "factory_retry" in payload:
        return LegacyImportDecision("not_legacy", work_item, mode, repository, expected_branch, None)
    if row.get("state") not in LEGACY_SOURCE_TERMINAL_STATES:
        if (
            row.get("state") == "failed"
            and not source_attempts
            and not conflicts
            and not source_leases
            and not source_events
            and not source_outbox_rows
            and _legacy_failed_source_is_side_effect_free(sources, mode=mode, expected_branch=expected_branch)
        ):
            return LegacyImportDecision("not_legacy", work_item, mode, repository, expected_branch, None)
        raise ValueError("legacy source is not an approved terminal receipt")

    evidence = LegacyArtifactEvidence(frozenset(), frozenset(), frozenset())
    for source in sources:
        observed = collect_legacy_artifact_evidence(source, mode=mode)
        evidence = LegacyArtifactEvidence(
            evidence.urls | observed.urls,
            evidence.branches | observed.branches,
            evidence.dry_run_sentinels | observed.dry_run_sentinels,
        )
    urls, branches = validate_artifact_refs(repository, [("pr", url) for url in evidence.urls] + [("branch", branch) for branch in evidence.branches])
    if len(evidence.dry_run_sentinels) > 1:
        raise ValueError("conflicting dry-run sentinel references")
    if mode == "real" and evidence.dry_run_sentinels:
        raise ValueError("real legacy row has a dry-run sentinel")
    if mode == "dry-run" and urls:
        raise ValueError("dry-run legacy row has an external artifact")
    if branches != {expected_branch}:
        raise ValueError("legacy branch artifact is incomplete or non-deterministic")

    if conflicts:
        raise ValueError("legacy import conflict exists")
    if related_attempts:
        # Do not select a convenient generation-one row: anything else tied to
        # this job or work item makes the historical lineage ambiguous.
        if len(related_attempts) != 1:
            raise ValueError("legacy source has conflicting attempt lineage")
        attempt = related_attempts[0]
        required = {
            "action": "create_draft_pr",
            "work_item_key": work_item,
            "mode": mode,
            "generation": 1,
            "job_id": job_id,
            "idempotency_key": key,
            "branch": expected_branch,
            "attempt_id": f"legacy-{job_id}",
            "request_id": f"legacy:{job_id}",
            "actor": "legacy-import",
            "status": f"legacy_{row['state']}",
        }
        if any(attempt.get(field) != value for field, value in required.items()):
            raise ValueError("legacy generation-one receipt linkage is invalid")
        attempt_id = attempt.get("attempt_id")
        if attempt.get("status") not in LEGACY_RECEIPT_STATUSES:
            raise ValueError("legacy generation-one receipt status is not approved")
        if any(attempt.get(field) not in {None, ""} for field in ("prior_attempt_id", "prior_job_id", "supersede_pr_url", "artifact_fence_json", "trusted_snapshot_digest", "reason")):
            raise ValueError("legacy generation-one receipt has non-import lineage")
        prior_pr_url = _optional_pr_url(attempt.get("prior_pr_url"), repository=repository)
        if mode == "real":
            # The first importer persisted the immutable artifact on the
            # historical bridge job, before `prior_pr_url` existed.  That
            # bridge field and its event evidence are therefore the durable
            # generation-one receipt.  A populated newer attempt field must
            # still agree exactly; absence cannot be retroactively invented.
            if len(urls) != 1 or (prior_pr_url is not None and prior_pr_url != next(iter(urls))):
                raise ValueError("legacy real artifact evidence disagrees with receipt")
        elif prior_pr_url is not None or (evidence.dry_run_sentinels and urls):
            raise ValueError("legacy dry-run receipt has an external artifact")
        imported_events = [event for event in events if event.get("attempt_id") == attempt_id]
        if len(imported_events) != 1 or imported_events[0].get("event_type") != LEGACY_IMPORTED_EVENT:
            raise ValueError("legacy receipt import event is invalid")
        details = json_object(imported_events[0].get("details_json"))
        if details.get("job_id") != job_id or (
            "artifact_present" in details and details["artifact_present"] is not (mode == "real")
        ):
            raise ValueError("legacy receipt import event is inconsistent")
        if any(
            lease.get("released_at") in {None, ""}
            or (lease.get("effect_authorized_at") not in {None, ""} and lease.get("effect_observed_at") in {None, ""})
            for lease in leases
        ):
            raise ValueError("legacy receipt has active or pending effect")
        if any(outbox.get("acknowledged_at") in {None, ""} for outbox in outbox_rows):
            raise ValueError("legacy receipt has pending delivery")
        return LegacyImportDecision("already_imported", work_item, mode, repository, expected_branch, prior_pr_url, attempt_id)

    if mode == "dry-run":
        return LegacyImportDecision("import", work_item, mode, repository, expected_branch, None)
    if repository not in enabled_repositories:
        raise ValueError("real legacy repository is not execution enabled")
    if len(urls) != 1:
        raise ValueError("real legacy artifact receipt is incomplete")
    return LegacyImportDecision("import", work_item, mode, repository, expected_branch, next(iter(urls)))
