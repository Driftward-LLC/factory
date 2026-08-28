from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .legacy_artifacts import (
    LEGACY_IMPORT_CONFLICT_REASONS,
    deterministic_branch,
    json_object,
    reconcile_legacy_active_row,
    routing_repositories,
)
from .runtime import acquire_shared, migration_capability, release

DEFAULT_DB_PATH = "/var/lib/driftward-hatchet-bridge/bridge.db"
SCHEMA_VERSION = 4
RESOLVER_SEMANTIC_VERSION = "legacy-conflict-attestation-v1"
# The contract gives names to immutable attestation fields.  The attestation
# identity also binds the transitive source closure that implements these
# fields; changing labels here is not sufficient to cover code changes.
RESOLVER_ATTESTATION_CONTRACT = {
    "semantic_version": RESOLVER_SEMANTIC_VERSION,
    "evidence_schema": "legacy-conflict-resolution-v1",
    "reason_code": "legacy_receipt_revalidated",
    "operator": "migration",
}
RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$")
LINEAR_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")

ALLOWED_ACTIONS = {"create_draft_pr", "repair_draft_pr", "inspect_draft_pr_state"}
DRAFT_PR_EFFECT_ACTIONS = {"create_draft_pr", "repair_draft_pr"}
REPAIRABLE_VERIFICATION_REASONS = {
    "verification_github_check_failed",
    "verification_github_required_check_missing",
    "verification_github_checks_timeout",
}
ACTIVE_STATES = {"accepted", "running", "effect_committed", "verification_pending"}
TERMINAL_STATES = {"completed", "ready_for_human_review", "failed"}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES
FACTORY_ATTEMPT_ACTIVE = {"reserved", "accepted", "running", "effect_authorized", "effect_committed", "verification_pending", "cancellation_requested", "effect_outcome_unknown"}
FACTORY_ATTEMPT_TERMINAL = {"completed", "failed", "verified_ready", "canceled", "canceled_after_effect", "superseded"}
MIGRATION_FAILPOINT_STAGES = {
    "after-ddl", "during-backfill", "during-resolution-insertion",
    "during-attestation-insertion",
    "before-version-update", "before-commit",
}
RECONCILIATION_KINDS = {
    "bridge_job_without_events", "bridge_job_missing_accepted_event", "bridge_job_event_history_incomplete", "stale_active_bridge_job",
    "running_create_draft_pr_missing_repo_or_branch", "effect_committed_without_pr_url", "verification_pending_without_pr_url",
    "completed_create_draft_pr_without_pr_url", "ready_for_human_review_without_pr_url", "failed_job_has_external_effect_refs",
    "duplicate_bridge_branch", "duplicate_bridge_pr_url", "reconciliation_scan_incomplete",
    "ingress_inventory_unavailable", "ingress_inventory_incomplete", "ingress_stored_not_published",
    "ingress_published_unconsumed", "hatchet_inventory_unavailable", "hatchet_inventory_incomplete",
    "hatchet_run_without_bridge", "hatchet_run_not_found", "hatchet_run_timeout", "hatchet_run_unavailable",
    "hatchet_run_malformed", "hatchet_run_auth", "hatchet_task_missing_from_run",
    "hatchet_terminal_run_with_active_bridge_job", "hatchet_provenance_missing", "hermes_execution_ref_missing",
    "hermes_receipt_contradicts_bridge_history", "github_pr_observation_missing", "github_observation_timeout",
    "github_observation_unavailable", "github_observation_malformed", "github_observation_auth",
    "github_observation_stale", "github_pr_url_mismatch", "github_pr_repo_mismatch", "github_pr_branch_mismatch", "github_pr_commit_mismatch", "github_pr_not_draft",
    "github_pr_closed_unmerged_after_effect", "github_pr_missing_linear_link", "linear_issue_observation_missing",
    "linear_observation_timeout", "linear_observation_unavailable", "linear_observation_malformed",
    "linear_observation_auth", "linear_observation_stale", "linear_terminal_issue_with_active_bridge_job",
    "linear_issue_blocked_with_active_bridge_job", "linear_issue_ineligible_with_active_bridge_job", "linear_route_mismatch",
}

TRANSITIONS = {
    "accepted": {"running", "failed"},
    "running": {"effect_committed", "completed", "failed"},
    "effect_committed": {"completed", "verification_pending", "failed"},
    "verification_pending": {"ready_for_human_review", "failed"},
    "completed": set(),
    "ready_for_human_review": set(),
    "failed": set(),
}


class BridgeError(Exception):
    """Base bridge exception."""


class BridgeConflict(BridgeError):
    """Raised when an idempotency key is reused for different intent."""


class BridgeNotFound(BridgeError):
    """Raised when a job cannot be found."""


class BridgeValidationError(BridgeError):
    """Raised when a request is malformed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sanitize_detail(value: Any, limit: int = 320) -> Any:
    if isinstance(value, str):
        import re
        return re.sub(r"(?:ghp_|github_pat_|xox[baprs]-|Bearer\s+)[A-Za-z0-9_.-]+", "[redacted]", value, flags=re.I).replace("\n", " ").replace("\r", " ")[:limit]
    if isinstance(value, list): return [sanitize_detail(item, limit) for item in value]
    if isinstance(value, dict): return {str(key): sanitize_detail(item, limit) for key, item in value.items()}
    return value


CORRELATION_PAYLOAD_KEYS = {"source_event_key", "source_delivery_id", "source_event_keys", "source_delivery_ids"}
FINDING_FIELDS = {"finding_key", "job_id", "subject", "kind", "severity", "details"}
FINDING_DETAIL_FIELDS = {"expected", "observed"}


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(_effect_payload(payload)).encode("utf-8")).hexdigest()


def _effect_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key not in CORRELATION_PAYLOAD_KEYS}


class BridgeConnection(sqlite3.Connection):
    _maintenance_fd: int | None = None
    def close(self) -> None:
        try: super().close()
        finally:
            if self._maintenance_fd is not None: release(self._maintenance_fd); self._maintenance_fd=None


def _connection(path: str, *, check_same_thread: bool, migration: bool = False) -> BridgeConnection:
    fd = migration_capability() if migration else acquire_shared()
    try:
        conn = sqlite3.connect(path, check_same_thread=check_same_thread, factory=BridgeConnection)
        conn._maintenance_fd=fd
        return conn
    except Exception: release(fd); raise


def connect_migrate(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the installer-only migration connection with exclusive capability."""
    _validate_migration_failpoint()
    parent = Path(db_path).parent
    if parent and str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)

    conn = _connection(db_path, check_same_thread=False, migration=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        init_db(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _require_runtime_schema(conn: sqlite3.Connection) -> None:
    required = {
        "bridge_schema_meta", "bridge_jobs", "bridge_events", "factory_attempts",
        "factory_attempt_events", "factory_attempt_outbox", "factory_execution_leases",
        "factory_import_conflicts", "factory_import_conflict_resolutions",
        "factory_import_conflict_attestations",
    }
    found = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= found:
        raise BridgeValidationError("bridge migration is required before runtime access")
    version = conn.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()
    if not version or version[0] != SCHEMA_VERSION:
        raise BridgeValidationError("bridge migration is required before runtime access")


def connect_runtime(db_path: str = DEFAULT_DB_PATH, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """Open an existing migrated ledger without DDL, backfill, or WAL setup."""
    path = Path(db_path)
    if not path.is_file():
        raise BridgeValidationError("bridge migration is required before runtime access")
    conn: sqlite3.Connection | None = None
    try:
        conn = _connection(str(path), check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        _require_runtime_schema(conn)
        return conn
    except RuntimeError as exc:
        raise BridgeValidationError(str(exc)) from exc
    except (sqlite3.Error, BridgeValidationError) as exc:
        if conn is not None:
            conn.close()
        raise BridgeValidationError("bridge migration is required before runtime access") from exc


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Backward-compatible runtime connection; it never migrates implicitly."""
    return connect_runtime(db_path)


def connect_readonly(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the operator projection without migrations, WAL changes, or writes."""
    path = Path(db_path)
    if not path.is_file():
        raise BridgeValidationError("bridge predeploy is required before read-only operator access")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _require_runtime_schema(conn)
        return conn
    except sqlite3.Error as exc:
        raise BridgeValidationError("bridge predeploy is required before read-only operator access") from exc


def init_db(conn: sqlite3.Connection) -> None:
    _validate_migration_failpoint()
    schema_sql = """
        CREATE TABLE IF NOT EXISTS bridge_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            requested_action TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            branch TEXT,
            commit_sha TEXT,
            pr_url TEXT,
            hermes_session_id TEXT,
            hatchet_workflow_run_id TEXT,
            hatchet_step_run_id TEXT,
            verification_context_json TEXT,
            verification_original_context_json TEXT,
            verification_observed_context_json TEXT,
            verification_observed_digest TEXT,
            verification_receipt_json TEXT,
            verification_digest TEXT,
            reason_code TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bridge_events (
            event_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            state TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );

        CREATE INDEX IF NOT EXISTS idx_bridge_jobs_state
            ON bridge_jobs(state, updated_at);

        CREATE INDEX IF NOT EXISTS idx_bridge_events_job
            ON bridge_events(job_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bridge_events_lifecycle
            ON bridge_events(created_at, event_id);

        CREATE TABLE IF NOT EXISTS verification_leases (
            job_id TEXT PRIMARY KEY, lease_key TEXT NOT NULL UNIQUE, action TEXT NOT NULL,
            owner_attempt_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            claimed_at TEXT, closed_at TEXT, outcome TEXT, work_item_key TEXT, branch TEXT, pr_url TEXT,
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );

        CREATE TABLE IF NOT EXISTS factory_attempts (
            attempt_id TEXT PRIMARY KEY,
            work_item_key TEXT NOT NULL,
            action TEXT NOT NULL,
            mode TEXT NOT NULL,
            generation INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            job_id TEXT,
            prior_attempt_id TEXT,
            prior_job_id TEXT,
            prior_pr_url TEXT,
            supersede_pr_url TEXT,
            artifact_fence_json TEXT,
            trusted_snapshot_digest TEXT,
            reason TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_item_key, action, mode, generation),
            UNIQUE(work_item_key, action, mode, request_id),
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS factory_attempt_events (
            event_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(attempt_id) REFERENCES factory_attempts(attempt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_factory_attempts_work_item
            ON factory_attempts(work_item_key, action, mode, generation DESC);
        CREATE INDEX IF NOT EXISTS idx_factory_attempt_events_attempt
            ON factory_attempt_events(attempt_id, created_at);

        CREATE TABLE IF NOT EXISTS factory_execution_leases (
            attempt_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 1, fence_token TEXT NOT NULL DEFAULT '',
            last_heartbeat_at TEXT, effect_authorized_at TEXT, released_at TEXT, cancel_requested_at TEXT, effect_observed_at TEXT,
            FOREIGN KEY(attempt_id) REFERENCES factory_attempts(attempt_id)
        );
        CREATE TABLE IF NOT EXISTS factory_attempt_outbox (
            outbox_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, job_id TEXT NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
            delivery_attempts INTEGER NOT NULL DEFAULT 0, acknowledged_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(attempt_id, event_type), FOREIGN KEY(attempt_id) REFERENCES factory_attempts(attempt_id),
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS factory_inspection_outbox (
            outbox_key TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL, state TEXT NOT NULL, delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivery_lease_id TEXT, delivery_lease_expires_at TEXT, acknowledged_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS factory_inspection_requests (
            request_key TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
            url TEXT NOT NULL, job_id TEXT, work_item_key TEXT, prior_attempt_id TEXT,
            capability TEXT, state TEXT NOT NULL, result_json TEXT, result_digest TEXT, observed_at TEXT, reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(repository, number, url)
        );
        CREATE TABLE IF NOT EXISTS factory_recovery_requests (
            request_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL, evidence_digest TEXT NOT NULL, result_json TEXT,
            state TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(attempt_id) REFERENCES factory_attempts(attempt_id),
            FOREIGN KEY(job_id) REFERENCES bridge_jobs(job_id)
        );
        CREATE TABLE IF NOT EXISTS factory_import_conflicts (
            conflict_key TEXT PRIMARY KEY, work_item_key TEXT, job_id TEXT, reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bridge_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS factory_import_conflict_resolutions (
            conflict_key TEXT PRIMARY KEY,
            resolution_id TEXT NOT NULL UNIQUE,
            job_id TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            code_version TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            operator TEXT NOT NULL,
            decision_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(conflict_key) REFERENCES factory_import_conflicts(conflict_key)
        );
        CREATE TABLE IF NOT EXISTS factory_import_conflict_attestations (
            conflict_key TEXT NOT NULL,
            resolver_semantic_version TEXT NOT NULL CHECK(length(resolver_semantic_version) BETWEEN 1 AND 120),
            resolver_contract_digest TEXT NOT NULL CHECK(length(resolver_contract_digest)=64 AND resolver_contract_digest NOT GLOB '*[^0-9a-f]*'),
            routing_policy_digest TEXT NOT NULL CHECK(length(routing_policy_digest)=64 AND routing_policy_digest NOT GLOB '*[^0-9a-f]*'),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
            job_id TEXT NOT NULL CHECK(length(job_id)>0),
            reason_code TEXT NOT NULL CHECK(length(reason_code)>0),
            decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
            producing_release_sha TEXT NOT NULL CHECK(producing_release_sha='migration-unversioned' OR (length(producing_release_sha)=40 AND producing_release_sha NOT GLOB '*[^0-9a-f]*')),
            operator TEXT NOT NULL CHECK(length(operator)>0),
            created_at TEXT NOT NULL,
            UNIQUE(conflict_key,resolver_semantic_version,resolver_contract_digest,routing_policy_digest),
            FOREIGN KEY(conflict_key) REFERENCES factory_import_conflicts(conflict_key)
        );
        CREATE INDEX IF NOT EXISTS idx_factory_import_conflict_attestations_conflict
            ON factory_import_conflict_attestations(conflict_key, created_at);
    """

    # All backfill and marker writes are one SQLite transaction.  This is the
    # only call path allowed to alter schema/history; runtime connections never
    # invoke it.
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in (item.strip() for item in schema_sql.split(";") if item.strip()):
            conn.execute(statement)
        conn.execute("CREATE TRIGGER IF NOT EXISTS protect_factory_import_conflict_attestations_update BEFORE UPDATE ON factory_import_conflict_attestations FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'factory import conflict attestations are append-only'); END")
        conn.execute("CREATE TRIGGER IF NOT EXISTS protect_factory_import_conflict_attestations_delete BEFORE DELETE ON factory_import_conflict_attestations FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'factory import conflict attestations are append-only'); END")
        _migration_failpoint("after-ddl")
        _migrate_bridge_verification_columns(conn)
        _migrate_reconciliation_findings(conn)
        _migrate_factory_retry_columns(conn)
        _migrate_conflict_resolution_columns(conn)
        _migrate_canonical_times(conn, "bridge_jobs", ("created_at", "updated_at"))
        _migrate_canonical_times(conn, "bridge_events", ("created_at",))
        _backfill_factory_attempts(conn)
        _migration_failpoint("during-backfill")
        _append_deterministic_conflict_resolutions(conn, operator="migration")
        _migration_failpoint("during-resolution-insertion")
        _append_deterministic_conflict_attestations(conn, operator="migration")
        _migration_failpoint("during-attestation-insertion")
        _migration_failpoint("before-version-update")
        conn.execute(
            "INSERT INTO bridge_schema_meta(singleton,schema_version,migrated_at) VALUES (1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version,migrated_at=excluded.migrated_at",
            (SCHEMA_VERSION, utc_now()),
        )
        _migration_failpoint("before-commit")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_failpoint(stage: str) -> None:
    """Test-only transactional crash point; production leaves it unset."""
    if os.environ.get("HATCHET_BRIDGE_MIGRATION_FAIL_AT") == stage:
        raise BridgeError(f"injected migration failure: {stage}")


def _validate_migration_failpoint() -> None:
    configured = os.environ.get("HATCHET_BRIDGE_MIGRATION_FAIL_AT")
    if configured and configured not in MIGRATION_FAILPOINT_STAGES:
        raise BridgeError(f"unknown migration failpoint: {configured}")


def _legacy_attempt_key(payload: dict[str, Any]) -> str | None:
    key = payload.get("linear_issue") or payload.get("work_item_id")
    return key if isinstance(key, str) and LINEAR_KEY_RE.fullmatch(key) else None


def _backfill_factory_attempts(conn: sqlite3.Connection) -> None:
    # Legacy active-key jobs are immutable generation one records. A malformed
    # legacy payload is intentionally left unimported so operator preview fails
    # visibly instead of inventing a retry lineage.
    rows = conn.execute("SELECT * FROM bridge_jobs WHERE idempotency_key LIKE 'hatchet:work-item:linear:%:create_draft_pr:%:active'").fetchall()
    default_registry = Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json"
    registry_path = Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(default_registry)))
    try:
        routed_repositories, enabled_repositories = routing_repositories(registry_path.read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        routed_repositories, enabled_repositories = set(), set()
    seen_work_items: set[tuple[str, str]] = set()
    seen_urls: dict[str, str] = {}
    seen_branches: dict[str, str] = {}
    for row in rows:
        payload: dict[str, Any] | None = None
        try:
            source = dict(row)
            payload = json_object(source["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            _record_import_conflict(conn, None, row["job_id"], "legacy_payload_malformed")
            continue
        try:
            work_item = _legacy_attempt_key(payload)
            mode = "dry-run" if payload.get("dry_run") is True else "real"
            if not work_item:
                raise ValueError("legacy work item is invalid")
            attempts = [dict(item) for item in conn.execute(
                "SELECT * FROM factory_attempts WHERE job_id=? OR work_item_key=?",
                (source["job_id"], work_item),
            )]
            conflicts = [dict(item) for item in conn.execute(
                f"SELECT * FROM factory_import_conflicts WHERE (job_id=? OR work_item_key=?) AND reason IN ({','.join('?' for _ in LEGACY_IMPORT_CONFLICT_REASONS)})",
                (source["job_id"], work_item or "", *sorted(LEGACY_IMPORT_CONFLICT_REASONS)),
            )]
            attempt_ids = [item["attempt_id"] for item in attempts if isinstance(item.get("attempt_id"), str)]
            leases = [dict(item) for item in conn.execute(
                f"SELECT * FROM factory_execution_leases WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})",
                attempt_ids,
            )] if attempt_ids else []
            attempt_events = [dict(item) for item in conn.execute(
                f"SELECT * FROM factory_attempt_events WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})",
                attempt_ids,
            )] if attempt_ids else []
            attempt_outbox = [dict(item) for item in conn.execute(
                f"SELECT * FROM factory_attempt_outbox WHERE job_id=? OR attempt_id IN ({','.join('?' for _ in attempt_ids)})",
                (source["job_id"], *attempt_ids),
            )] if attempt_ids else [dict(item) for item in conn.execute("SELECT * FROM factory_attempt_outbox WHERE job_id=?", (source["job_id"],))]
            def artifacts() -> Any:
                yield ("bridge", {"pr_url": source["pr_url"], "branch": source["branch"]})
                for event in conn.execute("SELECT details_json FROM bridge_events WHERE job_id=?", (source["job_id"],)):
                    yield ("event", json_object(event["details_json"]))
                yield from _legacy_inspection_sources(conn, work_item or "")

            decision = reconcile_legacy_active_row(
                source,
                artifact_sources=artifacts(),
                attempts=attempts,
                import_conflicts=conflicts,
                execution_leases=leases,
                attempt_events=attempt_events,
                attempt_outbox=attempt_outbox,
                routed_repositories=routed_repositories,
                enabled_repositories=enabled_repositories,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            _record_import_conflict(conn, _legacy_attempt_key(payload), row["job_id"], "legacy_artifact_reference_conflict")
            continue
        if decision.action == "not_legacy":
            continue
        lineage = (decision.work_item_key, decision.mode)
        if lineage in seen_work_items:
            _record_import_conflict(conn, decision.work_item_key, source["job_id"], "duplicate_legacy_work_item")
            continue
        seen_work_items.add(lineage)
        if decision.prior_pr_url:
            if decision.prior_pr_url in seen_urls and seen_urls[decision.prior_pr_url] != source["job_id"]:
                _record_import_conflict(conn, decision.work_item_key, source["job_id"], "duplicate_legacy_artifact_reference")
                continue
            seen_urls[decision.prior_pr_url] = source["job_id"]
        if decision.branch in seen_branches and seen_branches[decision.branch] != source["job_id"]:
            _record_import_conflict(conn, decision.work_item_key, source["job_id"], "duplicate_legacy_artifact_reference")
            continue
        seen_branches[decision.branch] = source["job_id"]
        if decision.action == "already_imported":
            continue
        now = source["created_at"]
        attempt_id = "legacy-" + source["job_id"]
        conn.execute("INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,prior_pr_url,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, decision.work_item_key, "create_draft_pr", decision.mode, 1, "legacy:" + source["job_id"], source["idempotency_key"], decision.branch, "legacy_" + source["state"], source["job_id"], decision.prior_pr_url, "legacy-import", now, now))
        _append_factory_attempt_event(conn, attempt_id, "legacy_imported", {"job_id": source["job_id"], "artifact_present": bool(decision.prior_pr_url)})
        if decision.prior_pr_url:
            _queue_pr_inspection(conn, decision.work_item_key, decision.prior_pr_url, attempt_id)


def _record_import_conflict(conn: sqlite3.Connection, work_item_key: str | None, job_id: str, reason: str) -> None:
    conn.execute("INSERT OR IGNORE INTO factory_import_conflicts (conflict_key,work_item_key,job_id,reason,created_at) VALUES (?,?,?,?,?)", (f"{job_id}:{reason}", work_item_key, job_id, reason, utc_now()))


def _routing_policy_digest(registry_path: Path) -> str:
    """Digest only routing semantics; formatting and route order are irrelevant."""
    parsed = json.loads(registry_path.read_text())
    if not isinstance(parsed, dict) or not isinstance(parsed.get("version"), int) or not isinstance(parsed.get("routes"), list):
        raise ValueError("routing registry is invalid")
    normalized: list[dict[str, Any]] = []
    for route in parsed["routes"]:
        if not isinstance(route, dict):
            raise ValueError("routing registry is invalid")
        project_id, repository, enabled = route.get("project_id"), route.get("repo"), route.get("execution_enabled")
        if not isinstance(project_id, str) or not isinstance(repository, str) or not isinstance(enabled, bool):
            raise ValueError("routing registry is invalid")
        normalized.append({"project_id": project_id, "repo": repository, "execution_enabled": enabled})
    normalized.sort(key=lambda item: (item["repo"], item["project_id"], item["execution_enabled"]))
    # Keep routing_repositories as the authoritative syntax/duplicate validator.
    routing_repositories(registry_path.read_text())
    return hashlib.sha256(canonical_json({"version": parsed["version"], "routes": normalized}).encode()).hexdigest()


def _resolver_attestation_contract() -> dict[str, str]:
    contract = dict(RESOLVER_ATTESTATION_CONTRACT)
    expected = {"semantic_version", "evidence_schema", "reason_code", "operator"}
    if set(contract) != expected or any(not isinstance(value, str) or not value for value in contract.values()):
        raise BridgeValidationError("resolver attestation contract is invalid")
    if contract["semantic_version"] != RESOLVER_SEMANTIC_VERSION or contract["operator"] != "migration":
        raise BridgeValidationError("resolver attestation contract is invalid")
    return contract


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Return a copy whose documentation-only nodes cannot perturb identity."""
    copied = copy.deepcopy(node)
    for item in ast.walk(copied):
        if isinstance(item, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.body:
            first = item.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                item.body.pop(0)
    return copied


def _canonical_ast(node: ast.AST) -> Any:
    """Serialize syntax without source positions, comments, or formatting."""
    if isinstance(node, ast.AST):
        return {
            "type": type(node).__name__,
            "fields": [[field, _canonical_ast(getattr(node, field))] for field in node._fields],
        }
    if isinstance(node, list):
        return [_canonical_ast(item) for item in node]
    if isinstance(node, tuple):
        return [_canonical_ast(item) for item in node]
    if node is Ellipsis:
        return {"literal": "Ellipsis"}
    return node


def _resolver_source_closure(module_name: str, source: str, roots: tuple[str, ...]) -> list[dict[str, Any]]:
    """Select only resolver-reachable top-level code, failing closed on drift."""
    try:
        tree = ast.parse(source, filename=module_name)
    except SyntaxError as exc:
        raise BridgeValidationError("resolver implementation source is invalid") from exc
    bindings: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[statement.name] = statement
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            bindings[statement.target.id] = statement
    missing = set(roots) - set(bindings)
    if missing:
        raise BridgeValidationError("resolver implementation source is incomplete")
    selected = set(roots)
    pending = list(roots)
    while pending:
        statement = bindings[pending.pop()]
        for reference in ast.walk(statement):
            if isinstance(reference, ast.Name) and isinstance(reference.ctx, ast.Load) and reference.id in bindings and reference.id not in selected:
                selected.add(reference.id)
                pending.append(reference.id)
    return [
        {"name": name, "syntax": _canonical_ast(_strip_docstrings(bindings[name]))}
        for name in sorted(selected)
    ]


def _resolver_contract_digest(*, source_overrides: Mapping[str, str] | None = None) -> str:
    """Bind attestations to the resolver's reviewed implementation contract.

    The only selected code is the dependency closure of reconciliation,
    artifact parsing, and evidence construction.  This keeps a commit-only
    release (or unrelated helper/comment/formatting change) stable while any
    relevant source or canonical-contract change needs fresh attestations.
    """
    overrides = dict(source_overrides or {})
    unexpected = set(overrides) - {"store", "legacy_artifacts"}
    if unexpected or any(not isinstance(value, str) for value in overrides.values()):
        raise BridgeValidationError("resolver implementation source override is invalid")
    sources = {
        "store": overrides.get("store", Path(__file__).read_text(encoding="utf-8")),
        "legacy_artifacts": overrides.get("legacy_artifacts", Path(__file__).with_name("legacy_artifacts.py").read_text(encoding="utf-8")),
    }
    try:
        contract = _resolver_attestation_contract()
        modules = {
            "store": _resolver_source_closure(
                "store", sources["store"],
                ("_legacy_resolution_evidence", "_legacy_attempt_key", "_legacy_inspection_sources", "_valid_v3_resolution", "_resolver_attestation_contract"),
            ),
            "legacy_artifacts": _resolver_source_closure(
                "legacy_artifacts", sources["legacy_artifacts"],
                ("reconcile_legacy_active_row", "json_object", "routing_repositories"),
            ),
        }
    except (OSError, TypeError, ValueError) as exc:
        raise BridgeValidationError("resolver implementation contract is unavailable") from exc
    payload = {"schema": "resolver-implementation-contract-v1", "contract": contract, "modules": modules}
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _release_version_for_audit() -> str:
    value = os.environ.get("DRIFTWARD_BRIDGE_RELEASE_VERSION", "migration-unversioned")
    # Unit tests and one-off forensic copies have no immutable release.  The
    # production installer always supplies an exact SHA; it remains audit data
    # and is intentionally excluded from the attestation identity.
    if not isinstance(value, str) or not value or (value != "migration-unversioned" and not RELEASE_SHA_RE.fullmatch(value)):
        raise BridgeValidationError("bridge release version is required for conflict resolution")
    return value


def _legacy_resolution_evidence(conn: sqlite3.Connection, conflict: Mapping[str, Any], *, routing_registry: Path | None = None) -> tuple[str, str, str] | None:
    """Return a deterministic resolution digest only for superseded derivations.

    This deliberately reuses the same pure legacy decision boundary as
    predeploy/backfill.  A conflict row is immutable evidence; a resolution is
    permitted only when today's complete source/receipt lineage proves that the
    earlier derived conflict is no longer valid.
    """
    if conflict.get("reason") != "legacy_artifact_reference_conflict":
        return None
    row = conn.execute("SELECT * FROM bridge_jobs WHERE job_id=?", (conflict.get("job_id"),)).fetchone()
    if not row:
        return None
    source = dict(row)
    try:
        contract = _resolver_attestation_contract()
        payload = json_object(source["payload_json"])
        work_item = _legacy_attempt_key(payload)
        if not work_item:
            return None
        attempts = [dict(item) for item in conn.execute("SELECT * FROM factory_attempts WHERE job_id=? OR work_item_key=?", (source["job_id"], work_item))]
        attempt_ids = [item["attempt_id"] for item in attempts if isinstance(item.get("attempt_id"), str)]
        leases = [dict(item) for item in conn.execute(
            f"SELECT * FROM factory_execution_leases WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})", attempt_ids,
        )] if attempt_ids else []
        events = [dict(item) for item in conn.execute(
            f"SELECT * FROM factory_attempt_events WHERE attempt_id IN ({','.join('?' for _ in attempt_ids)})", attempt_ids,
        )] if attempt_ids else []
        outbox = [dict(item) for item in conn.execute(
            f"SELECT * FROM factory_attempt_outbox WHERE job_id=? OR attempt_id IN ({','.join('?' for _ in attempt_ids)})", (source["job_id"], *attempt_ids),
        )] if attempt_ids else []
        def artifacts() -> Any:
            yield ("bridge", {"pr_url": source.get("pr_url"), "branch": source.get("branch")})
            for event in conn.execute("SELECT details_json FROM bridge_events WHERE job_id=?", (source["job_id"],)):
                yield ("event", json_object(event["details_json"]))
            yield from _legacy_inspection_sources(conn, work_item)
        registry_path = routing_registry or Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json")))
        routed, enabled = routing_repositories(registry_path.read_text())
        decision = reconcile_legacy_active_row(
            source, artifact_sources=artifacts(), attempts=attempts, import_conflicts=(),
            execution_leases=leases, attempt_events=events, attempt_outbox=outbox,
            routed_repositories=routed, enabled_repositories=enabled,
        )
        if decision.action != "already_imported":
            return None
        evidence = {
            "schema": contract["evidence_schema"], "conflict": dict(conflict),
            "source": source, "attempts": attempts, "leases": leases,
            "attempt_events": events, "attempt_outbox": outbox,
            "decision": {"action": decision.action, "work_item": decision.work_item_key, "mode": decision.mode, "repository": decision.repository, "branch": decision.branch, "prior_pr_url": decision.prior_pr_url, "attempt_id": decision.attempt_id},
        }
        decision_json = canonical_json(evidence["decision"])
        return hashlib.sha256(canonical_json(evidence).encode()).hexdigest(), decision.work_item_key, decision_json
    except (OSError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError):
        return None


def _append_deterministic_conflict_resolutions(conn: sqlite3.Connection, *, operator: str) -> None:
    code_version = _release_version_for_audit()
    contract = _resolver_attestation_contract()
    conflicts = [dict(row) for row in conn.execute("SELECT * FROM factory_import_conflicts ORDER BY conflict_key")]
    for conflict in conflicts:
        derived = _legacy_resolution_evidence(conn, conflict)
        if not derived:
            continue
        digest, _work_item, decision_json = derived
        resolution_id = f"legacy-resolution:{conflict['conflict_key']}"
        existing = conn.execute("SELECT resolution_id,job_id,evidence_digest,code_version,reason_code,operator,decision_json,created_at FROM factory_import_conflict_resolutions WHERE conflict_key=?", (conflict["conflict_key"],)).fetchone()
        expected = (resolution_id, conflict["job_id"], digest, code_version, contract["reason_code"], operator, decision_json)
        if existing:
            observed = tuple(existing[index] for index in range(7))
            try:
                timestamp_ok = _canonical_time(existing[7]) == existing[7]
            except (TypeError, ValueError):
                timestamp_ok = False
            # v3 markers are immutable historical evidence.  Later releases
            # must verify, not rewrite, their original producing SHA.
            version_ok = existing[3] == "migration-unversioned" or (isinstance(existing[3], str) and RELEASE_SHA_RE.fullmatch(existing[3]))
            if observed[:3] != expected[:3] or not version_ok or observed[4:] != expected[4:] or not timestamp_ok:
                raise BridgeConflict("legacy conflict resolution reuse conflicts with immutable evidence")
            continue
        conn.execute(
            "INSERT INTO factory_import_conflict_resolutions(conflict_key,resolution_id,job_id,evidence_digest,code_version,reason_code,operator,decision_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (conflict["conflict_key"], resolution_id, conflict["job_id"], digest, code_version, contract["reason_code"], operator, decision_json, utc_now()),
        )


def _valid_v3_resolution(marker: sqlite3.Row | tuple[Any, ...], conflict: Mapping[str, Any], digest: str, decision_json: str) -> bool:
    try:
        contract = _resolver_attestation_contract()
        return (
            marker[0] == f"legacy-resolution:{conflict['conflict_key']}"
            and marker[1] == conflict["job_id"]
            and marker[2] == digest
            and (marker[3] == "migration-unversioned" or (isinstance(marker[3], str) and RELEASE_SHA_RE.fullmatch(marker[3])))
            and marker[4] == contract["reason_code"]
            and marker[5] == contract["operator"]
            and marker[6] == decision_json
            and _marker_time_is_canonical(marker[7])
        )
    except (IndexError, TypeError):
        return False


def _append_deterministic_conflict_attestations(conn: sqlite3.Connection, *, operator: str) -> None:
    registry_path = Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json")))
    routing_digest = _routing_policy_digest(registry_path)
    contract_digest = _resolver_contract_digest()
    contract = _resolver_attestation_contract()
    release_sha = _release_version_for_audit()
    for conflict in (dict(row) for row in conn.execute("SELECT * FROM factory_import_conflicts ORDER BY conflict_key")):
        derived = _legacy_resolution_evidence(conn, conflict, routing_registry=registry_path)
        if not derived:
            continue
        evidence_digest, _work_item, decision_json = derived
        resolution = conn.execute("SELECT resolution_id,job_id,evidence_digest,code_version,reason_code,operator,decision_json,created_at FROM factory_import_conflict_resolutions WHERE conflict_key=?", (conflict["conflict_key"],)).fetchone()
        if not resolution or not _valid_v3_resolution(resolution, conflict, evidence_digest, decision_json):
            raise BridgeConflict("legacy conflict resolution is not immutable evidence")
        identity = (conflict["conflict_key"], RESOLVER_SEMANTIC_VERSION, contract_digest, routing_digest)
        expected = (evidence_digest, conflict["job_id"], contract["reason_code"], decision_json, operator)
        existing = conn.execute(
            "SELECT evidence_digest,job_id,reason_code,decision_json,producing_release_sha,operator,created_at FROM factory_import_conflict_attestations WHERE conflict_key=? AND resolver_semantic_version=? AND resolver_contract_digest=? AND routing_policy_digest=?",
            identity,
        ).fetchone()
        if existing:
            # A commit-only equivalent upgrade must reuse this row.  Its
            # original producing SHA is validated but never part of identity.
            if tuple(existing[index] for index in (0, 1, 2, 3, 5)) != expected or not isinstance(existing[4], str) or (existing[4] != "migration-unversioned" and not RELEASE_SHA_RE.fullmatch(existing[4])) or not _marker_time_is_canonical(existing[6]):
                raise BridgeConflict("legacy conflict attestation reuse conflicts with immutable evidence")
            continue
        conn.execute(
            "INSERT INTO factory_import_conflict_attestations(conflict_key,resolver_semantic_version,resolver_contract_digest,routing_policy_digest,evidence_digest,job_id,reason_code,decision_json,producing_release_sha,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*identity, evidence_digest, conflict["job_id"], contract["reason_code"], decision_json, release_sha, operator, utc_now()),
        )


def classify_factory_import_conflicts(conn: sqlite3.Connection, *, routing_registry: Path | None = None, legacy_only: bool = False) -> dict[str, int]:
    """Read-only conflict gate with explicit derivable-versus-attested state."""
    original = resolvable = unresolved = attested = attestation_required = 0
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_markers = "factory_import_conflict_resolutions" in tables
    has_attestations = "factory_import_conflict_attestations" in tables
    conflict_query = "SELECT * FROM factory_import_conflicts"
    conflict_args: tuple[Any, ...] = ()
    if legacy_only:
        conflict_query += " WHERE reason=?"
        conflict_args = ("legacy_artifact_reference_conflict",)
    conflict_query += " ORDER BY conflict_key"
    try:
        registry_path = routing_registry or Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json")))
        routing_digest = _routing_policy_digest(registry_path)
        contract_digest = _resolver_contract_digest()
        contract = _resolver_attestation_contract()
    except (OSError, TypeError, ValueError, json.JSONDecodeError, BridgeValidationError):
        count = sum(1 for _ in conn.execute(conflict_query, conflict_args))
        return {"original": count, "resolvable": 0, "unresolved": count, "attested": 0, "attestation_required": 0}
    for conflict in conn.execute(conflict_query, conflict_args):
        original += 1
        conflict_map = dict(conflict)
        derived = _legacy_resolution_evidence(conn, conflict_map, routing_registry=registry_path)
        if not derived:
            unresolved += 1
            continue
        digest, _work_item, decision_json = derived
        marker = conn.execute(
            "SELECT resolution_id,job_id,evidence_digest,code_version,reason_code,operator,decision_json,created_at FROM factory_import_conflict_resolutions WHERE conflict_key=?",
            (conflict_map["conflict_key"],),
        ).fetchone() if has_markers else None
        # A pre-v3 historical ledger may have a derivable conflict but no
        # resolution marker yet.  Read-only predeploy must expose that as an
        # installer migration requirement, not as a false evidence failure.
        if not marker:
            resolvable += 1
            attestation_required += 1
            continue
        if not _valid_v3_resolution(marker, conflict_map, digest, decision_json):
            unresolved += 1
            continue
        resolvable += 1
        attestation = conn.execute(
            "SELECT evidence_digest,job_id,reason_code,decision_json,producing_release_sha,operator,created_at FROM factory_import_conflict_attestations WHERE conflict_key=? AND resolver_semantic_version=? AND resolver_contract_digest=? AND routing_policy_digest=?",
            (conflict_map["conflict_key"], RESOLVER_SEMANTIC_VERSION, contract_digest, routing_digest),
        ).fetchone() if has_attestations else None
        if not attestation:
            attestation_required += 1
        elif (
            attestation[0] == digest and attestation[1] == conflict_map["job_id"]
            and attestation[2] == contract["reason_code"] and attestation[3] == decision_json
            and isinstance(attestation[4], str) and (attestation[4] == "migration-unversioned" or RELEASE_SHA_RE.fullmatch(attestation[4]))
            and attestation[5] == contract["operator"] and _marker_time_is_canonical(attestation[6])
        ):
            attested += 1
        else:
            unresolved += 1
    return {"original": original, "resolvable": resolvable, "unresolved": unresolved, "attested": attested, "attestation_required": attestation_required}


def _marker_time_is_canonical(value: Any) -> bool:
    try:
        return isinstance(value, str) and _canonical_time(value) == value
    except ValueError:
        return False


def _legacy_inspection_sources(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    """Read the only recognized inspection shape; old populated shapes block import."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "factory_inspection_requests" not in tables:
        return []
    columns = {row[1] for row in conn.execute("PRAGMA table_info(factory_inspection_requests)")}
    required = {"work_item_key", "url", "result_json"}
    count = int(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests").fetchone()[0])
    if count and not required <= columns:
        raise ValueError("legacy inspection schema is unsupported")
    if not required <= columns:
        return []
    sources: list[tuple[str, dict[str, Any]]] = []
    for row in conn.execute("SELECT url,result_json FROM factory_inspection_requests WHERE work_item_key=?", (work_item_key,)):
        sources.append(("inspection", {"url": row["url"]}))
        if row["result_json"] is not None:
            sources.append(("inspection", json_object(row["result_json"])))
    return sources


def _migrate_conflict_resolution_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(factory_import_conflict_resolutions)")}
    if "decision_json" not in columns:
        conn.execute("ALTER TABLE factory_import_conflict_resolutions ADD COLUMN decision_json TEXT NOT NULL DEFAULT '{}'")


def _migrate_factory_retry_columns(conn: sqlite3.Connection) -> None:
    """Keep write-time migration separate from read-only operator access."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(factory_inspection_requests)")}
    for column in ("job_id TEXT", "work_item_key TEXT", "prior_attempt_id TEXT", "capability TEXT", "result_digest TEXT", "observed_at TEXT"):
        if column.split()[0] not in columns:
            conn.execute(f"ALTER TABLE factory_inspection_requests ADD COLUMN {column}")
    outbox = {row[1] for row in conn.execute("PRAGMA table_info(factory_attempt_outbox)")}
    # Older pilot databases legitimately have no delivery lease fields.  The
    # migration is idempotent and is intentionally never called by read paths.
    for column in ("delivery_lease_id TEXT", "delivery_lease_expires_at TEXT"):
        if column.split()[0] not in outbox:
            conn.execute(f"ALTER TABLE factory_attempt_outbox ADD COLUMN {column}")
    leases = {row[1] for row in conn.execute("PRAGMA table_info(factory_execution_leases)")}
    for column in ("epoch INTEGER NOT NULL DEFAULT 1", "fence_token TEXT NOT NULL DEFAULT ''", "last_heartbeat_at TEXT", "effect_authorized_at TEXT"):
        if column.split()[0] not in leases:
            conn.execute(f"ALTER TABLE factory_execution_leases ADD COLUMN {column}")
    attempts = {row[1] for row in conn.execute("PRAGMA table_info(factory_attempts)")}
    for column in ("artifact_fence_json TEXT", "trusted_snapshot_digest TEXT"):
        if column.split()[0] not in attempts:
            conn.execute(f"ALTER TABLE factory_attempts ADD COLUMN {column}")


def _migrate_canonical_times(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        rows = conn.execute(f"SELECT rowid, {column} FROM {table}").fetchall()
        updates = []
        for row in rows:
            try:
                canonical = _canonical_time(row[column])
            except (TypeError, ValueError):
                continue
            if canonical != row[column]:
                updates.append((canonical, row["rowid"]))
        if updates:
            conn.executemany(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", updates)


def _migrate_reconciliation_findings(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reconciliation_findings)").fetchall()}
    if not columns:
        conn.execute(
            """CREATE TABLE reconciliation_findings (
                finding_key TEXT PRIMARY KEY, job_id TEXT, subject TEXT NOT NULL, kind TEXT NOT NULL,
                details_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL
            )"""
        )
        return
    if "subject" not in columns:
        conn.executescript(
            """ALTER TABLE reconciliation_findings RENAME TO reconciliation_findings_legacy;
            CREATE TABLE reconciliation_findings (
                finding_key TEXT PRIMARY KEY, job_id TEXT, subject TEXT NOT NULL, kind TEXT NOT NULL,
                details_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL
            );
            INSERT INTO reconciliation_findings (finding_key, job_id, subject, kind, details_json, first_seen_at, last_seen_at, occurrence_count)
                SELECT finding_key, job_id, 'bridge:' || job_id, kind, details_json, first_seen_at, last_seen_at, occurrence_count
                FROM reconciliation_findings_legacy;
            DROP TABLE reconciliation_findings_legacy;"""
        )


def _migrate_bridge_verification_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bridge_jobs)").fetchall()}
    for name in ("verification_context_json", "verification_original_context_json", "verification_observed_context_json", "verification_observed_digest", "verification_receipt_json", "verification_digest"):
        if name not in columns:
            conn.execute(f"ALTER TABLE bridge_jobs ADD COLUMN {name} TEXT")
    # Older rows stored either original command evidence or the final receipt in
    # verification_context_json. Copy it once for read compatibility; new rows
    # never overwrite this immutable original context.
    conn.execute("UPDATE bridge_jobs SET verification_original_context_json = verification_context_json WHERE verification_original_context_json IS NULL AND verification_context_json IS NOT NULL")
    lease_columns = {row[1] for row in conn.execute("PRAGMA table_info(verification_leases)").fetchall()}
    for name in ("work_item_key", "branch", "pr_url"):
        if name not in lease_columns:
            conn.execute(f"ALTER TABLE verification_leases ADD COLUMN {name} TEXT")


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    receipt = json.loads(row["verification_receipt_json"]) if row["verification_receipt_json"] else None
    context_raw = (row["verification_original_context_json"] if "verification_original_context_json" in row.keys() else None) or row["verification_context_json"]
    context = json.loads(context_raw) if context_raw else None
    observed = json.loads(row["verification_observed_context_json"]) if "verification_observed_context_json" in row.keys() and row["verification_observed_context_json"] else None
    if receipt is not None and row["verification_digest"] != hashlib.sha256(canonical_json(receipt).encode()).hexdigest():
        raise BridgeValidationError("stored verification receipt digest mismatch")
    if observed is not None and row["verification_observed_digest"] != hashlib.sha256(canonical_json(observed).encode()).hexdigest():
        raise BridgeValidationError("stored verification observed context digest mismatch")
    return {
        "job_id": row["job_id"],
        "idempotency_key": row["idempotency_key"],
        "state": row["state"],
        "requested_action": row["requested_action"],
        "payload": json.loads(row["payload_json"]),
        "payload_hash": row["payload_hash"],
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "pr_url": row["pr_url"],
        "hermes_session_id": row["hermes_session_id"],
        "hatchet_workflow_run_id": row["hatchet_workflow_run_id"],
        "hatchet_step_run_id": row["hatchet_step_run_id"],
        "verification_context": context,
        "verification_observed_context": observed,
        "verification_receipt": receipt,
        "verification_digest": row["verification_digest"],
        "reason_code": row["reason_code"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "job_id": row["job_id"],
        "event_type": row["event_type"],
        "state": row["state"],
        "details": json.loads(row["details_json"]),
        "created_at": row["created_at"],
    }


def _row_to_factory_attempt(row: sqlite3.Row) -> dict[str, Any]:
    """Return the narrow operator representation; reasons are never raw logs."""
    result = {key: row[key] for key in row.keys()}
    result["reason"] = sanitize_detail(result.get("reason"))
    return result


def _append_factory_attempt_event(conn: sqlite3.Connection, attempt_id: str, event_type: str, details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), attempt_id, event_type, canonical_json(sanitize_detail(details or {})), utc_now()),
    )


def _append_event(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    event_type: str,
    state: str,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO bridge_events (
            event_id, job_id, event_type, state, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            job_id,
            event_type,
            state,
            canonical_json(sanitize_detail(details or {})),
            utc_now(),
        ),
    )


def _canonical_routed_repo(repository: str) -> bool:
    """Use the checked-in routing registry, never caller-provided repo scope."""
    default_registry = Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json"
    path = Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(default_registry)))
    try:
        registry = json.loads(path.read_text())
        return isinstance(registry.get("version"), int) and sum(1 for row in registry.get("routes", []) if isinstance(row, dict) and row.get("repo") == repository) == 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _canonical_enabled_repo(repository: str) -> bool:
    default_registry = Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json"
    path = Path(os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", str(default_registry)))
    try:
        registry = json.loads(path.read_text())
        return _canonical_routed_repo(repository) and sum(1 for row in registry.get("routes", []) if isinstance(row, dict) and row.get("repo") == repository and row.get("execution_enabled") is True) == 1
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _validate_repair_payload(payload: dict[str, Any]) -> None:
    repair = payload.get("factory_repair")
    required = {"source_job_id", "source_attempt_id", "source_generation", "reason_code", "error", "pr_url", "branch", "commit_sha", "verification_attempt_id"}
    if not isinstance(repair, dict) or set(repair) != required:
        raise BridgeValidationError("repair action payload is invalid")
    repo = payload.get("repo")
    if not isinstance(repo, str):
        raise BridgeValidationError("repair repository is invalid")
    _parse_pr_reference(repo, repair.get("pr_url"))
    if payload.get("dry_run") is not False:
        raise BridgeValidationError("repair action payload is invalid")
    if repair.get("reason_code") not in REPAIRABLE_VERIFICATION_REASONS:
        raise BridgeValidationError("repair reason is not supported")
    if not isinstance(repair.get("source_job_id"), str) or not repair["source_job_id"]:
        raise BridgeValidationError("repair action payload is invalid")
    if not isinstance(repair.get("source_attempt_id"), str) or not repair["source_attempt_id"]:
        raise BridgeValidationError("repair action payload is invalid")
    if not isinstance(repair.get("source_generation"), int) or repair["source_generation"] < 1:
        raise BridgeValidationError("repair action payload is invalid")
    if not isinstance(repair.get("branch"), str) or not repair["branch"].strip() or len(repair["branch"]) > 200:
        raise BridgeValidationError("repair action payload is invalid")
    if not isinstance(repair.get("error"), str) or len(repair["error"]) > 320:
        raise BridgeValidationError("repair action payload is invalid")
    if repair.get("commit_sha") is not None and not isinstance(repair.get("commit_sha"), str):
        raise BridgeValidationError("repair action payload is invalid")
    if repair.get("verification_attempt_id") is not None and not isinstance(repair.get("verification_attempt_id"), str):
        raise BridgeValidationError("repair action payload is invalid")


def _validate_submit(request: dict[str, Any], *, internal_inspection: bool = False, internal_repair: bool = False) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(request, dict):
        raise BridgeValidationError("request must be a JSON object")

    idempotency_key = request.get("idempotency_key")
    action = request.get("requested_action") or request.get("action")
    payload = request.get("payload", {})

    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise BridgeValidationError("idempotency_key is required")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise BridgeValidationError(f"requested_action must be one of {sorted(ALLOWED_ACTIONS)}")
    if not isinstance(payload, dict):
        raise BridgeValidationError("payload must be a JSON object")
    if action == "inspect_draft_pr_state":
        if not internal_inspection:
            raise BridgeValidationError("inspection jobs are created only by the factory operator lineage")
        if set(payload) != {"repo", "inspection", "inspection_request_key", "inspection_capability", "dry_run"} or payload.get("dry_run") is not False or not isinstance(payload.get("inspection_request_key"), str) or not isinstance(payload.get("inspection_capability"), str) or len(payload["inspection_capability"]) < 20:
            raise BridgeValidationError("inspection action payload is invalid")
        inspection = payload.get("inspection")
        if not isinstance(inspection, dict) or set(inspection) != {"repository", "number", "url"}:
            raise BridgeValidationError("inspection action payload is invalid")
        repository = payload.get("repo")
        if not isinstance(repository, str) or inspection.get("repository") != repository or not isinstance(inspection.get("number"), int) or inspection["number"] < 1:
            raise BridgeValidationError("inspection action payload is invalid")
        _parse_pr_reference(repository, inspection.get("url"))
        if not _canonical_enabled_repo(repository):
            raise BridgeValidationError("inspection repository is not canonical and enabled")
        if inspection["number"] != int(inspection["url"].rsplit("/", 1)[1]):
            raise BridgeValidationError("inspection action payload is invalid")
    if action == "repair_draft_pr":
        if not internal_repair:
            raise BridgeValidationError("repair jobs are created only by the factory repair lineage")
        _validate_repair_payload(payload)

    return idempotency_key.strip(), action, payload


def submit_job(conn: sqlite3.Connection, request: dict[str, Any], *, internal_inspection: bool = False, internal_repair: bool = False) -> tuple[dict[str, Any], bool]:
    idempotency_key, action, payload = _validate_submit(request, internal_inspection=internal_inspection, internal_repair=internal_repair)
    payload = _normalize_correlation_payload(payload)
    serialized_payload = canonical_json(payload)
    digest = payload_hash(payload)

    with conn:
        existing = conn.execute(
            "SELECT * FROM bridge_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            job = _row_to_job(existing)
            if job["requested_action"] != action or job["payload_hash"] != digest:
                raise BridgeConflict("idempotency_key already exists with different intent")
            merged_payload, new_event_keys = _merge_correlation_payload(job["payload"], payload)
            if merged_payload != job["payload"]:
                conn.execute(
                    "UPDATE bridge_jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?",
                    (canonical_json(merged_payload), utc_now(), job["job_id"]),
                )
            _append_event(
                conn,
                job_id=job["job_id"],
                event_type="submit_deduplicated",
                state=job["state"],
                details={"requested_action": action, "source_event_keys": new_event_keys},
            )
            return get_job(conn, job["job_id"]), False

        now = utc_now()
        job_id = str(uuid.uuid4())
        branch = request.get("branch") or payload.get("branch") or deterministic_branch(idempotency_key)
        if not isinstance(branch, str) or not branch:
            raise BridgeValidationError("branch must be a non-empty string when provided")
        if action == "repair_draft_pr" and branch != payload["factory_repair"]["branch"]:
            raise BridgeValidationError("repair branch must match the failed draft PR branch")

        conn.execute(
            """
            INSERT INTO bridge_jobs (
                job_id, idempotency_key, state, requested_action,
                payload_json, payload_hash, branch,
                hatchet_workflow_run_id, hatchet_step_run_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                idempotency_key,
                "accepted",
                action,
                serialized_payload,
                digest,
                branch,
                request.get("hatchet_workflow_run_id"),
                request.get("hatchet_step_run_id"),
                now,
                now,
            ),
        )
        _append_event(
            conn,
            job_id=job_id,
            event_type="accepted",
            state="accepted",
            details={
                "requested_action": action,
                "branch": branch,
                "hatchet_workflow_run_id": request.get("hatchet_workflow_run_id"),
                "hatchet_step_run_id": request.get("hatchet_step_run_id"),
            },
        )

    return get_job(conn, job_id), True


def get_job(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM bridge_jobs
        WHERE job_id = ? OR idempotency_key = ?
        """,
        (ref, ref),
    ).fetchone()
    if not row:
        raise BridgeNotFound(f"job not found: {ref}")
    return _row_to_job(row)


def acquire_verification_lease(conn: sqlite3.Connection, ref: str, *, attempt_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 200:
        raise BridgeValidationError("verification attempt_id is invalid")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 30 or ttl_seconds > 900:
        raise BridgeValidationError("verification lease ttl_seconds must be between 30 and 900")
    with conn:
        job = get_job(conn, ref)
        if job["state"] not in {"effect_committed", "verification_pending"} or not job.get("pr_url") or not job.get("branch"):
            # There is no valid owner to authorize a verifier completion. Keep
            # the artifact recoverable, but make the blocked recovery visible
            # and defer another attempt through updated_at rather than looping.
            conn.execute("UPDATE bridge_jobs SET reason_code = ?, error = ?, updated_at = ? WHERE job_id = ?", ("verification_recovery_lease_unavailable", "immutable verification references are unavailable", utc_now(), job["job_id"]))
            _append_event(conn, job_id=job["job_id"], event_type="verification_lease_blocked", state=job["state"], details={"reason_code": "verification_recovery_lease_unavailable"})
            raise BridgeValidationError("verification lease requires a committed draft PR job")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        expires_text = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        lease_key = hashlib.sha256(f"{job['job_id']}|{job['branch']}|{job['pr_url']}|verify_draft_pr".encode()).hexdigest()
        row = conn.execute("SELECT * FROM verification_leases WHERE job_id = ?", (job["job_id"],)).fetchone()
        if row and row["closed_at"] is None and row["expires_at"] > now_text:
            return {"acquired": False, "lease": dict(row)}
        work_item_key = str(job["payload"].get("work_item_id") or job["payload"].get("linear_issue") or job["idempotency_key"])
        if row:
            conn.execute("UPDATE verification_leases SET owner_attempt_id = ?, acquired_at = ?, expires_at = ?, claimed_at = NULL, closed_at = NULL, outcome = NULL, work_item_key = ?, branch = ?, pr_url = ? WHERE job_id = ?", (attempt_id, now_text, expires_text, work_item_key, job["branch"], job["pr_url"], job["job_id"]))
        else:
            conn.execute("INSERT INTO verification_leases (job_id, lease_key, action, owner_attempt_id, acquired_at, expires_at, work_item_key, branch, pr_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (job["job_id"], lease_key, "verify_draft_pr", attempt_id, now_text, expires_text, work_item_key, job["branch"], job["pr_url"]))
        _append_event(conn, job_id=job["job_id"], event_type="verification_lease_acquired", state=job["state"], details={"action": "verify_draft_pr"})
        return {"acquired": True, "lease": dict(conn.execute("SELECT * FROM verification_leases WHERE job_id = ?", (job["job_id"],)).fetchone())}


def claim_verification_lease(conn: sqlite3.Connection, ref: str, *, attempt_id: str) -> bool:
    with conn:
        job = get_job(conn, ref)
        now = utc_now()
        updated = conn.execute("UPDATE verification_leases SET claimed_at = ? WHERE job_id = ? AND owner_attempt_id = ? AND claimed_at IS NULL AND closed_at IS NULL AND expires_at > ?", (now, job["job_id"], attempt_id, now)).rowcount
        if updated:
            _append_event(conn, job_id=job["job_id"], event_type="verification_lease_claimed", state=job["state"], details={"action": "verify_draft_pr"})
        return bool(updated)


def _close_verification_lease(conn: sqlite3.Connection, job_id: str, outcome: str) -> None:
    conn.execute("UPDATE verification_leases SET closed_at = ?, outcome = ? WHERE job_id = ? AND closed_at IS NULL", (utc_now(), outcome, job_id))


def _require_claimed_verification_lease(conn: sqlite3.Connection, job: dict[str, Any], attempt_id: Any) -> None:
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 200:
        raise BridgeValidationError("verification completion requires verification_attempt_id")
    lease = conn.execute("SELECT * FROM verification_leases WHERE job_id = ?", (job["job_id"],)).fetchone()
    now = utc_now()
    work_item_key = str(job["payload"].get("work_item_id") or job["payload"].get("linear_issue") or job["idempotency_key"])
    if not lease or lease["action"] != "verify_draft_pr" or lease["owner_attempt_id"] != attempt_id or lease["claimed_at"] is None or lease["closed_at"] is not None or lease["expires_at"] <= now or lease["work_item_key"] != work_item_key or lease["branch"] != job["branch"] or lease["pr_url"] != job["pr_url"]:
        raise BridgeValidationError("verification completion does not own an active claimed lease")


def record_verification_observation(conn: sqlite3.Connection, ref: str, *, attempt_id: Any, receipt: Any) -> dict[str, Any]:
    with conn:
        job = get_job(conn, ref)
        if job["state"] != "verification_pending":
            raise BridgeValidationError("verification observation requires verification_pending")
        _require_claimed_verification_lease(conn, job, attempt_id)
        validated, _ = _validate_verification_receipt(receipt, job)
        existing = job.get("verification_observed_context")
        if existing is not None and existing != validated:
            raise BridgeConflict("verification observation is immutable for an active attempt")
        if existing is None:
            conn.execute("UPDATE bridge_jobs SET verification_observed_context_json = ?, verification_observed_digest = ?, updated_at = ? WHERE job_id = ?", (canonical_json(validated), hashlib.sha256(canonical_json(validated).encode()).hexdigest(), utc_now(), job["job_id"]))
            _append_event(conn, job_id=job["job_id"], event_type="verification_observed", state=job["state"], details={"verification_attempt_id": attempt_id})
    return get_job(conn, job["job_id"])


def list_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM bridge_jobs
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_job(row) for row in rows]


def count_jobs(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM bridge_jobs").fetchone()[0])


def list_factory_attempts(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    if not isinstance(work_item_key, str) or not work_item_key or len(work_item_key) > 200:
        raise BridgeValidationError("work item key is invalid")
    rows = conn.execute("SELECT * FROM factory_attempts WHERE work_item_key = ? ORDER BY generation DESC", (work_item_key,)).fetchall()
    return [_row_to_factory_attempt(row) for row in rows]


def get_factory_attempt(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref or len(ref) > 200:
        raise BridgeValidationError("factory attempt reference is invalid")
    row = conn.execute("SELECT * FROM factory_attempts WHERE attempt_id = ?", (ref,)).fetchone()
    if not row:
        raise BridgeNotFound("factory attempt not found")
    return _row_to_factory_attempt(row)


def get_factory_attempt_for_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM factory_attempts WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_factory_attempt(row) if row else None


def list_factory_attempt_events(conn: sqlite3.Connection, attempt_id: str, limit: int = 100) -> list[dict[str, Any]]:
    if limit < 1 or limit > 250:
        raise BridgeValidationError("factory attempt event limit must be between 1 and 250")
    get_factory_attempt(conn, attempt_id)
    rows = conn.execute("SELECT * FROM factory_attempt_events WHERE attempt_id=? ORDER BY created_at,event_id LIMIT ?", (attempt_id, limit)).fetchall()
    return [{"event_id": row["event_id"], "attempt_id": row["attempt_id"], "event_type": row["event_type"], "details": json.loads(row["details_json"]), "created_at": row["created_at"]} for row in rows]


def list_factory_import_conflicts(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT conflict_key,work_item_key,job_id,reason,created_at FROM factory_import_conflicts WHERE work_item_key=? ORDER BY created_at,conflict_key", (work_item_key,)).fetchall()
    return [dict(row) for row in rows]


def _parse_pr_reference(repository: str, url: str) -> tuple[str, int, str]:
    match = PR_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if not isinstance(repository, str) or not match or match.group(0).split("/pull/")[0].removeprefix("https://github.com/") != repository:
        raise BridgeValidationError("inspection reference is invalid")
    return repository, int(url.rsplit("/", 1)[1]), url


def _queue_pr_inspection(conn: sqlite3.Connection, work_item_key: str, url: str, prior_attempt_id: str | None = None) -> None:
    """Durably enqueue the one fixed, read-only inspection action.

    This is called from write/migration paths only.  Operator preview merely
    observes its result, preserving the no-write property of list/show/preview.
    """
    repository = None
    row = conn.execute("SELECT payload_json FROM bridge_jobs WHERE idempotency_key LIKE ? ORDER BY created_at DESC LIMIT 1", (f"hatchet:work-item:linear:{work_item_key}:create_draft_pr:%",)).fetchone()
    if row:
        try: repository = json.loads(row["payload_json"]).get("repo")
        except (TypeError, ValueError, json.JSONDecodeError): pass
    if not isinstance(repository, str):
        repository = url.split("/pull/")[0].removeprefix("https://github.com/")
    repository, number, url = _parse_pr_reference(repository, url)
    # A request is bound to the artifact lineage rather than globally to PR
    # number, so a later retry cannot silently reuse a stale observation.
    request_key = f"factory:inspect:{work_item_key}:{prior_attempt_id or 'legacy'}:{repository}:{number}"
    existing = conn.execute("SELECT request_key FROM factory_inspection_requests WHERE request_key=?", (request_key,)).fetchone()
    if existing:
        return
    capability = str(uuid.uuid4())
    job, created = submit_job(conn, {
        "idempotency_key": request_key,
        "requested_action": "inspect_draft_pr_state",
        "payload": {"repo": repository, "inspection": {"repository": repository, "number": number, "url": url}, "inspection_request_key": request_key, "inspection_capability": capability, "dry_run": False},
    }, internal_inspection=True)
    if not created and job["requested_action"] != "inspect_draft_pr_state":
        raise BridgeConflict("inspection idempotency key conflicts with a non-inspection job")
    now = utc_now()
    conn.execute("INSERT INTO factory_inspection_requests (request_key,repository,number,url,job_id,work_item_key,prior_attempt_id,capability,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (request_key, repository, number, url, job["job_id"], work_item_key, prior_attempt_id, capability, "pending", now, now))
    conn.execute("INSERT INTO factory_inspection_outbox (outbox_key,request_key,job_id,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (f"{request_key}:accepted", request_key, job["job_id"], canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", now, now))


def get_pr_inspection(conn: sqlite3.Connection, repository: str, url: str, *, prior_attempt_id: str | None = None) -> dict[str, Any] | None:
    repository, number, url = _parse_pr_reference(repository, url)
    clause, args = (" AND prior_attempt_id=?", (repository, number, url, prior_attempt_id)) if prior_attempt_id else ("", (repository, number, url))
    row = conn.execute(f"SELECT * FROM factory_inspection_requests WHERE repository=? AND number=? AND url=?{clause} ORDER BY observed_at DESC,created_at DESC LIMIT 1", args).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeValidationError("stored inspection result is malformed") from exc
    if result is not None:
        if not isinstance(result, dict) or row["result_digest"] != hashlib.sha256(canonical_json(result).encode()).hexdigest():
            raise BridgeValidationError("stored inspection result digest is invalid")
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise BridgeValidationError("stored inspection timestamp is invalid") from exc
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            raise BridgeValidationError("stored inspection timestamp is in the future")
    return {"request_key": row["request_key"], "job_id": row["job_id"], "state": row["state"], "reason": row["reason"], "result": result, "result_digest": row["result_digest"], "observed_at": row["observed_at"], "prior_attempt_id": row["prior_attempt_id"]}


def complete_pr_inspection(conn: sqlite3.Connection, job_id: str, result: dict[str, Any], *, request_key: str | None = None, capability: str | None = None) -> dict[str, Any]:
    allowed = {"repository", "number", "url", "state", "merged", "draft", "head_branch", "head_sha", "base_ref", "reason"}
    if not isinstance(result, dict) or set(result) != allowed or result.get("state") not in {"open", "closed", "unknown"} or not isinstance(result.get("merged"), bool) or not isinstance(result.get("draft"), bool) or not isinstance(result.get("reason"), str) or (result.get("head_branch") is not None and not isinstance(result.get("head_branch"), str)) or (result.get("head_sha") is not None and not re.fullmatch(r"[0-9a-f]{7,64}", result.get("head_sha"))) or (result.get("base_ref") is not None and not isinstance(result.get("base_ref"), str)):
        raise BridgeValidationError("inspection result is invalid")
    repository, number, url = _parse_pr_reference(result["repository"], result["url"])
    if result["number"] != number:
        raise BridgeValidationError("inspection result does not match request")
    stored_result = sanitize_detail(result)
    with conn:
        request = conn.execute("SELECT * FROM factory_inspection_requests WHERE job_id=?", (job_id,)).fetchone()
        if not request or request_key != request["request_key"] or capability != request["capability"] or (request["repository"], request["number"], request["url"]) != (repository, number, url):
            raise BridgeValidationError("inspection result does not match a pending request")
        # A lost response may be redelivered after the one-time capability has
        # expired. It is safe only when the stored canonical result is exactly
        # identical; an expired capability can never create or alter evidence.
        if request["result_json"]:
            if canonical_json(json.loads(request["result_json"])) != canonical_json(stored_result):
                raise BridgeConflict("inspection result is immutable")
            return get_pr_inspection(conn, repository, url, prior_attempt_id=request["prior_attempt_id"]) or {}
        try:
            capability_created = datetime.fromisoformat(str(request["created_at"]).replace("Z", "+00:00"))
            capability_age = (datetime.now(timezone.utc) - capability_created.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError) as exc:
            raise BridgeValidationError("inspection capability timestamp is invalid") from exc
        if capability_age < 0 or capability_age > 120:
            raise BridgeValidationError("inspection capability is expired")
        job = get_job(conn, job_id)
        if job["requested_action"] != "inspect_draft_pr_state" or job["payload"].get("inspection_request_key") != request_key or job["payload"].get("inspection_capability") != capability or job["state"] not in {"accepted", "running", "completed"}:
            raise BridgeValidationError("inspection completion requires a running inspection job")
        digest = hashlib.sha256(canonical_json(stored_result).encode()).hexdigest(); observed = utc_now()
        if job["state"] != "completed":
            conn.execute("UPDATE bridge_jobs SET state='completed',reason_code=?,updated_at=? WHERE job_id=?", (sanitize_detail(stored_result["reason"], 120), observed, job_id))
            _append_event(conn, job_id=job_id, event_type="inspection_completed", state="completed", details={"reason": stored_result["reason"]})
        conn.execute("UPDATE factory_inspection_requests SET state='completed',result_json=?,result_digest=?,observed_at=?,reason=?,updated_at=? WHERE request_key=?", (canonical_json(stored_result), digest, observed, sanitize_detail(stored_result["reason"], 120), observed, request["request_key"]))
    return get_pr_inspection(conn, repository, url, prior_attempt_id=request["prior_attempt_id"]) or {}


record_pr_inspection = complete_pr_inspection


def _reserve_factory_attempt_locked(
    conn: sqlite3.Connection, *, work_item_key: str, mode: str, request_id: str,
    reason: str, actor: str, supersede_pr_url: str | None = None,
    artifact_fence: dict[str, Any] | None = None,
    trusted_snapshot_digest: str | None = None,
    reserved_event_type: str = "retry_reserved",
) -> tuple[dict[str, Any], bool]:
    if not all(isinstance(value, str) and value and len(value) <= 200 for value in (work_item_key, request_id, actor)) or mode not in {"real", "dry-run"}:
        raise BridgeValidationError("factory retry request is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("factory retry reason is invalid")
    safe_reason = sanitize_detail(reason.strip(), 320)
    existing = conn.execute("SELECT * FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND mode=? AND request_id=?", (work_item_key, mode, request_id)).fetchone()
    if existing:
        if existing["reason"] != safe_reason or existing["supersede_pr_url"] != supersede_pr_url:
            raise BridgeConflict("factory retry request_id conflicts with existing intent")
        return _row_to_factory_attempt(existing), False
    active = conn.execute("SELECT 1 FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','cancellation_requested','effect_outcome_unknown')", (work_item_key,)).fetchone()
    if active:
        raise BridgeConflict("factory retry has an active attempt")
    generation, key = next_factory_attempt_key(conn, work_item_key=work_item_key, mode=mode)
    now, attempt_id, branch = utc_now(), str(uuid.uuid4()), deterministic_branch(key)
    fence_json = canonical_json(artifact_fence) if artifact_fence is not None else None
    conn.execute("INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,supersede_pr_url,artifact_fence_json,trusted_snapshot_digest,reason,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, work_item_key, "create_draft_pr", mode, generation, request_id, key, branch, "reserved", supersede_pr_url, fence_json, trusted_snapshot_digest, safe_reason, actor, now, now))
    _append_factory_attempt_event(conn, attempt_id, reserved_event_type, {"generation": generation, "mode": mode})
    return get_factory_attempt(conn, attempt_id), True


def _validate_absence_evidence(evidence: dict[str, Any], *, repository: str, branch: str) -> dict[str, Any]:
    required = {"kind", "repository", "head_branch", "branch_exists", "pull_requests", "observed_at"}
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise BridgeValidationError("factory absence evidence is invalid")
    if evidence.get("kind") != "github_branch_pr_absence_v1" or evidence.get("repository") != repository or evidence.get("head_branch") != branch:
        raise BridgeValidationError("factory absence evidence does not bind to this attempt")
    if evidence.get("branch_exists") is not False or evidence.get("pull_requests") != []:
        raise BridgeValidationError("factory absence evidence reports an artifact")
    try:
        _canonical_time(str(evidence["observed_at"]))
    except (TypeError, ValueError) as exc:
        raise BridgeValidationError("factory absence evidence timestamp is invalid") from exc
    return {key: evidence[key] for key in sorted(required)}


def next_factory_attempt_key(conn: sqlite3.Connection, *, work_item_key: str, mode: str) -> tuple[int, str]:
    generation = int(conn.execute("SELECT COALESCE(MAX(generation), 0) FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND mode=?", (work_item_key, mode)).fetchone()[0]) + 1
    while True:
        key = _factory_attempt_idempotency_key(work_item_key, mode, generation)
        collision = conn.execute(
            "SELECT 1 FROM bridge_jobs WHERE idempotency_key=? UNION SELECT 1 FROM factory_attempts WHERE idempotency_key=?",
            (key, key),
        ).fetchone()
        if not collision:
            return generation, key
        generation += 1


def _factory_attempt_idempotency_key(work_item_key: str, mode: str, generation: int) -> str:
    if generation >= 2:
        return f"hatchet:work-item:linear:{work_item_key}:create_draft_pr:{mode}:attempt:{generation}"
    return f"hatchet:work-item:linear:{work_item_key}:create_draft_pr:{mode}:active"


def get_supersedable_bridge_job(conn: sqlite3.Connection, job_id: str, *, work_item_key: str, repository: str) -> dict[str, Any]:
    job = get_job(conn, job_id)
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_work_item = payload.get("work_item_id") or payload.get("linear_issue")
    if job["requested_action"] != "create_draft_pr":
        raise BridgeValidationError("superseded bridge job must be create_draft_pr")
    if job["state"] not in TERMINAL_STATES:
        raise BridgeConflict("superseded bridge job must be terminal")
    if job_work_item != work_item_key:
        raise BridgeValidationError("superseded bridge job does not match work item")
    if payload.get("repo") != repository:
        raise BridgeValidationError("superseded bridge job does not match repository")
    if conn.execute("SELECT 1 FROM factory_attempts WHERE job_id=?", (job["job_id"],)).fetchone():
        raise BridgeConflict("superseded bridge job already belongs to a factory attempt")
    return {
        "job_id": job["job_id"],
        "idempotency_key": job["idempotency_key"],
        "state": job["state"],
        "reason_code": job.get("reason_code"),
        "error": job.get("error"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def record_bridge_job_supersession(
    conn: sqlite3.Connection, *, job_id: str, work_item_key: str, repository: str,
    by_attempt_id: str, by_job_id: str, actor: str, reason: str, request_id: str,
) -> dict[str, Any]:
    artifact = get_supersedable_bridge_job(conn, job_id, work_item_key=work_item_key, repository=repository)
    details = {
        "work_item_key": work_item_key,
        "by_attempt_id": by_attempt_id,
        "by_job_id": by_job_id,
        "actor": sanitize_detail(actor, 200),
        "reason": sanitize_detail(reason.strip(), 320),
        "request_id": sanitize_detail(request_id, 200),
    }
    existing = conn.execute(
        "SELECT details_json FROM bridge_events WHERE job_id=? AND event_type='job_superseded_by_attempt' ORDER BY created_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing:
        try:
            previous = json.loads(existing["details_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeValidationError("stored supersession event is malformed") from exc
        if previous != details:
            raise BridgeConflict("superseded bridge job already has conflicting supersession evidence")
        return artifact
    _append_event(conn, job_id=job_id, event_type="job_superseded_by_attempt", state=artifact["state"], details=details)
    return artifact


def reserve_factory_attempt(conn: sqlite3.Connection, **kwargs: Any) -> tuple[dict[str, Any], bool]:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return _reserve_factory_attempt_locked(conn, **kwargs)


def reserve_factory_retry(
    conn: sqlite3.Connection, *, work_item_key: str, mode: str, request_id: str,
    reason: str, actor: str, payload: dict[str, Any], supersede_pr_url: str | None = None,
    prior_attempt_id: str | None = None, supersede_job_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Atomically reserve a generation and its accepted bridge job.

    The caller supplies only already-validated, trusted routing and ingress data.
    The immutable retry lineage is copied into the bridge payload before the job
    exists, so a worker cannot accidentally execute a request divorced from its
    operator audit record.
    """
    if not isinstance(payload, dict):
        raise BridgeValidationError("factory retry payload must be an object")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt, created = _reserve_factory_attempt_locked(
            conn, work_item_key=work_item_key, mode=mode, request_id=request_id,
            reason=reason, actor=actor, supersede_pr_url=supersede_pr_url,
            artifact_fence=payload.get("factory_artifact_fence"), trusted_snapshot_digest=payload.get("trusted_snapshot_digest"),
        )
        if not created:
            job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
            if not job:
                raise BridgeConflict("existing factory retry is incomplete")
            return attempt, job, False
        lineage = {
            "attempt_id": attempt["attempt_id"], "generation": attempt["generation"],
            "request_id": request_id, "reason": sanitize_detail(reason.strip(), 320),
            "actor": actor, "prior_attempt_id": prior_attempt_id,
            "supersede_pr_url": supersede_pr_url,
            "superseded_bridge_job_id": supersede_job_id,
        }
        sealed_payload = dict(payload)
        if supersede_job_id:
            sealed_payload["superseded_bridge_job"] = get_supersedable_bridge_job(
                conn, supersede_job_id, work_item_key=work_item_key, repository=sealed_payload.get("repo")
            )
        sealed_payload["factory_retry"] = lineage
        sealed_payload["work_item_id"] = work_item_key
        sealed_payload["linear_issue"] = work_item_key
        sealed_payload["dry_run"] = mode == "dry-run"
        job, job_created = submit_job(conn, {
            "idempotency_key": attempt["idempotency_key"], "requested_action": "create_draft_pr",
            "branch": attempt["branch"], "payload": sealed_payload,
        })
        if not job_created:
            raise BridgeConflict("reserved factory retry bridge job already exists")
        conn.execute("UPDATE factory_attempts SET status='accepted', job_id=?, prior_attempt_id=?, updated_at=? WHERE attempt_id=?", (job["job_id"], prior_attempt_id, utc_now(), attempt["attempt_id"]))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "retry_requested", {"generation": attempt["generation"], "job_id": job["job_id"]})
        _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_accepted", {"job_id": job["job_id"]})
        outbox_key = f"factory:accepted:{attempt['attempt_id']}"
        conn.execute("INSERT INTO factory_attempt_outbox (outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", utc_now(), utc_now()))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "accepted_event_enqueued", {"outbox_key": outbox_key})
        if supersede_job_id:
            superseded = record_bridge_job_supersession(
                conn, job_id=supersede_job_id, work_item_key=work_item_key, repository=sealed_payload["repo"],
                by_attempt_id=attempt["attempt_id"], by_job_id=job["job_id"], actor=actor, reason=reason, request_id=request_id,
            )
            _append_factory_attempt_event(conn, attempt["attempt_id"], "bridge_job_superseded", superseded)
        if prior_attempt_id:
            conn.execute("UPDATE factory_attempts SET status='superseded', updated_at=? WHERE attempt_id=? AND status NOT IN ('superseded','verified_ready')", (utc_now(), prior_attempt_id))
            _append_factory_attempt_event(conn, prior_attempt_id, "superseded", {"by_attempt_id": attempt["attempt_id"], "supersede_pr_url": supersede_pr_url})
    return get_factory_attempt(conn, attempt["attempt_id"]), job, True


def reserve_factory_absence_retry(
    conn: sqlite3.Connection, *, work_item_key: str, mode: str, request_id: str,
    reason: str, actor: str, payload: dict[str, Any], prior_attempt_id: str,
    absence_evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Supersede one expired unknown generation after reviewed GitHub absence proof."""
    if not isinstance(payload, dict) or not isinstance(prior_attempt_id, str) or not prior_attempt_id:
        raise BridgeValidationError("factory absence retry payload is invalid")
    if not all(isinstance(value, str) and value and len(value) <= 200 for value in (work_item_key, request_id, actor)) or mode not in {"real", "dry-run"}:
        raise BridgeValidationError("factory absence retry request is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("factory absence retry reason is invalid")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        safe_reason = sanitize_detail(reason.strip(), 320)
        existing = conn.execute("SELECT * FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND mode=? AND request_id=?", (work_item_key, mode, request_id)).fetchone()
        if existing:
            if existing["reason"] != safe_reason or existing["supersede_pr_url"] is not None or existing["prior_attempt_id"] != prior_attempt_id or existing["trusted_snapshot_digest"] != payload.get("trusted_snapshot_digest"):
                raise BridgeConflict("factory absence retry request_id conflicts with existing intent")
            attempt = _row_to_factory_attempt(existing)
            job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
            if not job or job.get("payload", {}).get("factory_absence_retry", {}).get("prior_attempt_id") != prior_attempt_id:
                raise BridgeConflict("existing factory absence retry is incomplete")
            return attempt, job, False
        prior = get_factory_attempt(conn, prior_attempt_id)
        if prior["work_item_key"] != work_item_key or prior["action"] != "create_draft_pr" or prior["mode"] != mode:
            raise BridgeValidationError("factory absence retry prior attempt mismatch")
        if prior["status"] != "effect_outcome_unknown":
            raise BridgeValidationError("factory absence retry prior attempt is not outcome-unknown")
        if prior.get("prior_pr_url"):
            raise BridgeConflict("factory absence retry cannot supersede an attempt with a PR artifact")
        if not prior.get("job_id"):
            raise BridgeValidationError("factory absence retry prior attempt has no bridge job")
        active_other = conn.execute(
            "SELECT attempt_id FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','cancellation_requested','effect_outcome_unknown') AND attempt_id<>?",
            (work_item_key, prior_attempt_id),
        ).fetchone()
        if active_other:
            raise BridgeConflict("factory retry has another active attempt")
        prior_job = get_job(conn, prior["job_id"])
        repository = payload.get("repo")
        if prior_job["requested_action"] != "create_draft_pr" or prior_job.get("branch") != prior["branch"] or prior_job.get("payload", {}).get("repo") != repository:
            raise BridgeValidationError("factory absence retry prior job mismatch")
        lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL", (prior_attempt_id,)).fetchone()
        if not lease or lease["effect_authorized_at"] is None or lease["effect_observed_at"] is not None:
            raise BridgeValidationError("factory absence retry requires an expired unobserved authorized lease")
        try:
            lease_expires_at = _canonical_time(lease["expires_at"])
        except (TypeError, ValueError) as exc:
            raise BridgeValidationError("factory absence retry lease timestamp is invalid") from exc
        if lease_expires_at > utc_now():
            raise BridgeValidationError("factory absence retry requires an expired unobserved authorized lease")
        if conn.execute("SELECT 1 FROM factory_recovery_requests WHERE attempt_id=?", (prior_attempt_id,)).fetchone():
            raise BridgeConflict("factory absence retry conflicts with existing recovery evidence")
        bound_evidence = _validate_absence_evidence(absence_evidence, repository=repository, branch=prior["branch"])
        evidence_digest = hashlib.sha256(canonical_json(bound_evidence).encode()).hexdigest()
        artifact_fence = payload.get("factory_artifact_fence")
        expected_fence = {
            "kind": "github_branch_pr_absence_v1",
            "prior_attempt_id": prior_attempt_id,
            "prior_job_id": prior["job_id"],
            "repository": repository,
            "head_branch": prior["branch"],
            "lease_id": lease["lease_id"],
            "lease_epoch": lease["epoch"],
            "effect_authorized_at": lease["effect_authorized_at"],
            "lease_expires_at": lease["expires_at"],
            "branch_exists": False,
            "pull_request_count": 0,
            "observed_at": bound_evidence["observed_at"],
            "evidence_digest": evidence_digest,
        }
        if artifact_fence != expected_fence:
            raise BridgeValidationError("factory absence retry artifact fence mismatch")
        conn.execute("UPDATE factory_attempts SET status='superseded', updated_at=? WHERE attempt_id=?", (utc_now(), prior_attempt_id))
        _append_factory_attempt_event(conn, prior_attempt_id, "absence_superseded_unknown_attempt", {"by_request_id": request_id, "actor": actor, "reason": safe_reason, "evidence_digest": evidence_digest, "observed_at": bound_evidence["observed_at"]})
        attempt, created = _reserve_factory_attempt_locked(
            conn, work_item_key=work_item_key, mode=mode, request_id=request_id,
            reason=reason, actor=actor, artifact_fence=artifact_fence,
            trusted_snapshot_digest=payload.get("trusted_snapshot_digest"),
            reserved_event_type="absence_retry_reserved",
        )
        if not created:
            job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
            if not job:
                raise BridgeConflict("existing factory absence retry is incomplete")
            return attempt, job, False
        lineage = {
            "attempt_id": attempt["attempt_id"], "generation": attempt["generation"],
            "request_id": request_id, "reason": sanitize_detail(reason.strip(), 320),
            "actor": actor, "prior_attempt_id": prior_attempt_id,
            "supersede_pr_url": None, "superseded_bridge_job_id": None,
        }
        sealed_payload = dict(payload)
        sealed_payload["factory_retry"] = lineage
        sealed_payload["factory_absence_evidence"] = bound_evidence
        sealed_payload["work_item_id"] = work_item_key
        sealed_payload["linear_issue"] = work_item_key
        sealed_payload["dry_run"] = mode == "dry-run"
        job, job_created = submit_job(conn, {
            "idempotency_key": attempt["idempotency_key"], "requested_action": "create_draft_pr",
            "branch": attempt["branch"], "payload": sealed_payload,
        })
        if not job_created:
            raise BridgeConflict("reserved factory absence retry bridge job already exists")
        conn.execute("UPDATE factory_attempts SET status='accepted', job_id=?, prior_attempt_id=?, updated_at=? WHERE attempt_id=?", (job["job_id"], prior_attempt_id, utc_now(), attempt["attempt_id"]))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "retry_requested", {"generation": attempt["generation"], "job_id": job["job_id"], "absence_evidence_digest": evidence_digest})
        _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_accepted", {"job_id": job["job_id"]})
        outbox_key = f"factory:accepted:{attempt['attempt_id']}"
        conn.execute("INSERT INTO factory_attempt_outbox (outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", utc_now(), utc_now()))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "accepted_event_enqueued", {"outbox_key": outbox_key})
    return get_factory_attempt(conn, attempt["attempt_id"]), job, True


def _factory_auto_repair_max_generations() -> int:
    try:
        value = int(os.environ.get("HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS", "3"))
    except ValueError:
        value = 3
    return max(1, min(value, 10))


def _reserve_factory_verification_repair(
    conn: sqlite3.Connection,
    *,
    failed_job: dict[str, Any],
    factory_attempt: dict[str, Any],
    details: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    reason_code = failed_job.get("reason_code")
    if (
        failed_job.get("requested_action") not in DRAFT_PR_EFFECT_ACTIONS
        or factory_attempt.get("mode") != "real"
        or factory_attempt.get("actor") == "legacy-import"
        or reason_code not in REPAIRABLE_VERIFICATION_REASONS
    ):
        return None
    if int(factory_attempt.get("generation") or 0) >= _factory_auto_repair_max_generations():
        _append_factory_attempt_event(conn, factory_attempt["attempt_id"], "repair_budget_exhausted", {"reason_code": reason_code, "max_generations": _factory_auto_repair_max_generations()})
        return None

    payload = failed_job.get("payload")
    repo, pr_url, branch = (payload or {}).get("repo"), failed_job.get("pr_url"), failed_job.get("branch")
    if not isinstance(payload, dict) or not isinstance(repo, str) or not isinstance(pr_url, str) or not isinstance(branch, str):
        _append_factory_attempt_event(conn, factory_attempt["attempt_id"], "repair_skipped", {"reason": "repair_target_unavailable", "reason_code": reason_code})
        return None
    try:
        _parse_pr_reference(repo, pr_url)
    except BridgeValidationError:
        _append_factory_attempt_event(conn, factory_attempt["attempt_id"], "repair_skipped", {"reason": "repair_target_invalid", "reason_code": reason_code})
        return None

    request_id = f"auto-repair:{failed_job['job_id']}:{reason_code}"
    reason = f"automated verifier repair after {reason_code}"
    repair_context = {
        "source_job_id": failed_job["job_id"],
        "source_attempt_id": factory_attempt["attempt_id"],
        "source_generation": factory_attempt["generation"],
        "reason_code": reason_code,
        "error": sanitize_detail(details.get("error") or failed_job.get("error") or reason_code, 320),
        "pr_url": pr_url,
        "branch": branch,
        "commit_sha": failed_job.get("commit_sha"),
        "verification_attempt_id": details.get("verification_attempt_id") if isinstance(details.get("verification_attempt_id"), str) else None,
    }
    repair_payload = copy.deepcopy(payload)
    repair_payload["factory_repair"] = repair_context
    repair_payload["expected_head_sha"] = failed_job.get("commit_sha")
    repair_payload["instructions"] = (
        "Automated bounded repair. Reuse the existing draft PR and branch from factory_repair. "
        "Fix only the verifier failure evidence, then rerun the required validation commands."
    )

    attempt, created = _reserve_factory_attempt_locked(
        conn,
        work_item_key=factory_attempt["work_item_key"],
        mode=factory_attempt["mode"],
        request_id=request_id,
        reason=reason,
        actor="verification-repair",
        artifact_fence=repair_payload.get("factory_artifact_fence"),
        trusted_snapshot_digest=repair_payload.get("trusted_snapshot_digest"),
        reserved_event_type="repair_reserved",
    )
    if not created:
        job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
        if not job:
            raise BridgeConflict("existing factory repair is incomplete")
        return attempt, job, False

    lineage = {
        "attempt_id": attempt["attempt_id"],
        "generation": attempt["generation"],
        "request_id": request_id,
        "reason": reason,
        "actor": "verification-repair",
        "prior_attempt_id": factory_attempt["attempt_id"],
        "supersede_pr_url": None,
        "repair_source_job_id": failed_job["job_id"],
    }
    sealed_payload = dict(repair_payload)
    sealed_payload["factory_retry"] = lineage
    sealed_payload["work_item_id"] = factory_attempt["work_item_key"]
    sealed_payload["linear_issue"] = factory_attempt["work_item_key"]
    sealed_payload["dry_run"] = False
    job, job_created = submit_job(conn, {
        "idempotency_key": attempt["idempotency_key"],
        "requested_action": "repair_draft_pr",
        "branch": branch,
        "payload": sealed_payload,
    }, internal_repair=True)
    if not job_created:
        raise BridgeConflict("reserved factory repair bridge job already exists")

    now = utc_now()
    conn.execute(
        "UPDATE factory_attempts SET status='accepted', job_id=?, prior_attempt_id=?, prior_pr_url=?, updated_at=? WHERE attempt_id=?",
        (job["job_id"], factory_attempt["attempt_id"], pr_url, now, attempt["attempt_id"]),
    )
    _append_factory_attempt_event(conn, attempt["attempt_id"], "repair_requested", {"generation": attempt["generation"], "job_id": job["job_id"], "source_job_id": failed_job["job_id"], "reason_code": reason_code})
    _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_accepted", {"job_id": job["job_id"]})
    outbox_key = f"factory:accepted:{attempt['attempt_id']}"
    conn.execute("INSERT INTO factory_attempt_outbox (outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", now, now))
    _append_factory_attempt_event(conn, attempt["attempt_id"], "accepted_event_enqueued", {"outbox_key": outbox_key})
    _append_factory_attempt_event(conn, factory_attempt["attempt_id"], "repair_queued", {"by_attempt_id": attempt["attempt_id"], "by_job_id": job["job_id"], "reason_code": reason_code})
    return get_factory_attempt(conn, attempt["attempt_id"]), job, True


def reserve_factory_intake(
    conn: sqlite3.Connection, *, work_item_key: str, mode: str, request_id: str,
    reason: str, actor: str, payload: dict[str, Any],
    hatchet_workflow_run_id: str | None = None,
    hatchet_step_run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Atomically reserve fresh Linear intake through the factory attempt path."""
    if not isinstance(payload, dict):
        raise BridgeValidationError("factory intake payload must be an object")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND mode=? AND request_id=?",
            (work_item_key, mode, request_id),
        ).fetchone()
        if existing:
            attempt = _row_to_factory_attempt(existing)
            if attempt["reason"] != sanitize_detail(reason.strip(), 320):
                raise BridgeConflict("factory intake request_id conflicts with existing intent")
            job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
            if not job:
                raise BridgeConflict("existing factory intake is incomplete")
            if not _factory_intake_job_matches(job, work_item_key=work_item_key, mode=mode, payload=payload):
                raise BridgeConflict("factory intake request_id conflicts with existing intent")
            return attempt, job, False

        active = conn.execute(
            "SELECT * FROM factory_attempts WHERE work_item_key=? AND action='create_draft_pr' AND status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','cancellation_requested','effect_outcome_unknown') ORDER BY updated_at DESC LIMIT 1",
            (work_item_key,),
        ).fetchone()
        if active:
            attempt = _row_to_factory_attempt(active)
            job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
            if not job:
                raise BridgeConflict("active factory intake is incomplete")
            if not _factory_intake_job_matches(job, work_item_key=work_item_key, mode=attempt["mode"], payload=payload):
                raise BridgeConflict("active factory intake conflicts with resolved route")
            correlation = {key: payload[key] for key in ("source_event_key", "source_delivery_id") if isinstance(payload.get(key), str)}
            if correlation:
                merged_payload, new_event_keys = _merge_correlation_payload(job["payload"], correlation)
                if merged_payload != job["payload"]:
                    conn.execute(
                        "UPDATE bridge_jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?",
                        (canonical_json(merged_payload), utc_now(), job["job_id"]),
                    )
                    _append_event(conn, job_id=job["job_id"], event_type="correlation_recorded", state=job["state"], details={"source_event_keys": new_event_keys})
                    job = get_job(conn, job["job_id"])
            return attempt, job, False

        attempt, created = _reserve_factory_attempt_locked(
            conn, work_item_key=work_item_key, mode=mode, request_id=request_id,
            reason=reason, actor=actor, artifact_fence=payload.get("factory_artifact_fence"),
            trusted_snapshot_digest=payload.get("trusted_snapshot_digest"),
            reserved_event_type="intake_reserved",
        )
        if not created:
            raise BridgeConflict("factory intake reservation unexpectedly deduplicated")
        sealed_payload = dict(payload)
        sealed_payload["factory_retry"] = {
            "attempt_id": attempt["attempt_id"], "generation": attempt["generation"],
            "request_id": request_id, "reason": sanitize_detail(reason.strip(), 320),
            "actor": actor, "prior_attempt_id": None, "supersede_pr_url": None,
        }
        sealed_payload["work_item_id"] = work_item_key
        sealed_payload["linear_issue"] = work_item_key
        sealed_payload["dry_run"] = mode == "dry-run"
        job, job_created = submit_job(conn, {
            "idempotency_key": attempt["idempotency_key"],
            "requested_action": "create_draft_pr",
            "branch": attempt["branch"],
            "hatchet_workflow_run_id": hatchet_workflow_run_id,
            "hatchet_step_run_id": hatchet_step_run_id,
            "payload": sealed_payload,
        })
        if not job_created:
            raise BridgeConflict("reserved factory intake bridge job already exists")
        now = utc_now()
        conn.execute("UPDATE factory_attempts SET status='accepted', job_id=?, updated_at=? WHERE attempt_id=?", (job["job_id"], now, attempt["attempt_id"]))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "intake_requested", {"generation": attempt["generation"], "job_id": job["job_id"]})
        _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_accepted", {"job_id": job["job_id"]})
        outbox_key = f"factory:accepted:{attempt['attempt_id']}"
        conn.execute("INSERT INTO factory_attempt_outbox (outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", now, now))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "accepted_event_enqueued", {"outbox_key": outbox_key})
    return get_factory_attempt(conn, attempt["attempt_id"]), job, True


def _factory_intake_job_matches(job: dict[str, Any], *, work_item_key: str, mode: str, payload: dict[str, Any]) -> bool:
    job_payload = job.get("payload")
    if not isinstance(job_payload, dict):
        return False
    return (
        job.get("requested_action") == "create_draft_pr"
        and job_payload.get("repo") == payload.get("repo")
        and job_payload.get("linear_issue") == work_item_key
        and job_payload.get("dry_run") is (mode == "dry-run")
    )


def cancel_factory_attempt(conn: sqlite3.Connection, attempt_id: str, *, reason: str, actor: str) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320 or not isinstance(actor, str) or not actor or len(actor) > 200:
        raise BridgeValidationError("factory cancellation is invalid")
    with conn:
        attempt = get_factory_attempt(conn, attempt_id)
        if attempt["status"] in FACTORY_ATTEMPT_TERMINAL:
            return attempt
        job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
        after_effect = bool(job and job["state"] in {"effect_committed", "verification_pending", "ready_for_human_review"})
        lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL", (attempt_id,)).fetchone()
        # Before effect authorization cancellation is cooperative. After the
        # durable grant, the provider call is in flight by definition: report
        # too-late/in-flight and preserve the one authorized owner.
        if lease and not after_effect:
            conn.execute("UPDATE factory_execution_leases SET cancel_requested_at=? WHERE attempt_id=?", (utc_now(), attempt_id))
            conn.execute("UPDATE factory_attempts SET status='cancellation_requested', reason=?, updated_at=? WHERE attempt_id=?", (sanitize_detail(reason.strip(), 320), utc_now(), attempt_id))
            _append_factory_attempt_event(conn, attempt_id, "cancellation_requested", {"after_effect": False, "cooperative": lease["effect_authorized_at"] is None, "too_late_in_flight": lease["effect_authorized_at"] is not None})
        else:
            status = "canceled_after_effect" if after_effect else "canceled"
            conn.execute("UPDATE factory_attempts SET status=?, reason=?, updated_at=? WHERE attempt_id=?", (status, sanitize_detail(reason.strip(), 320), utc_now(), attempt_id))
            _append_factory_attempt_event(conn, attempt_id, "cancellation_requested", {"after_effect": after_effect, "cooperative": False})
    return get_factory_attempt(conn, attempt_id)


def claim_factory_execution(conn: sqlite3.Connection, job_id: str, *, owner: str, ttl_seconds: int = 900) -> dict[str, Any]:
    if not isinstance(owner, str) or not owner or len(owner) > 200 or not isinstance(ttl_seconds, int) or not 30 <= ttl_seconds <= 1800:
        raise BridgeValidationError("factory execution claim is invalid")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_factory_attempt_for_job(conn, job_id)
        if not attempt:
            job = get_job(conn, job_id)
            if job["requested_action"] == "create_draft_pr" and job["payload"].get("dry_run") is not True:
                return {"claimed": False, "reason": "factory_attempt_required", "attempt": None}
            return {"claimed": True, "attempt": None, "lease": None}
        if attempt["status"] in {"canceled", "canceled_after_effect", "superseded", "failed", "verified_ready", "effect_outcome_unknown", "effect_committed", "verification_pending"}:
            return {"claimed": False, "reason": "factory_attempt_terminal", "attempt": attempt}
        now = datetime.now(timezone.utc); now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        active = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL", (attempt["attempt_id"],)).fetchone()
        if active and active["expires_at"] > now_text:
            return {"claimed": False, "reason": "factory_execution_in_progress", "attempt": attempt}
        if active:
            # An expired executor may have crossed the external-effect boundary
            # before it died.  Do not reclaim and create a second PR path; the
            # reconciler must establish the artifact outcome first.
            # Preserve a post-authorization cancellation marker so the sole
            # owner can later truthfully record canceled-after-effect. Otherwise
            # surface the timeout as an operator-visible unknown outcome.
            if attempt["status"] != "cancellation_requested":
                conn.execute("UPDATE factory_attempts SET status='effect_outcome_unknown',updated_at=? WHERE attempt_id=?", (now_text, attempt["attempt_id"]))
            _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_execution_recovery_required", {"expired_lease_id": active["lease_id"]})
            return {"claimed": False, "reason": "factory_execution_recovery_required", "attempt": attempt}
        lease_id, fence_token = str(uuid.uuid4()), str(uuid.uuid4())
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        prior_epoch = conn.execute("SELECT COALESCE(MAX(epoch),0) FROM factory_execution_leases WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0]
        epoch = int(prior_epoch) + 1
        conn.execute("INSERT OR REPLACE INTO factory_execution_leases (attempt_id,lease_id,owner,acquired_at,expires_at,epoch,fence_token,last_heartbeat_at,effect_authorized_at,released_at,cancel_requested_at,effect_observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (attempt["attempt_id"], lease_id, owner, now_text, expires, epoch, fence_token, now_text, None, None, None, None))
        conn.execute("UPDATE factory_attempts SET status='running', updated_at=? WHERE attempt_id=?", (now_text, attempt["attempt_id"]))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_execution_claimed", {"lease_id": lease_id, "epoch": epoch})
        return {"claimed": True, "attempt": get_factory_attempt(conn, attempt["attempt_id"]), "lease": {"lease_id": lease_id, "owner": owner, "epoch": epoch, "fence_token": fence_token, "expires_at": expires}}


def _current_execution_lease(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any], *, allow_cancel: bool = False, require_live: bool = True, require_authorized: bool = False) -> sqlite3.Row | None:
    required = {"lease_id", "owner", "epoch", "fence_token"}
    if not isinstance(claim, dict) or not required <= set(claim) or not isinstance(claim["lease_id"], str) or not isinstance(claim["owner"], str) or not isinstance(claim["epoch"], int) or not isinstance(claim["fence_token"], str):
        raise BridgeValidationError("factory execution fence is invalid")
    row = conn.execute("SELECT l.* FROM factory_execution_leases l JOIN factory_attempts a ON a.attempt_id=l.attempt_id WHERE a.job_id=?", (job_id,)).fetchone()
    if not row or row["lease_id"] != claim["lease_id"] or row["owner"] != claim["owner"] or row["epoch"] != claim["epoch"] or row["fence_token"] != claim["fence_token"] or row["released_at"] is not None:
        return None
    if require_live and row["expires_at"] <= utc_now():
        return None
    if not allow_cancel and row["cancel_requested_at"] is not None:
        return None
    if require_authorized and row["effect_authorized_at"] is None:
        return None
    return row


def heartbeat_factory_execution(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any], *, ttl_seconds: int = 900) -> bool:
    if not isinstance(ttl_seconds, int) or not 30 <= ttl_seconds <= 1800:
        raise BridgeValidationError("factory execution heartbeat is invalid")
    with conn:
        row = _current_execution_lease(conn, job_id, claim)
        if not row:
            return False
        now = datetime.now(timezone.utc); now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute("UPDATE factory_execution_leases SET last_heartbeat_at=?,expires_at=? WHERE attempt_id=? AND epoch=? AND fence_token=?", (now_text, expires, row["attempt_id"], row["epoch"], row["fence_token"]))
        return True


def execution_may_effect(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any]) -> bool:
    return _current_execution_lease(conn, job_id, claim) is not None


def authorize_factory_effect(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any]) -> bool:
    """Irrevocably assign the one external-effect authority for a generation."""
    with conn:
        row = _current_execution_lease(conn, job_id, claim)
        if not row:
            return False
        job = get_job(conn, job_id)
        attempt = get_factory_attempt(conn, row["attempt_id"])
        if job["state"] != "running" or attempt["job_id"] != job_id or attempt["status"] not in {"running", "effect_authorized"}:
            raise BridgeConflict("factory effect authorization requires a running attempt and job")
        if row["effect_authorized_at"] is not None:
            # Only this same authenticated claim can observe its idempotent grant.
            return True
        now = utc_now()
        conn.execute("UPDATE factory_execution_leases SET effect_authorized_at=? WHERE attempt_id=? AND epoch=? AND fence_token=? AND effect_authorized_at IS NULL", (now, row["attempt_id"], row["epoch"], row["fence_token"]))
        conn.execute("UPDATE factory_attempts SET status='effect_authorized',updated_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
        _append_factory_attempt_event(conn, row["attempt_id"], "effect_authorized", {"owner": sanitize_detail(row["owner"], 120), "epoch": row["epoch"]})
        return True


def settle_factory_execution(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any], *, effect_observed: bool) -> None:
    with conn:
        row = _current_execution_lease(conn, job_id, claim, allow_cancel=True, require_live=False)
        if not row:
            return
        now = utc_now()
        if row["effect_authorized_at"]:
            # Authorization is absorbing. Observation is not a transfer or a
            # release: the only owner/fence remains able to commit the receipt.
            conn.execute("UPDATE factory_execution_leases SET effect_observed_at=COALESCE(effect_observed_at,?) WHERE attempt_id=? AND epoch=? AND fence_token=?", (now if effect_observed else None, row["attempt_id"], row["epoch"], row["fence_token"]))
            if effect_observed:
                _append_factory_attempt_event(conn, row["attempt_id"], "effect_authorized_observed", {})
                return
            conn.execute("UPDATE factory_attempts SET status='effect_outcome_unknown',updated_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
            _append_factory_attempt_event(conn, row["attempt_id"], "effect_authorized_outcome_unknown", {})
            return
        conn.execute("UPDATE factory_execution_leases SET released_at=?, effect_observed_at=? WHERE attempt_id=? AND epoch=? AND fence_token=?", (now, now if effect_observed else None, row["attempt_id"], row["epoch"], row["fence_token"]))
        if row["cancel_requested_at"] and not effect_observed:
            conn.execute("UPDATE factory_attempts SET status='canceled', updated_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
            _append_factory_attempt_event(conn, row["attempt_id"], "attempt_canceled_before_effect", {})


def commit_factory_execution(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """Fence the only post-provider effect commit for a retry generation."""
    if not isinstance(details, dict):
        raise BridgeValidationError("factory execution commit details are invalid")
    with conn:
        row = _current_execution_lease(conn, job_id, claim, allow_cancel=True, require_live=False, require_authorized=True)
        if not row:
            raise BridgeConflict("factory execution fence is stale or canceled")
        attempt = get_factory_attempt(conn, row["attempt_id"])
        if attempt["status"] == "superseded":
            raise BridgeConflict("factory execution attempt was superseded")
        job = transition_job(
            conn,
            job_id,
            state="effect_committed",
            details=details,
            _factory_execution_commit=True,
        )
        now = utc_now()
        conn.execute("UPDATE factory_execution_leases SET released_at=?,effect_observed_at=? WHERE attempt_id=? AND epoch=? AND fence_token=?", (now, now, row["attempt_id"], row["epoch"], row["fence_token"]))
        return job


def recover_factory_execution(conn: sqlite3.Connection, job_id: str, *, request_key: str, evidence: dict[str, Any], actor: str) -> dict[str, Any]:
    """Record authenticated recovery evidence; never reauthorize this generation."""
    if not isinstance(request_key, str) or not request_key or len(request_key) > 200 or not isinstance(evidence, dict) or not isinstance(actor, str) or not actor:
        raise BridgeValidationError("factory execution recovery is invalid")
    with conn:
        attempt = get_factory_attempt_for_job(conn, job_id)
        digest = hashlib.sha256(canonical_json(evidence).encode()).hexdigest()
        existing = conn.execute("SELECT * FROM factory_recovery_requests WHERE request_key=?", (request_key,)).fetchone()
        if existing:
            if not attempt or existing["attempt_id"] != attempt["attempt_id"] or existing["evidence_digest"] != digest:
                raise BridgeConflict("factory recovery request conflicts with existing intent")
            return get_factory_attempt(conn, attempt["attempt_id"])
        if not attempt or attempt["status"] != "effect_outcome_unknown":
            raise BridgeValidationError("factory execution is not awaiting recovery")
        lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL", (attempt["attempt_id"],)).fetchone()
        if not lease or lease["expires_at"] > utc_now():
            raise BridgeValidationError("factory execution lease is not expired")
        now = utc_now()
        required = {"inspection_request_key", "inspection_result_digest", "repository", "observed_at", "number", "state", "pr_url", "head_sha", "base_ref", "head_branch", "merged", "draft"}
        job = get_job(conn, job_id)
        if set(evidence) != required or evidence["repository"] != job["payload"].get("repo"):
            raise BridgeValidationError("recovery evidence does not bind to this generation")
        inspection = conn.execute("SELECT * FROM factory_inspection_requests WHERE request_key=?", (evidence["inspection_request_key"],)).fetchone()
        if not inspection or inspection["prior_attempt_id"] != attempt["attempt_id"] or inspection["result_digest"] != evidence["inspection_result_digest"] or inspection["state"] != "completed":
            raise BridgeValidationError("recovery inspection evidence is unavailable")
        observed = get_pr_inspection(conn, inspection["repository"], inspection["url"], prior_attempt_id=inspection["prior_attempt_id"])
        if not observed or observed["result_digest"] != evidence["inspection_result_digest"] or observed["observed_at"] != evidence["observed_at"]:
            raise BridgeValidationError("recovery inspection evidence is invalid")
        try:
            receipt_time = datetime.fromisoformat(str(observed["observed_at"]).replace("Z", "+00:00"))
            receipt_age = (datetime.now(timezone.utc) - receipt_time.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError) as exc:
            raise BridgeValidationError("recovery inspection timestamp is invalid") from exc
        if receipt_age < 0 or receipt_age > 120:
            raise BridgeValidationError("recovery inspection evidence is stale")
        observed_result = observed["result"]
        field_map = {"number": "number", "state": "state", "pr_url": "url", "head_sha": "head_sha", "base_ref": "base_ref", "head_branch": "head_branch", "merged": "merged", "draft": "draft"}
        if not isinstance(observed_result, dict) or any(evidence[key] != observed_result.get(field_map[key]) for key in field_map):
            raise BridgeValidationError("recovery inspection evidence does not match receipt")
        if evidence["head_branch"] != (job.get("branch") or attempt["branch"]):
            raise BridgeValidationError("recovery inspection head does not match attempt branch")
        if attempt.get("artifact_fence_json"):
            try:
                fence = json.loads(attempt["artifact_fence_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BridgeValidationError("attempt artifact fence is malformed") from exc
            expected = {"repository": fence.get("repository"), "pr_url": fence.get("prior_pr_url"), "head_branch": fence.get("head_branch"), "head_sha": fence.get("head_sha"), "base_ref": fence.get("base_ref"), "state": fence.get("state"), "merged": fence.get("merged"), "draft": fence.get("draft")}
            if any(expected[key] is not None and evidence[key] != expected[key] for key in expected):
                raise BridgeValidationError("recovery inspection does not match durable artifact fence")
        conflict = conn.execute("SELECT request_key FROM factory_recovery_requests WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        if conflict:
            raise BridgeConflict("factory execution already has a different recovery decision")
        conn.execute("INSERT INTO factory_recovery_requests (request_key,attempt_id,job_id,evidence_json,evidence_digest,state,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (request_key, attempt["attempt_id"], job_id, canonical_json(evidence), digest, "recorded", sanitize_detail(actor, 120), now, now))
        # External calls cannot be process-fenced.  Even a trustworthy absence
        # observation is only evidence for a human/new-generation decision, not
        # authorization to invoke Hermes again with this generation.
        # An authorized owner remains the sole writer even after expiry.  Do
        # not release/reassign it: it may resume with a real provider receipt.
        if lease["effect_authorized_at"] is None:
            conn.execute("UPDATE factory_execution_leases SET released_at=? WHERE attempt_id=? AND epoch=?", (now, attempt["attempt_id"], lease["epoch"]))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "attempt_execution_recovery_recorded", {"request_key": request_key, "evidence_digest": digest, "actor": sanitize_detail(actor, 120)})
    return get_factory_attempt(conn, attempt["attempt_id"])


def list_factory_outbox(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM factory_attempt_outbox WHERE acknowledged_at IS NULL ORDER BY created_at,outbox_key LIMIT ?", (limit,)).fetchall()
    return [{key: row[key] for key in row.keys() if key != "payload_json"} | {"payload": json.loads(row["payload_json"])} for row in rows]


def claim_factory_outbox_delivery(conn: sqlite3.Connection, outbox_key: str) -> dict[str, Any] | None:
    with conn:
        row = conn.execute("SELECT * FROM factory_attempt_outbox WHERE outbox_key=? AND acknowledged_at IS NULL", (outbox_key,)).fetchone()
        if not row: return None
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        if row["delivery_lease_expires_at"] and row["delivery_lease_expires_at"] > now_text:
            return None
        lease_id = str(uuid.uuid4()); expires = (now + timedelta(seconds=60)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute("UPDATE factory_attempt_outbox SET delivery_attempts=delivery_attempts+1, state='delivering', delivery_lease_id=?, delivery_lease_expires_at=?, updated_at=? WHERE outbox_key=?", (lease_id, expires, now_text, outbox_key))
    return next(item for item in list_factory_outbox(conn, 250) if item["outbox_key"] == outbox_key)


def acknowledge_factory_outbox(conn: sqlite3.Connection, outbox_key: str) -> None:
    with conn:
        conn.execute("UPDATE factory_attempt_outbox SET state='acknowledged', acknowledged_at=?, delivery_lease_id=NULL, delivery_lease_expires_at=NULL, updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL", (utc_now(), utc_now(), outbox_key))


def list_factory_inspection_outbox(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM factory_inspection_outbox WHERE acknowledged_at IS NULL ORDER BY created_at,outbox_key LIMIT ?", (limit,)).fetchall()
    return [{key: row[key] for key in row.keys() if key != "payload_json"} | {"payload": json.loads(row["payload_json"])} for row in rows]


def claim_factory_inspection_outbox(conn: sqlite3.Connection, outbox_key: str) -> dict[str, Any] | None:
    with conn:
        row = conn.execute("SELECT * FROM factory_inspection_outbox WHERE outbox_key=? AND acknowledged_at IS NULL", (outbox_key,)).fetchone()
        if not row: return None
        now = datetime.now(timezone.utc); now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        if row["delivery_lease_expires_at"] and row["delivery_lease_expires_at"] > now_text: return None
        conn.execute("UPDATE factory_inspection_outbox SET state='delivering',delivery_attempts=delivery_attempts+1,delivery_lease_id=?,delivery_lease_expires_at=?,updated_at=? WHERE outbox_key=?", (str(uuid.uuid4()), (now + timedelta(seconds=60)).isoformat(timespec="microseconds").replace("+00:00", "Z"), now_text, outbox_key))
    return next((item for item in list_factory_inspection_outbox(conn, 250) if item["outbox_key"] == outbox_key), None)


def acknowledge_factory_inspection_outbox(conn: sqlite3.Connection, outbox_key: str) -> None:
    with conn:
        now = utc_now()
        conn.execute("UPDATE factory_inspection_outbox SET state='acknowledged',acknowledged_at=?,delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL", (now, now, outbox_key))


def _sync_factory_attempt_state(conn: sqlite3.Connection, job: dict[str, Any], state: str) -> None:
    row = conn.execute("SELECT * FROM factory_attempts WHERE job_id=?", (job["job_id"],)).fetchone()
    if not row or row["status"] in {"canceled", "canceled_after_effect", "superseded"}:
        return
    mapping = {"accepted": "accepted", "running": "running", "effect_committed": "effect_committed", "verification_pending": "verification_pending", "completed": "completed", "failed": "failed", "ready_for_human_review": "verified_ready"}
    status = mapping.get(state)
    if not status or row["status"] == status:
        return
    if row["status"] == "cancellation_requested" and state in {"effect_committed", "verification_pending", "ready_for_human_review"}:
        conn.execute("UPDATE factory_attempts SET status='canceled_after_effect', prior_pr_url=COALESCE(?,prior_pr_url), updated_at=? WHERE attempt_id=?", (job.get("pr_url"), utc_now(), row["attempt_id"]))
        _append_factory_attempt_event(conn, row["attempt_id"], "attempt_canceled_after_effect", {"job_id": job["job_id"]})
        return
    conn.execute("UPDATE factory_attempts SET status=?, prior_pr_url=COALESCE(?, prior_pr_url), updated_at=? WHERE attempt_id=?", (status, job.get("pr_url"), utc_now(), row["attempt_id"]))
    event = {"running": "attempt_started", "effect_committed": "attempt_effect_committed", "verification_pending": "attempt_verification_pending", "completed": "attempt_completed", "failed": "attempt_failed", "verified_ready": "attempt_verified_ready"}.get(status)
    if event:
        _append_factory_attempt_event(conn, row["attempt_id"], event, {"job_id": job["job_id"], "generation": row["generation"]})
    if job.get("pr_url") and row["mode"] == "real":
        try:
            _queue_pr_inspection(conn, row["work_item_key"], job["pr_url"], row["attempt_id"])
        except BridgeValidationError:
            _record_import_conflict(conn, row["work_item_key"], job["job_id"], "inspection_repository_not_canonical")


def list_jobs_by_states(
    conn: sqlite3.Connection,
    states: set[str] | list[str] | tuple[str, ...],
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not states:
        return []

    ordered_states = sorted(states)
    invalid = [state for state in ordered_states if state not in ALL_STATES]
    if invalid:
        raise BridgeValidationError(f"states must be one of {sorted(ALL_STATES)}")

    placeholders = ",".join("?" for _ in ordered_states)
    rows = conn.execute(
        f"""
        SELECT * FROM bridge_jobs
        WHERE state IN ({placeholders})
        ORDER BY updated_at ASC
        LIMIT ?
        """,
        (*ordered_states, limit),
    ).fetchall()
    return [_row_to_job(row) for row in rows]


def list_events(conn: sqlite3.Connection, ref: str, limit: int = 100) -> list[dict[str, Any]]:
    if limit < 1 or limit > 250:
        raise BridgeValidationError("event limit must be between 1 and 250")
    job = get_job(conn, ref)
    rows = conn.execute(
        """
        SELECT * FROM bridge_events
        WHERE job_id = ?
        ORDER BY created_at ASC, event_id ASC
        LIMIT ?
        """,
        (job["job_id"], limit),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def list_lifecycle_events(conn: sqlite3.Connection, *, after_created_at: str | None = None, after_event_id: str | None = None, before_created_at: str | None = None, before_event_id: str | None = None, limit: int = 250) -> list[dict[str, Any]]:
    if limit < 1 or limit > 250:
        raise BridgeValidationError("event limit must be between 1 and 250")
    if bool(after_created_at) != bool(after_event_id):
        raise BridgeValidationError("lifecycle cursor requires both created_at and event_id")
    if bool(before_created_at) != bool(before_event_id):
        raise BridgeValidationError("lifecycle bound requires both created_at and event_id")
    rows = conn.execute("""
        SELECT * FROM (
        SELECT e.event_id, e.job_id, e.event_type, e.state, e.created_at,
               j.idempotency_key, j.pr_url, j.reason_code, j.payload_json, NULL AS attempt_id, NULL AS generation
        FROM bridge_events e JOIN bridge_jobs j ON j.job_id = e.job_id
        UNION ALL
        SELECT ae.event_id, a.job_id, ae.event_type, a.status, ae.created_at,
               j.idempotency_key, a.prior_pr_url, j.reason_code, j.payload_json, a.attempt_id, a.generation
        FROM factory_attempt_events ae JOIN factory_attempts a ON a.attempt_id=ae.attempt_id
        LEFT JOIN bridge_jobs j ON j.job_id=a.job_id
        ) ORDER BY created_at ASC, event_id ASC
    """).fetchall()
    events = []
    for row in rows:
        if after_created_at and after_event_id and (row["created_at"], row["event_id"]) <= (after_created_at, after_event_id): continue
        if before_created_at and before_event_id and (row["created_at"], row["event_id"]) > (before_created_at, before_event_id): continue
        try: payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError, json.JSONDecodeError): payload = {}
        events.append({
            "eventId": row["event_id"], "jobId": row["job_id"], "eventType": row["event_type"],
            "state": row["state"], "createdAt": row["created_at"], "idempotencyKey": row["idempotency_key"],
            "prUrl": row["pr_url"], "reasonCode": row["reason_code"],
            "payload": {key: payload.get(key) for key in ("linear_issue_id", "linear_issue", "linear_issue_url", "source_event_key", "source_delivery_id", "repo", "dry_run")},
            "attemptId": row["attempt_id"], "generation": row["generation"], "sourceKind": "factory_attempt" if row["attempt_id"] else "bridge_event",
        })
        if len(events) >= limit: break
    return events


def count_events(conn: sqlite3.Connection, ref: str) -> int:
    job = get_job(conn, ref)
    return int(conn.execute("SELECT COUNT(*) FROM bridge_events WHERE job_id = ?", (job["job_id"],)).fetchone()[0])


def record_job_correlation(conn: sqlite3.Connection, ref: str, correlation: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(correlation, dict) or set(correlation) - {"source_event_key", "source_delivery_id"}:
        raise BridgeValidationError("bridge correlation may contain only source_event_key and source_delivery_id")
    normalized = _normalize_correlation_payload(correlation)
    if not normalized:
        raise BridgeValidationError("bridge correlation requires a bounded source event or delivery identifier")
    with conn:
        job = get_job(conn, ref)
        merged, new_keys = _merge_correlation_payload(job["payload"], normalized)
        if merged != job["payload"]:
            conn.execute("UPDATE bridge_jobs SET payload_json = ?, updated_at = ? WHERE job_id = ?", (canonical_json(merged), utc_now(), job["job_id"]))
            _append_event(conn, job_id=job["job_id"], event_type="correlation_recorded", state=job["state"], details={"source_event_keys": new_keys})
    return get_job(conn, job["job_id"])


def record_reconciliation_findings(conn: sqlite3.Connection, findings: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not isinstance(findings, list) or len(findings) > 250:
        raise BridgeValidationError("findings must be a list of at most 250 entries")
    sanitized = [_sanitize_finding(conn, finding) for finding in findings]
    now = utc_now()
    created: list[str] = []
    existing: list[str] = []
    with conn:
        for finding in sanitized:
            key, job_id, subject, kind = finding["finding_key"], finding["job_id"], finding["subject"], finding["kind"]
            row = conn.execute("SELECT occurrence_count FROM reconciliation_findings WHERE finding_key = ?", (key,)).fetchone()
            if row:
                conn.execute("UPDATE reconciliation_findings SET last_seen_at = ?, occurrence_count = occurrence_count + 1 WHERE finding_key = ?", (now, key))
                existing.append(key)
            else:
                conn.execute(
                    "INSERT INTO reconciliation_findings (finding_key, job_id, subject, kind, details_json, first_seen_at, last_seen_at, occurrence_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (key, job_id, subject, kind, canonical_json(finding), now, now),
                )
                created.append(key)
    return created, existing


def _sanitize_finding(conn: sqlite3.Connection, finding: Any) -> dict[str, Any]:
    if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
        raise BridgeValidationError("reconciliation finding must contain only required fields")
    key, job_id, subject, kind, severity, details = (finding[name] for name in ("finding_key", "job_id", "subject", "kind", "severity", "details"))
    if not isinstance(key, str) or not key or len(key) > 300:
        raise BridgeValidationError("reconciliation finding_key must be a non-empty string of at most 300 characters")
    if not isinstance(subject, str) or not subject or len(subject) > 300:
        raise BridgeValidationError("reconciliation finding subject must be a non-empty string of at most 300 characters")
    if not isinstance(kind, str) or kind not in RECONCILIATION_KINDS:
        raise BridgeValidationError("reconciliation finding kind is not recognized")
    if not isinstance(severity, str) or severity not in {"warning", "critical"}:
        raise BridgeValidationError("reconciliation finding severity must be warning or critical")
    if job_id is not None:
        if not isinstance(job_id, str):
            raise BridgeValidationError("reconciliation finding job_id must be a UUID or null")
        try:
            uuid.UUID(job_id)
        except ValueError as exc:
            raise BridgeValidationError("reconciliation finding job_id must be a UUID or null") from exc
        if not conn.execute("SELECT 1 FROM bridge_jobs WHERE job_id = ?", (job_id,)).fetchone():
            raise BridgeValidationError("reconciliation finding job_id does not exist")
    if not isinstance(details, dict) or set(details) != FINDING_DETAIL_FIELDS or not _valid_finding_details(details):
        raise BridgeValidationError("reconciliation finding details must contain expected and observed sanitized state")
    return {"finding_key": key, "job_id": job_id, "subject": subject, "kind": kind, "severity": severity, "details": {"expected": details["expected"], "observed": details["observed"]}}


def _valid_finding_details(details: Any) -> bool:
    forbidden = {"raw_payload", "rawpayload", "description", "body", "token", "secret", "credential", "session", "error"}
    try:
        encoded = canonical_json(details)
    except (TypeError, ValueError):
        return False
    if not isinstance(details, dict) or len(encoded) > 4096:
        return False

    def walk(value: Any, depth: int = 0) -> bool:
        if depth > 5:
            return False
        if isinstance(value, dict):
            return all(isinstance(key, str) and key.lower().replace("_", "") not in forbidden and walk(item, depth + 1) for key, item in value.items())
        if isinstance(value, list):
            return len(value) <= 50 and all(walk(item, depth + 1) for item in value)
        return value is None or isinstance(value, (str, int, float, bool))
    return walk(details)


def _normalize_correlation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    keys = _bounded_strings([payload.get("source_event_key"), *(payload.get("source_event_keys") or [])], 16)
    deliveries = _bounded_strings([payload.get("source_delivery_id"), *(payload.get("source_delivery_ids") or [])], 16)
    if keys:
        normalized["source_event_key"] = keys[0]
        normalized["source_event_keys"] = keys
    if deliveries:
        normalized["source_delivery_id"] = deliveries[0]
        normalized["source_delivery_ids"] = deliveries
    return normalized


def _merge_correlation_payload(existing: dict[str, Any], incoming: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    merged = _normalize_correlation_payload(existing)
    incoming = _normalize_correlation_payload(incoming)
    old_keys = _bounded_strings([merged.get("source_event_key"), *(merged.get("source_event_keys") or [])], 16)
    keys = _bounded_strings([*old_keys, incoming.get("source_event_key"), *(incoming.get("source_event_keys") or [])], 16)
    deliveries = _bounded_strings([merged.get("source_delivery_id"), *(merged.get("source_delivery_ids") or []), incoming.get("source_delivery_id"), *(incoming.get("source_delivery_ids") or [])], 16)
    if keys:
        merged["source_event_key"] = keys[0]
        merged["source_event_keys"] = keys
    if deliveries:
        merged["source_delivery_id"] = deliveries[0]
        merged["source_delivery_ids"] = deliveries
    return merged, [key for key in keys if key not in old_keys]


def _bounded_strings(values: list[Any], limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 300 or value in result:
            continue
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _validate_verification_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"local_commands", "secret_check"}:
        raise BridgeValidationError("verification_context must contain only local_commands and secret_check")
    commands = value.get("local_commands")
    secret = value.get("secret_check")
    if not isinstance(commands, list) or not commands or len(commands) > 32:
        raise BridgeValidationError("verification_context.local_commands must be a non-empty bounded list")
    normalized_commands = []
    for command in commands:
        if not isinstance(command, dict) or set(command) != {"command", "exit_code"} or not isinstance(command["command"], str) or not command["command"].strip() or len(command["command"]) > 300 or not isinstance(command["exit_code"], int):
            raise BridgeValidationError("verification_context.local_commands is malformed")
        normalized_commands.append({"command": command["command"].strip(), "exit_code": command["exit_code"]})
    if not isinstance(secret, dict) or set(secret) != {"command", "exit_code"} or not isinstance(secret.get("command"), str) or not secret["command"].strip() or len(secret["command"]) > 300 or not isinstance(secret.get("exit_code"), int):
        raise BridgeValidationError("verification_context.secret_check is malformed")
    return {"local_commands": normalized_commands, "secret_check": {"command": secret["command"].strip(), "exit_code": secret["exit_code"]}}


def _canonical_verifier_policy(repo: str) -> dict[str, Any]:
    configured = os.environ.get("HATCHET_VERIFIER_POLICY_PATH")
    if not configured:
        raise BridgeValidationError("HATCHET_VERIFIER_POLICY_PATH is required for real verification")
    path = Path(configured)
    try:
        catalog = json.loads(path.read_text())
        version = catalog["version"]
        policies = [item for item in catalog["policies"] if isinstance(item, dict) and item.get("repo") == repo]
        if not isinstance(version, int) or version < 1 or len(policies) != 1:
            raise ValueError("catalog is invalid")
        policy = policies[0]
        required = {"id", "repo", "expected_base", "required_github_checks", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command"}
        if set(policy) != required or any(not isinstance(policy[key], str) or not policy[key].strip() for key in ("id", "repo", "expected_base", "secret_check_command")) or any(not isinstance(policy[key], list) or not policy[key] or any(not isinstance(item, str) or not item.strip() for item in policy[key]) for key in ("required_github_checks", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands")):
            raise ValueError("repository policy is invalid")
        return {"catalog_version": version, "digest": hashlib.sha256(canonical_json(policy).encode()).hexdigest(), **policy}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeValidationError("checked-in verifier policy is unavailable or invalid") from exc


def _validate_payload_policy(job: dict[str, Any]) -> dict[str, Any]:
    repo = job["payload"].get("repo")
    if not isinstance(repo, str):
        raise BridgeValidationError("real draft PR job requires repository policy")
    canonical = _canonical_verifier_policy(repo)
    supplied = job["payload"].get("verification_policy")
    if not isinstance(supplied, dict) or supplied != canonical:
        raise BridgeValidationError("submitted verifier policy does not exactly match the checked-in policy")
    for key in ("expected_base", "required_github_checks", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command"):
        if job["payload"].get(key) != canonical[key]:
            raise BridgeValidationError("submitted verifier policy fields do not exactly match the checked-in policy")
    return canonical


def _validate_verification_receipt(value: Any, job: dict[str, Any]) -> tuple[dict[str, Any], str]:
    required = {"version", "verifier_version", "verified_at", "repo", "pr_url", "branch", "commit_sha", "policy_id", "policy_catalog_version", "policy_digest", "checks", "pr_identity", "file_policy", "local_policy", "secret_policy"}
    if not isinstance(value, dict) or set(value) != required:
        raise BridgeValidationError("ready_for_human_review requires a complete verification receipt")
    for field in ("version", "verifier_version", "verified_at", "repo", "pr_url", "branch", "commit_sha", "policy_id", "policy_digest"):
        if not isinstance(value[field], str) or not value[field].strip() or len(value[field]) > 500:
            raise BridgeValidationError(f"verification receipt {field} is invalid")
    if value["version"] != "draft-pr-verification-v1" or value["repo"] != job["payload"].get("repo") or value["pr_url"] != job.get("pr_url") or value["branch"] != job.get("branch"):
        raise BridgeValidationError("verification receipt does not match bridge job identity")
    try:
        _canonical_time(value["verified_at"])
    except (TypeError, ValueError):
        raise BridgeValidationError("verification receipt verified_at is not a timezone-aware timestamp") from None
    policy = _validate_payload_policy(job)
    if value["policy_id"] != policy["id"] or value["policy_catalog_version"] != policy["catalog_version"] or value["policy_digest"] != policy["digest"]:
        raise BridgeValidationError("verification receipt does not match the checked-in verifier policy")
    expected_checks = policy["required_github_checks"]
    if not isinstance(value["checks"], list) or {item.get("name") for item in value["checks"] if isinstance(item, dict)} != set(expected_checks) or len(value["checks"]) != len(expected_checks) or any(not isinstance(item, dict) or set(item) != {"name", "conclusion"} or item.get("conclusion") != "SUCCESS" for item in value["checks"]):
        raise BridgeValidationError("verification receipt checks do not exactly match the required successful set")
    pr_identity, files, local, secret = (value[field] for field in ("pr_identity", "file_policy", "local_policy", "secret_policy"))
    expected_author = os.environ.get("DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN", "factory-hermes[bot]")
    allowed_authors = _github_app_author_aliases(expected_author)
    expected_identity = {"base": policy["expected_base"], "draft": True, "author": pr_identity.get("author") if isinstance(pr_identity, dict) else None, "linear_reference": f"Refs Linear {job['payload'].get('linear_issue')}"}
    if not isinstance(pr_identity, dict) or pr_identity != expected_identity or pr_identity.get("author") not in allowed_authors:
        raise BridgeValidationError("verification receipt PR identity is incomplete or mismatched")
    observed_files = files.get("files") if isinstance(files, dict) else None
    if not isinstance(files, dict) or files.get("allowed_changed_paths") != policy["allowed_changed_paths"] or files.get("forbidden_changed_paths") != policy["forbidden_changed_paths"] or not isinstance(observed_files, list) or not observed_files or any(not _valid_repo_path(item) for item in observed_files) or len(set(observed_files)) != len(observed_files) or len({item.casefold() for item in observed_files}) != len(observed_files) or any(not _path_allowed(item, policy["allowed_changed_paths"]) or _path_allowed(item, policy["forbidden_changed_paths"]) for item in observed_files):
        raise BridgeValidationError("verification receipt file evidence is incomplete or mismatched")
    if not isinstance(local, dict) or local.get("required_commands") != policy["required_local_commands"] or not isinstance(local.get("results"), list) or len(local["results"]) != len(policy["required_local_commands"]) or {item.get("command") for item in local["results"] if isinstance(item, dict)} != set(policy["required_local_commands"]) or any(not isinstance(item, dict) or set(item) != {"command", "exit_code"} or item.get("exit_code") != 0 for item in local["results"]):
        raise BridgeValidationError("verification receipt local command evidence is incomplete or mismatched")
    if not isinstance(secret, dict) or secret.get("required_command") != policy["secret_check_command"] or secret.get("result") != {"command": policy["secret_check_command"], "exit_code": 0}:
        raise BridgeValidationError("verification receipt secret-check evidence is incomplete or mismatched")
    original = job.get("verification_context")
    if not isinstance(original, dict) or original.get("local_commands") != local.get("results") or original.get("secret_check") != secret.get("result"):
        raise BridgeValidationError("verification receipt does not exactly match original verification context")
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return value, digest


def _github_app_author_aliases(expected_login: str) -> set[str]:
    if expected_login.endswith("[bot]") and all(ch.isalnum() or ch == "-" for ch in expected_login[:-5]):
        return {expected_login, f"app/{expected_login[:-5]}"}
    return {expected_login}


def _valid_repo_path(value: Any) -> bool:
    return isinstance(value, str) and value == __import__("unicodedata").normalize("NFC", value) and not value.startswith("/") and "\\" not in value and all(part not in {"", ".", ".."} for part in value.split("/"))


def _path_allowed(path: str, rules: list[str]) -> bool:
    return any(path == rule.rstrip("/") or path.startswith(rule.rstrip("/") + "/") for rule in rules)


def transition_job(
    conn: sqlite3.Connection,
    ref: str,
    *,
    state: str,
    details: dict[str, Any] | None = None,
    _factory_execution_commit: bool = False,
    _factory_verifier_completion: bool = False,
) -> dict[str, Any]:
    if state not in ALL_STATES:
        raise BridgeValidationError(f"state must be one of {sorted(ALL_STATES)}")

    details = sanitize_detail(details or {})
    if not isinstance(details, dict):
        raise BridgeValidationError("details must be a JSON object")

    with conn:
        row = conn.execute(
            """
            SELECT * FROM bridge_jobs
            WHERE job_id = ? OR idempotency_key = ?
            """,
            (ref, ref),
        ).fetchone()
        if not row:
            raise BridgeNotFound(f"job not found: {ref}")

        job = _row_to_job(row)
        current_state = job["state"]
        if state not in TRANSITIONS[current_state]:
            raise BridgeConflict(f"invalid transition from {current_state} to {state}")
        if state == "completed" and job["payload"].get("dry_run") is not True:
            raise BridgeValidationError("completed is reserved for dry_run bridge jobs")
        factory_attempt = get_factory_attempt_for_job(conn, job["job_id"])
        if factory_attempt and factory_attempt["mode"] == "real":
            immutable_legacy_import = (
                factory_attempt["generation"] == 1
                and factory_attempt["actor"] == "legacy-import"
            )
            active_execution = conn.execute(
                "SELECT effect_authorized_at FROM factory_execution_leases WHERE attempt_id=? AND released_at IS NULL",
                (factory_attempt["attempt_id"],),
            ).fetchone()
            protected_factory_phase = factory_attempt["status"] in {
                "effect_authorized", "effect_committed", "verification_pending",
                "verified_ready", "canceled_after_effect", "effect_outcome_unknown",
            } or (active_execution and active_execution["effect_authorized_at"] is not None)
            if state == "effect_committed" and not _factory_execution_commit and not immutable_legacy_import:
                raise BridgeValidationError("factory effect completion requires the fenced execution endpoint")
            if state == "ready_for_human_review" and not _factory_verifier_completion and not immutable_legacy_import:
                raise BridgeValidationError("factory verification completion requires the verifier endpoint")
            if (protected_factory_phase or active_execution) and state in {"failed", "completed", "ready_for_human_review", "effect_committed"}:
                permitted = (
                    (state == "effect_committed" and _factory_execution_commit)
                    or (state in {"failed", "ready_for_human_review"} and _factory_verifier_completion and current_state == "verification_pending")
                    # This cannot succeed: the legacy-specific guard below
                    # rejects it with the durable migration reason.
                    or (state == "ready_for_human_review" and immutable_legacy_import)
                )
                if not permitted:
                    raise BridgeValidationError("factory execution terminal transition requires the dedicated execution, cancel, or verifier endpoint")
        if current_state in {"effect_committed", "verification_pending"}:
            for field in ("commit_sha", "pr_url", "branch", "repo"):
                if field in details:
                    raise BridgeValidationError("effect references are immutable after effect_committed")
        if state == "effect_committed" and job["payload"].get("dry_run") is not True:
            if not isinstance(details.get("commit_sha"), str):
                raise BridgeValidationError("real effect_committed requires an immutable commit reference")
        supplied_context = details.get("verification_context")
        if "verification_context" in details:
            if not (current_state == "effect_committed" and state == "verification_pending"):
                raise BridgeValidationError("verification_context may be established only from effect_committed to verification_pending")
            if job.get("verification_context") is not None:
                raise BridgeValidationError("verification_context is immutable once established")
            supplied_context = _validate_verification_context(supplied_context)
        verification_completion = current_state == "verification_pending" and state in {"ready_for_human_review", "failed"}
        if verification_completion and factory_attempt and factory_attempt["mode"] == "real" and factory_attempt["actor"] != "legacy-import" and not _factory_verifier_completion:
            raise BridgeValidationError("factory verification completion requires the verifier endpoint")
        if verification_completion:
            _require_claimed_verification_lease(conn, job, details.get("verification_attempt_id"))
        if state == "ready_for_human_review":
            legacy_attempt = conn.execute("SELECT generation, mode, actor FROM factory_attempts WHERE job_id = ?", (job["job_id"],)).fetchone()
            if legacy_attempt and legacy_attempt["generation"] == 1 and legacy_attempt["mode"] == "real" and legacy_attempt["actor"] == "legacy-import":
                raise BridgeValidationError("legacy generation-one real jobs cannot complete verification; reserve a fresh retry")
            if not (details.get("pr_url") or job.get("pr_url")):
                raise BridgeValidationError("ready_for_human_review requires a draft PR URL")
            if details.get("reason_code") != "draft_pr_verified":
                raise BridgeValidationError("ready_for_human_review requires draft_pr_verified evidence")
            receipt, digest = _validate_verification_receipt(details.get("verification_receipt"), job)
            if job.get("verification_observed_context") != receipt:
                raise BridgeValidationError("verification receipt does not exactly match durable observed context")
            expected_commit = job.get("commit_sha")
            if receipt["commit_sha"] != expected_commit:
                raise BridgeValidationError("verification receipt commit does not match bridge job")
            details = {**details, "verification_receipt": receipt, "verification_digest": digest}
        context = supplied_context if "verification_context" in details else job.get("verification_context")

        fields = {
            "state": state,
            "commit_sha": details.get("commit_sha", job["commit_sha"]),
            "pr_url": details.get("pr_url", job["pr_url"]),
            "hermes_session_id": details.get("hermes_session_id", job["hermes_session_id"]),
            "verification_context_json": canonical_json(context) if context is not None else None,
            "verification_original_context_json": canonical_json(context) if context is not None else None,
            "verification_observed_context_json": canonical_json(job["verification_observed_context"]) if job.get("verification_observed_context") is not None else None,
            "verification_receipt_json": canonical_json(details["verification_receipt"]) if state == "ready_for_human_review" else job.get("verification_receipt") and canonical_json(job["verification_receipt"]),
            "verification_digest": details.get("verification_digest", job.get("verification_digest")),
            "reason_code": details.get("reason_code", job["reason_code"]),
            "error": details.get("error", job["error"]),
            "updated_at": utc_now(),
        }
        conn.execute(
            """
            UPDATE bridge_jobs
            SET state = ?,
                commit_sha = ?,
                pr_url = ?,
                hermes_session_id = ?,
                verification_context_json = ?,
                verification_original_context_json = ?,
                verification_observed_context_json = ?,
                verification_receipt_json = ?,
                verification_digest = ?,
                reason_code = ?,
                error = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                fields["state"],
                fields["commit_sha"],
                fields["pr_url"],
                fields["hermes_session_id"],
                fields["verification_context_json"],
                fields["verification_original_context_json"],
                fields["verification_observed_context_json"],
                fields["verification_receipt_json"],
                fields["verification_digest"],
                fields["reason_code"],
                fields["error"],
                fields["updated_at"],
                job["job_id"],
            ),
        )
        if state in {"ready_for_human_review", "failed"}:
            _close_verification_lease(conn, job["job_id"], "verified" if state == "ready_for_human_review" else "failed")
        _sync_factory_attempt_state(conn, job, state)
        _append_event(
            conn,
            job_id=job["job_id"],
            event_type=f"transition:{state}",
            state=state,
            details=details,
        )
        if verification_completion and state == "failed" and factory_attempt:
            _reserve_factory_verification_repair(
                conn,
                failed_job=get_job(conn, job["job_id"]),
                factory_attempt=factory_attempt,
                details=details,
            )

    return get_job(conn, job["job_id"])
