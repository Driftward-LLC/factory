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
SUPPORTED_INGRESS_SCHEMA_VERSION = 3
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
ACTIVE_JOB_STATES = {
    "accepted", "running", "effect_authorized", "effect_committed",
    "verification_pending", "cancellation_requested",
}
RECOVERY_EVENTS = {
    "retry_reserved", "absence_retry_reserved", "repair_reserved",
    "execution_effect_commit_recovered", "execution_effect_commit_recovery_refreshed",
}
RECOVERY_MATURITY_DAYS = 7


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
        schema_row = bridge.execute(
            "SELECT schema_version, migrated_at FROM bridge_schema_meta WHERE singleton=1"
        ).fetchone()
        ended = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        started = ended - timedelta(days=days)
        started_at, ended_at = _timestamp(started), _timestamp(ended)
        attempt_source_rows = [dict(row) for row in bridge.execute(
            """
            SELECT a.*, j.state AS job_state, j.pr_url, j.reason_code AS job_reason_code,
                   j.requested_action AS job_requested_action,
                   j.payload_json AS job_payload_json, j.commit_sha AS job_commit_sha,
                   j.created_at AS job_created_at,
                   j.updated_at AS job_updated_at
            FROM factory_attempts a
            LEFT JOIN bridge_jobs j ON j.job_id = a.job_id
            ORDER BY a.created_at, a.attempt_id
            """
        )]
        all_attempts = [
            row for row in attempt_source_rows
            if (created_at := _parse_time(row.get("created_at"))) is not None
            and created_at <= ended
        ]
        window_attempts = [
            row for row in all_attempts
            if (created_at := _parse_time(row.get("created_at"))) is not None
            and created_at >= started
        ]
        all_real_attempts = [
            row for row in all_attempts
            if row["mode"] == "real" and row["actor"] != "legacy-import"
        ]
        cohort = [
            row for row in all_real_attempts
            if (created_at := _parse_time(row.get("created_at"))) is not None
            and created_at >= started
        ]
        cohort_ids = {row["attempt_id"] for row in cohort}
        cohort_jobs = {row["job_id"] for row in cohort if row.get("job_id")}
        all_ids = {row["attempt_id"] for row in all_attempts}
        all_jobs = {row["job_id"] for row in all_attempts if row.get("job_id")}

        all_attempt_events = _attempt_events(bridge, ended_at, all_ids)
        all_bridge_events = _bridge_events(bridge, ended_at, all_jobs)
        reconciliation = _reconciliation_report(bridge, ended, cohort_jobs)
        bridge_delivery = _bridge_delivery_report(bridge, ended, cohort_ids, cohort_jobs)
        bridge_source = _bridge_source_health(
            bridge, ended, int(schema_row["schema_version"]), str(schema_row["migrated_at"])
        )
        time_series = _daily_activity_series(bridge, attempt_source_rows, started, ended)
    finally:
        bridge.close()

    events_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in all_attempt_events:
        events_by_attempt[event["attempt_id"]].append(event)
    bridge_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in all_bridge_events:
        bridge_by_job[event["job_id"]].append(event)

    attempt_events = [event for event in all_attempt_events if event["attempt_id"] in cohort_ids]
    bridge_events = [event for event in all_bridge_events if event["job_id"] in cohort_jobs]

    kinds = {row["attempt_id"]: _attempt_kind(events_by_attempt[row["attempt_id"]]) for row in cohort}
    work_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in cohort:
        work_items[attempt["work_item_key"]].append(attempt)

    stages = {
        "attempted": len(work_items),
        "executor_started": _work_item_count(work_items, events_by_attempt, "attempt_started"),
        "effect_committed": _effect_work_item_count(work_items, events_by_attempt, bridge_by_job),
        "draft_pr_available": len({
            row["work_item_key"] for row in cohort if _attempt_pr_urls(row)
        }),
        "verified_ready": _work_item_count(work_items, events_by_attempt, "attempt_verified_ready"),
    }
    funnel = {
        "grain": "unique_work_item",
        "stages": {
            key: {"count": value, "rate_from_attempted": _rate(value, stages["attempted"])}
            for key, value in stages.items()
        },
    }

    attempt_failures = _attempt_failures(cohort, bridge_by_job, events_by_attempt)
    invocation_failures = _execution_failures(attempt_events, "execution_backend_failed")
    dispatch_failures = _execution_failures(attempt_events, "execution_dispatch_failed")
    commit_failures = _execution_failures(attempt_events, "execution_effect_commit_failed")
    backend_rows, attributed_attempts, authorized_attempts, claimed_attempts, configured_auth_attempts = _backends(
        cohort, events_by_attempt, bridge_by_job
    )
    routes = _route_report(cohort, all_real_attempts, events_by_attempt, bridge_by_job)
    status_counts = Counter(str(row["status"]) for row in cohort)
    pr_urls = {
        url for row in cohort for url in _attempt_pr_urls(row)
    }

    ingress, github_outcomes, ingress_source = _ingress_report(
        ingress_db, started, ended, pr_urls, all_attempts, cohort,
        events_by_attempt, bridge_by_job
    )
    merge_times = github_outcomes.pop("_merge_times", {})
    merged_pr_urls = set(github_outcomes.pop("_merged_pr_urls", []))
    lifecycle, lifecycle_source = _lifecycle_report(lifecycle_db, started, ended, cohort_jobs)
    latency = _latencies(cohort, events_by_attempt, bridge_by_job)
    recovery = _recovery_report(
        cohort, all_real_attempts, events_by_attempt, bridge_by_job, attempt_failures,
        merged_pr_urls, merge_times, github_outcomes.get("coverage"),
        github_outcomes.get("right_censored_prs"),
        github_outcomes.get("merged_prs"),
        github_outcomes.get("merge_timestamp_coverage"),
        github_outcomes.get("merged_prs") is not None, ended,
    )
    activity = _activity_report(
        all_real_attempts, events_by_attempt, bridge_by_job, started, ended
    )
    effort = _effort_report(cohort, all_real_attempts, events_by_attempt, bridge_by_job, ended)
    window_attempt_events = [
        event for event in all_attempt_events
        if (event_time := _parse_time(event["created_at"])) is not None
        and started <= event_time <= ended
    ]
    intake_reservations = {
        "grain": "durable_intake_decision_event",
        "new_attempts": sum(
            event["event_type"] == "intake_reserved" for event in window_attempt_events
        ),
        "request_deduplications": sum(
            event["event_type"] == "intake_request_deduplicated" for event in window_attempt_events
        ),
        "active_attempt_reuses": sum(
            event["event_type"] == "intake_active_job_reused" for event in window_attempt_events
        ),
    }
    ingress["reservation_decisions"] = intake_reservations

    reservation_counts = Counter(kinds.values())
    cohort_event_lists = [events_by_attempt[row["attempt_id"]] for row in cohort]
    verified_by_kind = Counter(
        kinds[row["attempt_id"]]
        for row in cohort
        if _has_event(events_by_attempt[row["attempt_id"]], "attempt_verified_ready")
    )
    repair_budgets = sum(
        1 for events in cohort_event_lists for event in events
        if event["event_type"] == "repair_budget_exhausted"
    )
    unknown_effects = sum(
        1 for events in cohort_event_lists
        if any(
            event["event_type"] == "effect_authorized_outcome_unknown"
            and event["details"].get("effect_outcome") != "observed"
            for event in events
        )
    )
    receipt_recovery_incidents = sum(
        1 for events in cohort_event_lists
        if _has_event(events, "execution_effect_commit_failed")
    )
    unresolved_receipts = sum(
        1 for row in cohort
        if row["status"] in {"effect_outcome_unknown", "cancellation_requested"}
        and _has_event(events_by_attempt[row["attempt_id"]], "execution_effect_commit_failed")
    )
    exhausted_receipt_recovery = sum(
        1 for events in cohort_event_lists
        if _has_event(events, "execution_effect_commit_recovery_exhausted")
    )
    rejected_receipts = sum(
        1 for events in cohort_event_lists
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
    recovered_failure_attempts = recovery["failed_attempts_eventually_verified"]
    backend_outcomes = Counter()
    for row in backend_rows:
        backend_outcomes.update(row["backend_outcomes"])

    unavailable = [
        "model_time_to_first_token: the Hermes result is currently buffered",
        "provider_oauth_model_failure_attribution: Hermes does not return bounded structured provider/auth failures",
        "duplicate_effect_rate: reconciliation does not attest complete duplicate-effect observation",
        "spec_quality_and_human_approval_time: no canonical producer is connected yet",
        "complete_human_time: recorded durations are self-reported; missing entries and unrecorded work remain unknown",
        "first_pass_ci_rate: check-suite observations are not the required-check aggregate bound to the initial head SHA",
        "bridge_conflict_rate: rejected bridge mutations are not durably audited",
        "retry_deduplication_rate: retry reservation reuse is not durably counted",
        "review_requested_to_review_time: no canonical review-request producer is connected",
        "human_rework_time: review observations do not identify or time the resulting code changes",
    ]
    if not ingress["available"]:
        unavailable.append("intake_and_github_outcomes: ingress ledger is unavailable or incompatible")
    elif not ingress["correlation"]["available"]:
        if not ingress.get("receipts", {}).get("signature_provenance_available"):
            unavailable.append(
                "authenticated_eligible_to_attempt_conversion: signature-verification receipt provenance is unavailable"
            )
        else:
            unavailable.append("eligible_to_attempt_conversion: ingress event_key is unavailable")
    if not ingress.get("receipts", {}).get("available"):
        unavailable.append("exact_ingress_redelivery_rate: ingress receipt audit is unavailable")
    elif not ingress["receipts"].get("full_window_rate_available"):
        unavailable.append(
            "exact_ingress_redelivery_rate: receipt instrumentation epoch or full-window coverage is incomplete; only the observed-receipt conditional rate is reported"
        )
    receipt_health = ingress.get("receipts", {})
    if ingress["available"] and not receipt_health.get("canonical_authenticity_available"):
        if not receipt_health.get("signature_provenance_available"):
            unavailable.append(
                "authenticated_provider_outcomes: ingress evidence does not persist signature-verification provenance"
            )
        else:
            unavailable.append(
                "authenticated_provider_outcomes: ingress event_key correlation is unavailable"
            )
    elif (
        receipt_health.get("signature_provenance_available")
        and not receipt_health.get("authenticated_event_coverage", {}).get("complete", False)
    ):
        unavailable.append(
            "authenticated_provider_outcomes_full_coverage: some ingress events lack signature-verified receipt evidence"
        )
    if ingress["available"] and ingress_source.get("schema_version") is None:
        unavailable.append(
            "ingress_schema_version: ingress does not persist an explicit durable schema version; compatibility is inferred from columns"
        )
    if not lifecycle["available"]:
        unavailable.append("lifecycle_delivery: lifecycle ledger is unavailable or incompatible")
    if not effort["execution"]["coverage"]["complete"]:
        unavailable.append("total_execution_time: some invocations are unfinished or lack duration or invocation evidence")

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
            "dry_run_attempts_excluded": sum(1 for row in window_attempts if row["mode"] == "dry-run"),
            "legacy_attempts_excluded": sum(1 for row in window_attempts if row["actor"] == "legacy-import"),
            "execution_claimed_attempts": claimed_attempts,
            "effect_authorized_attempts": authorized_attempts,
            "backend_attribution": _coverage(attributed_attempts, authorized_attempts),
            "configured_auth_label": _coverage(configured_auth_attempts, attributed_attempts),
            "execution_route_assignment": routes["assignment_coverage"],
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
            "open_prs": github_outcomes.get("open_prs"),
            "unobserved_prs": github_outcomes.get("unobserved_prs"),
            "terminal_observation_coverage": github_outcomes.get("terminal_coverage"),
            "right_censored_prs": github_outcomes.get("right_censored_prs"),
            "maturity": github_outcomes.get("maturity"),
        },
        "github": github_outcomes,
        "latency_ms": latency,
        "effort": effort,
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
            **recovery,
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
            "reconciliation_duplicate_reference_findings": reconciliation["duplicate_reference_findings"],
        },
        "human": {
            "work_items_ready_for_review": stages["verified_ready"],
            "ready_for_review_grain": "unique_work_item",
            "exception_intervention_attempts": len(interventions),
            "exception_intervention_grain": "attempt",
            "actual_interventions": None,
        },
        "backends": backend_rows,
        "backend_summary": {
            "backend_outcomes": _counter(backend_outcomes),
            "completed_invocations": sum(row["completed_invocations"] for row in backend_rows),
            "outcome_attributed_invocations": sum(
                row["backend_outcome_coverage"]["observed"] for row in backend_rows
            ),
        },
        "routes": routes,
        "models": _model_selection_report(cohort, events_by_attempt),
        "lifecycle": lifecycle,
        "activity": activity,
        "time_series": time_series,
        "delivery": {
            "ingress_publication": ingress.get("publication"),
            "factory_attempt_outbox": bridge_delivery["factory_attempt_outbox"],
            "factory_inspection_outbox": bridge_delivery["factory_inspection_outbox"],
            "lifecycle_outbox": lifecycle.get("delivery"),
        },
        "reconciliation": reconciliation,
        "sources": {
            "bridge": bridge_source,
            "ingress": ingress_source,
            "lifecycle": lifecycle_source,
        },
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


def _attempt_events(conn: sqlite3.Connection, end: str, attempt_ids: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.attempt_id, e.event_type, e.details_json, e.created_at
        FROM factory_attempt_events e
        JOIN factory_attempts a ON a.attempt_id = e.attempt_id
        WHERE a.created_at <= ? AND e.created_at <= ?
        ORDER BY e.created_at, e.event_id
        """,
        (end, end),
    )
    return [
        {**dict(row), "details": _json_object(row["details_json"])}
        for row in rows if row["attempt_id"] in attempt_ids
    ]


def _bridge_events(conn: sqlite3.Connection, end: str, job_ids: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.job_id, e.event_type, e.state, e.details_json, e.created_at
        FROM bridge_events e
        WHERE e.created_at <= ?
        ORDER BY e.created_at, e.event_id
        """,
        (end,),
    )
    return [
        {**dict(row), "details": _json_object(row["details_json"])}
        for row in rows if row["job_id"] in job_ids
    ]


def _reconciliation_report(
    conn: sqlite3.Connection, ended: datetime, cohort_jobs: set[str]
) -> dict[str, Any]:
    empty = {
        "available": False, "active_within_minutes": 30, "current": None,
        "historical": None, "total": None, "by_kind": None, "by_severity": None,
        "by_scope": None, "actionable": None, "occurrences": None,
        "last_seen_lag_ms": _measured_distribution([], 0), "duplicate_reference_findings": 0,
    }
    required = {
        "job_id", "subject", "kind", "details_json", "first_seen_at",
        "last_seen_at", "occurrence_count",
    }
    if not required <= _table_columns(conn, "reconciliation_findings"):
        return empty
    cutoff = ended - timedelta(minutes=30)
    rows = [dict(row) for row in conn.execute(
        "SELECT job_id,subject,kind,details_json,first_seen_at,last_seen_at,occurrence_count "
        "FROM reconciliation_findings WHERE first_seen_at <= ?",
        (_timestamp(ended),),
    )]
    job_states = {
        str(row["job_id"]): str(row["state"])
        for row in conn.execute("SELECT job_id,state FROM bridge_jobs")
    }
    current: list[dict[str, Any]] = []
    lag_values: list[float] = []
    severity = Counter()
    scope = Counter()
    kind = Counter()
    actionable = 0
    occurrences = 0
    for row in rows:
        details = _json_object(row["details_json"])
        last_seen = _parse_time(row["last_seen_at"])
        if last_seen and cutoff <= last_seen <= ended:
            current.append(row)
            level = _dimension(details.get("severity"))
            severity[level] += 1
            kind[str(row["kind"])] += 1
            if row["job_id"] is None:
                finding_scope = "system"
            elif job_states.get(str(row["job_id"])) in ACTIVE_JOB_STATES:
                finding_scope = "active_work"
            else:
                finding_scope = "terminal_work"
            scope[finding_scope] += 1
            if finding_scope != "terminal_work":
                actionable += 1
            _duration(lag_values, last_seen, ended)
            occurrences += int(row["occurrence_count"] or 0)
    duplicate = sum(
        1 for row in rows
        if row["job_id"] in cohort_jobs
        and row["kind"] in {"duplicate_bridge_branch", "duplicate_bridge_pr_url"}
    )
    return {
        "available": True,
        "active_within_minutes": 30,
        "current": len(current),
        "historical": len(rows) - len(current),
        "total": len(rows),
        "by_kind": _counter(kind),
        "by_severity": _counter(severity),
        "by_scope": _counter(scope),
        "actionable": actionable,
        "occurrences": occurrences,
        "last_seen_lag_ms": _measured_distribution(lag_values, len(current)),
        "duplicate_reference_findings": duplicate,
    }


def _ingress_report(
    path: str,
    started: datetime,
    ended: datetime,
    pr_urls: set[str],
    attempts: list[dict[str, Any]],
    cohort_attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    empty = {
        "available": False,
        "linear_events": None,
        "eligible_events": None,
        "authenticated_eligible_events": None,
        "unverified_eligible_events": None,
        "eligible_work_items": None,
        "authenticated_eligible_work_items": None,
        "rejected_events": None,
        "published_events": None,
        "unpublished_events": None,
        "rejection_reasons": None,
        "correlation": {"available": False},
        "unverified_observations": None,
        "publication": None,
        "receipts": {"available": False},
    }
    outcomes = {
        "merged_prs": None, "closed_unmerged_prs": None, "open_prs": None,
        "unobserved_prs": None, "right_censored_prs": None, "coverage": None,
        "terminal_coverage": None, "maturity": None,
    }
    unavailable_source = {
        "available": False, "schema_contract": "unavailable", "schema_version": None,
        "schema_version_source": "not_persisted", "row_count": None,
        "window_rows": None, "latest_recorded_at": None, "latest_activity_at": None,
        "lag_ms": None, "invalid_timestamp_rows": None,
        "invalid_normalized_json_rows": None, "by_source": {},
        "invalid_provider_timestamp_rows": None,
        "future_provider_timestamp_rows": None,
    }
    try:
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error):
        return empty, outcomes, unavailable_source
    try:
        conn.execute("BEGIN")
        columns = _table_columns(conn, "ingress_events")
        schema_version: int | None = None
        schema_migrated_at: str | None = None
        if {"singleton", "schema_version", "migrated_at"} <= _table_columns(
            conn, "ingress_schema_meta"
        ):
            schema_row = conn.execute(
                "SELECT schema_version,migrated_at FROM ingress_schema_meta WHERE singleton=1"
            ).fetchone()
            if schema_row is not None:
                schema_version = int(schema_row["schema_version"])
                schema_migrated_at = str(schema_row["migrated_at"])
        if (
            schema_version is not None
            and schema_version > SUPPORTED_INGRESS_SCHEMA_VERSION
        ):
            return empty, outcomes, {
                **unavailable_source,
                "schema_contract": "unsupported-forward-schema",
                "schema_version": schema_version,
                "schema_migrated_at": schema_migrated_at,
                "schema_version_source": "ingress_schema_meta",
                "schema_contract_basis": "durable_metadata",
            }
        required = {
            "event_id", "source", "subject", "normalized_json", "decision",
            "reason_code", "created_at", "hatchet_published", "provider_updated_at_us",
        }
        if not required <= columns:
            return empty, outcomes, unavailable_source
        optional = (
            "event_key", "provider_event", "action", "updated_at", "published_at",
            "publish_attempts", "publish_last_error", "provider_timestamp_source",
        )
        select = [*sorted(required), *(
            f"{name}" if name in columns else f"NULL AS {name}" for name in optional
        )]
        all_source_rows = [dict(row) for row in conn.execute(
            f"SELECT {','.join(select)} FROM ingress_events ORDER BY created_at,event_id"
        )]
        all_rows = [
            row for row in all_source_rows
            if (created_at := _parse_time(row.get("created_at"))) is not None
            and created_at <= ended
        ]
        rows = [
            row for row in all_rows
            if (_parse_time(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
            >= started
        ]
        linear = [row for row in rows if row["source"] == "linear"]
        eligible = [row for row in linear if row["decision"] == "ready_for_implementation"]
        rejected = [row for row in linear if row["decision"] != "ready_for_implementation"]
        receipts = _ingress_receipts(conn, started, ended)
        authenticated_event_keys = set(receipts.pop("_signature_verified_event_keys", []))
        signature_provenance_available = bool(
            receipts.get("signature_provenance_available")
        )
        event_key_join_available = "event_key" in columns
        authenticity_available = (
            signature_provenance_available and event_key_join_available
        )
        receipts["event_key_join_available"] = event_key_join_available
        receipts["canonical_authenticity_available"] = authenticity_available
        outcomes = _github_report(
            all_rows, pr_urls, cohort_attempts, events_by_attempt, bridge_by_job,
            authenticated_event_keys, authenticity_available, started, ended,
        )
        merge_times = outcomes.get("_merge_times", {})
        github_observations = outcomes.pop("_observations", {})
        github_states = outcomes.pop("_states", {})
        # The report-level GitHub cohort is intentionally limited to attempts
        # created inside the reporting window. Ready intake correlation has a
        # different grain: an authenticated Ready event can reuse an attempt
        # created before the window. Build a separate authenticated observation
        # projection for that correlation so a reused attempt's existing PR is
        # not falsely reported as unobserved, while leaving top-level outcomes
        # scoped to the attempt-creation cohort.
        ready_pr_urls = {
            url for attempt in attempts for url in _attempt_pr_urls(attempt)
        }
        # URL-set equality is not enough to reuse the cohort projection: a
        # pre-window Ready lineage can have an earlier durable association for
        # the same URL than the in-window attempt cohort. Always project Ready
        # against its own all-attempt association population.
        ready_outcomes = _github_report(
            all_rows, ready_pr_urls, attempts, events_by_attempt, bridge_by_job,
            authenticated_event_keys, authenticity_available, started, ended,
        )
        ready_merge_times = ready_outcomes.get("_merge_times", {})
        ready_github_observations = ready_outcomes.get("_observations", {})
        ready_github_states = ready_outcomes.get("_states", {})
        authenticated_eligible = [
            row for row in eligible if row.get("event_key") in authenticated_event_keys
        ]
        unverified_eligible = [
            row for row in eligible if row.get("event_key") not in authenticated_event_keys
        ]
        correlation = _ready_correlation(
            authenticated_eligible, attempts, events_by_attempt, bridge_by_job,
            ready_merge_times, ready_github_states, ready_github_observations,
            ended,
        ) if "event_key" in columns and authenticity_available else {"available": False}
        unverified_correlation = (
            _ready_correlation(
                unverified_eligible, attempts, events_by_attempt, bridge_by_job,
                {}, {}, {}, ended,
            )
            if "event_key" in columns else {"available": False}
        )
        if unverified_correlation.get("available"):
            unverified_correlation["authority"] = "observation_only_not_used_in_canonical_metrics"
        publication = _ingress_publication(all_rows, linear, eligible, ended)
        report = {
            "available": True,
            "linear_events": len(linear),
            "eligible_events": len(eligible),
            "authenticated_eligible_events": (
                len(authenticated_eligible) if authenticity_available else None
            ),
            "eligible_event_authenticity": (
                _coverage(len(authenticated_eligible), len(eligible))
                if authenticity_available else None
            ),
            "eligible_work_items": len({row["subject"] for row in eligible}),
            "authenticated_eligible_work_items": (
                len({row["subject"] for row in authenticated_eligible})
                if authenticity_available else None
            ),
            "unverified_eligible_events": (
                len(unverified_eligible) if authenticity_available else None
            ),
            "unknown_authenticity_eligible_events": (
                len(eligible) if not authenticity_available else 0
            ),
            "rejected_events": len(rejected),
            "published_events": sum(1 for row in linear if row["hatchet_published"]),
            "unpublished_events": sum(1 for row in linear if not row["hatchet_published"]),
            "rejection_reasons": _counter(Counter(str(row["reason_code"]) for row in rejected)),
            "repeated_eligible_events": max(
                0, len(eligible) - len({row["subject"] for row in eligible})
            ),
            "authenticated_repeated_eligible_events": (
                max(
                    0,
                    len(authenticated_eligible)
                    - len({row["subject"] for row in authenticated_eligible}),
                )
                if authenticity_available else None
            ),
            "repeated_eligible_event_rate": (
                _rate(
                    max(
                        0,
                        len(authenticated_eligible)
                        - len({row["subject"] for row in authenticated_eligible}),
                    ),
                    len(authenticated_eligible),
                )
                if authenticity_available else None
            ),
            "correlation": correlation,
            "unverified_observations": {
                "authority": (
                    "observation_only_not_used_in_canonical_metrics"
                    if authenticity_available
                    else "observation_only_authenticity_unknown"
                ),
                "eligible_events": (
                    len(unverified_eligible) if authenticity_available else None
                ),
                "eligible_work_items": (
                    len({row["subject"] for row in unverified_eligible})
                    if authenticity_available else None
                ),
                "unknown_authenticity_eligible_events": (
                    len(eligible) if not authenticity_available else 0
                ),
                "correlation": unverified_correlation,
            },
            "publication": publication,
            "receipts": receipts,
        }
        valid_times = [_parse_time(row["created_at"]) for row in all_source_rows]
        latest = max(
            (value for value in valid_times if value is not None and value <= ended),
            default=None,
        )
        activity_times = [
            _parse_time(row.get("updated_at")) or _parse_time(row.get("created_at"))
            for row in all_source_rows
        ]
        latest_activity = max(
            (value for value in activity_times if value is not None and value <= ended),
            default=None,
        )
        source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        window_source_counts = Counter(str(row["source"]) for row in rows)
        for row in all_source_rows:
            source_groups[str(row["source"])].append(row)
        by_source: dict[str, dict[str, Any]] = {}
        for name, source_rows in sorted(source_groups.items()):
            source_activity = [
                _parse_time(row.get("updated_at")) or _parse_time(row.get("created_at"))
                for row in source_rows
            ]
            source_latest = max(
                (
                    value for value in source_activity
                    if value is not None and value <= ended
                ),
                default=None,
            )
            source_created = [
                _parse_time(row.get("created_at")) for row in source_rows
            ]
            source_provider_times = [
                _provider_time(row.get("provider_updated_at_us"))
                for row in source_rows
                if row.get("provider_timestamp_source") == "provider"
            ]
            by_source[name] = {
                "row_count": len(source_rows),
                "window_rows": window_source_counts[name],
                "latest_activity_at": _timestamp(source_latest) if source_latest else None,
                "lag_ms": _elapsed_ms(source_latest, ended),
                "invalid_normalized_json_rows": sum(
                    not _is_json_object(row.get("normalized_json")) for row in source_rows
                ),
                "invalid_timestamp_rows": sum(
                    value is None for value in source_created
                ),
                "future_timestamp_rows": sum(
                    value is not None and value > ended for value in source_activity
                ),
                "invalid_provider_timestamp_rows": sum(
                    value is None for value in source_provider_times
                ),
                "future_provider_timestamp_rows": sum(
                    value is not None and value > ended
                    for value in source_provider_times
                ),
                "provider_timestamp_evidence": _coverage(
                    sum(
                        row.get("provider_timestamp_source") == "provider"
                        and (
                            provider_time := _provider_time(
                                row.get("provider_updated_at_us")
                            )
                        ) is not None
                        and provider_time <= ended
                        for row in source_rows
                    ),
                    len(source_rows),
                ),
            }
        inferred_contract = (
            "ingress-events-v3" if {
                "event_key", "publish_attempts", "published_at",
                "provider_timestamp_source",
            } <= columns
            else "ingress-events-v2" if {
                "event_key", "publish_attempts", "published_at"
            } <= columns
            else "legacy-compatible"
        )
        source = {
            "available": True,
            "schema_contract": (
                f"ingress-schema-v{schema_version}"
                if schema_version is not None else inferred_contract
            ),
            "schema_version": schema_version,
            "schema_migrated_at": schema_migrated_at,
            "schema_version_source": (
                "ingress_schema_meta" if schema_version is not None else "not_persisted"
            ),
            "schema_contract_basis": (
                "durable_metadata" if schema_version is not None else "inferred_columns"
            ),
            "column_coverage": _coverage(len(required | (set(optional) & columns)), len(required | set(optional))),
            "row_count": len(all_source_rows),
            "window_rows": len(rows),
            "latest_recorded_at": _timestamp(latest) if latest else None,
            "latest_activity_at": _timestamp(latest_activity) if latest_activity else None,
            "lag_ms": _elapsed_ms(latest_activity, ended),
            "invalid_timestamp_rows": sum(value is None for value in valid_times),
            "invalid_normalized_json_rows": sum(
                not _is_json_object(row.get("normalized_json")) for row in all_source_rows
            ),
            "future_timestamp_rows": sum(value is not None and value > ended for value in activity_times),
            "invalid_provider_timestamp_rows": sum(
                row.get("provider_timestamp_source") == "provider"
                and _provider_time(row.get("provider_updated_at_us")) is None
                for row in all_source_rows
            ),
            "future_provider_timestamp_rows": sum(
                row.get("provider_timestamp_source") == "provider"
                and (
                    provider_time := _provider_time(
                        row.get("provider_updated_at_us")
                    )
                ) is not None
                and provider_time > ended
                for row in all_source_rows
            ),
            "receipt_timestamp_health": {
                "available": bool(receipts.get("available")),
                "source_receipts": receipts.get("source_receipts"),
                "invalid_timestamp_rows": receipts.get("invalid_timestamp_rows"),
                "future_timestamp_rows": receipts.get("future_timestamp_rows"),
                "timestamp_coverage": receipts.get("timestamp_coverage"),
                "authentication_cutoff_rule": (
                    "only_receipts_with_valid_received_at_no_later_than_report_cutoff"
                ),
            },
            "invalid_receipt_timestamp_rows": receipts.get(
                "invalid_timestamp_rows"
            ),
            "future_receipt_timestamp_rows": receipts.get(
                "future_timestamp_rows"
            ),
            "by_source": by_source,
        }
        return report, outcomes, source
    except (sqlite3.Error, ValueError, TypeError):
        return empty, outcomes, unavailable_source
    finally:
        conn.close()


def _ready_correlation(
    eligible: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    merge_times: dict[str, datetime],
    github_states: dict[str, str],
    github_observations: dict[str, list[dict[str, Any]]],
    ended: datetime,
) -> dict[str, Any]:
    by_source_key: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for attempt in attempts:
        keys = {attempt.get("request_id")}
        payload = _json_object(attempt.get("job_payload_json"))
        keys.add(payload.get("source_event_key"))
        source_keys = payload.get("source_event_keys")
        if isinstance(source_keys, list):
            keys.update(item for item in source_keys if isinstance(item, str))
        for key in keys:
            if isinstance(key, str) and key:
                by_source_key[key][attempt["attempt_id"]] = attempt

    outcomes = Counter()
    linked: dict[str, dict[str, Any]] = {}
    for row in eligible:
        event_key = row.get("event_key")
        if not isinstance(event_key, str) or not event_key:
            outcomes["missing_event_key"] += 1
            continue
        matches = list(by_source_key.get(event_key, {}).values())
        real = [item for item in matches if item["mode"] == "real" and item["actor"] != "legacy-import"]
        dry_run = [item for item in matches if item["mode"] == "dry-run"]
        direct = [item for item in real if item.get("request_id") == event_key]
        received_at = _parse_time(row.get("created_at"))
        existing_real = [
            item for item in real
            if received_at is None
            or (_parse_time(item.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc)) <= received_at
        ]
        if len(direct) == 1:
            attempt = direct[0]
            outcome = "real_attempt_created"
            linked[event_key] = attempt
        elif len(direct) > 1 or len(existing_real) > 1:
            outcome = "ambiguous"
        elif len(existing_real) == 1:
            attempt = existing_real[0]
            outcome = "active_attempt_reused"
            linked[event_key] = attempt
        elif dry_run:
            outcome = "dry_run_only"
        elif real:
            outcome = "unlinked"
        elif matches:
            outcome = "legacy_only"
        else:
            outcome = "unlinked"
        if outcome in {"real_attempt_created", "active_attempt_reused"}:
            if str(attempt["work_item_key"]) != str(row["subject"]):
                linked.pop(event_key, None)
                outcome = "subject_mismatch"
        outcomes[outcome] += 1

    first_by_work_item: dict[str, dict[str, Any]] = {}
    for row in eligible:
        prior = first_by_work_item.get(str(row["subject"]))
        if prior is None or (
            str(row.get("created_at") or ""), str(row.get("event_id") or "")
        ) < (
            str(prior.get("created_at") or ""), str(prior.get("event_id") or "")
        ):
            first_by_work_item[str(row["subject"])] = row

    linked_roots: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in eligible:
        attempt = linked.get(str(row.get("event_key")))
        if attempt is not None and str(attempt["work_item_key"]) == str(row["subject"]):
            linked_roots[str(row["subject"])][attempt["attempt_id"]] = attempt
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        if isinstance(attempt.get("prior_attempt_id"), str):
            children[str(attempt["prior_attempt_id"])].append(attempt)
    lineages: dict[str, list[dict[str, Any]]] = {}
    for subject, roots in linked_roots.items():
        lineage = dict(roots)
        queue = list(roots.values())
        while queue:
            current = queue.pop(0)
            relatives = list(children.get(current["attempt_id"], []))
            parent_id = current.get("prior_attempt_id")
            if isinstance(parent_id, str) and parent_id in attempts_by_id:
                relatives.append(attempts_by_id[parent_id])
            for relative in relatives:
                if (
                    relative["attempt_id"] not in lineage
                    and str(relative["work_item_key"]) == subject
                ):
                    lineage[relative["attempt_id"]] = relative
                    queue.append(relative)
        lineages[subject] = list(lineage.values())

    latency_values: dict[str, list[float]] = defaultdict(list)
    latency_eligible = Counter()
    latency_suffixes = (
        "attempt_reserved", "executor_started", "effect_committed",
        "draft_pr_available", "verified_ready", "merged",
    )
    linked_work_items: set[str] = set()
    receipt_latency_starts: set[str] = set()
    provider_latency_starts: set[str] = set()
    for subject, row in first_by_work_item.items():
        lineage = lineages.get(subject, [])
        if not lineage:
            continue
        linked_work_items.add(subject)
        receipt_ready = _parse_time(row.get("created_at"))
        provider_ready = (
            _provider_time(row.get("provider_updated_at_us"))
            if row.get("provider_timestamp_source") == "provider"
            else None
        )
        if provider_ready is not None and provider_ready > ended:
            provider_ready = None
        if receipt_ready is not None:
            receipt_latency_starts.add(subject)
        if provider_ready is not None:
            provider_latency_starts.add(subject)
        reservation_times = [_parse_time(attempt.get("created_at")) for attempt in lineage]
        started_times = [
            _event_time(events_by_attempt[attempt["attempt_id"]], "attempt_started")
            for attempt in lineage
        ]
        effect_times = [
            _effect_time(
                attempt, events_by_attempt[attempt["attempt_id"]], bridge_by_job
            )
            for attempt in lineage
        ]
        verified_times = [
            _event_time(events_by_attempt[attempt["attempt_id"]], "attempt_verified_ready")
            for attempt in lineage
        ]
        pr_associations = _lineage_pr_association_times(
            lineage, events_by_attempt, bridge_by_job
        )
        pr_effect_times = list(pr_associations.values())
        merged_times = [
            merged_at
            for url, associated_at in pr_associations.items()
            if associated_at is not None
            and (merged_at := merge_times.get(url)) is not None
            and associated_at <= merged_at
            and (receipt_ready is None or receipt_ready <= merged_at)
        ]
        milestones = {
            "attempt_reserved": _first_at_or_after(reservation_times, receipt_ready),
            "executor_started": _first_at_or_after(started_times, receipt_ready),
            "effect_committed": _first_at_or_after(effect_times, receipt_ready),
            "verified_ready": _first_at_or_after(verified_times, receipt_ready),
            "draft_pr_available": _first_at_or_after(pr_effect_times, receipt_ready),
            "merged": _first_at_or_after(merged_times, receipt_ready),
        }
        for suffix in latency_suffixes:
            if receipt_ready is not None:
                latency_eligible[f"ready_received_to_{suffix}"] += 1
            if provider_ready is not None:
                latency_eligible[f"provider_ready_to_{suffix}"] += 1
        for suffix, milestone in milestones.items():
            if milestone is None:
                continue
            _duration(latency_values[f"ready_received_to_{suffix}"], receipt_ready, milestone)
            if provider_ready is not None:
                _duration(latency_values[f"provider_ready_to_{suffix}"], provider_ready, milestone)

    latency = {
        key: {
            **_distribution(values),
            "eligible": latency_eligible[key],
            "coverage_rate": _rate(len(values), latency_eligible[key]),
        }
        for key, values in sorted(latency_values.items())
    }
    for prefix in ("ready_received", "provider_ready"):
        for suffix in latency_suffixes:
            key = f"{prefix}_to_{suffix}"
            latency.setdefault(key, {
                **_distribution([]), "eligible": latency_eligible[key],
                "coverage_rate": _rate(0, latency_eligible[key]),
            })

    real_events = outcomes["real_attempt_created"] + outcomes["active_attempt_reused"]
    conversion = _ready_work_item_conversion(
        first_by_work_item, lineages, events_by_attempt,
        bridge_by_job, merge_times, github_states, github_observations, ended,
    )
    return {
        "available": True,
        "grain": "eligible_ingress_event_key",
        "evidence_scope": "caller_selected_ingress_receipt_provenance",
        "event_key_coverage": _coverage(
            sum(1 for row in eligible if isinstance(row.get("event_key"), str) and row["event_key"]),
            len(eligible),
        ),
        "by_outcome": _counter(outcomes),
        "eligible_to_real_attempt": _coverage(real_events, len(eligible)),
        "eligible_work_item_to_real_attempt": _coverage(
            len(linked_work_items), len(first_by_work_item)
        ),
        "work_item_conversion": conversion,
        "latency_start_coverage": {
            "factory_received_ready": _coverage(
                len(receipt_latency_starts), len(linked_work_items)
            ),
            "provider_ready": _coverage(
                len(provider_latency_starts), len(linked_work_items)
            ),
        },
        "active_attempt_reuse_events": outcomes["active_attempt_reused"],
        "active_attempt_reuse_rate": _rate(
            outcomes["active_attempt_reused"], len(eligible)
        ),
        "latency_ms": latency,
    }


def _ready_work_item_conversion(
    ready_by_work_item: dict[str, dict[str, Any]],
    lineages: dict[str, list[dict[str, Any]]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    merge_times: dict[str, datetime],
    github_states: dict[str, str],
    github_observations: dict[str, list[dict[str, Any]]],
    ended: datetime,
) -> dict[str, Any]:
    def summarize(subjects: set[str]) -> dict[str, Any]:
        linked = draft = verified = merged = merge_evidenced = 0
        active = terminal_unverified = 0
        github_open = github_closed = github_unobserved = right_censored = 0
        for subject in subjects:
            lineage = lineages.get(subject, [])
            if not lineage:
                continue
            linked += 1
            origin = _parse_time(ready_by_work_item[subject].get("created_at"))
            pr_urls = {
                url for attempt in lineage for url in _attempt_pr_urls(attempt)
            }
            associations = _lineage_pr_association_times(
                lineage, events_by_attempt, bridge_by_job
            )
            has_draft = bool(pr_urls)
            has_verified = any(
                _has_event(events_by_attempt[attempt["attempt_id"]], "attempt_verified_ready")
                or attempt["status"] == "verified_ready"
                for attempt in lineage
            )
            has_merged = any(
                associated_at is not None
                and (merged_at := merge_times.get(url)) is not None
                and associated_at <= merged_at
                and (origin is None or origin <= merged_at)
                for url, associated_at in associations.items()
            )
            has_active = any(
                attempt["status"] not in TERMINAL_ATTEMPT_STATUSES for attempt in lineage
            )
            states = [
                _github_state_at_or_after(
                    github_observations.get(url, []),
                    max(
                        value for value in (origin, associations.get(url))
                        if value is not None
                    ) if origin is not None or associations.get(url) is not None else None,
                    ended,
                    require_provider_time=True,
                )
                for url in pr_urls
            ]
            # Work-item GitHub buckets are mutually exclusive. A merge wins;
            # otherwise every candidate PR must be observed closed before the
            # lineage is called closed-unmerged. Any open PR wins over missing
            # evidence, while a closed+unknown mixture remains unobserved.
            is_open = not has_merged and "open" in states
            is_closed = (
                not has_merged
                and bool(pr_urls)
                and all(state == "closed_unmerged" for state in states)
            )
            is_github_unobserved = (
                not has_merged and bool(pr_urls) and not is_open and not is_closed
            )
            has_merge_outcome_evidence = (
                has_merged
                or (
                    not has_active
                    and (not pr_urls or is_closed)
                )
            )
            draft += has_draft
            verified += has_verified
            merged += has_merged
            merge_evidenced += has_merge_outcome_evidence
            active += has_active
            terminal_unverified += not has_active and not has_verified
            github_open += is_open
            github_closed += is_closed
            github_unobserved += is_github_unobserved
            if not has_merged and (has_active or is_open or is_github_unobserved):
                right_censored += 1
        eligible = len(subjects)
        right_censored += eligible - linked
        return {
            "eligible_work_items": eligible,
            "real_attempt": _coverage(linked, eligible),
            "draft_pr_available": _coverage(draft, eligible),
            "verified_ready": _coverage(verified, eligible),
            "merge_outcome_evidence": _coverage(merge_evidenced, eligible),
            "merged": _evidence_bounded_coverage(
                merged, eligible, merge_evidenced
            ),
            "active_factory_work_items": active,
            "terminal_without_verified": terminal_unverified,
            "github_open_work_items": github_open,
            "github_closed_unmerged_work_items": github_closed,
            "github_unobserved_work_items": github_unobserved,
            "uncorrelated_work_items": eligible - linked,
            "missing_correlation_evidence_work_items": eligible - linked,
            "right_censored_work_items": right_censored,
        }

    all_subjects = set(ready_by_work_item)
    mature_24h = {
        subject for subject, row in ready_by_work_item.items()
        if (ready_at := _parse_time(row.get("created_at"))) is not None
        and ended - ready_at >= timedelta(hours=24)
    }
    mature_7d = {
        subject for subject, row in ready_by_work_item.items()
        if (ready_at := _parse_time(row.get("created_at"))) is not None
        and ended - ready_at >= timedelta(days=7)
    }
    def summarize_within(subjects: set[str], horizon: timedelta) -> dict[str, Any]:
        linked = draft = verified = merged = merge_evidenced = 0
        for subject in subjects:
            origin = _parse_time(ready_by_work_item[subject].get("created_at"))
            lineage = lineages.get(subject, [])
            if origin is None or not lineage:
                continue
            linked += 1
            cutoff = origin + horizon
            raw_pr_urls = {
                url for attempt in lineage for url in _attempt_pr_urls(attempt)
            }
            associations = _lineage_pr_association_times(
                lineage, events_by_attempt, bridge_by_job
            )
            horizon_prs = {
                url: associated_at
                for url, associated_at in associations.items()
                if associated_at is not None and associated_at <= cutoff
            }
            prs_with_unknown_association = {
                url for url in raw_pr_urls if associations.get(url) is None
            }
            if horizon_prs:
                draft += 1
            if any(
                (verified_at := (
                    _event_time(
                        events_by_attempt[attempt["attempt_id"]], "attempt_verified_ready"
                    )
                    or (
                        _parse_time(attempt.get("updated_at"))
                        if attempt["status"] == "verified_ready" else None
                    )
                )) is not None
                and origin <= verified_at <= cutoff
                for attempt in lineage
            ):
                verified += 1
            merged_within = any(
                (merged_at := merge_times.get(url)) is not None
                and origin <= merged_at <= cutoff
                and associated_at <= merged_at
                for url, associated_at in horizon_prs.items()
            )
            if merged_within:
                merged += 1
                # A positive authenticated merge observation is sufficient
                # evidence even if another repair PR has no observation.
                merge_evidenced += 1
            elif not prs_with_unknown_association and all(
                any(
                    (observed_at := _provider_observation_time(row)) is not None
                    and cutoff <= observed_at <= ended
                    and _github_state(row["normalized"]) != "unknown"
                    for row in github_observations.get(url, [])
                )
                for url in horizon_prs
            ):
                # With no durable PR inside the horizon, Factory evidence is
                # itself sufficient to establish that no merge occurred.
                # Otherwise every candidate PR needs authenticated state at or
                # after the horizon boundary; an early open snapshot is not
                # converted into a measured non-merge.
                merge_evidenced += 1
        eligible = len(subjects)
        return {
            "eligible_age_qualified_work_items": eligible,
            "real_attempt": _coverage(linked, eligible),
            "missing_correlation_evidence_work_items": eligible - linked,
            "draft_pr_within_horizon": _coverage(draft, eligible),
            "verified_ready_within_horizon": _coverage(verified, eligible),
            "merge_outcome_evidence": _coverage(merge_evidenced, eligible),
            "merged_within_horizon": _evidence_bounded_coverage(
                merged, eligible, merge_evidenced
            ),
            "right_censored_work_items": eligible - merge_evidenced,
        }

    current_24h = summarize(mature_24h)
    current_7d = summarize(mature_7d)
    return {
        "grain": "unique_work_item_first_eligible_ready",
        "all": summarize(all_subjects),
        "maturity": {
            "basis": "age_qualified_cohort_with_current_outcome_observed_through_report_cutoff",
            "outcomes_observed_through": _timestamp(ended),
            "at_24h": current_24h,
            "at_7d": current_7d,
            "age_qualified_current_outcome": {
                "at_24h": current_24h,
                "at_7d": current_7d,
            },
            "outcome_within_horizon": {
                "within_24h": summarize_within(mature_24h, timedelta(hours=24)),
                "within_7d": summarize_within(mature_7d, timedelta(days=7)),
            },
        },
    }


def _first_at_or_after(
    values: Iterable[datetime | None], origin: datetime | None
) -> datetime | None:
    candidates = [
        value for value in values
        if value is not None and (origin is None or value >= origin)
    ]
    return min(candidates, default=None)


def _ingress_publication(
    all_rows: list[dict[str, Any]],
    window_linear: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    ended: datetime,
) -> dict[str, Any]:
    by_decision: dict[str, Counter[str]] = defaultdict(Counter)
    for row in window_linear:
        by_decision[str(row["decision"])][
            "published" if row["hatchet_published"] else "unpublished"
        ] += 1
    backlog = [row for row in all_rows if not row["hatchet_published"]]
    publish_latency: list[float] = []
    for row in window_linear:
        _duration(
            publish_latency,
            _parse_time(row.get("created_at")),
            _parse_time(row.get("published_at")),
        )
    attempts = [
        int(row.get("publish_attempts") or 0)
        for row in window_linear
        if isinstance(row.get("publish_attempts"), int)
    ]
    backlog_ages = [
        value for row in backlog
        if (value := _elapsed_ms(_parse_time(row.get("created_at")), ended)) is not None
    ]
    return {
        "grain": "ingress_event",
        "by_decision": {
            decision: {
                "published": counts["published"],
                "unpublished": counts["unpublished"],
            }
            for decision, counts in sorted(by_decision.items())
        },
        "eligible_publication": _coverage(
            sum(1 for row in eligible if row["hatchet_published"]), len(eligible)
        ),
        "publication_attempts": sum(attempts),
        "retried_events": sum(value > 1 for value in attempts),
        "publish_latency_ms": _measured_distribution(
            publish_latency, sum(bool(row["hatchet_published"]) for row in window_linear)
        ),
        "current_backlog": len(backlog),
        "current_backlog_by_source": _counter(Counter(str(row["source"]) for row in backlog)),
        "current_backlog_by_decision": _counter(Counter(str(row["decision"]) for row in backlog)),
        "current_eligible_backlog": sum(
            row["source"] == "linear" and row["decision"] == "ready_for_implementation"
            for row in backlog
        ),
        "current_backlog_with_error": sum(bool(row.get("publish_last_error")) for row in backlog),
        "current_backlog_age_ms": _age_distribution(backlog_ages, len(backlog)),
    }


def _ingress_receipts(
    conn: sqlite3.Connection, started: datetime, ended: datetime
) -> dict[str, Any]:
    required = {
        "receipt_id", "event_key", "source", "disposition", "received_at",
        "signature_verified",
    }
    if not required <= _table_columns(conn, "ingress_receipts"):
        return {
            "available": False,
            "signature_provenance_available": False,
            "_signature_verified_event_keys": [],
        }
    source_rows = [dict(row) for row in conn.execute(
        "SELECT receipt_id,event_key,source,disposition,received_at,signature_verified "
        "FROM ingress_receipts ORDER BY received_at,receipt_id"
    )]
    parsed_receipts = [
        (row, _parse_time(row.get("received_at"))) for row in source_rows
    ]
    all_rows = [
        row for row, received_at in parsed_receipts
        if received_at is not None and received_at <= ended
    ]
    rows = [
        row for row in all_rows
        if (received_at := _parse_time(row.get("received_at"))) is not None
        and received_at >= started
    ]
    counts = Counter(str(row["disposition"]) for row in rows)
    redeliveries = counts["duplicate"] + counts["payload_conflict"]
    verified_rows = [row for row in rows if row["signature_verified"] == 1]
    unverified_rows = [row for row in rows if row["signature_verified"] != 1]
    first_receipt = min(
        (_parse_time(row["received_at"]) for row in all_rows), default=None
    )
    coverage_start = max(started, first_receipt) if first_receipt else None
    eligible_event_keys: set[str] = set()
    window_event_keys: set[str] = set()
    if "event_key" in _table_columns(conn, "ingress_events"):
        window_event_keys = {
            str(row["event_key"]) for row in conn.execute(
                "SELECT event_key FROM ingress_events WHERE created_at >= ? AND created_at <= ?",
                (_timestamp(started), _timestamp(ended)),
            ) if isinstance(row["event_key"], str) and row["event_key"]
        }
        if coverage_start is not None:
            eligible_event_keys = {
                str(row["event_key"]) for row in conn.execute(
                    "SELECT event_key FROM ingress_events WHERE created_at >= ? AND created_at <= ?",
                    (_timestamp(coverage_start), _timestamp(ended)),
                ) if isinstance(row["event_key"], str) and row["event_key"]
            }
    observed_event_keys = {str(row["event_key"]) for row in all_rows}
    authenticated_event_keys = {
        str(row["event_key"]) for row in all_rows
        if row["signature_verified"] == 1
        and row["disposition"] in {"accepted", "duplicate"}
    }
    event_coverage = _coverage(
        len(eligible_event_keys & observed_event_keys), len(eligible_event_keys)
    )
    full_window_available = bool(
        first_receipt is not None
        and first_receipt <= started
        and event_coverage["complete"]
    )
    observed_rate = _rate(redeliveries, len(rows))
    def provenance_bucket(bucket: list[dict[str, Any]]) -> dict[str, Any]:
        bucket_counts = Counter(str(row["disposition"]) for row in bucket)
        bucket_redeliveries = bucket_counts["duplicate"] + bucket_counts["payload_conflict"]
        return {
            "receipts": len(bucket),
            "unique_event_keys": len({str(row["event_key"]) for row in bucket}),
            "by_disposition": _counter(bucket_counts),
            "redeliveries": bucket_redeliveries,
            "observed_receipt_redelivery_rate": _rate(bucket_redeliveries, len(bucket)),
        }
    return {
        "available": True,
        "signature_provenance_available": True,
        "grain": "webhook_receipt",
        "source_receipts": len(source_rows),
        "invalid_timestamp_rows": sum(
            received_at is None for _row, received_at in parsed_receipts
        ),
        "future_timestamp_rows": sum(
            received_at is not None and received_at > ended
            for _row, received_at in parsed_receipts
        ),
        "timestamp_coverage": _coverage(
            sum(received_at is not None for _row, received_at in parsed_receipts),
            len(source_rows),
        ),
        "instrumentation_observed": first_receipt is not None,
        "instrumentation_first_observed_at": _timestamp(first_receipt) if first_receipt else None,
        "coverage_scope": "ingress_events_created_since_first_observed_receipt",
        "event_receipt_coverage": event_coverage,
        "authenticated_event_coverage": _coverage(
            len(window_event_keys & authenticated_event_keys), len(window_event_keys)
        ),
        "full_window_rate_available": full_window_available,
        "conditional_rate_window_started_at": _timestamp(coverage_start) if coverage_start else None,
        "provenance_stratification_available": True,
        "by_authenticity": {
            "signature_verified": provenance_bucket(verified_rows),
            "unverified": provenance_bucket(unverified_rows),
        },
        "receipts": len(rows),
        "unique_event_keys": len({str(row["event_key"]) for row in rows}),
        "by_disposition": _counter(counts),
        "by_source": _counter(Counter(str(row["source"]) for row in rows)),
        "signature_verified_receipts": len(verified_rows),
        "unverified_receipts": len(unverified_rows),
        "signature_verified_event_keys": len({
            str(row["event_key"]) for row in rows if row["signature_verified"] == 1
        }),
        "redeliveries": redeliveries,
        "redelivery_rate": observed_rate if full_window_available else None,
        "observed_receipt_redelivery_rate": observed_rate,
        "_signature_verified_event_keys": sorted(authenticated_event_keys),
    }


def _github_report(
    rows: list[dict[str, Any]],
    pr_urls: set[str],
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    authenticated_event_keys: set[str],
    authenticity_available: bool,
    started: datetime,
    ended: datetime,
) -> dict[str, Any]:
    if not pr_urls:
        return {
            "authenticated_outcomes_available": authenticity_available,
            "future_observations_excluded": 0,
            "pre_association_observations_excluded": 0,
            "grain": "unique_pr_url", "merged_prs": 0, "closed_unmerged_prs": 0,
            "open_prs": 0, "unknown_state_prs": 0, "unobserved_prs": 0,
            "right_censored_prs": 0,
            "provider_timed_merged_prs": 0,
            "merge_timestamp_coverage": _coverage(0, 0),
            "closed_unmerged_rate": None,
            "closed_unmerged_rate_denominator": "authenticated_terminal_prs",
            "closed_unmerged_rates": _closed_unmerged_rates(0, 0, 0, 0),
            "coverage": _coverage(0, 0), "terminal_coverage": _coverage(0, 0),
            "observation_freshness_ms": _measured_distribution([], 0),
            "authenticity": {
                "available": authenticity_available,
                "scope": "signature_verified_receipts_only",
                "matching_observations": 0,
                "authenticated_matching_observations": 0,
                "authenticated_observations": 0,
                "unverified_matching_observations": 0,
                "event_coverage": _coverage(0, 0),
                "pr_coverage": _coverage(0, 0),
            },
            "unverified_observations": {
                "authority": "observation_only_not_used_in_canonical_metrics",
                "matching_events": 0, "unique_prs": 0,
                "by_kind": {}, "pull_request_state_events": {},
            },
            "maturity_origin": {
                "kind": "first_durable_pr_association", "coverage": _coverage(0, 0),
            },
            "maturity": {
                "basis": "pr_effect_age_qualified_cohort_with_current_outcome_observed_through_report_cutoff",
                "outcomes_observed_through": _timestamp(ended),
                "at_24h": _maturity({}, set()), "at_7d": _maturity({}, set()),
                "age_qualified_current_outcome": {
                    "at_24h": _maturity({}, set()), "at_7d": _maturity({}, set()),
                },
                "outcome_within_horizon": {
                    "within_24h": _github_within_horizon(
                        {}, {}, set(), timedelta(hours=24), ended
                    ),
                    "within_7d": _github_within_horizon(
                        {}, {}, set(), timedelta(days=7), ended
                    ),
                },
            },
            "check_suite_observations": {
                "available": authenticity_available, "coverage": _coverage(0, 0),
                "observations": 0,
                "by_conclusion": {}, "head_sha_binding": _coverage(0, 0),
            },
            "first_pass_checks": {
                "available": False, "success_rate": None,
                "reason": "required check-set aggregate bound to the initial head SHA is unavailable",
            },
            "reviews": {
                "available": authenticity_available, "coverage": _coverage(0, 0), "observations": 0,
                "by_state": {}, "changes_requested_prs": 0,
                "changes_requested_observations": 0, "by_actor_kind": {},
                "changes_requested_by_actor_kind": {},
                "changes_requested_rate": None,
                "changes_requested_observed_review_rate": None,
                "changes_requested_all_eligible_pr_rate": None,
                "changes_requested_rate_denominator": "prs_with_review_observation",
            },
            "_merge_times": {},
            "_observations": {},
            "_states": {},
            "_merged_pr_urls": [],
        }
    origin_by_url: dict[str, datetime] = {}
    expected_heads: dict[str, set[str]] = defaultdict(set)
    for attempt in attempts:
        urls = _attempt_pr_urls(attempt) & pr_urls
        associations = _lineage_pr_association_times(
            [attempt], events_by_attempt, bridge_by_job
        ) if urls else {}
        for url, associated_at in associations.items():
            if (
                associated_at is not None
                and (url not in origin_by_url or associated_at < origin_by_url[url])
            ):
                origin_by_url[url] = associated_at
        commit_sha = attempt.get("job_commit_sha")
        if isinstance(commit_sha, str) and commit_sha:
            for url in urls:
                expected_heads[url].add(commit_sha)

    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    checks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reviews: dict[str, list[dict[str, Any]]] = defaultdict(list)
    merge_times: dict[str, datetime] = {}
    matching_observations = 0
    authenticated_matching_observations = 0
    unverified_matching_observations = 0
    future_matching_observations = 0
    pre_association_matching_observations = 0
    authenticated_prs: set[str] = set()
    unverified_prs: set[str] = set()
    unverified_kinds: Counter[str] = Counter()
    unverified_pr_states: Counter[str] = Counter()
    for row in rows:
        if row["source"] != "github":
            continue
        normalized = _json_object(row["normalized_json"])
        url = normalized.get("prUrl")
        if url not in pr_urls:
            continue
        enriched = {**row, "normalized": normalized}
        matching_observations += 1
        reason = str(row.get("reason_code") or "")
        if reason == "github_checks_completed":
            kind = "check_suite"
        elif reason == "github_review_submitted":
            kind = "review"
        elif "state" in normalized or "merged" in normalized:
            kind = "pull_request_state"
        else:
            kind = "other"
        if row.get("event_key") not in authenticated_event_keys:
            unverified_matching_observations += 1
            unverified_prs.add(str(url))
            unverified_kinds[kind] += 1
            if kind == "pull_request_state":
                unverified_pr_states[_github_state(normalized)] += 1
            continue
        authenticated_matching_observations += 1
        observed_at = _observation_time(enriched)
        if observed_at is not None and observed_at > ended:
            future_matching_observations += 1
            continue
        associated_at = origin_by_url.get(str(url))
        if (
            associated_at is None
            or observed_at is None
            or observed_at < associated_at
        ):
            pre_association_matching_observations += 1
            continue
        authenticated_prs.add(str(url))
        if reason == "github_checks_completed":
            checks[str(url)].append(enriched)
        elif reason == "github_review_submitted":
            reviews[str(url)].append(enriched)
        elif "state" in normalized or "merged" in normalized:
            observations[str(url)].append(enriched)
            if normalized.get("merged") is True:
                occurred_at = _provider_observation_time(enriched)
                if occurred_at and (
                    url not in merge_times or occurred_at < merge_times[str(url)]
                ):
                    merge_times[str(url)] = occurred_at

    latest = {
        url: max(items, key=_github_observation_order)
        for url, items in observations.items()
    }
    states = {
        url: _github_state(row["normalized"])
        for url, row in latest.items()
    }
    merged = sum(state == "merged" for state in states.values())
    closed = sum(state == "closed_unmerged" for state in states.values())
    opened = sum(state == "open" for state in states.values())
    observed_unknown = sum(state == "unknown" for state in states.values())
    unobserved = len(pr_urls) - len(latest)
    terminal = merged + closed
    current_merged_urls = {
        url for url, state in states.items() if state == "merged"
    }
    provider_timed_current_merged_urls = current_merged_urls & set(merge_times)
    freshness = [
        value for row in latest.values()
        if (value := _elapsed_ms(_observation_time(row), ended)) is not None
    ]

    if not authenticity_available:
        return {
            "authenticated_outcomes_available": False,
            "future_observations_excluded": future_matching_observations,
            "pre_association_observations_excluded": (
                pre_association_matching_observations
            ),
            "unavailable_reason": (
                "ingress receipt signature-verification provenance is unavailable"
            ),
            "grain": "unique_pr_url",
            "merged_prs": None,
            "closed_unmerged_prs": None,
            "open_prs": None,
            "unknown_state_prs": None,
            "unobserved_prs": None,
            "right_censored_prs": None,
            "provider_timed_merged_prs": None,
            "merge_timestamp_coverage": None,
            "closed_unmerged_rate": None,
            "closed_unmerged_rate_denominator": "authenticated_terminal_prs",
            "closed_unmerged_rates": None,
            "coverage": None,
            "terminal_coverage": None,
            "observation_freshness_ms": None,
            "authenticity": {
                "available": False,
                "scope": "signature_verified_receipts_only",
                "matching_observations": matching_observations,
                "authenticated_matching_observations": None,
                "authenticated_observations": None,
                "unverified_matching_observations": None,
                "unknown_authenticity_matching_observations": matching_observations,
                "event_coverage": None,
                "pr_coverage": None,
            },
            "unverified_observations": {
                "authority": "observation_only_authenticity_unknown",
                "matching_events": unverified_matching_observations,
                "unique_prs": len(unverified_prs),
                "by_kind": _counter(unverified_kinds),
                "pull_request_state_events": _counter(unverified_pr_states),
            },
            "maturity_origin": {
                "kind": "first_durable_pr_association", "coverage": None,
            },
            "maturity": {
                "available": False,
                "reason": "authenticated GitHub outcomes are unavailable",
            },
            "check_suite_observations": {
                "available": False,
                "coverage": None,
                "observations": None,
                "by_conclusion": None,
                "successful_observations": None,
                "head_sha_binding": None,
            },
            "first_pass_checks": {
                "available": False,
                "success_rate": None,
                "reason": "required check-set aggregate bound to the initial head SHA is unavailable",
            },
            "reviews": {
                "available": False,
                "coverage": None,
                "observations": None,
                "by_state": None,
                "by_actor_kind": None,
                "changes_requested_prs": None,
                "changes_requested_observations": None,
                "changes_requested_by_actor_kind": None,
                "changes_requested_rate": None,
                "changes_requested_observed_review_rate": None,
                "changes_requested_all_eligible_pr_rate": None,
                "changes_requested_rate_denominator": "prs_with_review_observation",
            },
            "_merge_times": {},
            "_observations": {},
            "_states": {},
            "_merged_pr_urls": [],
        }

    mature_24h = {url for url, origin in origin_by_url.items() if ended - origin >= timedelta(hours=24)}
    mature_7d = {url for url, origin in origin_by_url.items() if ended - origin >= timedelta(days=7)}

    check_observations = [
        (url, row) for url, items in checks.items() for row in items
    ]
    conclusions = Counter(
        _dimension(
            row["normalized"].get("conclusion")
            or row["normalized"].get("checkConclusion")
            or row["normalized"].get("checksConclusion")
            or row["normalized"].get("status")
        ).lower()
        for _url, row in check_observations
    )
    bound_checks = sum(
        isinstance(row["normalized"].get("headSha"), str)
        and row["normalized"]["headSha"] in expected_heads.get(url, set())
        for url, row in check_observations
    )
    reviewed = set(reviews)
    review_states = Counter(
        _dimension(
            item["normalized"].get("reviewState")
            or item["normalized"].get("state")
        ).lower()
        for items in reviews.values() for item in items
    )
    review_actor_kinds = Counter(
        _dimension(item["normalized"].get("actorKind"))
        for items in reviews.values() for item in items
    )
    changes_requested_actor_kinds = Counter(
        _dimension(item["normalized"].get("actorKind"))
        for items in reviews.values() for item in items
        if str(
            item["normalized"].get("reviewState")
            or item["normalized"].get("state")
            or ""
        ).lower() in {"changes_requested", "changes requested"}
    )
    changes_requested = {
        url for url, items in reviews.items()
        if any(
            str(
                item["normalized"].get("reviewState")
                or item["normalized"].get("state")
                or ""
            ).lower() in {"changes_requested", "changes requested"}
            for item in items
        )
    }
    return {
        "authenticated_outcomes_available": authenticity_available,
        "future_observations_excluded": future_matching_observations,
        "pre_association_observations_excluded": (
            pre_association_matching_observations
        ),
        "grain": "unique_pr_url",
        "merged_prs": merged,
        "closed_unmerged_prs": closed,
        "open_prs": opened,
        "unknown_state_prs": observed_unknown,
        "unobserved_prs": unobserved,
        "right_censored_prs": opened + observed_unknown + unobserved,
        "provider_timed_merged_prs": len(provider_timed_current_merged_urls),
        "merge_timestamp_coverage": _coverage(
            len(provider_timed_current_merged_urls), len(current_merged_urls)
        ),
        "closed_unmerged_rate": _rate(closed, terminal),
        "closed_unmerged_rate_denominator": "authenticated_terminal_prs",
        "closed_unmerged_rates": _closed_unmerged_rates(
            closed, len(pr_urls), len(latest), terminal
        ),
        "coverage": _coverage(len(latest), len(pr_urls)),
        "terminal_coverage": _coverage(terminal, len(pr_urls)),
        "observation_freshness_ms": _measured_distribution(freshness, len(latest)),
        "authenticity": {
            "available": authenticity_available,
            "scope": "signature_verified_receipts_only",
            "matching_observations": matching_observations,
            "authenticated_matching_observations": authenticated_matching_observations,
            "authenticated_observations": (
                sum(len(items) for items in observations.values())
                + sum(len(items) for items in checks.values())
                + sum(len(items) for items in reviews.values())
            ),
            "unverified_matching_observations": unverified_matching_observations,
            "event_coverage": _coverage(
                authenticated_matching_observations, matching_observations
            ),
            "pr_coverage": _coverage(len(authenticated_prs), len(pr_urls)),
        },
        "unverified_observations": {
            "authority": "observation_only_not_used_in_canonical_metrics",
            "matching_events": unverified_matching_observations,
            "unique_prs": len(unverified_prs),
            "by_kind": _counter(unverified_kinds),
            "pull_request_state_events": _counter(unverified_pr_states),
        },
        "maturity_origin": {
            "kind": "first_durable_pr_association",
            "coverage": _coverage(len(origin_by_url), len(pr_urls)),
        },
        "maturity": {
            "basis": "pr_effect_age_qualified_cohort_with_current_outcome_observed_through_report_cutoff",
            "outcomes_observed_through": _timestamp(ended),
            "at_24h": _maturity(states, mature_24h),
            "at_7d": _maturity(states, mature_7d),
            "age_qualified_current_outcome": {
                "at_24h": _maturity(states, mature_24h),
                "at_7d": _maturity(states, mature_7d),
            },
            "outcome_within_horizon": {
                "within_24h": _github_within_horizon(
                    observations, origin_by_url, mature_24h,
                    timedelta(hours=24), ended,
                ),
                "within_7d": _github_within_horizon(
                    observations, origin_by_url, mature_7d,
                    timedelta(days=7), ended,
                ),
            },
        },
        "check_suite_observations": {
            "available": authenticity_available,
            "coverage": _coverage(len(checks), len(pr_urls)),
            "observations": len(check_observations),
            "by_conclusion": _counter(conclusions),
            "successful_observations": conclusions["success"],
            "head_sha_binding": _coverage(bound_checks, len(check_observations)),
        },
        "first_pass_checks": {
            "available": False,
            "success_rate": None,
            "reason": "required check-set aggregate bound to the initial head SHA is unavailable",
        },
        "reviews": {
            "available": authenticity_available,
            "coverage": _coverage(len(reviewed), len(pr_urls)),
            "observations": sum(len(items) for items in reviews.values()),
            "by_state": _counter(review_states),
            "by_actor_kind": _counter(review_actor_kinds),
            "changes_requested_prs": len(changes_requested),
            "changes_requested_observations": review_states["changes_requested"],
            "changes_requested_by_actor_kind": _counter(changes_requested_actor_kinds),
            "changes_requested_rate": _rate(len(changes_requested), len(reviewed)),
            "changes_requested_observed_review_rate": _rate(
                len(changes_requested), len(reviewed)
            ),
            "changes_requested_all_eligible_pr_rate": _rate(
                len(changes_requested), len(pr_urls)
            ),
            "changes_requested_rate_denominator": "prs_with_review_observation",
        },
        "_merge_times": merge_times,
        "_observations": dict(observations),
        "_states": states,
        "_merged_pr_urls": sorted(merge_times),
    }


def _maturity(states: dict[str, str], eligible: set[str]) -> dict[str, Any]:
    counts = Counter(states.get(url, "unobserved") for url in eligible)
    terminal = counts["merged"] + counts["closed_unmerged"]
    return {
        "eligible_prs": len(eligible),
        "by_state": _counter(counts),
        "terminal_observation": _coverage(terminal, len(eligible)),
        "right_censored_prs": counts["open"] + counts["unknown"] + counts["unobserved"],
    }


def _closed_unmerged_rates(
    closed: int,
    eligible: int,
    observed: int,
    terminal: int,
) -> dict[str, Any]:
    """Expose every useful denominator instead of leaving it to consumers."""
    return {
        "terminal_prs": {
            "numerator": closed,
            "denominator": terminal,
            "rate": _rate(closed, terminal),
            "denominator_definition": (
                "authenticated_prs_with_latest_state_merged_or_closed_unmerged"
            ),
        },
        "observed_prs": {
            "numerator": closed,
            "denominator": observed,
            "rate": _rate(closed, observed),
            "denominator_definition": (
                "authenticated_prs_with_any_recognized_latest_state_observation"
            ),
        },
        "all_eligible_prs": {
            "numerator": closed,
            "denominator": eligible,
            "rate": _rate(closed, eligible),
            "denominator_definition": "all_exact_factory_cohort_pr_urls",
            "interpretation": "current_snapshot_lower_bound_while_right_censored",
            "right_censored": eligible - terminal,
        },
    }


def _github_within_horizon(
    observations: dict[str, list[dict[str, Any]]],
    origins: dict[str, datetime],
    eligible: set[str],
    horizon: timedelta,
    observed_through: datetime,
) -> dict[str, Any]:
    states = Counter()
    observation_count = 0
    outcome_evidence = 0
    for url in eligible:
        cutoff = origins[url] + horizon
        timeline = [
            (observed_at, row)
            for row in observations.get(url, [])
            if (observed_at := _provider_observation_time(row)) is not None
            and origins[url] <= observed_at <= observed_through
        ]
        if timeline:
            observation_count += 1
        in_horizon = [item for item in timeline if item[0] <= cutoff]
        if any(_github_state(row["normalized"]) == "merged" for _time, row in in_horizon):
            states["merged"] += 1
            outcome_evidence += 1
            continue
        later_state_evidence = [
            row for observed_at, row in timeline
            if observed_at >= cutoff and _github_state(row["normalized"]) != "unknown"
        ]
        if later_state_evidence:
            # GitHub merges are irreversible. A recognized state observed at
            # or after the completed horizon proves that no merge occurred
            # inside it unless an earlier authenticated merge was seen above.
            states["not_merged_within_horizon"] += 1
            outcome_evidence += 1
        elif not timeline:
            states["unobserved_within_horizon"] += 1
        else:
            # An early open/unknown snapshot does not prove the state at the
            # horizon boundary; a missing later delivery must not become a
            # measured non-merge.
            states["insufficient_horizon_evidence"] += 1
    reversible_outcome_unavailable = {
        "available": False,
        "observed": None,
        "eligible": len(eligible),
        "rate": None,
        "complete": False,
        "reason": (
            "event observations do not establish reversible closed state at the exact horizon boundary"
        ),
    }
    return {
        "eligible_prs_with_full_horizon": len(eligible),
        "by_outcome": _counter(states),
        "observation_evidence": _coverage(observation_count, len(eligible)),
        "outcome_evidence": _coverage(outcome_evidence, len(eligible)),
        "terminal_within_horizon": reversible_outcome_unavailable,
        "merged_within_horizon": _evidence_bounded_coverage(
            states["merged"], len(eligible), outcome_evidence
        ),
        "closed_unmerged_within_horizon": reversible_outcome_unavailable,
        "right_censored_prs": len(eligible) - outcome_evidence,
    }


def _github_state(normalized: dict[str, Any]) -> str:
    if normalized.get("merged") is True:
        return "merged"
    if str(normalized.get("state") or "").lower() == "closed":
        return "closed_unmerged"
    if str(normalized.get("state") or "").lower() == "open":
        return "open"
    return "unknown"


def _github_observation_order(
    row: dict[str, Any],
) -> tuple[datetime, int, str, str]:
    # Provider time is preferred when its provenance is explicit; otherwise
    # receipt time is the ordering timestamp. Do not put every fallback row at
    # a synthetic -1, which would let an older provider snapshot outrank a
    # later close or reopen received without provider time.
    observed_at = _observation_time(row) or datetime.min.replace(tzinfo=timezone.utc)
    state_rank = {"unknown": 0, "open": 1, "closed_unmerged": 2, "merged": 3}
    return (
        observed_at,
        state_rank[_github_state(row["normalized"])],
        str(row.get("created_at") or ""),
        str(row.get("event_id") or ""),
    )


def _observation_time(row: dict[str, Any]) -> datetime | None:
    provider = (
        _provider_time(row.get("provider_updated_at_us"))
        if row.get("provider_timestamp_source") == "provider" else None
    )
    return provider or _parse_time(row.get("created_at"))


def _provider_observation_time(row: dict[str, Any]) -> datetime | None:
    """Return occurrence time only when provider provenance is explicit."""
    if row.get("provider_timestamp_source") != "provider":
        return None
    return _provider_time(row.get("provider_updated_at_us"))


def _provider_time(value: Any) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _lifecycle_report(
    path: str, started: datetime, ended: datetime, cohort_jobs: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    empty = {
        "available": False,
        "cohort_records": None,
        "by_state": None,
        "current_dead_letters": None,
        "dead_letter_event_reasons": None,
        "dead_letter_delivery_errors": None,
        "delivery": None,
    }
    unavailable_source = {
        "available": False, "schema_contract": "unavailable", "row_count": None,
        "window_rows": None, "latest_recorded_at": None, "latest_activity_at": None,
        "lag_ms": None,
        "invalid_timestamp_rows": None,
    }
    try:
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error):
        return empty, unavailable_source
    try:
        conn.execute("BEGIN")
        columns = _table_columns(conn, "lifecycle_outbox")
        required = {"job_id", "delivery_state", "reason_code", "last_error_code", "first_seen_at"}
        if not required <= columns:
            return empty, unavailable_source
        optional = (
            "attempts", "last_seen_at", "delivered_at", "suppressed_at",
            "next_attempt_at", "archived_at",
        )
        select = [*sorted(required), *(
            name if name in columns else f"NULL AS {name}" for name in optional
        )]
        source_rows = [dict(row) for row in conn.execute(
            f"SELECT {','.join(select)} FROM lifecycle_outbox"
        )]
        rows = [
            row for row in source_rows
            if (first_seen_at := _parse_time(row.get("first_seen_at"))) is not None
            and first_seen_at <= ended
        ]
        cohort_rows = [row for row in rows if row["job_id"] in cohort_jobs]
        dead = [row for row in rows if row["delivery_state"] == "dead_letter"]
        backlog = [
            row for row in rows
            if row["delivery_state"] in {"pending", "retrying", "claimed"}
        ]
        delivery_latency: list[float] = []
        for row in cohort_rows:
            _duration(
                delivery_latency,
                _parse_time(row["first_seen_at"]),
                _parse_time(row.get("delivered_at")),
            )
        backlog_ages = [
            value for row in backlog
            if (value := _elapsed_ms(_parse_time(row["first_seen_at"]), ended)) is not None
        ]
        delivery = {
            "grain": "lifecycle_outbox_record",
            "cohort_delivery_coverage": _coverage(
                sum(row["delivery_state"] == "delivered" for row in cohort_rows),
                len(cohort_rows),
            ),
            "cohort_first_seen_to_delivered_ms": _measured_distribution(
                delivery_latency,
                sum(row["delivery_state"] == "delivered" for row in cohort_rows),
            ),
            "cohort_delivery_attempts": sum(
                int(row.get("attempts") or 0) for row in cohort_rows
                if isinstance(row.get("attempts"), int)
            ),
            "cohort_retried_records": sum(
                isinstance(row.get("attempts"), int) and row["attempts"] > 1
                for row in cohort_rows
            ),
            "current_backlog": len(backlog),
            "current_backlog_by_state": _counter(Counter(str(row["delivery_state"]) for row in backlog)),
            "current_backlog_age_ms": _age_distribution(backlog_ages, len(backlog)),
        }
        report = {
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
            "delivery": delivery,
        }
        valid_times = [_parse_time(row["first_seen_at"]) for row in source_rows]
        latest = max(
            (value for value in valid_times if value is not None and value <= ended),
            default=None,
        )
        activity_times = [
            _parse_time(row.get("last_seen_at")) or _parse_time(row["first_seen_at"])
            for row in source_rows
        ]
        latest_activity = max(
            (value for value in activity_times if value is not None and value <= ended),
            default=None,
        )
        source = {
            "available": True,
            "schema_contract": (
                "lifecycle-outbox-v2"
                if {"attempts", "last_seen_at", "delivered_at", "suppressed_at"} <= columns
                else "legacy-compatible"
            ),
            "column_coverage": _coverage(len(required | (set(optional) & columns)), len(required | set(optional))),
            "row_count": len(source_rows),
            "window_rows": sum(
                value is not None and started <= value <= ended for value in valid_times
            ),
            "latest_recorded_at": _timestamp(latest) if latest else None,
            "latest_activity_at": _timestamp(latest_activity) if latest_activity else None,
            "lag_ms": _elapsed_ms(latest_activity, ended),
            "invalid_timestamp_rows": sum(value is None for value in valid_times),
            "future_timestamp_rows": sum(value is not None and value > ended for value in activity_times),
        }
        return report, source
    except (sqlite3.Error, TypeError):
        return empty, unavailable_source
    finally:
        conn.close()


def _bridge_delivery_report(
    conn: sqlite3.Connection,
    ended: datetime,
    cohort_attempts: set[str],
    cohort_jobs: set[str],
) -> dict[str, Any]:
    return {
        "factory_attempt_outbox": _bridge_outbox_health(
            conn, "factory_attempt_outbox", ended,
            cohort_attempts=cohort_attempts, cohort_jobs=cohort_jobs,
        ),
        "factory_inspection_outbox": _bridge_outbox_health(
            conn, "factory_inspection_outbox", ended,
            cohort_attempts=cohort_attempts, cohort_jobs=cohort_jobs,
        ),
    }


def _bridge_outbox_health(
    conn: sqlite3.Connection,
    table: str,
    ended: datetime,
    *,
    cohort_attempts: set[str],
    cohort_jobs: set[str],
) -> dict[str, Any]:
    columns = _table_columns(conn, table)
    required = {"state", "delivery_attempts", "acknowledged_at", "created_at", "updated_at"}
    if not required <= columns:
        return {"available": False}
    if table == "factory_inspection_outbox":
        if "request_key" not in columns or "prior_attempt_id" not in _table_columns(conn, "factory_inspection_requests"):
            return {"available": False}
        select = [f"o.{name}" for name in sorted(required)]
        select.extend(("r.prior_attempt_id AS attempt_id", "o.job_id AS job_id"))
        rows = [dict(row) for row in conn.execute(
            f"SELECT {','.join(select)} FROM factory_inspection_outbox o "
            "LEFT JOIN factory_inspection_requests r ON r.request_key=o.request_key "
            "WHERE o.created_at <= ?",
            (_timestamp(ended),),
        )]
    else:
        select = [*sorted(required)]
        for name in ("attempt_id", "job_id"):
            select.append(name if name in columns else f"NULL AS {name}")
        rows = [dict(row) for row in conn.execute(
            f"SELECT {','.join(select)} FROM {table} WHERE created_at <= ?",
            (_timestamp(ended),),
        )]
    cohort = [
        row for row in rows
        if row.get("attempt_id") in cohort_attempts or row.get("job_id") in cohort_jobs
    ]
    pending = [row for row in rows if row.get("acknowledged_at") is None]
    ack_latency: list[float] = []
    for row in cohort:
        _duration(
            ack_latency,
            _parse_time(row["created_at"]),
            _parse_time(row.get("acknowledged_at")),
        )
    backlog_ages = [
        value for row in pending
        if (value := _elapsed_ms(_parse_time(row["created_at"]), ended)) is not None
    ]
    return {
        "available": True,
        "grain": "outbox_record",
        "cohort_records": len(cohort),
        "cohort_by_state": _counter(Counter(str(row["state"]) for row in cohort)),
        "cohort_acknowledgement": _coverage(
            sum(row.get("acknowledged_at") is not None for row in cohort), len(cohort)
        ),
        "cohort_created_to_acknowledged_ms": _measured_distribution(
            ack_latency, sum(row.get("acknowledged_at") is not None for row in cohort)
        ),
        "cohort_delivery_attempts": sum(int(row["delivery_attempts"] or 0) for row in cohort),
        "cohort_retried_records": sum(int(row["delivery_attempts"] or 0) > 1 for row in cohort),
        "current_backlog": len(pending),
        "current_backlog_by_state": _counter(Counter(str(row["state"]) for row in pending)),
        "current_backlog_age_ms": _age_distribution(backlog_ages, len(pending)),
    }


def _bridge_source_health(
    conn: sqlite3.Connection,
    ended: datetime,
    schema_version: int,
    migrated_at: str,
) -> dict[str, Any]:
    timestamp_columns: dict[str, tuple[tuple[str, bool], ...]] = {
        "bridge_jobs": (("created_at", True), ("updated_at", True)),
        "bridge_events": (("created_at", True),),
        "factory_attempts": (("created_at", True), ("updated_at", True)),
        "factory_attempt_events": (("created_at", True),),
        "reconciliation_findings": (
            ("first_seen_at", True), ("last_seen_at", True),
        ),
        "factory_attempt_outbox": (
            ("created_at", True), ("updated_at", True),
            ("acknowledged_at", False),
        ),
        "factory_inspection_outbox": (
            ("created_at", True), ("updated_at", True),
            ("acknowledged_at", False),
        ),
        "factory_inspection_requests": (
            ("created_at", True), ("updated_at", True), ("observed_at", False),
        ),
    }
    row_counts: dict[str, int] = {}
    valid: list[datetime] = []
    invalid = 0
    future = 0
    by_field: dict[str, dict[str, Any]] = {}
    for table, configured_columns in timestamp_columns.items():
        available_columns = _table_columns(conn, table)
        selected = [
            (column, required)
            for column, required in configured_columns
            if column in available_columns
        ]
        if not selected:
            continue
        row_counts[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for column, required in selected:
            values = [
                row[column]
                for row in conn.execute(f"SELECT {column} FROM {table}")
            ]
            parsed_values = [
                (raw, _parse_time(raw))
                for raw in values
                if required or raw is not None
            ]
            field_invalid = sum(
                parsed is None for _raw, parsed in parsed_values
            )
            field_future = sum(
                parsed is not None and parsed > ended
                for _raw, parsed in parsed_values
            )
            field_valid = [
                parsed for _raw, parsed in parsed_values
                if parsed is not None and parsed <= ended
            ]
            invalid += field_invalid
            future += field_future
            valid.extend(field_valid)
            by_field[f"{table}.{column}"] = {
                "required": required,
                "values": len(parsed_values),
                "invalid": field_invalid,
                "future": field_future,
                "latest_at_or_before_cutoff": (
                    _timestamp(max(field_valid)) if field_valid else None
                ),
            }
    latest = max(valid, default=None)
    return {
        "available": True,
        "schema_contract": f"bridge-schema-v{schema_version}",
        "schema_version": schema_version,
        "schema_migrated_at": migrated_at,
        "row_counts": row_counts,
        "timestamp_health_by_field": by_field,
        "timestamp_error_grain": "table_column_value",
        "latest_recorded_at": _timestamp(latest) if latest else None,
        "lag_ms": _elapsed_ms(latest, ended),
        "invalid_timestamp_rows": invalid,
        "future_timestamp_rows": future,
    }


def _model_selection_report(
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    groups: dict[tuple[str, str], Counter] = {}
    eligible = observed = 0
    for attempt in attempts:
        events = events_by_attempt[attempt["attempt_id"]]
        if not any(event["event_type"] == "execution_backend_invoked" for event in events):
            continue
        eligible += 1
        selected = next((event["details"] for event in events
                         if event["event_type"] == "execution_model_selected"), None)
        if selected is None:
            continue
        observed += 1
        key = (selected["selected_provider"], selected["selected_model"])
        groups.setdefault(key, Counter())[attempt["status"]] += 1
    return {
        "basis": "executor_selected_configuration_not_provider_usage",
        "coverage": _coverage(observed, eligible),
        "attempts_before_invocation": len(attempts) - eligible,
        "unknown_attempts": eligible - observed,
        "groups": [{"selected_provider": provider, "selected_model": model,
                    "attempts": sum(statuses.values()), "current_status": _counter(statuses)}
                   for (provider, model), statuses in sorted(groups.items())],
    }


def _route_report(
    attempts: list[dict[str, Any]],
    all_attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    assigned = 0
    for attempt in attempts:
        events = events_by_attempt[attempt["attempt_id"]]
        selected = next(
            (event for event in events if event["event_type"] == "execution_route_selected"),
            None,
        )
        if selected is None:
            continue
        assigned += 1
        details = selected["details"]
        payload = _json_object(attempt.get("job_payload_json"))
        dimensions = (
            _dimension(details.get("experiment_id")),
            _dimension(details.get("variant")),
            _dimension(details.get("assignment_method")),
            _dimension(details.get("intended_backend")),
            _dimension(details.get("intended_transport")),
            _dimension(details.get("configured_provider")),
            _dimension(details.get("configured_model")),
            _dimension(details.get("configured_auth_mode")),
            _dimension(details.get("backend_version")),
            _dimension(details.get("factory_release_version")),
            _dimension(details.get("factory_release_sha")),
            (
                str(details["routing_registry_version"])
                if isinstance(details.get("routing_registry_version"), int)
                and not isinstance(details.get("routing_registry_version"), bool)
                else "unknown"
            ),
            _dimension(details.get("verifier_policy_digest")),
            details.get("fallback_allowed")
            if isinstance(details.get("fallback_allowed"), bool) else None,
            _dimension(payload.get("repo")),
            _attempt_kind(events),
        )
        row = groups.setdefault(dimensions, {
            "attempts": 0, "claimed": 0, "authorized": 0, "invoked": 0,
            "backend_completed": 0, "effect_committed": 0, "verified": 0,
            "draft_pr_available": 0, "backend_outcomes": Counter(),
            "actual_identity_observed": 0, "identity_mismatches": 0,
            "mismatch_fields": Counter(), "current_status": Counter(),
            "failures": Counter(), "failure_boundaries": Counter(),
        })
        row["attempts"] += 1
        row["current_status"][str(attempt["status"])] += 1
        row["claimed"] += _has_event(events, "attempt_execution_claimed")
        row["authorized"] += _has_event(events, "effect_authorized")
        row["invoked"] += _has_event(events, "execution_backend_invoked")
        row["backend_completed"] += _has_event(events, "execution_backend_completed")
        completed = next(
            (event for event in events if event["event_type"] == "execution_backend_completed"),
            None,
        )
        if completed is not None:
            row["backend_outcomes"][_dimension(completed["details"].get("backend_outcome"))] += 1
        row["effect_committed"] += _effect_committed(attempt, events, bridge_by_job)
        row["verified"] += _has_event(events, "attempt_verified_ready")
        row["draft_pr_available"] += bool(_attempt_pr_urls(attempt))
        actual = next(
            (event for event in events if event["event_type"] == "execution_backend_invoked"),
            None,
        )
        if actual is not None:
            row["actual_identity_observed"] += 1
            comparisons = {
                "execution_backend": (
                    details.get("intended_backend"), actual["details"].get("execution_backend")
                ),
                "execution_transport": (
                    details.get("intended_transport"), actual["details"].get("execution_transport")
                ),
                "configured_auth_mode": (
                    details.get("configured_auth_mode"), actual["details"].get("configured_auth_mode")
                ),
                "backend_version": (
                    details.get("backend_version"), actual["details"].get("backend_version")
                ),
            }
            mismatches = [
                field for field, (intended, observed) in comparisons.items()
                if intended not in {None, "unknown"} and intended != observed
            ]
            if mismatches:
                row["identity_mismatches"] += 1
                row["mismatch_fields"].update(mismatches)
        for failure in _attempt_failures([attempt], bridge_by_job, events_by_attempt):
            row["failures"][(
                failure["failure_domain"], failure["failure_stage"], failure["reason_code"]
            )] += 1
        seen_boundaries: set[tuple[str, str, str]] = set()
        for event in events:
            if event["event_type"] not in {
                "execution_backend_failed", "execution_dispatch_failed",
                "execution_effect_commit_failed",
            }:
                continue
            invocation = _dimension(event["details"].get("invocation_id"))
            boundary_key = (attempt["attempt_id"], invocation, event["event_type"])
            if boundary_key in seen_boundaries:
                continue
            seen_boundaries.add(boundary_key)
            row["failure_boundaries"][(
                event["event_type"],
                _dimension(event["details"].get("failure_domain")),
                _dimension(event["details"].get("failure_stage")),
                _dimension(event["details"].get("reason_code")),
            )] += 1
    result = []
    names = (
        "experiment_id", "variant", "assignment_method", "intended_backend",
        "intended_transport", "configured_provider", "configured_model",
        "configured_auth_mode", "backend_version", "factory_release_version",
        "factory_release_sha", "routing_registry_version", "verifier_policy_digest",
        "fallback_allowed", "repository", "attempt_kind",
    )
    for dimensions in sorted(groups, key=lambda values: tuple(str(value) for value in values)):
        row = groups[dimensions]
        terminal_attempts = sum(
            count for status, count in row["current_status"].items()
            if status in TERMINAL_ATTEMPT_STATUSES
        )
        active_attempts = row["attempts"] - terminal_attempts
        result.append({
            **dict(zip(names, dimensions)),
            "attempts": row["attempts"],
            "executor_claimed": row["claimed"],
            "effect_authorized": row["authorized"],
            "backend_invoked": row["invoked"],
            "backend_completed": row["backend_completed"],
            "backend_outcomes": _counter(row["backend_outcomes"]),
            "effect_committed": row["effect_committed"],
            "draft_pr_available": row["draft_pr_available"],
            "verified_ready": row["verified"],
            "verified_ready_rate": _rate(row["verified"], row["attempts"]),
            "verified_ready_snapshot_rate": _rate(row["verified"], row["attempts"]),
            "verified_ready_among_terminal_rate": _rate(row["verified"], terminal_attempts),
            "verified_ready_rate_denominator": "all_assigned_attempts_at_snapshot",
            "by_current_status": _counter(row["current_status"]),
            "terminal_attempts": terminal_attempts,
            "active_attempts": active_attempts,
            "right_censored_attempts": active_attempts,
            "actual_identity_coverage": _coverage(
                row["actual_identity_observed"], row["attempts"]
            ),
            "identity_mismatches": row["identity_mismatches"],
            "identity_matches": row["actual_identity_observed"] - row["identity_mismatches"],
            "identity_mismatch_fields": _counter(row["mismatch_fields"]),
            "failure_classification": [
                {
                    "failure_domain": key[0], "failure_stage": key[1],
                    "reason_code": key[2], "count": count,
                }
                for key, count in sorted(
                    row["failures"].items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "failure_boundary_classification": [
                {
                    "event_type": key[0], "failure_domain": key[1],
                    "failure_stage": key[2], "reason_code": key[3], "count": count,
                }
                for key, count in sorted(
                    row["failure_boundaries"].items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
        })
    route_by_attempt = {
        attempt["attempt_id"]: selected["details"]
        for attempt in all_attempts
        if (selected := next(
            (
                event for event in events_by_attempt[attempt["attempt_id"]]
                if event["event_type"] == "execution_route_selected"
            ),
            None,
        )) is not None
    }
    attempts_with_prior = [attempt for attempt in attempts if attempt.get("prior_attempt_id")]
    route_pairs = []
    for attempt in attempts_with_prior:
        current = route_by_attempt.get(attempt["attempt_id"])
        prior = route_by_attempt.get(str(attempt["prior_attempt_id"]))
        if current is not None and prior is not None:
            route_pairs.append((attempt, prior, current))
    variant_transitions = Counter(
        (_dimension(prior.get("variant")), _dimension(current.get("variant")))
        for _attempt, prior, current in route_pairs
        if prior.get("variant") != current.get("variant")
    )
    identity_fields = (
        "variant", "intended_backend", "intended_transport", "configured_provider",
        "configured_model", "configured_auth_mode", "backend_version",
    )
    identity_crossovers = [
        attempt for attempt, prior, current in route_pairs
        if any(prior.get(field) != current.get(field) for field in identity_fields)
    ]
    variant_crossovers = [
        attempt for attempt, prior, current in route_pairs
        if prior.get("variant") != current.get("variant")
    ]
    observed = sum(row["actual_identity_coverage"]["observed"] for row in result)
    mismatches = sum(row["identity_mismatches"] for row in result)
    return {
        "grain": "route_assignment",
        "assignment_coverage": _coverage(assigned, len(attempts)),
        "actual_identity_coverage": _coverage(observed, assigned),
        "identity_matches": observed - mismatches,
        "identity_mismatches": mismatches,
        "identity_match_rate": _rate(observed - mismatches, observed),
        "lineage": {
            "grain": "cohort_attempt_with_prior_attempt",
            "route_pair_coverage": _coverage(len(route_pairs), len(attempts_with_prior)),
            "identity_crossovers": len(identity_crossovers),
            "variant_crossovers": len(variant_crossovers),
            "variant_crossover_rate": _rate(len(variant_crossovers), len(route_pairs)),
            "verified_after_variant_crossover": sum(
                _has_event(events_by_attempt[attempt["attempt_id"]], "attempt_verified_ready")
                or attempt["status"] == "verified_ready"
                for attempt in variant_crossovers
            ),
            "by_variant_transition": [
                {"from_variant": key[0], "to_variant": key[1], "count": count}
                for key, count in sorted(
                    variant_transitions.items(), key=lambda item: (-item[1], item[0])
                )
            ],
        },
        "groups": result,
    }


def _effort_report(
    cohort: list[dict[str, Any]],
    all_attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    ended: datetime,
) -> dict[str, Any]:
    # Include unsuccessful work and earlier retries so success cannot erase effort.
    work_items = {row["work_item_key"] for row in cohort}
    attempts = [row for row in all_attempts if row["work_item_key"] in work_items]
    verified: set[str] = set()
    durations: list[int] = []
    eligible = active_attempts = active_invocations = unidentified_attempts = 0
    human_entries: dict[tuple[str, str], dict[str, Any]] = {}
    execution_events = {
        "execution_backend_invoked", "execution_backend_completed",
        "execution_backend_failed", "execution_effect_commit_failed",
    }
    for attempt in attempts:
        attempt_id = attempt["attempt_id"]
        events = [
            event for event in events_by_attempt.get(attempt_id, [])
            if (at := _parse_time(event["created_at"])) is not None and at <= ended
        ]
        bridges = [
            event for event in bridge_by_job.get(attempt.get("job_id"), [])
            if (at := _parse_time(event["created_at"])) is not None and at <= ended
        ]
        if _has_event(events, "attempt_verified_ready"):
            verified.add(attempt["work_item_key"])
        updated = _parse_time(attempt.get("updated_at"))
        terminal = any(event["event_type"] in TERMINAL_ATTEMPT_EVENTS for event in events) or (
            attempt.get("status") in TERMINAL_ATTEMPT_STATUSES
            and updated is not None and updated <= ended
        )
        active_attempts += not terminal

        invocations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        missing_identity = False
        for event in events + bridges:
            kind, details = event["event_type"], event["details"]
            if kind in execution_events or (
                kind == "transition:effect_committed" and details.get("execution_backend")
            ):
                invocation = details.get("invocation_id")
                if isinstance(invocation, str) and invocation:
                    invocations[invocation].append(event)
                else:
                    missing_identity = True
            if kind == "human_time_recorded":
                duration, category = details.get("duration_ms"), details.get("category")
                request_id = details.get("request_id")
                if (
                    type(details.get("schema_version")) is int and details["schema_version"] == 1
                    and type(duration) is int and 0 <= duration <= 86_400_000
                    and isinstance(category, str) and category in {"intervention", "review", "maintenance"}
                    and isinstance(request_id, str) and request_id
                ):
                    human_entries.setdefault((attempt_id, request_id), details)
        for invocation_events in invocations.values():
            eligible += 1
            elapsed = _invocation_effort_ms(invocation_events)
            if elapsed is not None:
                durations.append(elapsed)
            if not any(event["event_type"] != "execution_backend_invoked" for event in invocation_events):
                active_invocations += 1

        execution_markers = [event for event in events + bridges if event["event_type"] in {
            "attempt_started", "effect_authorized", "attempt_effect_committed",
            "transition:running", "transition:effect_committed",
        }]
        dispatch_times = [
            _parse_time(event["created_at"]) for event in events
            if event["event_type"] == "execution_dispatch_failed"
            and event["details"].get("effect_outcome") == "none"
        ]
        # A pre-invocation failure explains earlier starts, never a later run or effect.
        dispatch_only = bool(dispatch_times) and all(
            event["event_type"] not in {"attempt_effect_committed", "transition:effect_committed"}
            and _parse_time(event["created_at"]) <= max(dispatch_times)
            for event in execution_markers
        )
        if missing_identity or (not invocations and execution_markers and not dispatch_only):
            eligible += 1
            unidentified_attempts += 1

    coverage = _coverage(len(durations), eligible)
    observed_ms = sum(durations)
    total_ms = observed_ms if coverage["complete"] else None
    recorded_attempts = {attempt_id for attempt_id, _ in human_entries}
    entries = list(human_entries.values())
    categories = {
        category: [entry["duration_ms"] for entry in entries if entry["category"] == category]
        for category in ("intervention", "review", "maintenance")
    }
    recorded_ms = sum(entry["duration_ms"] for entry in entries) if entries else None
    return {
        "basis": "cohort_work_item_history_through_cutoff",
        "work_items": len(work_items),
        "verified_work_items": len(verified),
        "attempts": len(attempts),
        "active_attempts": active_attempts,
        "execution": {
            "observed_ms": observed_ms,
            "total_ms": total_ms,
            "ms_per_verified_work_item": _average(total_ms, len(verified)) if total_ms is not None else None,
            "coverage": coverage,
            "coverage_grain": "invocations_plus_attempts_without_invocation_identity",
            "attempts_without_invocation_identity": unidentified_attempts,
            "active_invocations": active_invocations,
        },
        "human": {
            "recorded_ms": recorded_ms,
            "recorded_ms_per_verified_work_item": (
                _average(recorded_ms, len(verified)) if recorded_ms is not None else None
            ),
            "recorded_interventions": (
                sum(duration > 0 for duration in categories["intervention"])
                if categories["intervention"] else None
            ),
            "entries": len(entries),
            "by_category_ms": {
                category: sum(values) if values else None for category, values in categories.items()
            },
            "attempt_recording_coverage": _coverage(len(recorded_attempts), len(attempts)),
            "completeness": "self_reported_not_attested",
        },
    }


def _invocation_effort_ms(events: list[dict[str, Any]]) -> int | None:
    """Prefer a backend duration over later settlement and mirrored receipts."""
    primary = [event for event in events if event["event_type"] in {
        "execution_backend_completed", "execution_backend_failed",
    }]
    fallback = [event for event in events if event["event_type"] in {
        "execution_effect_commit_failed", "transition:effect_committed",
    }]
    for event in primary + fallback:
        field = "backend_elapsed_ms" if event["event_type"] == "transition:effect_committed" else "elapsed_ms"
        elapsed = event["details"].get(field)
        if type(elapsed) is int and 0 <= elapsed <= 604_800_000:
            return elapsed
    # Settlement timestamps can include recovery waits; only a direct completion
    # and its matching invocation are compatible with the existing latency fallback.
    started = _event_time(events, "execution_backend_invoked")
    completed = _event_time(events, "execution_backend_completed")
    return _elapsed_ms(started, completed)


def _backends(
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    groups: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
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
        payload = _json_object(attempt.get("job_payload_json"))
        repository = _dimension(payload.get("repo"))
        attempt_kind = _attempt_kind(events_by_attempt[attempt["attempt_id"]])
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
        attempt_groups: set[tuple[str, str, str, str, str, str]] = set()
        invocation_started: dict[
            tuple[tuple[str, str, str, str, str, str], tuple[str, str]], datetime
        ] = {}
        for event in telemetry:
            details = event["details"]
            key = (
                _dimension(details.get("execution_backend")),
                _dimension(details.get("execution_transport")),
                _dimension(details.get("backend_version")),
                _dimension(details.get("configured_auth_mode")),
                repository,
                attempt_kind,
            )
            row = groups.setdefault(key, {
                "execution_backend": key[0], "execution_transport": key[1],
                "backend_version": key[2], "configured_auth_mode": key[3],
                "repository": key[4], "attempt_kind": key[5], "attempt_ids": set(),
                "invocations": set(), "completed": set(), "failed": set(),
                "outcomes": {}, "current_status": Counter(),
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
                    row["outcomes"][invocation_key] = _dimension(details.get("backend_outcome"))
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
                    if details.get("backend_outcome"):
                        row["outcomes"][invocation_key] = _dimension(details.get("backend_outcome"))
                    if invocation_key not in row["completed"]:
                        row["completed"].add(invocation_key)
                        elapsed = details.get("elapsed_ms")
                        if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                            row["completed_duration_samples"].append(elapsed)
                elif event["event_type"] == "transition:effect_committed":
                    if details.get("backend_outcome"):
                        row["outcomes"][invocation_key] = _dimension(details.get("backend_outcome"))
                    if invocation_key not in row["completed"]:
                        row["completed"].add(invocation_key)
                        elapsed = details.get("backend_elapsed_ms")
                        if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
                            row["completed_duration_samples"].append(elapsed)
        for key in attempt_groups:
            groups[key]["current_status"][str(attempt["status"])] += 1
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
        terminal_attempts = sum(
            count for status, count in row["current_status"].items()
            if status in TERMINAL_ATTEMPT_STATUSES
        )
        active_attempts = len(row["attempt_ids"]) - terminal_attempts
        result.append({
            "execution_backend": row["execution_backend"],
            "execution_transport": row["execution_transport"],
            "backend_version": row["backend_version"],
            "configured_auth_mode": row["configured_auth_mode"],
            "repository": row["repository"],
            "attempt_kind": row["attempt_kind"],
            "attempts": len(row["attempt_ids"]),
            "invocations": len(row["invocations"]),
            "completed_invocations": len(row["completed"]),
            "failed_invocations": len(row["failed"]),
            "verified_attempts": len(row.get("verified_attempt_ids", set())),
            "unknown_effect_attempts": len(row.get("unknown_attempt_ids", set())),
            "observed_receipt_recovery_attempts": len(row.get("commit_recovery_attempt_ids", set())),
            "backend_outcomes": _counter(Counter(row["outcomes"].values())),
            "backend_outcome_coverage": _coverage(len(row["outcomes"]), len(row["completed"])),
            "created_invocations": sum(value == "created" for value in row["outcomes"].values()),
            "reused_invocations": sum(value == "reused" for value in row["outcomes"].values()),
            "verified_attempt_rate": _rate(
                len(row.get("verified_attempt_ids", set())), len(row["attempt_ids"])
            ),
            "verified_attempt_snapshot_rate": _rate(
                len(row.get("verified_attempt_ids", set())), len(row["attempt_ids"])
            ),
            "verified_among_terminal_attempts_rate": _rate(
                len(row.get("verified_attempt_ids", set())), terminal_attempts
            ),
            "verified_attempt_rate_denominator": "all_attributed_attempts_at_snapshot",
            "by_current_status": _counter(row["current_status"]),
            "terminal_attempts": terminal_attempts,
            "active_attempts": active_attempts,
            "right_censored_attempts": active_attempts,
            "completed_invocation_latency_ms": _measured_distribution(
                row["completed_duration_samples"], len(row["completed"])
            ),
            "failed_invocation_latency_ms": _measured_distribution(
                row["failed_duration_samples"], len(row["failed"])
            ),
        })
    attributed_authorized = attributed & authorized
    return (
        result,
        len(attributed_authorized),
        len(authorized),
        len(claimed),
        len(configured_auth & attributed_authorized),
    )


def _attempt_failures(
    attempts: list[dict[str, Any]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    events_by_attempt: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    result = []
    for attempt in attempts:
        events = [event for event in bridge_by_job.get(attempt.get("job_id"), []) if event["event_type"] == "transition:failed"]
        boundaries = [
            event for event in (events_by_attempt or {}).get(attempt["attempt_id"], [])
            if event["event_type"] in {
                "execution_backend_failed", "execution_dispatch_failed",
                "execution_effect_commit_failed",
            }
        ]
        if not events and not boundaries and attempt["status"] != "failed":
            continue
        evidence = events[-1] if events else boundaries[-1] if boundaries else None
        details = evidence["details"] if evidence else {}
        result.append({
            "attempt_id": attempt["attempt_id"],
            "current_status": attempt["status"],
            "failure_domain": _dimension(details.get("failure_domain")),
            "failure_stage": _dimension(details.get("failure_stage")),
            "reason_code": _dimension(details.get("reason_code") or attempt.get("job_reason_code")),
            "retryable": details.get("retryable") if isinstance(details.get("retryable"), bool) else None,
            "failure_disposition": _dimension(details.get("failure_disposition")),
            "operator_intervention_required": details.get("operator_intervention_required") if isinstance(details.get("operator_intervention_required"), bool) else None,
            "failed_at": evidence["created_at"] if evidence else attempt.get("updated_at"),
            "evidence_type": (
                "attempt_terminal" if events or attempt["status"] == "failed"
                else str(evidence["event_type"])
            ),
        })
    return result


def _recovery_report(
    cohort: list[dict[str, Any]],
    all_attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    failures: list[dict[str, Any]],
    merged_pr_urls: set[str],
    merge_times: dict[str, datetime],
    github_observation_coverage: dict[str, Any] | None,
    right_censored_prs: int | None,
    current_merged_prs: int | None,
    merge_timestamp_coverage: dict[str, Any] | None,
    github_outcomes_available: bool,
    ended: datetime,
) -> dict[str, Any]:
    by_id = {row["attempt_id"]: row for row in all_attempts}
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_attempts:
        if row.get("prior_attempt_id") in by_id:
            children[str(row["prior_attempt_id"])].append(row)
    recovered: list[tuple[dict[str, Any], dict[str, Any], int, datetime]] = []
    retry_latency: list[float] = []
    verified_latency: list[float] = []
    attempts_to_recovery: list[float] = []
    failures_with_descendant = 0
    repairs_to_success = 0
    recovered_in_place = 0
    recovered_classification = Counter()
    descendants_by_failure: dict[str, list[dict[str, Any]]] = {}
    for failure in failures:
        source_id = failure["attempt_id"]
        queue = [(child, 1) for child in children.get(source_id, [])]
        descendants: list[tuple[dict[str, Any], int]] = []
        seen: set[str] = set()
        while queue:
            candidate, depth = queue.pop(0)
            if candidate["attempt_id"] in seen:
                continue
            seen.add(candidate["attempt_id"])
            descendants.append((candidate, depth))
            queue.extend((child, depth + 1) for child in children.get(candidate["attempt_id"], []))
        descendants_by_failure[source_id] = [candidate for candidate, _depth in descendants]
        if descendants:
            failures_with_descendant += 1
            first_retry = min(descendants, key=lambda item: (item[0]["created_at"], item[0]["attempt_id"]))[0]
            _duration(
                retry_latency,
                _parse_time(failure.get("failed_at")),
                _parse_time(first_retry.get("created_at")),
            )
        verified = []
        source_attempt = by_id.get(source_id)
        if source_attempt is not None and failure["evidence_type"] != "attempt_terminal":
            source_verified_at = _event_time(
                events_by_attempt[source_id], "attempt_verified_ready"
            )
            if source_verified_at is None and source_attempt["status"] == "verified_ready":
                source_verified_at = _parse_time(source_attempt.get("updated_at"))
            failed_at = _parse_time(failure.get("failed_at"))
            if source_verified_at and (failed_at is None or source_verified_at >= failed_at):
                verified.append((source_verified_at, source_attempt, 0))
        for candidate, depth in descendants:
            verified_at = _event_time(
                events_by_attempt[candidate["attempt_id"]], "attempt_verified_ready"
            )
            if verified_at is None and candidate["status"] == "verified_ready":
                verified_at = _parse_time(candidate.get("updated_at"))
            if verified_at:
                verified.append((verified_at, candidate, depth))
        if not verified:
            continue
        verified_at, winner, depth = min(verified, key=lambda item: (item[0], item[1]["attempt_id"]))
        recovered.append((failure, winner, depth, verified_at))
        recovered_in_place += depth == 0
        _duration(verified_latency, _parse_time(failure.get("failed_at")), verified_at)
        attempts_to_recovery.append(depth)
        repairs_to_success += depth
        recovered_classification[
            (failure["failure_domain"], failure["failure_stage"], failure["reason_code"])
        ] += 1
    recovered_failure_ids = {
        failure["attempt_id"] for failure, _winner, _depth, _verified_at in recovered
    }
    recovery_time_by_failure = {
        failure["attempt_id"]: verified_at
        for failure, _winner, _depth, verified_at in recovered
    }

    def terminal_at_cutoff(attempt: dict[str, Any]) -> bool:
        if any(
            event["event_type"] in TERMINAL_ATTEMPT_EVENTS
            for event in events_by_attempt.get(attempt["attempt_id"], [])
        ):
            return True
        updated_at = _parse_time(attempt.get("updated_at"))
        return (
            attempt.get("status") in TERMINAL_ATTEMPT_STATUSES
            and updated_at is not None
            and updated_at <= ended
        )

    active_unrecovered_failure_ids: set[str] = set()
    age_qualified_failure_ids: set[str] = set()
    immature_failure_ids: set[str] = set()
    unaged_unrecovered_failure_ids: set[str] = set()
    maturity = timedelta(days=RECOVERY_MATURITY_DAYS)
    for failure in failures:
        source_id = failure["attempt_id"]
        failed_at = _parse_time(failure.get("failed_at"))
        if failed_at is None:
            unaged_unrecovered_failure_ids.add(source_id)
        elif ended - failed_at < maturity:
            immature_failure_ids.add(source_id)
        else:
            age_qualified_failure_ids.add(source_id)
        if source_id not in recovered_failure_ids:
            lineage = [
                candidate for candidate in (
                    by_id.get(source_id), *descendants_by_failure.get(source_id, [])
                )
                if candidate is not None
            ]
            if any(not terminal_at_cutoff(candidate) for candidate in lineage):
                active_unrecovered_failure_ids.add(source_id)

    recovered_within_horizon_failure_ids = {
        failure["attempt_id"]
        for failure in failures
        if failure["attempt_id"] in age_qualified_failure_ids
        and (failed_at := _parse_time(failure.get("failed_at"))) is not None
        and (verified_at := recovery_time_by_failure.get(failure["attempt_id"])) is not None
        and failed_at <= verified_at <= failed_at + maturity
    }
    late_recovered_failure_ids = (
        age_qualified_failure_ids
        & recovered_failure_ids
        - recovered_within_horizon_failure_ids
    )
    settled_failed_attempts = len(age_qualified_failure_ids)
    right_censored_failed_attempt_ids = (
        immature_failure_ids | unaged_unrecovered_failure_ids
    )
    recovered_work_items = {
        by_id[failure["attempt_id"]]["work_item_key"]
        for failure, _winner, _depth, _verified_at in recovered
        if failure["attempt_id"] in by_id
    }
    failed_work_items = {
        by_id[failure["attempt_id"]]["work_item_key"]
        for failure in failures if failure["attempt_id"] in by_id
    }
    failures_by_work_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attempts_by_work_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in all_attempts:
        attempts_by_work_item[str(attempt["work_item_key"])].append(attempt)
    for failure in failures:
        if failure["attempt_id"] in by_id:
            failures_by_work_item[
                str(by_id[failure["attempt_id"]]["work_item_key"])
            ].append(failure)
    recovered_work_items = {str(value) for value in recovered_work_items}
    failed_work_items = {str(value) for value in failed_work_items}
    recovery_times_by_work_item: dict[str, list[datetime]] = defaultdict(list)
    for failure, _winner, _depth, verified_at in recovered:
        if failure["attempt_id"] in by_id:
            recovery_times_by_work_item[
                str(by_id[failure["attempt_id"]]["work_item_key"])
            ].append(verified_at)
    active_unrecovered_work_items: set[str] = set()
    age_qualified_failed_work_items: set[str] = set()
    recovered_within_horizon_work_items: set[str] = set()
    late_recovered_work_items: set[str] = set()
    immature_failed_work_items: set[str] = set()
    unaged_unrecovered_work_items: set[str] = set()
    for key in failed_work_items:
        failed_times = [
            _parse_time(failure.get("failed_at"))
            for failure in failures_by_work_item.get(key, [])
        ]
        if not failed_times or any(value is None for value in failed_times):
            unaged_unrecovered_work_items.add(key)
        else:
            origin = min(value for value in failed_times if value is not None)
            if ended - origin < maturity:
                immature_failed_work_items.add(key)
            else:
                age_qualified_failed_work_items.add(key)
                recovery_times = recovery_times_by_work_item.get(key, [])
                if any(origin <= value <= origin + maturity for value in recovery_times):
                    recovered_within_horizon_work_items.add(key)
                elif recovery_times:
                    late_recovered_work_items.add(key)
        if key not in recovered_work_items and any(
            not terminal_at_cutoff(attempt)
            for attempt in attempts_by_work_item.get(key, [])
        ):
            active_unrecovered_work_items.add(key)
    settled_failed_work_items = len(age_qualified_failed_work_items)
    right_censored_failed_work_items = (
        immature_failed_work_items | unaged_unrecovered_work_items
    )
    pr_work_items = {
        attempt["work_item_key"] for attempt in cohort if _attempt_pr_urls(attempt)
    }
    cohort_pr_urls = {
        url for attempt in cohort for url in _attempt_pr_urls(attempt)
    }
    observed_merged_pr_urls = merged_pr_urls & cohort_pr_urls
    parent_by_id = {
        attempt["attempt_id"]: str(attempt["prior_attempt_id"])
        for attempt in all_attempts
        if attempt.get("prior_attempt_id") in by_id
    }

    def connected_lineage(seed_ids: set[str]) -> set[str]:
        lineage = set(seed_ids)
        for seed_id in seed_ids:
            attempt_id = seed_id
            while (parent := parent_by_id.get(attempt_id)) is not None:
                if parent in lineage:
                    break
                lineage.add(parent)
                attempt_id = parent
        queue = list(seed_ids)
        while queue:
            attempt_id = queue.pop(0)
            for child in children.get(attempt_id, []):
                candidate = child["attempt_id"]
                if candidate not in lineage:
                    lineage.add(candidate)
                    queue.append(candidate)
        return lineage

    lineage_by_merged_pr: dict[str, set[str]] = {}
    for url in observed_merged_pr_urls:
        merged_at = merge_times.get(url)
        seeds = {
            attempt["attempt_id"] for attempt in all_attempts
            if url in _attempt_pr_urls(attempt)
            and (
                merged_at is None
                or (
                    (
                        associated_at := _lineage_pr_association_times(
                            [attempt], events_by_attempt, bridge_by_job
                        ).get(url)
                    ) is not None
                    and associated_at <= merged_at
                )
            )
        }
        if seeds:
            lineage_by_merged_pr[url] = {
                attempt_id for attempt_id in connected_lineage(seeds)
                if merged_at is None
                or (
                    (created_at := _parse_time(by_id[attempt_id].get("created_at")))
                    is not None and created_at <= merged_at
                )
            }
    lineage_memberships = sum(len(lineage) for lineage in lineage_by_merged_pr.values())
    merged_repair_attempts = sum(
        _attempt_kind(events_by_attempt[attempt_id]) == "repair"
        for lineage in lineage_by_merged_pr.values() for attempt_id in lineage
    )
    merged_retry_and_repair_attempts = sum(
        _attempt_kind(events_by_attempt[attempt_id]) in {"retry", "absence_retry", "repair"}
        for lineage in lineage_by_merged_pr.values() for attempt_id in lineage
    )
    merged_work_items = {
        by_id[attempt_id]["work_item_key"]
        for lineage in lineage_by_merged_pr.values() for attempt_id in lineage
    }
    merged_lineage_complete = (
        isinstance(current_merged_prs, int)
        and len(lineage_by_merged_pr) == current_merged_prs
        and bool((merge_timestamp_coverage or {}).get("complete"))
    )
    if github_outcomes_available:
        repair_efficiency: dict[str, Any] = {
            "available": True,
            "grain": "unique_authenticated_current_merged_pr_url",
            "observed_merged_prs": current_merged_prs,
            "provider_timed_merged_prs": len(observed_merged_pr_urls),
            "merge_timestamp_coverage": merge_timestamp_coverage,
            "canonical_efficiency_available": merged_lineage_complete,
            "observed_merged_work_items": (
                len(merged_work_items) if merged_lineage_complete else None
            ),
            "provider_timed_merged_work_items": len(merged_work_items),
            "work_items_with_pr": len(pr_work_items),
            "github_pr_observation_coverage": github_observation_coverage,
            "right_censored_prs": right_censored_prs,
            "merged_pr_lineage_coverage": _coverage(
                len(lineage_by_merged_pr), current_merged_prs or 0
            ),
            "lineage_basis": "prior_attempt_id_connected_component_from_attempts_with_exact_pr_url_durably_associated_no_later_than_authenticated_merge_observation",
            "lineage_attempts": lineage_memberships,
            "lineage_attempt_membership_grain": "attempt_per_merged_pr",
            "repair_attempts": merged_repair_attempts,
            "retry_and_repair_attempts": merged_retry_and_repair_attempts,
            "attempts_per_merged_pr": _average(
                lineage_memberships, len(observed_merged_pr_urls)
            ) if merged_lineage_complete else None,
            "repair_attempts_per_merged_pr": _average(
                merged_repair_attempts, len(observed_merged_pr_urls)
            ) if merged_lineage_complete else None,
            "retry_and_repair_attempts_per_merged_pr": _average(
                merged_retry_and_repair_attempts, len(observed_merged_pr_urls)
            ) if merged_lineage_complete else None,
        }
    else:
        repair_efficiency = {
            "available": False,
            "grain": "unique_authenticated_current_merged_pr_url",
            "observed_merged_prs": None,
            "provider_timed_merged_prs": None,
            "merge_timestamp_coverage": None,
            "canonical_efficiency_available": False,
            "observed_merged_work_items": None,
            "provider_timed_merged_work_items": None,
            "work_items_with_pr": len(pr_work_items),
            "github_pr_observation_coverage": None,
            "right_censored_prs": None,
            "merged_pr_lineage_coverage": None,
            "lineage_basis": "unavailable_without_authenticated_github_outcomes",
            "lineage_attempts": None,
            "lineage_attempt_membership_grain": "attempt_per_merged_pr",
            "repair_attempts": None,
            "retry_and_repair_attempts": None,
            "attempts_per_merged_pr": None,
            "repair_attempts_per_merged_pr": None,
            "retry_and_repair_attempts_per_merged_pr": None,
        }
    return {
        "lineage_grain": "failed_cohort_attempt",
        "recovery_maturity_days": RECOVERY_MATURITY_DAYS,
        "recovery_rate_denominator": (
            "failed_attempts_at_least_7d_old"
        ),
        "failed_attempts_with_descendant": failures_with_descendant,
        "failed_attempts_eventually_verified": len(recovered),
        "failed_attempts_verified_within_horizon": len(
            recovered_within_horizon_failure_ids
        ),
        "late_observed_attempt_recoveries": len(late_recovered_failure_ids),
        "early_observed_attempt_recoveries_pending_maturity": len(
            recovered_failure_ids & immature_failure_ids
        ),
        "failure_boundaries_recovered_in_place_to_verified": recovered_in_place,
        "age_qualified_failed_attempts": settled_failed_attempts,
        "settled_failed_attempts": settled_failed_attempts,
        "failed_attempt_recovery_rate": _rate(
            len(recovered_within_horizon_failure_ids), settled_failed_attempts
        ),
        "failed_attempt_recovery_outcome_coverage": _coverage(
            settled_failed_attempts, len(failures)
        ),
        "active_recovery_lineages": len(active_unrecovered_failure_ids),
        "immature_unrecovered_failed_attempts": len(
            immature_failure_ids - recovered_failure_ids
        ),
        "immature_failed_attempts": len(immature_failure_ids),
        "unaged_unrecovered_failed_attempts": len(
            unaged_unrecovered_failure_ids
        ),
        "right_censored_failed_attempts": len(right_censored_failed_attempt_ids),
        "failed_work_items": len(failed_work_items),
        "failed_work_items_eventually_verified": len(recovered_work_items),
        "failed_work_items_verified_within_horizon": len(
            recovered_within_horizon_work_items
        ),
        "late_observed_work_item_recoveries": len(late_recovered_work_items),
        "age_qualified_failed_work_items": settled_failed_work_items,
        "settled_failed_work_items": settled_failed_work_items,
        "failed_work_item_recovery_rate": _rate(
            len(recovered_within_horizon_work_items), settled_failed_work_items
        ),
        "failed_work_item_recovery_outcome_coverage": _coverage(
            settled_failed_work_items, len(failed_work_items)
        ),
        "active_unrecovered_failed_work_items": len(active_unrecovered_work_items),
        "immature_unrecovered_failed_work_items": len(
            immature_failed_work_items - recovered_work_items
        ),
        "immature_failed_work_items": len(immature_failed_work_items),
        "unaged_unrecovered_failed_work_items": len(
            unaged_unrecovered_work_items
        ),
        "right_censored_failed_work_items": len(right_censored_failed_work_items),
        "unrecovered_failed_attempts": len(failures) - len(recovered_failure_ids),
        "failed_attempts_not_recovered_within_horizon": (
            len(age_qualified_failure_ids)
            - len(recovered_within_horizon_failure_ids)
        ),
        "unrecovered_failed_work_items": (
            len(failed_work_items) - len(recovered_work_items)
        ),
        "failed_work_items_not_recovered_within_horizon": (
            len(age_qualified_failed_work_items)
            - len(recovered_within_horizon_work_items)
        ),
        "repairs_and_retries_to_recovery": repairs_to_success,
        "attempt_depth_to_verified": _age_distribution(attempts_to_recovery, len(recovered)),
        "failure_to_retry_reserved_ms": _measured_distribution(
            retry_latency, failures_with_descendant
        ),
        "failure_to_verified_ready_ms": _measured_distribution(
            verified_latency, len(recovered)
        ),
        "recovered_by_origin_classification": [
            {
                "failure_domain": key[0], "failure_stage": key[1],
                "reason_code": key[2], "count": count,
            }
            for key, count in sorted(
                recovered_classification.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "repair_efficiency_on_authenticated_merged_prs": repair_efficiency,
    }


def _daily_activity_series(
    bridge: sqlite3.Connection,
    attempts: list[dict[str, Any]],
    started: datetime,
    ended: datetime,
) -> dict[str, Any]:
    """Bucket durable activity independently of the attempt-creation cohort."""
    attempts = [
        row for row in attempts
        if row["mode"] == "real" and row["actor"] != "legacy-import"
        and ((created := _parse_time(row.get("created_at"))) is None or created <= ended)
    ]
    attempt_ids = {row["attempt_id"] for row in attempts}
    job_ids = {row["job_id"] for row in attempts if row.get("job_id")}
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    effects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Read the narrow timestamp evidence without a lexical SQL cutoff: otherwise
    # malformed timestamps disappear and UTC-offset dates can enter the wrong day.
    for row in bridge.execute(
        "SELECT attempt_id,event_type,created_at FROM factory_attempt_events "
        "WHERE event_type IN ('attempt_verified_ready','attempt_failed','repair_reserved','attempt_effect_committed')"
    ):
        if row["attempt_id"] in attempt_ids:
            events[row["attempt_id"]].append(dict(row))
    for row in bridge.execute(
        "SELECT job_id,event_type,created_at FROM bridge_events WHERE event_type='transition:effect_committed'"
    ):
        if row["job_id"] in job_ids:
            effects[row["job_id"]].append(dict(row))
    for group in (events, effects):
        for rows in group.values():
            rows.sort(key=lambda row: _parse_time(row["created_at"]) or datetime.max.replace(tzinfo=timezone.utc))

    metrics: dict[str, list[datetime | None]] = {
        name: [] for name in ("prs_recorded", "verified_attempts", "repair_attempts", "failed_attempts")
    }
    pr_candidates: dict[str, list[datetime | None]] = defaultdict(list)
    for attempt in attempts:
        rows = events[attempt["attempt_id"]]
        repair_rows = [row for row in rows if row["event_type"] == "repair_reserved"]
        is_repair = bool(repair_rows) or attempt.get("job_requested_action") == "repair_draft_pr"
        for metric, event_type, expected in (
            ("verified_attempts", "attempt_verified_ready", attempt["status"] == "verified_ready"),
            ("failed_attempts", "attempt_failed", attempt["status"] == "failed"),
            ("repair_attempts", "repair_reserved", is_repair),
        ):
            matching = [row for row in rows if row["event_type"] == event_type]
            if matching or expected:
                dates = [_parse_time(row["created_at"]) for row in matching]
                metrics[metric].append(min(dates) if dates and all(date is not None for date in dates) else None)
        # Normal commits mirror the produced URL into prior_pr_url. Only a repair
        # has an inherited target at reservation; never backdate an initial PR.
        repair_dates = [_parse_time(row["created_at"]) for row in repair_rows]
        repair_at = min(repair_dates) if repair_dates and all(at is not None for at in repair_dates) else None
        association_attempt = {
            **attempt, "job_updated_at": None,
            "prior_pr_url": attempt.get("prior_pr_url") if is_repair else None,
            "pr_url": attempt.get("pr_url") or (attempt.get("prior_pr_url") if not is_repair else None),
            "created_at": _timestamp(repair_at) if is_repair and repair_at else None,
        }
        # Mutable job updates are deliberately disabled as a compatibility fallback.
        associations = {url: None for url in _attempt_pr_urls(attempt)}
        associations.update(_lineage_pr_association_times([association_attempt], events, effects))
        direct_effects = [row for row in rows if row["event_type"] == "attempt_effect_committed"]
        direct_effects = direct_effects or effects.get(attempt.get("job_id"), [])
        for url, at in associations.items():
            if url == _github_pr(association_attempt.get("pr_url")) and any(_parse_time(row["created_at"]) is None for row in direct_effects):
                at = None
            if is_repair and url == _github_pr(attempt.get("prior_pr_url")) and repair_at is None:
                at = None
            pr_candidates[url].append(at)
    metrics["prs_recorded"] = [
        min(dates) if all(date is not None for date in dates) else None
        for dates in pr_candidates.values()
    ]
    # A future marker cannot contribute to either today's count or its coverage.
    metrics = {name: [at for at in dates if at is None or at <= ended] for name, dates in metrics.items()}
    coverage = {
        name: _coverage(sum(at is not None for at in dates), len(dates))
        for name, dates in metrics.items()
    }
    daily = {
        name: Counter(at.date().isoformat() for at in dates if at is not None and started <= at)
        for name, dates in metrics.items()
    }
    points = []
    day = started.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= ended:
        date = day.date().isoformat()
        points.append({
            "date": date,
            "partial": day < started or day + timedelta(days=1) > ended,
            **{name: counts[date] if coverage[name]["complete"] else None for name, counts in daily.items()},
        })
        day += timedelta(days=1)
    return {
        "timezone": "UTC", "basis": "event_time",
        "started_at": _timestamp(started), "ended_at": _timestamp(ended),
        "points": points, "coverage": coverage,
    }


def _activity_report(
    attempts: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
    started: datetime,
    ended: datetime,
) -> dict[str, Any]:
    daily: dict[str, Counter[str]] = defaultdict(Counter)

    def mark(name: str, at: datetime | None) -> None:
        if at is not None and started <= at <= ended:
            daily[at.date().isoformat()][name] += 1

    failures = _attempt_failures(attempts, bridge_by_job, events_by_attempt)
    for attempt in attempts:
        events = events_by_attempt[attempt["attempt_id"]]
        mark("attempts_created", _parse_time(attempt.get("created_at")))
        mark("executor_started", _event_time(events, "attempt_started"))
        mark("effect_committed", _effect_time(attempt, events, bridge_by_job))
        mark("verified_ready", _event_time(events, "attempt_verified_ready"))
        for event_type, label in (
            ("execution_route_selected", "routes_selected"),
            ("execution_backend_invoked", "backend_invocations"),
            ("execution_backend_completed", "backend_completions"),
            ("execution_backend_failed", "backend_failures"),
            ("intake_request_deduplicated", "intake_request_deduplications"),
            ("intake_active_job_reused", "active_attempt_reuses"),
        ):
            for event in events:
                if event["event_type"] == event_type:
                    mark(label, _parse_time(event["created_at"]))
        for event in events:
            if event["event_type"] in RECOVERY_EVENTS:
                mark("recovery_events", _parse_time(event["created_at"]))
    for failure in failures:
        mark("attempt_failures", _parse_time(failure.get("failed_at")))

    active = [row for row in attempts if row["status"] not in TERMINAL_ATTEMPT_STATUSES]
    ages = [
        value for row in active
        if (value := _elapsed_ms(_parse_time(row.get("created_at")), ended)) is not None
    ]
    return {
        "grain": "event_observed_in_window",
        "independent_of_attempt_creation_cohort": True,
        "daily": [
            {"date": date, **_counter(counts)} for date, counts in sorted(daily.items())
        ],
        "current_active_backlog": {
            "attempts": len(active),
            "work_items": len({row["work_item_key"] for row in active}),
            "by_status": _counter(Counter(str(row["status"]) for row in active)),
            "age_ms": _age_distribution(ages, len(active)),
        },
    }


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
    eligible = Counter()
    for attempt in attempts:
        events = events_by_attempt[attempt["attempt_id"]]
        created = _parse_time(attempt["created_at"])
        started = _event_time(events, "attempt_started")
        effect = _effect_time(attempt, events, bridge_by_job)
        verified = _event_time(events, "attempt_verified_ready")
        terminal = min((time for event in events if event["event_type"] in TERMINAL_ATTEMPT_EVENTS if (time := _parse_time(event["created_at"]))), default=None)
        if terminal is None and attempt["status"] in TERMINAL_ATTEMPT_STATUSES:
            terminal = _parse_time(attempt.get("updated_at"))
        eligible["attempt_to_executor_started"] += 1
        eligible["attempt_to_terminal"] += 1
        if started is not None:
            eligible["executor_started_to_effect_committed"] += 1
        if effect is not None:
            eligible["effect_committed_to_verified_ready"] += 1
        _duration(values["attempt_to_executor_started"], created, started)
        _duration(values["executor_started_to_effect_committed"], started, effect)
        _duration(values["effect_committed_to_verified_ready"], effect, verified)
        _duration(values["attempt_to_terminal"], created, terminal)
    return {
        key: {
            **_distribution(values[key]),
            "eligible": eligible[key],
            "coverage_rate": _rate(len(values[key]), eligible[key]),
        }
        for key in (
            "attempt_to_executor_started",
            "executor_started_to_effect_committed",
            "effect_committed_to_verified_ready",
            "attempt_to_terminal",
        )
    }


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


def _measured_distribution(values: Iterable[float], eligible: int) -> dict[str, Any]:
    materialized = list(values)
    return {
        **_distribution(materialized),
        "eligible": eligible,
        "coverage_rate": _rate(len(materialized), eligible),
    }


def _age_distribution(
    values: Iterable[float], eligible: int | None = None
) -> dict[str, int | float | None]:
    materialized = list(values)
    result = _distribution(materialized)
    ordered = sorted(max(0, round(value)) for value in materialized if math.isfinite(value) and value >= 0)
    report: dict[str, int | float | None] = {
        **result, "max": ordered[-1] if ordered else None
    }
    if eligible is not None:
        report.update({
            "eligible": eligible,
            "coverage_rate": _rate(len(ordered), eligible),
        })
    return report


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


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


def _evidence_bounded_coverage(
    observed: int,
    eligible: int,
    evidence_observed: int,
) -> dict[str, Any]:
    """Return a rate only when evidence covers the entire eligible cohort."""
    evidence_complete = evidence_observed == eligible
    return {
        "observed": observed,
        "eligible": eligible,
        "rate": _rate(observed, eligible) if evidence_complete else None,
        "complete": evidence_complete,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _average(total: int, count: int) -> float | None:
    return round(total / count, 4) if count else None


def _attempt_kind(events: list[dict[str, Any]]) -> str:
    return next((RESERVATION_KINDS[event["event_type"]] for event in events if event["event_type"] in RESERVATION_KINDS), "unknown")


def _attempt_pr_urls(attempt: dict[str, Any]) -> set[str]:
    """Return exact PR identities carried by the job or inherited repair target."""
    urls: set[str] = set()
    for value in (attempt.get("pr_url"), attempt.get("prior_pr_url")):
        url = _github_pr(value)
        if url is not None:
            urls.add(url)
    return urls


def _lineage_pr_association_times(
    lineage: list[dict[str, Any]],
    events_by_attempt: dict[str, list[dict[str, Any]]],
    bridge_by_job: dict[str, list[dict[str, Any]]],
) -> dict[str, datetime | None]:
    """Return the earliest durable time Factory can bind each PR to a lineage.

    An inherited repair target is known when that attempt is reserved. A PR
    produced by an execution is known at effect commit; ``job_updated_at`` is
    a conservative compatibility upper bound for historical rows missing the
    commit marker. Unknown association time stays unknown so a later lineage
    link cannot retroactively claim an earlier GitHub outcome.
    """
    candidates: dict[str, list[datetime]] = defaultdict(list)
    observed_urls: set[str] = set()
    for attempt in lineage:
        prior_url = _github_pr(attempt.get("prior_pr_url"))
        direct_url = _github_pr(attempt.get("pr_url"))
        if prior_url is not None:
            observed_urls.add(prior_url)
            if (created_at := _parse_time(attempt.get("created_at"))) is not None:
                candidates[prior_url].append(created_at)
        if direct_url is not None:
            observed_urls.add(direct_url)
            associated_at = _effect_time(
                attempt,
                events_by_attempt.get(attempt["attempt_id"], []),
                bridge_by_job,
            ) or _parse_time(attempt.get("job_updated_at"))
            if associated_at is not None:
                candidates[direct_url].append(associated_at)
    return {
        url: min(candidates[url]) if candidates.get(url) else None
        for url in observed_urls
    }


def _github_state_at_or_after(
    observations: list[dict[str, Any]],
    boundary: datetime | None,
    observed_through: datetime,
    *,
    require_provider_time: bool = False,
) -> str | None:
    eligible = [
        row for row in observations
        if (
            observed_at := (
                _provider_observation_time(row)
                if require_provider_time else _observation_time(row)
            )
        ) is not None
        and observed_at <= observed_through
        and (boundary is None or observed_at >= boundary)
        and _github_state(row["normalized"]) != "unknown"
    ]
    if not eligible:
        return None
    latest = max(eligible, key=_github_observation_order)
    return _github_state(latest["normalized"])


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


def _is_json_object(raw: Any) -> bool:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, dict)


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
