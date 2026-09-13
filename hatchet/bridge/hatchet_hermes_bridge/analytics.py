"""Read-only Factory analytics projected from the existing durable ledgers.

The bridge, ingress, and lifecycle databases remain the sources of truth.  This
module deliberately creates no tables and performs no backfill: missing
evidence is reported as missing coverage instead of being guessed.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .store import connect_readonly


ANALYTICS_SCHEMA = "factory-analytics-v1"
RESERVATION_KINDS = {
    "intake_reserved": "initial",
    "retry_reserved": "retry",
    "absence_retry_reserved": "absence_retry",
    "repair_reserved": "repair",
    "legacy_imported": "legacy",
}
TERMINAL_ATTEMPT_EVENTS = {
    "attempt_completed",
    "attempt_failed",
    "attempt_verified_ready",
    "attempt_canceled_before_effect",
    "attempt_canceled_after_effect",
    "superseded",
}
TERMINAL_ATTEMPT_STATUSES = {"completed", "failed", "verified_ready", "canceled", "canceled_after_effect", "superseded"}


def factory_analytics(
    bridge_db: str,
    ingress_db: str,
    lifecycle_db: str,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded report without mutating or migrating any source."""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise ValueError("analytics days must be between 1 and 365")
    bridge = connect_readonly(bridge_db)
    try:
        bridge.execute("BEGIN")
        # Establish the read snapshot before selecting the report cutoff. This
        # keeps mutable job/attempt state and append-only bridge events on one
        # consistent ledger view.
        bridge.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()
        ended = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        started = ended - timedelta(days=days)
        started_at, ended_at = _timestamp(started), _timestamp(ended)
        attempts = [dict(row) for row in bridge.execute(
            """
            SELECT a.*, j.state AS job_state, j.pr_url, j.reason_code AS job_reason_code
            FROM factory_attempts a
            LEFT JOIN bridge_jobs j ON j.job_id = a.job_id
            WHERE a.created_at >= ? AND a.created_at <= ?
            ORDER BY a.created_at, a.attempt_id
            """,
            (started_at, ended_at),
        )]
        cohort = [row for row in attempts if row["mode"] == "real" and row["actor"] != "legacy-import"]
        cohort_ids = {row["attempt_id"] for row in cohort}
        cohort_jobs = {row["job_id"] for row in cohort if row.get("job_id")}

        attempt_events = _attempt_events(bridge, started_at, ended_at, cohort_ids)
        bridge_events = _bridge_events(bridge, started_at, ended_at, cohort_jobs)
        findings = _cohort_findings(bridge, cohort_jobs)
    finally:
        bridge.close()

    events_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in attempt_events:
        events_by_attempt[event["attempt_id"]].append(event)
    bridge_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in bridge_events:
        bridge_by_job[event["job_id"]].append(event)

    kinds = {row["attempt_id"]: _attempt_kind(events_by_attempt[row["attempt_id"]]) for row in cohort}
    work_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in cohort:
        work_items[attempt["work_item_key"]].append(attempt)

    stages = {
        "attempted": len(work_items),
        "executor_started": _work_item_count(work_items, events_by_attempt, "attempt_started"),
        "effect_committed": _effect_work_item_count(work_items, events_by_attempt, bridge_by_job),
        "draft_pr_available": len({row["work_item_key"] for row in cohort if _github_pr(row.get("pr_url"))}),
        "verified_ready": _work_item_count(work_items, events_by_attempt, "attempt_verified_ready"),
    }
    funnel = {
        "grain": "unique_work_item",
        "stages": {
            key: {"count": value, "rate_from_attempted": _rate(value, stages["attempted"])}
            for key, value in stages.items()
        },
    }

    attempt_failures = _attempt_failures(cohort, bridge_by_job)
    invocation_failures = _execution_failures(attempt_events, "execution_backend_failed")
    dispatch_failures = _execution_failures(attempt_events, "execution_dispatch_failed")
    commit_failures = _execution_failures(attempt_events, "execution_effect_commit_failed")
    backend_rows, attributed_attempts, authorized_attempts, claimed_attempts, configured_auth_attempts = _backends(
        cohort, events_by_attempt, bridge_by_job
    )
    status_counts = Counter(str(row["status"]) for row in cohort)
    pr_urls = {_github_pr(row.get("pr_url")) for row in cohort} - {None}

    ingress, github_outcomes = _ingress_report(ingress_db, started_at, ended_at, pr_urls)
    lifecycle = _lifecycle_report(lifecycle_db, started_at, ended_at, cohort_jobs)
    latency = _latencies(cohort, events_by_attempt, bridge_by_job)

    reservation_counts = Counter(kinds.values())
    verified_by_kind = Counter(
        kinds[row["attempt_id"]]
        for row in cohort
        if _has_event(events_by_attempt[row["attempt_id"]], "attempt_verified_ready")
    )
    repair_budgets = sum(
        1 for events in events_by_attempt.values() for event in events
        if event["event_type"] == "repair_budget_exhausted"
    )
    unknown_effects = sum(
        1 for events in events_by_attempt.values()
        if any(
            event["event_type"] == "effect_authorized_outcome_unknown"
            and event["details"].get("effect_outcome") != "observed"
            for event in events
        )
    )
    receipt_recovery_incidents = sum(
        1 for events in events_by_attempt.values()
        if _has_event(events, "execution_effect_commit_failed")
    )
    unresolved_receipts = sum(
        1 for row in cohort
        if row["status"] in {"effect_outcome_unknown", "cancellation_requested"}
        and _has_event(events_by_attempt[row["attempt_id"]], "execution_effect_commit_failed")
    )
    exhausted_receipt_recovery = sum(
        1 for events in events_by_attempt.values()
        if _has_event(events, "execution_effect_commit_recovery_exhausted")
    )
    rejected_receipts = sum(
        1 for events in events_by_attempt.values()
        if _has_event(events, "execution_effect_commit_receipt_rejected")
    )
    deduplicated = sum(1 for event in bridge_events if event["event_type"] == "submit_deduplicated")
    interventions = {
        failure["attempt_id"]
        for failure in attempt_failures
        if failure["operator_intervention_required"] is True
    }
    interventions.update(
        event["attempt_id"] for event in attempt_events
        if event["event_type"] in {"attempt_execution_recovery_required", "absence_superseded_unknown_attempt", "repair_budget_exhausted"}
        or event["details"].get("operator_intervention_required") is True
    )
    attributed_failures = sum(
        1 for row in attempt_failures
        if row["failure_domain"] != "unknown" and row["failure_stage"] != "unknown"
    )
    committed_real_effects = sum(
        1 for row in cohort
        if _effect_committed(row, events_by_attempt[row["attempt_id"]], bridge_by_job)
    )
    terminal_status_attempts = sum(1 for row in cohort if row["status"] in TERMINAL_ATTEMPT_STATUSES)
    recovered_failure_attempts = sum(
        1 for row in attempt_failures if row["current_status"] == "verified_ready"
    )

    unavailable = [
        "eligible_to_attempt_conversion: ingress does not persist mode-bound attempt correlation",
        "exact_ingress_redelivery_rate: duplicate deliveries are not durably counted",
        "model_time_to_first_token: the Hermes result is currently buffered",
        "provider_oauth_model_failure_attribution: Hermes does not return bounded structured provider/auth failures",
        "intake_deduplication_rate: reservation reuse is not durably counted",
        "duplicate_effect_rate: reconciliation does not attest complete duplicate-effect observation",
        "spec_quality_and_human_approval_time: no canonical producer is connected yet",
    ]
    if not ingress["available"]:
        unavailable.append("intake_and_github_outcomes: ingress ledger is unavailable or incompatible")
    if not lifecycle["available"]:
        unavailable.append("lifecycle_delivery: lifecycle ledger is unavailable or incompatible")

    return {
        "schema": ANALYTICS_SCHEMA,
        "generated_at": ended_at,
        "window": {
            "days": days,
            "started_at": started_at,
            "ended_at": ended_at,
            "cohort": "non_legacy_real_attempt_created_at",
            "outcomes_observed_through": ended_at,
            "mutable_state": "current_at_bridge_read_snapshot",
        },
        "coverage": {
            "bridge_available": True,
            "ingress_available": ingress["available"],
            "lifecycle_available": lifecycle["available"],
            "cohort_attempts": len(cohort),
            "cohort_work_items": len(work_items),
            "dry_run_attempts_excluded": sum(1 for row in attempts if row["mode"] == "dry-run"),
            "legacy_attempts_excluded": sum(1 for row in attempts if row["actor"] == "legacy-import"),
            "execution_claimed_attempts": claimed_attempts,
            "effect_authorized_attempts": authorized_attempts,
            "backend_attribution": _coverage(attributed_attempts, authorized_attempts),
            "configured_auth_label": _coverage(configured_auth_attempts, attributed_attempts),
            "attempt_failure_attribution": _coverage(attributed_failures, len(attempt_failures)),
            "duplicate_effect_observation": _coverage(0, committed_real_effects, complete=False),
        },
        "intake": ingress,
        "funnel": funnel,
        "outcomes": {
            "attempt_status_grain": "attempt",
            "by_status": _counter(status_counts),
            "terminal_attempts": terminal_status_attempts,
            "active_attempts": len(cohort) - terminal_status_attempts,
            "right_censored": terminal_status_attempts != len(cohort),
            "github_grain": "unique_pr_url",
            "merged_prs": github_outcomes.get("merged_prs"),
            "closed_unmerged_prs": github_outcomes.get("closed_unmerged_prs"),
            "github_observation_coverage": github_outcomes.get("coverage"),
        },
        "latency_ms": latency,
        "failures": {
            "currently_failed_attempts": status_counts.get("failed", 0),
            "attempts_with_failure_evidence": len(attempt_failures),
            "recovered_failure_attempts": recovered_failure_attempts,
            "by_classification": _classified_failures(attempt_failures),
            "backend_invocations": invocation_failures,
            "dispatch_boundaries": dispatch_failures,
            "effect_commit_boundaries": commit_failures,
        },
        "recovery": {
            "by_attempt_kind": {
                key: {"attempts": reservation_counts.get(key, 0), "verified": verified_by_kind.get(key, 0)}
                for key in ("initial", "retry", "absence_retry", "repair", "unknown")
            },
            "repair_budgets_exhausted": repair_budgets,
        },
        "safety": {
            "bridge_submit_deduplicated_events": deduplicated,
            "unknown_effect_outcomes": unknown_effects,
            "observed_receipt_commit_recovery_incidents": receipt_recovery_incidents,
            "unresolved_observed_receipts": unresolved_receipts,
            "observed_receipt_recovery_exhausted": exhausted_receipt_recovery,
            "rejected_observed_receipts": rejected_receipts,
            "duplicate_effects_detected": None,
            "duplicate_effect_observation_complete": False,
            "reconciliation_duplicate_reference_findings": findings,
        },
        "human": {
            "work_items_ready_for_review": stages["verified_ready"],
            "ready_for_review_grain": "unique_work_item",
            "exception_intervention_attempts": len(interventions),
            "exception_intervention_grain": "attempt",
        },
        "backends": backend_rows,
        "lifecycle": lifecycle,
        "unavailable_metrics": unavailable,
    }


def _open_readonly(path: str) -> sqlite3.Connection:
    file = Path(path)
    if not file.is_file():
        raise OSError(f"analytics source does not exist: {path}")
    conn = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _attempt_events(conn: sqlite3.Connection, start: str, end: str, cohort_ids: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.attempt_id, e.event_type, e.details_json, e.created_at
        FROM factory_attempt_events e
        JOIN factory_attempts a ON a.attempt_id = e.attempt_id
        WHERE a.created_at >= ? AND a.created_at <= ? AND e.created_at <= ?
        ORDER BY e.created_at, e.event_id
        """,
        (start, end, end),
    )
    return [
        {**dict(row), "details": _json_object(row["details_json"])}
        for row in rows if row["attempt_id"] in cohort_ids
    ]


def _bridge_events(conn: sqlite3.Connection, start: str, end: str, cohort_jobs: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.job_id, e.event_type, e.state, e.details_json, e.created_at
        FROM bridge_events e
        JOIN factory_attempts a ON a.job_id = e.job_id
        WHERE a.created_at >= ? AND a.created_at <= ? AND e.created_at <= ?
        ORDER BY e.created_at, e.event_id
        """,
        (start, end, end),
    )
    return [
        {**dict(row), "details": _json_object(row["details_json"])}
        for row in rows if row["job_id"] in cohort_jobs
    ]


def _cohort_findings(conn: sqlite3.Connection, cohort_jobs: set[str]) -> int:
    if not cohort_jobs or not _table_columns(conn, "reconciliation_findings"):
        return 0
    return sum(
        1 for row in conn.execute(
            "SELECT job_id, kind FROM reconciliation_findings WHERE kind IN ('duplicate_bridge_branch','duplicate_bridge_pr_url')"
        ) if row["job_id"] in cohort_jobs
    )


def _ingress_report(path: str, start: str, end: str, pr_urls: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    empty = {
        "available": False,
        "linear_events": None,
        "eligible_events": None,
        "eligible_work_items": None,
        "rejected_events": None,
        "published_events": None,
        "unpublished_events": None,
        "rejection_reasons": None,
    }
    outcomes = {"merged_prs": None, "closed_unmerged_prs": None, "coverage": None}
    try:
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error):
        return empty, outcomes
    try:
        conn.execute("BEGIN")
        required = {
            "event_id", "source", "subject", "normalized_json", "decision",
            "reason_code", "created_at", "hatchet_published", "provider_updated_at_us",
        }
        if not required <= _table_columns(conn, "ingress_events"):
            return empty, outcomes
        rows = list(conn.execute(
            """
            SELECT source, subject, normalized_json, decision, reason_code, hatchet_published, created_at
            FROM ingress_events WHERE created_at >= ? AND created_at <= ?
            """,
            (start, end),
        ))
        linear = [row for row in rows if row["source"] == "linear"]
        eligible = [row for row in linear if row["decision"] == "ready_for_implementation"]
        rejected = [row for row in linear if row["decision"] != "ready_for_implementation"]
        report = {
            "available": True,
            "linear_events": len(linear),
            "eligible_events": len(eligible),
            "eligible_work_items": len({row["subject"] for row in eligible}),
            "rejected_events": len(rejected),
            "published_events": sum(1 for row in linear if row["hatchet_published"]),
            "unpublished_events": sum(1 for row in linear if not row["hatchet_published"]),
            "rejection_reasons": _counter(Counter(str(row["reason_code"]) for row in rejected)),
        }

        if not pr_urls:
            return report, {"merged_prs": 0, "closed_unmerged_prs": 0, "coverage": _coverage(0, 0)}
        latest: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT normalized_json, provider_updated_at_us, event_id FROM ingress_events WHERE source='github' AND created_at <= ? ORDER BY provider_updated_at_us, event_id",
            (end,),
        ):
            normalized = _json_object(row["normalized_json"])
            url = normalized.get("prUrl")
            if url in pr_urls:
                latest[str(url)] = normalized
        observed = set(latest)
        outcomes = {
            "merged_prs": sum(1 for row in latest.values() if row.get("merged") is True),
            "closed_unmerged_prs": sum(1 for row in latest.values() if row.get("state") == "closed" and row.get("merged") is not True),
            "coverage": _coverage(len(observed), len(pr_urls)),
        }
        return report, outcomes
    except (sqlite3.Error, ValueError, TypeError):
        return empty, outcomes
    finally:
        conn.close()


def _lifecycle_report(path: str, start: str, end: str, cohort_jobs: set[str]) -> dict[str, Any]:
    empty = {
        "available": False,
        "cohort_records": None,
        "by_state": None,
        "current_dead_letters": None,
        "dead_letter_event_reasons": None,
        "dead_letter_delivery_errors": None,
    }
    try:
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error):
        return empty
    try:
        conn.execute("BEGIN")
        required = {"job_id", "delivery_state", "reason_code", "last_error_code", "first_seen_at"}
        if not required <= _table_columns(conn, "lifecycle_outbox"):
            return empty
        cohort_rows = [
            row for row in conn.execute("SELECT job_id, delivery_state, reason_code, first_seen_at FROM lifecycle_outbox WHERE first_seen_at <= ?", (end,))
            if row["job_id"] in cohort_jobs
        ]
        dead = list(conn.execute(
            "SELECT reason_code, last_error_code FROM lifecycle_outbox WHERE delivery_state='dead_letter'"
        ))
        return {
            "available": True,
            "cohort_records": len(cohort_rows),
            "by_state": _counter(Counter(str(row["delivery_state"]) for row in cohort_rows)),
            "current_dead_letters": len(dead),
            "dead_letter_event_reasons": _counter(
                Counter(str(row["reason_code"] or "unknown") for row in dead)
            ),
            "dead_letter_delivery_errors": _counter(
                Counter(str(row["last_error_code"]) for row in dead if row["last_error_code"])
            ),
        }
    except (sqlite3.Error, TypeError):
        return empty
    finally:
        conn.close()


def _backends(
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    attributed: set[str] = set()
    configured_auth: set[str] = set()
    claimed = {
        row["attempt_id"] for row in attempts
        if _has_event(events_by_attempt[row["attempt_id"]], "attempt_execution_claimed")
    }
    authorized = {
        row["attempt_id"] for row in attempts
        if _has_event(events_by_attempt[row["attempt_id"]], "effect_authorized")
    }
    for attempt in attempts:
        telemetry = [
            event for event in events_by_attempt[attempt["attempt_id"]]
            if event["event_type"] in {
                "execution_backend_invoked", "execution_backend_completed",
                "execution_backend_failed", "execution_effect_commit_failed",
            }
        ]
        if attempt.get("job_id"):
            telemetry.extend(
                event for event in bridge_by_job[attempt["job_id"]]
                if event["event_type"] == "transition:effect_committed" and event["details"].get("execution_backend")
            )
        if not telemetry:
            continue
        attributed.add(attempt["attempt_id"])
        attempt_groups: set[tuple[str, str, str, str]] = set()
        invocation_started: dict[
            tuple[tuple[str, str, str, str], tuple[str, str]], datetime
        ] = {}
        for event in telemetry:
            details = event["details"]
            key = (
                _dimension(details.get("execution_backend")),
                _dimension(details.get("execution_transport")),
                _dimension(details.get("backend_version")),
                _dimension(details.get("configured_auth_mode")),
            )
            row = groups.setdefault(key, {
                "execution_backend": key[0], "execution_transport": key[1],
                "backend_version": key[2], "configured_auth_mode": key[3], "attempt_ids": set(),
                "invocations": set(), "completed": set(), "failed": set(),
                "completed_duration_samples": [], "failed_duration_samples": [],
            })
            row["attempt_ids"].add(attempt["attempt_id"]); attempt_groups.add(key)
            if key[3] != "unknown":
                configured_auth.add(attempt["attempt_id"])
            invocation = details.get("invocation_id")
            if isinstance(invocation, str) and invocation:
                invocation_key = (attempt["attempt_id"], invocation)
                row["invocations"].add(invocation_key)
                marker = (key, invocation_key)
                if event["event_type"] == "execution_backend_invoked":
                    parsed = _parse_time(event["created_at"])
                    if parsed: invocation_started[marker] = parsed
                elif event["event_type"] == "execution_backend_completed":
                    row["completed"].add(invocation_key)
                    elapsed = details.get("elapsed_ms")
                    if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                        row["completed_duration_samples"].append(elapsed)
                    else:
                        started = invocation_started.get(marker); ended = _parse_time(event["created_at"])
                        if started and ended and ended >= started: row["completed_duration_samples"].append((ended - started).total_seconds() * 1000)
                elif event["event_type"] == "execution_backend_failed":
                    row["failed"].add(invocation_key)
                    elapsed = details.get("elapsed_ms")
                    if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                        row["failed_duration_samples"].append(elapsed)
                elif event["event_type"] == "execution_effect_commit_failed":
                    if invocation_key not in row["completed"]:
                        row["completed"].add(invocation_key)
                        elapsed = details.get("elapsed_ms")
                        if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                            row["completed_duration_samples"].append(elapsed)
                elif event["event_type"] == "transition:effect_committed":
                    if invocation_key not in row["completed"]:
                        row["completed"].add(invocation_key)
                        elapsed = details.get("backend_elapsed_ms")
                        if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                            row["completed_duration_samples"].append(elapsed)
        for key in attempt_groups:
            groups[key].setdefault("verified_attempt_ids", set())
            groups[key].setdefault("unknown_attempt_ids", set())
            groups[key].setdefault("commit_recovery_attempt_ids", set())
            if _has_event(events_by_attempt[attempt["attempt_id"]], "attempt_verified_ready"):
                groups[key]["verified_attempt_ids"].add(attempt["attempt_id"])
            if any(
                event["event_type"] == "effect_authorized_outcome_unknown"
                and event["details"].get("effect_outcome") != "observed"
                for event in events_by_attempt[attempt["attempt_id"]]
            ):
                groups[key]["unknown_attempt_ids"].add(attempt["attempt_id"])
            if _has_event(events_by_attempt[attempt["attempt_id"]], "execution_effect_commit_failed"):
                groups[key]["commit_recovery_attempt_ids"].add(attempt["attempt_id"])
    result = []
    for key in sorted(groups):
        row = groups[key]
        result.append({
            "execution_backend": row["execution_backend"],
            "execution_transport": row["execution_transport"],
            "backend_version": row["backend_version"],
            "configured_auth_mode": row["configured_auth_mode"],
            "attempts": len(row["attempt_ids"]),
            "invocations": len(row["invocations"]),
            "completed_invocations": len(row["completed"]),
            "failed_invocations": len(row["failed"]),
            "verified_attempts": len(row.get("verified_attempt_ids", set())),
            "unknown_effect_attempts": len(row.get("unknown_attempt_ids", set())),
            "observed_receipt_recovery_attempts": len(row.get("commit_recovery_attempt_ids", set())),
            "completed_invocation_latency_ms": _distribution(row["completed_duration_samples"]),
            "failed_invocation_latency_ms": _distribution(row["failed_duration_samples"]),
        })
    attributed_authorized = attributed & authorized
    return (
        result,
        len(attributed_authorized),
        len(authorized),
        len(claimed),
        len(configured_auth & attributed_authorized),
    )


def _attempt_failures(attempts: list[dict[str, Any]], bridge_by_job: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for attempt in attempts:
        events = [event for event in bridge_by_job.get(attempt.get("job_id"), []) if event["event_type"] == "transition:failed"]
        if not events and attempt["status"] != "failed":
            continue
        details = events[-1]["details"] if events else {}
        result.append({
            "attempt_id": attempt["attempt_id"],
            "current_status": attempt["status"],
            "failure_domain": _dimension(details.get("failure_domain")),
            "failure_stage": _dimension(details.get("failure_stage")),
            "reason_code": _dimension(details.get("reason_code") or attempt.get("job_reason_code")),
            "retryable": details.get("retryable") if isinstance(details.get("retryable"), bool) else None,
            "failure_disposition": _dimension(details.get("failure_disposition")),
            "operator_intervention_required": details.get("operator_intervention_required") if isinstance(details.get("operator_intervention_required"), bool) else None,
        })
    return result


def _execution_failures(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    failures: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != event_type:
            continue
        details = event["details"]
        invocation = details.get("invocation_id")
        if isinstance(invocation, str) and invocation:
            failures[(event["attempt_id"], invocation)] = details
    buckets = Counter(
        (_dimension(row.get("failure_domain")), _dimension(row.get("failure_stage")), _dimension(row.get("reason_code")))
        for row in failures.values()
    )
    return {
        "failed": len(failures),
        "by_classification": [
            {"failure_domain": key[0], "failure_stage": key[1], "reason_code": key[2], "count": count}
            for key, count in sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def _classified_failures(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter((row["failure_domain"], row["failure_stage"], row["reason_code"]) for row in rows)
    return [
        {"failure_domain": key[0], "failure_stage": key[1], "reason_code": key[2], "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _latencies(
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    values: dict[str, list[float]] = defaultdict(list)
    for attempt in attempts:
        events = events_by_attempt[attempt["attempt_id"]]
        created = _parse_time(attempt["created_at"])
        started = _event_time(events, "attempt_started")
        effect = _effect_time(attempt, events, bridge_by_job)
        verified = _event_time(events, "attempt_verified_ready")
        terminal = min((time for event in events if event["event_type"] in TERMINAL_ATTEMPT_EVENTS if (time := _parse_time(event["created_at"]))), default=None)
        if terminal is None and attempt["status"] in TERMINAL_ATTEMPT_STATUSES:
            terminal = _parse_time(attempt.get("updated_at"))
        _duration(values["attempt_to_executor_started"], created, started)
        _duration(values["executor_started_to_effect_committed"], started, effect)
        _duration(values["effect_committed_to_verified_ready"], effect, verified)
        _duration(values["attempt_to_terminal"], created, terminal)
    return {key: _distribution(values[key]) for key in (
        "attempt_to_executor_started",
        "executor_started_to_effect_committed",
        "effect_committed_to_verified_ready",
        "attempt_to_terminal",
    )}


def _duration(target: list[float], start: datetime | None, end: datetime | None) -> None:
    if start and end and end >= start:
        target.append((end - start).total_seconds() * 1000)


def _distribution(values: Iterable[float]) -> dict[str, int | None]:
    ordered = sorted(max(0, round(value)) for value in values if math.isfinite(value) and value >= 0)
    return {
        "samples": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
    }


def _percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    return values[max(0, math.ceil(len(values) * quantile) - 1)]


def _coverage(observed: int, eligible: int, *, complete: bool | None = None) -> dict[str, Any]:
    return {
        "observed": observed,
        "eligible": eligible,
        "rate": _rate(observed, eligible),
        "complete": (observed == eligible) if complete is None else complete,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _attempt_kind(events: list[dict[str, Any]]) -> str:
    return next((RESERVATION_KINDS[event["event_type"]] for event in events if event["event_type"] in RESERVATION_KINDS), "unknown")


def _work_item_count(work_items: dict[str, list[dict[str, Any]]], events: dict[str, list[dict[str, Any]]], event_type: str) -> int:
    return sum(1 for rows in work_items.values() if any(_has_event(events[row["attempt_id"]], event_type) for row in rows))


def _effect_work_item_count(
    work_items: dict[str, list[dict[str, Any]]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> int:
    return sum(
        1 for rows in work_items.values()
        if any(_effect_committed(row, events_by_attempt[row["attempt_id"]], bridge_by_job) for row in rows)
    )


def _effect_committed(
    attempt: dict[str, Any],
    events: list[dict[str, Any]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> bool:
    if _has_event(events, "attempt_effect_committed"):
        return True
    return any(
        event["event_type"] == "transition:effect_committed"
        for event in bridge_by_job.get(attempt.get("job_id"), [])
    )


def _effect_time(
    attempt: dict[str, Any],
    events: list[dict[str, Any]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> datetime | None:
    direct = _event_time(events, "attempt_effect_committed")
    if direct is not None:
        return direct
    return next(
        (
            _parse_time(event["created_at"])
            for event in bridge_by_job.get(attempt.get("job_id"), [])
            if event["event_type"] == "transition:effect_committed"
        ),
        None,
    )


def _has_event(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(event["event_type"] == event_type for event in events)


def _event_time(events: list[dict[str, Any]], event_type: str) -> datetime | None:
    return next((_parse_time(event["created_at"]) for event in events if event["event_type"] == event_type), None)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _counter(value: Counter[Any]) -> dict[str, int]:
    return {str(key): value[key] for key in sorted(value, key=str)}


def _dimension(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _github_pr(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://github.com/") and "/pull/" in value else None
