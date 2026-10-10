from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import nullcontext
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
from . import work_evidence

DEFAULT_DB_PATH = "/var/lib/driftward-hatchet-bridge/bridge.db"
DOCKER_VERIFIER_POLICY_PATH = "/app/hatchet/workers/verifier-policy.json"
SCHEMA_VERSION = 8
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
ANALYTICS_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
SECRET_SHAPE_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|hf_|gsk_|lin_api_|glpat-|"
    r"npm_|pypi-|akia[a-z0-9]|asia[a-z0-9]|aiza[a-z0-9]|bearer(?:[._+-]|$))|"
    r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}",
    re.I,
)
JWT_SHAPE_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}",
    re.I,
)
OPAQUE_SECRET_SHAPE_RE = re.compile(r"[A-Za-z0-9_.-]{128,}")
EXECUTION_TRANSPORT_STATUSES = frozenset({
    "client_setup_failed", "grpc_aborted", "grpc_already_exists", "grpc_cancelled",
    "grpc_data_loss", "grpc_deadline_exceeded", "grpc_failed_precondition",
    "grpc_internal", "grpc_invalid_argument", "grpc_not_found", "grpc_ok",
    "grpc_out_of_range", "grpc_permission_denied", "grpc_resource_exhausted",
    "grpc_unauthenticated", "grpc_unavailable", "grpc_unimplemented", "grpc_unknown",
})
EXECUTION_PROCESS_SIGNALS = frozenset({
    "SIGABRT", "SIGALRM", "SIGBUS", "SIGCHLD", "SIGCONT", "SIGFPE", "SIGHUP",
    "SIGILL", "SIGINT", "SIGIO", "SIGIOT", "SIGKILL", "SIGPIPE", "SIGPOLL",
    "SIGPROF", "SIGPWR", "SIGQUIT", "SIGSEGV", "SIGSTKFLT", "SIGSTOP", "SIGSYS",
    "SIGTERM", "SIGTRAP", "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGURG", "SIGUSR1",
    "SIGUSR2", "SIGVTALRM", "SIGWINCH", "SIGXCPU", "SIGXFSZ",
})
EFFECT_COMMIT_INSPECTION_MAX_ATTEMPTS = 3
EXECUTION_ANALYTICS_COMMON_FIELDS = (
    "analytics_schema_version",
    "invocation_id",
    "execution_backend",
    "execution_transport",
    "backend_version",
    "configured_auth_mode",
)
EXECUTION_FAILURE_EVENT_TYPES = frozenset({
    "execution_backend_failed",
    "execution_dispatch_failed",
    "execution_effect_commit_failed",
})
EXECUTION_ROUTE_FIELDS = frozenset({
    "analytics_schema_version",
    "assignment_id",
    "experiment_id",
    "variant",
    "assignment_method",
    "intended_backend",
    "intended_transport",
    "configured_provider",
    "configured_model",
    "configured_auth_mode",
    "backend_version",
    "factory_release_sha",
    "factory_release_version",
    "routing_registry_version",
    "verifier_policy_digest",
    "fallback_allowed",
})
EXECUTION_FAILURE_DOMAINS = frozenset({
    "unknown", "factory", "executor", "executor_backend", "gateway_transport", "gateway",
    "hermes_process", "hermes_protocol", "auth", "provider", "tool", "github",
    "verification",
})
EXECUTION_FAILURE_STAGES = frozenset({
    "unknown", "preflight", "artifact_fence", "effect_authorization", "receipt_dispatch",
    "backend_invocation_fence", "backend_execution", "gateway_connect",
    "gateway_handler", "hermes_spawn", "hermes_exit", "hermes_timeout",
    "result_parse", "auth_resolve", "auth_refresh", "model_request",
    "model_stream", "tool_execution", "effect_commit", "verification_checks",
    "verification_recovery",
})
EXECUTION_FAILURE_OPTIONAL_FIELDS = frozenset({
    "transport_status", "process_exit_code", "process_signal", "timed_out",
    "buffer_exceeded",
})
EXECUTION_COMMIT_FIELDS = frozenset({
    "commit_sha", "pr_url", "branch", "reason_code", "hermes_session_id",
    "verification_context",
    *EXECUTION_ANALYTICS_COMMON_FIELDS, "backend_outcome", "backend_elapsed_ms",
})

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
INSPECTION_CAPABILITY_TTL_SECONDS = 120
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
        value = JWT_SHAPE_RE.sub("[redacted]", value)
        value = OPAQUE_SECRET_SHAPE_RE.sub("[redacted]", value)
        value = re.sub(
            r"(^|[^A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|hf_|gsk_|lin_api_|glpat-|"
            r"npm_|pypi-|akia|asia|aiza|bearer(?:[._+-]|$))[A-Za-z0-9_.+/-]*",
            r"\1[redacted]",
            value,
            flags=re.I | re.MULTILINE,
        )
        return re.sub(r"\bBearer\s+\S+", "[redacted]", value, flags=re.I).replace("\n", " ").replace("\r", " ")[:limit]
    if isinstance(value, list): return [sanitize_detail(item, limit) for item in value]
    if isinstance(value, dict): return {str(key): sanitize_detail(item, limit) for key, item in value.items()}
    return value


def _secret_shaped(value: str) -> bool:
    return bool(
        SECRET_SHAPE_RE.search(value) or OPAQUE_SECRET_SHAPE_RE.search(value)
    )


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
        "factory_execution_manifests", "factory_work_checkpoints",
        "reconciliation_scan_state",
        "factory_issue_controls", "factory_issue_control_commands",
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
        CREATE INDEX IF NOT EXISTS idx_bridge_jobs_scan
            ON bridge_jobs(created_at, job_id);
        CREATE INDEX IF NOT EXISTS idx_bridge_jobs_branch
            ON bridge_jobs(branch);
        CREATE INDEX IF NOT EXISTS idx_bridge_jobs_pr_url
            ON bridge_jobs(pr_url);

        CREATE TABLE IF NOT EXISTS reconciliation_scan_state (
            cohort TEXT PRIMARY KEY CHECK(cohort IN ('active','history')),
            cursor_created_at TEXT, cursor_job_id TEXT,
            upper_created_at TEXT, upper_job_id TEXT,
            page_token TEXT, pending_json TEXT CHECK(pending_json IS NULL OR (json_valid(pending_json) AND length(pending_json)<=32768)),
            last_ack_token TEXT,
            CHECK((cursor_created_at IS NULL)=(cursor_job_id IS NULL)),
            CHECK((upper_created_at IS NULL)=(upper_job_id IS NULL)),
            CHECK((page_token IS NULL)=(pending_json IS NULL))
        );

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

        CREATE TABLE IF NOT EXISTS factory_issue_controls (
            work_item_key TEXT PRIMARY KEY,
            held INTEGER NOT NULL CHECK(held IN (0,1)),
            revision INTEGER NOT NULL CHECK(revision>0),
            actor TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS factory_issue_control_commands (
            request_id TEXT PRIMARY KEY, work_item_key TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('take_control','return_control')),
            intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64),
            command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
            actor TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER NOT NULL,
            attempt_id TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(work_item_key) REFERENCES factory_issue_controls(work_item_key),
            FOREIGN KEY(attempt_id) REFERENCES factory_attempts(attempt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_factory_issue_control_commands_issue
            ON factory_issue_control_commands(work_item_key, revision);

        CREATE TABLE IF NOT EXISTS factory_execution_manifests (
            manifest_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES factory_attempts(attempt_id),
            phase TEXT NOT NULL CHECK(phase IN ('admission','execution')),
            parent_manifest_id TEXT,
            contract_version INTEGER NOT NULL CHECK(contract_version=1),
            document_json TEXT NOT NULL CHECK(json_valid(document_json)),
            document_digest TEXT NOT NULL CHECK(length(document_digest)=64),
            created_at TEXT NOT NULL,
            UNIQUE(attempt_id,phase),
            UNIQUE(manifest_id,attempt_id),
            CHECK((phase='admission' AND parent_manifest_id IS NULL) OR (phase='execution' AND parent_manifest_id IS NOT NULL)),
            FOREIGN KEY(parent_manifest_id,attempt_id) REFERENCES factory_execution_manifests(manifest_id,attempt_id)
        );
        CREATE TABLE IF NOT EXISTS factory_work_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES factory_attempts(attempt_id),
            checkpoint_type TEXT NOT NULL CHECK(checkpoint_type IN ('specification_snapshot','implementation_receipt','verification_receipt')),
            manifest_id TEXT NOT NULL,
            contract_version INTEGER NOT NULL CHECK(contract_version=1),
            document_json TEXT NOT NULL CHECK(json_valid(document_json)),
            document_digest TEXT NOT NULL CHECK(length(document_digest)=64),
            created_at TEXT NOT NULL,
            UNIQUE(attempt_id,checkpoint_type),
            FOREIGN KEY(manifest_id,attempt_id) REFERENCES factory_execution_manifests(manifest_id,attempt_id)
        );

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
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS protect_factory_issue_control_commands_{operation.lower()} BEFORE {operation} ON factory_issue_control_commands BEGIN SELECT RAISE(ABORT, 'issue control history is append-only'); END")
        conn.execute("CREATE TRIGGER IF NOT EXISTS protect_factory_issue_control_commands_replace BEFORE INSERT ON factory_issue_control_commands WHEN EXISTS (SELECT 1 FROM factory_issue_control_commands WHERE request_id=NEW.request_id) BEGIN SELECT RAISE(ABORT, 'issue control history is append-only'); END")
        for table in ("factory_execution_manifests", "factory_work_checkpoints"):
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS protect_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'work evidence is append-only'); END")
            identity, kind = ("manifest_id", "phase") if table == "factory_execution_manifests" else ("checkpoint_id", "checkpoint_type")
            # REPLACE otherwise bypasses DELETE triggers when recursive_triggers is off.
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS protect_{table}_replace BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE {identity}=NEW.{identity} OR (attempt_id=NEW.attempt_id AND {kind}=NEW.{kind})) BEGIN SELECT RAISE(ABORT, 'work evidence is append-only'); END")
        _migration_failpoint("after-ddl")
        _migrate_bridge_verification_columns(conn)
        _migrate_reconciliation_findings(conn)
        _migrate_factory_retry_columns(conn)
        _migrate_conflict_resolution_columns(conn)
        _migrate_canonical_times(conn, "bridge_jobs", ("created_at", "updated_at"))
        _migrate_canonical_times(conn, "bridge_events", ("created_at",))
        _backfill_factory_attempts(conn)
        _backfill_factory_inspections(conn)
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
    semantics = {"version": parsed["version"], "routes": normalized}
    if parsed.get("retired_repositories"):
        semantics["retired_repositories"] = sorted(parsed["retired_repositories"])
    return hashlib.sha256(canonical_json(semantics).encode()).hexdigest()


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
    _migrate_factory_inspection_request_uniqueness(conn)
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


def _migrate_factory_inspection_request_uniqueness(conn: sqlite3.Connection) -> None:
    """Inspection requests are keyed by attempt lineage, not globally by PR."""
    for index in conn.execute("PRAGMA index_list(factory_inspection_requests)").fetchall():
        unique = index["unique"] if isinstance(index, sqlite3.Row) else index[2]
        if not unique:
            continue
        name = str(index["name"] if isinstance(index, sqlite3.Row) else index[1]).replace('"', '""')
        fields = [
            row["name"] if isinstance(row, sqlite3.Row) else row[2]
            for row in conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        ]
        if fields != ["repository", "number", "url"]:
            continue
        conn.executescript(
            """
            ALTER TABLE factory_inspection_requests RENAME TO factory_inspection_requests_legacy_unique;
            CREATE TABLE factory_inspection_requests (
                request_key TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                url TEXT NOT NULL, job_id TEXT, work_item_key TEXT, prior_attempt_id TEXT,
                capability TEXT, state TEXT NOT NULL, result_json TEXT, result_digest TEXT, observed_at TEXT, reason TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO factory_inspection_requests (
                request_key, repository, number, url, job_id, work_item_key, prior_attempt_id,
                capability, state, result_json, result_digest, observed_at, reason, created_at, updated_at
            )
            SELECT
                request_key, repository, number, url, job_id, work_item_key, prior_attempt_id,
                capability, state, result_json, result_digest, observed_at, reason, created_at, updated_at
            FROM factory_inspection_requests_legacy_unique;
            DROP TABLE factory_inspection_requests_legacy_unique;
            """
        )
        return


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


def _row_to_factory_execution_lease(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
    """Return execution lease state without exposing the fence token."""
    now = now or datetime.now(timezone.utc)
    try:
        expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        expires_at = now
    if row["released_at"] is not None:
        status = "released"
    elif expires_at <= now:
        status = "expired"
    elif row["cancel_requested_at"] is not None:
        status = "cancel_requested"
    elif row["effect_authorized_at"] is not None:
        status = "effect_authorized"
    else:
        status = "active"
    return {
        "attempt_id": row["attempt_id"],
        "lease_id": row["lease_id"],
        "owner": sanitize_detail(row["owner"], 120),
        "acquired_at": row["acquired_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "cancel_requested_at": row["cancel_requested_at"],
        "effect_observed_at": row["effect_observed_at"],
        "effect_authorized_at": row["effect_authorized_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "epoch": row["epoch"],
        "status": status,
        "currently_live": row["released_at"] is None and expires_at > now,
        "fence_token_present": bool(row["fence_token"]),
    }


def _job_lifecycle_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    payload_keys = (
        "repo",
        "linear_issue",
        "linear_issue_id",
        "linear_issue_url",
        "routing",
        "source_event_key",
        "source_delivery_id",
        "source_event_keys",
        "source_delivery_ids",
        "dry_run",
        "factory_retry",
        "factory_repair",
        "factory_artifact_fence",
        "factory_absence_retry",
        "superseded_bridge_job",
    )
    return {
        "job_id": job["job_id"],
        "idempotency_key": job["idempotency_key"],
        "state": job["state"],
        "requested_action": job["requested_action"],
        "branch": job["branch"],
        "commit_sha": job["commit_sha"],
        "pr_url": job["pr_url"],
        "hermes_session_id": job["hermes_session_id"],
        "hatchet_workflow_run_id": job["hatchet_workflow_run_id"],
        "hatchet_step_run_id": job["hatchet_step_run_id"],
        "reason_code": job["reason_code"],
        "error": sanitize_detail(job["error"]),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "payload": sanitize_detail({key: payload.get(key) for key in payload_keys if key in payload}),
    }


def _factory_attempt_lifecycle_summary(conn: sqlite3.Connection, attempt: dict[str, Any]) -> dict[str, Any]:
    job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
    lease_row = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    return {
        "attempt": attempt,
        "job": _job_lifecycle_summary(job),
        "execution_lease": _row_to_factory_execution_lease(lease_row) if lease_row else None,
    }


def _append_factory_attempt_event(conn: sqlite3.Connection, attempt_id: str, event_type: str, details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), attempt_id, event_type, canonical_json(sanitize_detail(details or {})), utc_now()),
    )


def _insert_work_evidence(
    conn: sqlite3.Connection, *, attempt_id: str, kind: str,
    document: dict[str, Any], manifest_id: str | None = None,
) -> str:
    """Insert producer-built evidence in the caller's lifecycle transaction."""
    if not conn.in_transaction:
        raise BridgeValidationError("work evidence requires a lifecycle transaction")
    is_manifest = kind in {"admission", "execution"}
    table = "factory_execution_manifests" if is_manifest else "factory_work_checkpoints"
    identity = "manifest_id" if is_manifest else "checkpoint_id"
    kind_column = "phase" if is_manifest else "checkpoint_type"
    reference_column = "parent_manifest_id" if is_manifest else "manifest_id"
    serialized = work_evidence.canonical(document)
    if len(serialized) > 16384 or sanitize_detail(document) != document:
        raise BridgeValidationError("work evidence contains disallowed material")
    evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"driftward.work-evidence:v1:{attempt_id}:{kind}"))
    existing = conn.execute(f"SELECT * FROM {table} WHERE {identity}=?", (evidence_id,)).fetchone()
    if existing:
        if existing["document_json"] != serialized or existing[reference_column] != manifest_id:
            raise BridgeConflict("work evidence is immutable")
        return evidence_id
    conn.execute(
        f"INSERT INTO {table} ({identity},attempt_id,{kind_column},{reference_column},contract_version,document_json,document_digest,created_at) VALUES (?,?,?,?,1,?,?,?)",
        (evidence_id, attempt_id, kind, manifest_id, serialized, work_evidence.digest(document), utc_now()),
    )
    return evidence_id


def _record_factory_admission_evidence(conn: sqlite3.Connection, attempt_id: str) -> None:
    attempt = get_factory_attempt(conn, attempt_id)
    job = get_job(conn, attempt["job_id"])
    manifest = work_evidence.admission_manifest(attempt, job)
    manifest_id = _insert_work_evidence(conn, attempt_id=attempt_id, kind="admission", document=manifest)
    _insert_work_evidence(
        conn, attempt_id=attempt_id, kind="specification_snapshot", manifest_id=manifest_id,
        document=work_evidence.specification_snapshot(manifest),
    )


def _record_factory_transition_checkpoint(conn: sqlite3.Connection, job: dict[str, Any], *, verification_completion: bool) -> None:
    if job["payload"].get("dry_run") is True:
        return
    attempt = get_factory_attempt_for_job(conn, job["job_id"])
    if not attempt:
        return
    manifest = conn.execute(
        "SELECT manifest_id FROM factory_execution_manifests WHERE attempt_id=? ORDER BY phase DESC LIMIT 1",
        (attempt["attempt_id"],),
    ).fetchone()
    # In-flight pre-upgrade work has no historical admission snapshot. Never invent one.
    if manifest is None:
        return
    if job["state"] == "effect_committed":
        kind, document = "implementation_receipt", work_evidence.implementation_receipt(job)
    elif verification_completion:
        kind = "verification_receipt"
        document = work_evidence.verification_receipt(job, passed=job["state"] == "ready_for_human_review")
    else:
        return
    _insert_work_evidence(conn, attempt_id=attempt["attempt_id"], kind=kind, document=document, manifest_id=manifest["manifest_id"])


def get_factory_work_evidence(conn: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    """Root-operator read; no payloads, transcripts, arbitrary artifacts or writes."""
    def records(table: str, order: str) -> list[dict[str, Any]]:
        result = []
        for row in conn.execute(f"SELECT * FROM {table} WHERE attempt_id=? ORDER BY {order}", (attempt_id,)):
            item = dict(row)
            item["document"] = json.loads(item.pop("document_json"))
            result.append(item)
        return result
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        get_factory_attempt(conn, attempt_id)
        manifests = records("factory_execution_manifests", "phase")
        checkpoints = records("factory_work_checkpoints", "created_at,checkpoint_id")
    missing = work_evidence.replay_gaps(manifests[-1]["document"] if manifests else None)
    return {
        "schema": "factory-work-evidence-v1", "attempt_id": attempt_id,
        "available": bool(manifests), "manifests": manifests, "checkpoints": checkpoints,
        "replay": {"available": not missing, "missing": missing},
    }


def _append_factory_attempt_event_once(
    conn: sqlite3.Connection,
    attempt_id: str,
    event_type: str,
    occurrence_key: str,
) -> bool:
    """Append a payload-free event once for one trusted decision key."""
    event_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"driftward.factory.attempt-event:{attempt_id}:{event_type}:{occurrence_key}",
    ))
    existing = conn.execute(
        "SELECT attempt_id,event_type,details_json FROM factory_attempt_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if existing:
        if (
            existing["attempt_id"] != attempt_id
            or existing["event_type"] != event_type
            or existing["details_json"] != "{}"
        ):
            raise BridgeConflict("factory attempt decision event is immutable")
        return False
    conn.execute(
        "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
        (event_id, attempt_id, event_type, "{}", utc_now()),
    )
    return True


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

    # Admission, job, evidence and outbox must commit together when called by a reservation.
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if action in DRAFT_PR_EFFECT_ACTIONS:
            issue = payload.get("work_item_id") or payload.get("linear_issue")
            _require_issue_factory_control(conn, issue)
            if conn.execute("SELECT 1 FROM factory_issue_controls WHERE work_item_key=?", (issue,)).fetchone():
                lineage = payload.get("factory_retry")
                bound = conn.execute("SELECT 1 FROM factory_attempts WHERE attempt_id=? AND work_item_key=? AND idempotency_key=? AND status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','verified_ready','completed')",
                                     ((lineage or {}).get("attempt_id") if isinstance(lineage, dict) else None, issue, idempotency_key)).fetchone()
                if not bound:
                    raise BridgeConflict("controlled issue requires an explicitly reserved Factory attempt")
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


def _require_verification_eligible(conn: sqlite3.Connection, job: dict[str, Any]) -> dict[str, Any] | None:
    _require_issue_factory_control(conn, _job_work_item(job))
    if job["state"] not in {"effect_committed", "verification_pending"} or not job.get("pr_url") or not job.get("branch"):
        raise BridgeValidationError("verification lease requires a committed draft PR job")
    attempt = get_factory_attempt_for_job(conn, job["job_id"])
    if attempt:
        current = conn.execute(
            "SELECT attempt_id FROM factory_attempts WHERE work_item_key=? AND action=? AND mode=? ORDER BY generation DESC LIMIT 1",
            (attempt["work_item_key"], attempt["action"], attempt["mode"]),
        ).fetchone()
        if attempt["mode"] != "real" or attempt["status"] not in {"effect_committed", "verification_pending"} or current["attempt_id"] != attempt["attempt_id"]:
            raise BridgeValidationError("verification requires the current eligible Factory attempt")
    return attempt


def enqueue_verification_resume(conn: sqlite3.Connection, ref: str, *, stale_after_minutes: Any) -> dict[str, Any]:
    """Durably wake verification without holding a lease during event delivery."""
    if not isinstance(stale_after_minutes, int) or isinstance(stale_after_minutes, bool) or not 1 <= stale_after_minutes <= 10080:
        raise BridgeValidationError("verification resume stale_after_minutes must be between 1 and 10080")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        job = get_job(conn, ref)
        attempt = get_factory_attempt_for_job(conn, job["job_id"])
        if not attempt or (attempt["actor"] == "legacy-import" and attempt["generation"] == 1):
            return {"enqueued": False, "reason": "legacy_job"}
        try:
            _require_verification_eligible(conn, job)
            payload = job["payload"]
            _parse_pr_reference(payload.get("repo"), job["pr_url"])
            expected_branch = attempt["branch"]
            if job["requested_action"] == "repair_draft_pr":
                _validate_repair_payload(payload)
                repair = payload["factory_repair"]
                expected_branch = repair["branch"]
                if repair["pr_url"] != job["pr_url"] or repair["source_attempt_id"] != attempt["prior_attempt_id"]:
                    raise BridgeValidationError("verification recovery repair identity is invalid")
            # prior_pr_url can lag as a lineage summary. The committed job PR
            # is authoritative; repairs also bind their source artifact above.
            if (job["requested_action"] not in DRAFT_PR_EFFECT_ACTIONS or attempt["action"] != "create_draft_pr" or payload.get("dry_run") is not False
                or job["idempotency_key"] != attempt["idempotency_key"] or job["branch"] != expected_branch
                or (payload.get("work_item_id") or payload.get("linear_issue")) != attempt["work_item_key"]
                or not isinstance(job["commit_sha"], str) or not re.fullmatch(r"[0-9a-f]{7,64}", job["commit_sha"])):
                raise BridgeValidationError("verification recovery artifact identity is invalid")
            updated_at = _canonical_time(job["updated_at"])
        except (BridgeValidationError, BridgeConflict, TypeError, ValueError):
            return {"enqueued": False, "reason": "not_eligible"}
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=stale_after_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if updated_at > cutoff:
            return {"enqueued": False, "reason": "not_stale"}
        lease = conn.execute("SELECT * FROM verification_leases WHERE job_id=?", (job["job_id"],)).fetchone()
        if lease and lease["closed_at"] is None and lease["expires_at"] > now.isoformat(timespec="microseconds").replace("+00:00", "Z"):
            return {"enqueued": False, "reason": "lease_active"}
        enqueued = _enqueue_factory_attempt_resume(conn, attempt, job, preserve_pending=True, event_type="verification_recovery_wake_enqueued")
        return {"enqueued": enqueued, "reason": "enqueued" if enqueued else "already_queued"}


def _execution_receipt_recovery_eligible(conn: sqlite3.Connection, attempt: dict[str, Any]) -> bool:
    if attempt["mode"] != "real" or attempt["status"] not in {"effect_authorized", "effect_outcome_unknown", "cancellation_requested"}:
        return False
    lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
    return bool(lease and lease["effect_authorized_at"] and not lease["released_at"] and conn.execute(
        "SELECT 1 FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_backend_invoked'",
        (attempt["attempt_id"],),
    ).fetchone())


def _enqueue_execution_receipt_recovery(conn: sqlite3.Connection, attempt: dict[str, Any], job: dict[str, Any]) -> bool:
    if not _execution_receipt_recovery_eligible(conn, attempt) or job["state"] != "running":
        return False
    return _enqueue_factory_attempt_resume(conn, attempt, job, preserve_pending=True,
        event_type="execution_receipt_recovery_queued", receipt_recovery=True)


def enqueue_execution_receipt_recovery(conn: sqlite3.Connection, ref: str, *, stale_after_minutes: Any) -> dict[str, Any]:
    if not isinstance(stale_after_minutes, int) or isinstance(stale_after_minutes, bool) or not 1 <= stale_after_minutes <= 10080:
        raise BridgeValidationError("execution recovery stale_after_minutes must be between 1 and 10080")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        job = get_job(conn, ref)
        attempt = get_factory_attempt_for_job(conn, job["job_id"])
        if not attempt or not _execution_receipt_recovery_eligible(conn, attempt):
            return {"enqueued": False, "reason": "not_eligible"}
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if _canonical_time(job["updated_at"]) > cutoff:
            return {"enqueued": False, "reason": "not_stale"}
        enqueued = _enqueue_execution_receipt_recovery(conn, attempt, job)
        return {"enqueued": enqueued, "reason": "enqueued" if enqueued else "already_queued"}


def record_execution_receipt_observation(conn: sqlite3.Connection, ref: str, observation: Any) -> None:
    if (not isinstance(observation, dict) or set(observation) != {"state", "cancellation_requested", "cancellation_acknowledged"}
        or not isinstance(observation["state"], str) or observation["state"] not in {"not_found", "running", "outcome_unknown", "completed", "unavailable"}
        or any(type(observation[key]) is not bool for key in ("cancellation_requested", "cancellation_acknowledged"))
        or (observation["cancellation_acknowledged"] and (observation["state"] != "completed" or not observation["cancellation_requested"]))):
        raise BridgeValidationError("invalid execution receipt observation")
    with conn:
        job = get_job(conn, ref)
        attempt = get_factory_attempt_for_job(conn, job["job_id"])
        if not attempt or not _execution_receipt_recovery_eligible(conn, attempt):
            return
        # Observation alone never releases a fence or marks manual editing safe.
        _append_factory_attempt_event(conn, attempt["attempt_id"], "execution_receipt_observed", observation)


def acquire_verification_lease(conn: sqlite3.Connection, ref: str, *, attempt_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 200:
        raise BridgeValidationError("verification attempt_id is invalid")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 30 or ttl_seconds > 900:
        raise BridgeValidationError("verification lease ttl_seconds must be between 30 and 900")
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        job = get_job(conn, ref)
        _require_verification_eligible(conn, job)
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
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        job = get_job(conn, ref)
        _require_verification_eligible(conn, job)
        now = utc_now()
        updated = conn.execute("UPDATE verification_leases SET claimed_at = ? WHERE job_id = ? AND owner_attempt_id = ? AND claimed_at IS NULL AND closed_at IS NULL AND expires_at > ?", (now, job["job_id"], attempt_id, now)).rowcount
        if updated:
            _append_event(conn, job_id=job["job_id"], event_type="verification_lease_claimed", state=job["state"], details={"action": "verify_draft_pr"})
        return bool(updated)


def _close_verification_lease(conn: sqlite3.Connection, job_id: str, outcome: str) -> None:
    conn.execute("UPDATE verification_leases SET closed_at = ?, outcome = ? WHERE job_id = ? AND closed_at IS NULL", (utc_now(), outcome, job_id))


def _require_claimed_verification_lease(conn: sqlite3.Connection, job: dict[str, Any], attempt_id: Any) -> None:
    _require_verification_eligible(conn, job)
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 200:
        raise BridgeValidationError("verification completion requires verification_attempt_id")
    lease = conn.execute("SELECT * FROM verification_leases WHERE job_id = ?", (job["job_id"],)).fetchone()
    now = utc_now()
    work_item_key = str(job["payload"].get("work_item_id") or job["payload"].get("linear_issue") or job["idempotency_key"])
    if not lease or lease["action"] != "verify_draft_pr" or lease["owner_attempt_id"] != attempt_id or lease["claimed_at"] is None or lease["closed_at"] is not None or lease["expires_at"] <= now or lease["work_item_key"] != work_item_key or lease["branch"] != job["branch"] or lease["pr_url"] != job["pr_url"]:
        raise BridgeValidationError("verification completion does not own an active claimed lease")


def record_verification_observation(conn: sqlite3.Connection, ref: str, *, attempt_id: Any, receipt: Any) -> dict[str, Any]:
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
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


def _reconciliation_states(cohort: Any) -> tuple[str, ...]:
    if not isinstance(cohort, str) or cohort not in {"active", "history"}:
        raise BridgeValidationError("reconciliation cohort must be active or history")
    return tuple(sorted(ACTIVE_STATES if cohort == "active" else TERMINAL_STATES))


def _reconciliation_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    job = get_job(conn, job_id)
    duplicates = {}
    if job["state"] != "failed":
        for field, result in (("branch", "branch_job_id"), ("pr_url", "pr_job_id")):
            if job.get(field):
                other = conn.execute(
                    f"SELECT job_id FROM bridge_jobs WHERE {field}=? AND job_id<>? "
                    "AND state<>'failed' ORDER BY created_at,job_id LIMIT 1",
                    (job[field], job_id),
                ).fetchone()
                if other:
                    duplicates[result] = other["job_id"]
    job["reconciliation_duplicate_refs"] = duplicates
    return job


def reconciliation_scan(conn: sqlite3.Connection, *, cohort: Any, limit: Any = 250) -> dict[str, Any]:
    """Freeze one bounded page until acknowledged; a crash cannot skip its jobs."""
    states = _reconciliation_states(cohort)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 250:
        raise BridgeValidationError("reconciliation scan limit must be between 1 and 250")
    state_sql = f"state IN ({','.join('?' for _ in states)})"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO reconciliation_scan_state(cohort) VALUES (?)", (cohort,))
        scan = conn.execute("SELECT * FROM reconciliation_scan_state WHERE cohort=?", (cohort,)).fetchone()
        replayed = scan["pending_json"] is not None
        if scan["pending_json"] is None:
            upper = (scan["upper_created_at"], scan["upper_job_id"])
            if upper[0] is None:
                row = conn.execute(
                    f"SELECT created_at,job_id FROM bridge_jobs WHERE {state_sql} "
                    "ORDER BY created_at DESC,job_id DESC LIMIT 1", states,
                ).fetchone()
                upper = (row["created_at"], row["job_id"]) if row else (None, None)
            rows = []
            if upper[0] is not None:
                after = " AND (created_at,job_id)>(?,?)" if scan["cursor_created_at"] is not None else ""
                params = (*states, *upper, *((scan["cursor_created_at"], scan["cursor_job_id"]) if after else ()), limit + 1)
                rows = conn.execute(
                    f"SELECT created_at,job_id FROM bridge_jobs WHERE {state_sql} "
                    f"AND (created_at,job_id)<=(?,?){after} ORDER BY created_at,job_id LIMIT ?", params,
                ).fetchall()
            page = rows[:limit]
            pending = {
                "job_ids": [row["job_id"] for row in page],
                "end": [page[-1]["created_at"], page[-1]["job_id"]] if page else None,
                "sweep_complete": len(rows) <= limit,
                "first": scan["cursor_created_at"] is None,
            }
            token = str(uuid.uuid4())
            conn.execute(
                "UPDATE reconciliation_scan_state SET upper_created_at=?,upper_job_id=?,"
                "page_token=?,pending_json=? WHERE cohort=?",
                (*upper, token, canonical_json(pending), cohort),
            )
        else:
            pending = json.loads(scan["pending_json"])
            token = scan["page_token"]
        jobs = [_reconciliation_job(conn, job_id) for job_id in pending["job_ids"]]
        total = int(conn.execute(f"SELECT COUNT(*) FROM bridge_jobs WHERE {state_sql}", states).fetchone()[0])
        # Reaching the tail is not full coverage. New arrivals and changes of
        # cohort can also make even a formerly single-page scan incomplete.
        unseen = total
        if pending["job_ids"]:
            unseen = int(conn.execute(
                f"SELECT COUNT(*) FROM bridge_jobs WHERE {state_sql} "
                f"AND job_id NOT IN ({','.join('?' for _ in pending['job_ids'])})",
                (*states, *pending["job_ids"]),
            ).fetchone()[0])
        return {"cohort": cohort, "token": token, "jobs": jobs, "total": total,
                "complete": pending["first"] and pending["sweep_complete"] and unseen == 0,
                "sweep_complete": pending["sweep_complete"], "replayed": replayed}


def acknowledge_reconciliation_scan(conn: sqlite3.Connection, *, cohort: Any, token: Any) -> None:
    _reconciliation_states(cohort)
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", token):
        raise BridgeValidationError("reconciliation scan token is invalid")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        scan = conn.execute("SELECT * FROM reconciliation_scan_state WHERE cohort=?", (cohort,)).fetchone()
        if scan and scan["last_ack_token"] == token:
            return
        if not scan or scan["page_token"] != token:
            raise BridgeConflict("reconciliation scan token is stale or unknown")
        pending = json.loads(scan["pending_json"])
        end = (None, None) if pending["sweep_complete"] else pending["end"]
        upper = (None, None) if pending["sweep_complete"] else (scan["upper_created_at"], scan["upper_job_id"])
        conn.execute(
            "UPDATE reconciliation_scan_state SET cursor_created_at=?,cursor_job_id=?,"
            "upper_created_at=?,upper_job_id=?,last_ack_token=?,page_token=NULL,pending_json=NULL "
            "WHERE cohort=? AND page_token=?", (*end, *upper, token, cohort, token),
        )


def reconciliation_correlations(conn: sqlite3.Connection, request: Any) -> dict[str, list[str]]:
    keys = {"source_event_keys", "legacy_work_items", "hatchet_run_ids"}
    if not isinstance(request, dict) or set(request) != keys:
        raise BridgeValidationError("reconciliation correlations require only source_event_keys, legacy_work_items, hatchet_run_ids")
    for values in request.values():
        if not isinstance(values, list) or len(values) > 250 or any(
            not isinstance(value, str) or not value or len(value) > 500 or value != value.strip() for value in values
        ):
            raise BridgeValidationError("reconciliation correlations require at most 250 bounded strings per field")
    event_array = "CASE WHEN json_type(j.payload_json,'$.source_event_keys')='array' THEN json_extract(j.payload_json,'$.source_event_keys') ELSE '[]' END"
    legacy = f"COALESCE(json_type(j.payload_json,'$.source_event_key')='text' AND length(json_extract(j.payload_json,'$.source_event_key'))>0,0)=0 AND NOT EXISTS (SELECT 1 FROM json_each({event_array}) k WHERE k.type='text' AND length(k.value)>0)"
    sources = {
        "source_event_keys": f"SELECT json_extract(payload_json,'$.source_event_key') FROM bridge_jobs UNION SELECT k.value FROM bridge_jobs j,json_each({event_array}) k WHERE k.type='text'",
        "legacy_work_items": f"SELECT json_extract(j.payload_json,'$.linear_issue') FROM bridge_jobs j WHERE {legacy} UNION SELECT json_extract(j.payload_json,'$.work_item_id') FROM bridge_jobs j WHERE {legacy}",
        "hatchet_run_ids": "SELECT hatchet_workflow_run_id FROM bridge_jobs UNION SELECT hatchet_step_run_id FROM bridge_jobs",
    }
    owns_snapshot = not conn.in_transaction
    with conn if owns_snapshot else nullcontext():
        if owns_snapshot:
            conn.execute("BEGIN")
        return {
            key: [row[0] for row in conn.execute(
                f"SELECT DISTINCT requested.value FROM json_each(?) requested "
                f"WHERE requested.value IN ({sources[key]}) ORDER BY requested.key",
                (canonical_json(values),),
            ).fetchall()]
            for key, values in request.items()
        }


def list_factory_attempts(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    if not isinstance(work_item_key, str) or not work_item_key or len(work_item_key) > 200:
        raise BridgeValidationError("work item key is invalid")
    rows = conn.execute("SELECT * FROM factory_attempts WHERE work_item_key = ? ORDER BY generation DESC", (work_item_key,)).fetchall()
    return [_row_to_factory_attempt(row) for row in rows]


def list_active_factory_attempts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    statuses = tuple(sorted(FACTORY_ATTEMPT_ACTIVE))
    rows = conn.execute(
        f"SELECT * FROM factory_attempts WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY updated_at DESC, generation DESC",
        statuses,
    ).fetchall()
    return [_factory_attempt_lifecycle_summary(conn, _row_to_factory_attempt(row)) for row in rows]


def list_recent_factory_attempts(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise BridgeValidationError("recent factory attempt limit must be between 1 and 100")
    rows = conn.execute(
        "SELECT * FROM factory_attempts ORDER BY updated_at DESC, generation DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_factory_attempt_lifecycle_summary(conn, _row_to_factory_attempt(row)) for row in rows]


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


def get_factory_lifecycle(conn: sqlite3.Connection, work_item_key: str, limit: int = 250) -> dict[str, Any]:
    if limit < 1 or limit > 250:
        raise BridgeValidationError("factory lifecycle limit must be between 1 and 250")
    attempts = list_factory_attempts(conn, work_item_key)
    attempt_records = []
    timeline: list[dict[str, Any]] = []
    active_attempts = []
    for attempt in attempts:
        record = _factory_attempt_lifecycle_summary(conn, attempt)
        attempt_events = list_factory_attempt_events(conn, attempt["attempt_id"], 250)
        bridge_events = list_events(conn, attempt["job_id"], 250) if attempt.get("job_id") else []
        record["events"] = attempt_events
        record["bridge_events"] = bridge_events
        attempt_records.append(record)
        if attempt["status"] in FACTORY_ATTEMPT_ACTIVE:
            active_attempts.append(record)
        for event in attempt_events:
            timeline.append({
                "created_at": event["created_at"],
                "event_id": event["event_id"],
                "source_kind": "factory_attempt",
                "event_type": event["event_type"],
                "state": attempt["status"],
                "work_item_key": attempt["work_item_key"],
                "attempt_id": attempt["attempt_id"],
                "generation": attempt["generation"],
                "job_id": attempt.get("job_id"),
                "details": event["details"],
            })
        for event in bridge_events:
            timeline.append({
                "created_at": event["created_at"],
                "event_id": event["event_id"],
                "source_kind": "bridge_job",
                "event_type": event["event_type"],
                "state": event["state"],
                "work_item_key": attempt["work_item_key"],
                "attempt_id": attempt["attempt_id"],
                "generation": attempt["generation"],
                "job_id": event["job_id"],
                "details": event["details"],
            })
        lease = record.get("execution_lease")
        if lease:
            timeline.append({
                "created_at": lease["acquired_at"],
                "event_id": f"{lease['attempt_id']}:execution-lease:{lease['epoch']}:acquired",
                "source_kind": "factory_execution_lease",
                "event_type": "execution_lease_acquired",
                "state": lease["status"],
                "work_item_key": attempt["work_item_key"],
                "attempt_id": attempt["attempt_id"],
                "generation": attempt["generation"],
                "job_id": attempt.get("job_id"),
                "details": {"lease_id": lease["lease_id"], "owner": lease["owner"], "expires_at": lease["expires_at"]},
            })
            if lease["effect_authorized_at"]:
                timeline.append({
                    "created_at": lease["effect_authorized_at"],
                    "event_id": f"{lease['attempt_id']}:execution-lease:{lease['epoch']}:effect-authorized",
                    "source_kind": "factory_execution_lease",
                    "event_type": "execution_lease_effect_authorized",
                    "state": lease["status"],
                    "work_item_key": attempt["work_item_key"],
                    "attempt_id": attempt["attempt_id"],
                    "generation": attempt["generation"],
                    "job_id": attempt.get("job_id"),
                    "details": {"lease_id": lease["lease_id"], "owner": lease["owner"]},
                })
            if lease["released_at"]:
                timeline.append({
                    "created_at": lease["released_at"],
                    "event_id": f"{lease['attempt_id']}:execution-lease:{lease['epoch']}:released",
                    "source_kind": "factory_execution_lease",
                    "event_type": "execution_lease_released",
                    "state": lease["status"],
                    "work_item_key": attempt["work_item_key"],
                    "attempt_id": attempt["attempt_id"],
                    "generation": attempt["generation"],
                    "job_id": attempt.get("job_id"),
                    "details": {"lease_id": lease["lease_id"], "effect_observed_at": lease["effect_observed_at"]},
                })
    timeline.sort(key=lambda item: (item["created_at"], item["event_id"]))
    return {
        "work_item_key": work_item_key,
        "attempts": attempt_records,
        "active_attempts": active_attempts,
        "timeline": timeline[-limit:],
        "timeline_truncated": len(timeline) > limit,
    }


def list_factory_import_conflicts(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT conflict_key,work_item_key,job_id,reason,created_at FROM factory_import_conflicts WHERE work_item_key=? ORDER BY created_at,conflict_key", (work_item_key,)).fetchall()
    return [dict(row) for row in rows]


def _parse_pr_reference(repository: str, url: str) -> tuple[str, int, str]:
    match = PR_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if not isinstance(repository, str) or not match or match.group(0).split("/pull/")[0].removeprefix("https://github.com/") != repository:
        raise BridgeValidationError("inspection reference is invalid")
    return repository, int(url.rsplit("/", 1)[1]), url


def _inspection_age_seconds(value: Any, *, label: str) -> float:
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BridgeValidationError(f"{label} timestamp is invalid") from exc
    return (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()


def _inspection_result_is_fresh(row: sqlite3.Row) -> bool:
    if row["state"] != "completed" or not row["result_json"] or not row["observed_at"]:
        return False
    try:
        age = _inspection_age_seconds(row["observed_at"], label="inspection result")
    except BridgeValidationError:
        return False
    return 0 <= age <= INSPECTION_CAPABILITY_TTL_SECONDS


def _inspection_request_is_expired(row: sqlite3.Row) -> bool:
    if row["state"] == "completed" or row["result_json"]:
        return False
    try:
        age = _inspection_age_seconds(row["created_at"], label="inspection capability")
    except BridgeValidationError:
        return False
    return age > INSPECTION_CAPABILITY_TTL_SECONDS


def _expire_incomplete_inspection_request(conn: sqlite3.Connection, request: sqlite3.Row, *, reason_code: str = "inspection_capability_expired") -> bool:
    now = utc_now()
    updated = conn.execute(
        "UPDATE factory_inspection_requests SET state='expired', reason=?, updated_at=? WHERE request_key=? AND state='pending' AND result_json IS NULL",
        (reason_code, now, request["request_key"]),
    ).rowcount
    if not updated:
        return False
    conn.execute(
        """
        UPDATE factory_inspection_outbox
        SET state='expired', acknowledged_at=COALESCE(acknowledged_at,?), delivery_lease_id=NULL,
            delivery_lease_expires_at=NULL, updated_at=?
        WHERE request_key=? AND acknowledged_at IS NULL
        """,
        (now, now, request["request_key"]),
    )
    if isinstance(request["job_id"], str) and request["job_id"]:
        job = conn.execute("SELECT state FROM bridge_jobs WHERE job_id=?", (request["job_id"],)).fetchone()
        if job and job["state"] in {"accepted", "running"}:
            conn.execute(
                "UPDATE bridge_jobs SET state='failed', reason_code=?, error=?, updated_at=? WHERE job_id=?",
                (reason_code, "inspection capability expired before completion", now, request["job_id"]),
            )
            _append_event(conn, job_id=request["job_id"], event_type="inspection_expired", state="failed", details={"reason_code": reason_code})
    return True


def _inspection_request_base_key(work_item_key: str, prior_attempt_id: str | None, repository: str, number: int) -> str:
    return f"factory:inspect:{work_item_key}:{prior_attempt_id or 'legacy'}:{repository}:{number}"


def _validated_stranded_inspection_job(
    conn: sqlite3.Connection,
    request_key: str,
    *,
    repository: str,
    number: int,
    url: str,
) -> dict[str, Any] | None:
    stranded = conn.execute("SELECT * FROM bridge_jobs WHERE idempotency_key=?", (request_key,)).fetchone()
    if not stranded:
        return None
    job = _row_to_job(stranded)
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    inspection = payload.get("inspection") if isinstance(payload.get("inspection"), dict) else {}
    capability = payload.get("inspection_capability")
    if (
        job.get("requested_action") != "inspect_draft_pr_state"
        or payload.get("repo") != repository
        or payload.get("inspection_request_key") != request_key
        or not isinstance(capability, str)
        or len(capability) < 20
        or inspection.get("repository") != repository
        or inspection.get("number") != number
        or inspection.get("url") != url
        or payload.get("dry_run") is not False
    ):
        raise BridgeConflict("inspection idempotency key conflicts with a different inspection job")
    return job if job["state"] in ACTIVE_STATES else None


def _next_inspection_request_key(
    conn: sqlite3.Connection,
    base_key: str,
    *,
    repository: str,
    number: int,
    url: str,
) -> tuple[str, dict[str, Any] | None]:
    sequence = 1
    while True:
        request_key = base_key if sequence == 1 else f"{base_key}:refresh:{sequence}"
        if conn.execute("SELECT 1 FROM factory_inspection_requests WHERE request_key=?", (request_key,)).fetchone():
            sequence += 1
            continue
        stranded = _validated_stranded_inspection_job(conn, request_key, repository=repository, number=number, url=url)
        if stranded:
            return request_key, stranded
        if not conn.execute("SELECT 1 FROM bridge_jobs WHERE idempotency_key=?", (request_key,)).fetchone():
            return request_key, None
        sequence += 1


def _queue_pr_inspection(
    conn: sqlite3.Connection,
    work_item_key: str,
    url: str,
    prior_attempt_id: str | None = None,
    *,
    force_refresh: bool = False,
) -> bool:
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
    # number. The base key is stable, while refresh suffixes let expired or
    # stale incomplete evidence be replaced without mutating prior rows.
    base_key = _inspection_request_base_key(work_item_key, prior_attempt_id, repository, number)
    clause, args = ("prior_attempt_id IS NULL", (repository, number, url, work_item_key)) if prior_attempt_id is None else ("prior_attempt_id=?", (repository, number, url, work_item_key, prior_attempt_id))
    existing_requests = conn.execute(
        f"""
        SELECT * FROM factory_inspection_requests
        WHERE repository=? AND number=? AND url=? AND work_item_key=? AND {clause}
        ORDER BY created_at DESC, request_key DESC
        """,
        args,
    ).fetchall()
    for existing in existing_requests:
        if force_refresh and existing["state"] == "completed":
            continue
        if _inspection_result_is_fresh(existing):
            return False
        if existing["state"] == "completed" and existing["result_json"]:
            continue
        if _inspection_request_is_expired(existing):
            _expire_incomplete_inspection_request(conn, existing)
            continue
        return False
    request_key, job = _next_inspection_request_key(conn, base_key, repository=repository, number=number, url=url)
    if job:
        capability = job["payload"]["inspection_capability"]
        now = utc_now()
        conn.execute("INSERT INTO factory_inspection_requests (request_key,repository,number,url,job_id,work_item_key,prior_attempt_id,capability,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (request_key, repository, number, url, job["job_id"], work_item_key, prior_attempt_id, capability, "pending", now, now))
        outbox_key = f"{request_key}:accepted"
        outbox = conn.execute("SELECT acknowledged_at FROM factory_inspection_outbox WHERE outbox_key=?", (outbox_key,)).fetchone()
        if outbox:
            conn.execute("UPDATE factory_inspection_outbox SET state='pending', delivery_lease_id=NULL, delivery_lease_expires_at=NULL, acknowledged_at=NULL, updated_at=? WHERE outbox_key=? AND acknowledged_at IS NOT NULL", (now, outbox_key))
        else:
            conn.execute("INSERT INTO factory_inspection_outbox (outbox_key,request_key,job_id,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (outbox_key, request_key, job["job_id"], canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", now, now))
        return True
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
    return True


def _backfill_factory_inspections(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT a.attempt_id,a.work_item_key,a.job_id,a.prior_pr_url
        FROM factory_attempts a
        WHERE a.action='create_draft_pr'
          AND a.prior_pr_url IS NOT NULL
          AND a.prior_pr_url != ''
          AND a.generation = (
              SELECT MAX(b.generation)
              FROM factory_attempts b
              WHERE b.work_item_key=a.work_item_key AND b.action=a.action AND b.mode=a.mode
          )
        ORDER BY a.work_item_key,a.mode,a.generation
        """
    ).fetchall()
    for row in rows:
        try:
            _queue_pr_inspection(conn, row["work_item_key"], row["prior_pr_url"], row["attempt_id"])
        except BridgeValidationError:
            _record_import_conflict(conn, row["work_item_key"], row["job_id"] or row["attempt_id"], "inspection_repository_not_canonical")


def get_pr_inspection(conn: sqlite3.Connection, repository: str, url: str, *, prior_attempt_id: str | None = None) -> dict[str, Any] | None:
    repository, number, url = _parse_pr_reference(repository, url)
    clause, args = (" AND prior_attempt_id=?", (repository, number, url, prior_attempt_id)) if prior_attempt_id else ("", (repository, number, url))
    row = conn.execute(
        f"SELECT * FROM factory_inspection_requests WHERE repository=? AND number=? AND url=?{clause} "
        "ORDER BY created_at DESC,request_key DESC LIMIT 1",
        args,
    ).fetchone()
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
    return {
        "request_key": row["request_key"],
        "job_id": row["job_id"],
        "state": row["state"],
        "reason": row["reason"],
        "result": result,
        "result_digest": row["result_digest"],
        "observed_at": row["observed_at"],
        "prior_attempt_id": row["prior_attempt_id"],
        "fresh": _inspection_result_is_fresh(row),
    }


def _recover_effect_commit_from_inspection(
    conn: sqlite3.Connection,
    prior_attempt_id: str,
    result: dict[str, Any],
) -> bool:
    attempt_row = conn.execute(
        "SELECT * FROM factory_attempts WHERE attempt_id=?",
        (prior_attempt_id,),
    ).fetchone()
    if not attempt_row or attempt_row["status"] not in {"effect_outcome_unknown", "cancellation_requested"} or not attempt_row["job_id"]:
        return False
    event = conn.execute(
        "SELECT details_json FROM factory_attempt_events "
        "WHERE attempt_id=? AND event_type='execution_effect_commit_failed' "
        "ORDER BY created_at DESC,event_id DESC LIMIT 1",
        (prior_attempt_id,),
    ).fetchone()
    if not event:
        return False
    try:
        details = _execution_analytics_details(
            "execution_effect_commit_failed", json.loads(event["details_json"])
        )
    except (TypeError, ValueError, json.JSONDecodeError, BridgeValidationError):
        return False
    job = get_job(conn, attempt_row["job_id"])
    if (
        job["state"] != "running"
        or result.get("reason") != "ok"
        or result.get("repository") != job["payload"].get("repo")
        or result.get("url") != details["observed_pr_url"]
        or result.get("head_branch") != details["observed_branch"]
        or not isinstance(result.get("head_sha"), str)
        or (
            details.get("observed_commit_sha") is not None
            and result["head_sha"] != details["observed_commit_sha"]
        )
    ):
        return False
    lease = conn.execute(
        "SELECT * FROM factory_execution_leases "
        "WHERE attempt_id=? AND released_at IS NULL",
        (prior_attempt_id,),
    ).fetchone()
    if not lease or lease["effect_authorized_at"] is None or lease["effect_observed_at"] is None:
        return False
    return _commit_observed_execution_receipt(
        conn,
        job["job_id"],
        lease,
        {**details, "observed_commit_sha": result["head_sha"]},
    )


def _effect_commit_receipt_needs_recovery(
    conn: sqlite3.Connection,
    attempt_id: str,
) -> bool:
    return conn.execute(
        "SELECT 1 FROM factory_attempts a WHERE a.attempt_id=? "
        "AND a.status IN ('effect_outcome_unknown','cancellation_requested') "
        "AND EXISTS (SELECT 1 FROM factory_attempt_events e WHERE e.attempt_id=a.attempt_id "
        "AND e.event_type='execution_effect_commit_failed')",
        (attempt_id,),
    ).fetchone() is not None


def _refresh_effect_commit_inspection(
    conn: sqlite3.Connection,
    prior_attempt_id: str,
    url: str,
) -> bool:
    attempt = get_factory_attempt(conn, prior_attempt_id)
    count = conn.execute(
        "SELECT COUNT(*) FROM factory_inspection_requests "
        "WHERE prior_attempt_id=? AND url=?",
        (prior_attempt_id, url),
    ).fetchone()[0]
    if count >= EFFECT_COMMIT_INSPECTION_MAX_ATTEMPTS:
        if not conn.execute(
            "SELECT 1 FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type='execution_effect_commit_recovery_exhausted'",
            (prior_attempt_id,),
        ).fetchone():
            _append_factory_attempt_event(
                conn,
                prior_attempt_id,
                "execution_effect_commit_recovery_exhausted",
                {
                    "reason_code": "factory_effect_commit_inspection_exhausted",
                    "operator_intervention_required": True,
                    "inspection_attempts": count,
                },
            )
        return False
    return _queue_pr_inspection(
        conn,
        attempt["work_item_key"],
        url,
        prior_attempt_id,
        force_refresh=True,
    )


def sweep_factory_effect_receipt_inspections(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> dict[str, int]:
    """Expire silent receipt inspections and advance their bounded recovery."""
    if type(limit) is not int or limit < 1 or limit > 250:
        raise BridgeValidationError("receipt inspection sweep limit must be between 1 and 250")
    summary = {"expired": 0, "refreshed": 0, "exhausted": 0}
    with conn:
        # Recovery normally has only one pending request per receipt. Selecting
        # the newest pending row per lineage also keeps damaged duplicate rows
        # from exhausting a receipt while a newer request is still live.
        requests = conn.execute(
            """
            SELECT r.*
            FROM factory_inspection_requests r
            JOIN factory_attempts a ON a.attempt_id=r.prior_attempt_id
            WHERE r.prior_attempt_id IS NOT NULL
              AND r.state='pending'
              AND r.result_json IS NULL
              AND a.status IN ('effect_outcome_unknown','cancellation_requested')
              AND EXISTS (
                  SELECT 1 FROM factory_attempt_events e
                  WHERE e.attempt_id=r.prior_attempt_id
                    AND e.event_type='execution_effect_commit_failed'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM factory_inspection_requests newer
                  WHERE newer.prior_attempt_id=r.prior_attempt_id
                    AND newer.url=r.url
                    AND newer.state='pending'
                    AND newer.result_json IS NULL
                    AND (
                        newer.created_at > r.created_at
                        OR (newer.created_at = r.created_at AND newer.request_key > r.request_key)
                    )
              )
            ORDER BY r.created_at,r.request_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for request in requests:
            attempt_id = request["prior_attempt_id"]
            if (
                not _inspection_request_is_expired(request)
                or not _effect_commit_receipt_needs_recovery(conn, attempt_id)
                or not _expire_incomplete_inspection_request(conn, request)
            ):
                continue
            summary["expired"] += 1
            if _refresh_effect_commit_inspection(conn, attempt_id, request["url"]):
                summary["refreshed"] += 1
            elif conn.execute(
                "SELECT 1 FROM factory_attempt_events WHERE attempt_id=? "
                "AND event_type='execution_effect_commit_recovery_exhausted'",
                (attempt_id,),
            ).fetchone():
                summary["exhausted"] += 1
    return summary


def refresh_factory_effect_receipt_inspection(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Let a root operator resume bounded inspection without reauthorizing execution."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("receipt recovery refresh reason is invalid")
    if not isinstance(actor, str) or not actor or len(actor) > 200:
        raise BridgeValidationError("receipt recovery refresh actor is invalid")
    with conn:
        attempt = get_factory_attempt(conn, attempt_id)
        if attempt["status"] not in {"effect_outcome_unknown", "cancellation_requested"}:
            raise BridgeValidationError("attempt has no unresolved observed receipt")
        event = conn.execute(
            "SELECT details_json FROM factory_attempt_events "
            "WHERE attempt_id=? AND event_type='execution_effect_commit_failed' "
            "ORDER BY created_at DESC,event_id DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if not event:
            raise BridgeValidationError("attempt has no observed receipt evidence")
        details = _execution_analytics_details(
            "execution_effect_commit_failed", json.loads(event["details_json"])
        )
        if details.get("observed_commit_sha"):
            raise BridgeValidationError("observed receipt already contains a commit SHA")
        job = get_job(conn, attempt["job_id"])
        repository = job["payload"].get("repo")
        if not isinstance(repository, str):
            raise BridgeValidationError("observed receipt repository is unavailable")
        lease = conn.execute(
            "SELECT * FROM factory_execution_leases "
            "WHERE attempt_id=? AND released_at IS NULL",
            (attempt_id,),
        ).fetchone()
        if (
            job["state"] != "running"
            or not lease
            or lease["effect_authorized_at"] is None
            or lease["effect_observed_at"] is None
        ):
            raise BridgeValidationError("observed receipt recovery fence is unavailable")
        existing = get_pr_inspection(
            conn,
            repository,
            details["observed_pr_url"],
            prior_attempt_id=attempt_id,
        )
        if (
            existing
            and existing.get("fresh") is True
            and isinstance(existing.get("result"), dict)
            and _recover_effect_commit_from_inspection(conn, attempt_id, existing["result"])
        ):
            return {
                "created": False,
                "recovered": True,
                "attempt": get_factory_attempt(conn, attempt_id),
                "inspection": {"request_key": existing["request_key"], "state": existing["state"]},
            }
        created = _queue_pr_inspection(
            conn,
            attempt["work_item_key"],
            details["observed_pr_url"],
            attempt_id,
            force_refresh=True,
        )
        inspection = get_pr_inspection(
            conn,
            repository,
            details["observed_pr_url"],
            prior_attempt_id=attempt_id,
        )
        if created:
            _append_factory_attempt_event(
                conn,
                attempt_id,
                "execution_effect_commit_recovery_refreshed",
                {
                    "reason": sanitize_detail(reason.strip(), 320),
                    "actor": sanitize_detail(actor, 200),
                    "inspection_request_key": inspection["request_key"] if inspection else None,
                },
            )
        return {
            "created": created,
            "recovered": False,
            "attempt": get_factory_attempt(conn, attempt_id),
            "inspection": {
                "request_key": inspection["request_key"],
                "state": inspection["state"],
            } if inspection else None,
        }


def complete_pr_inspection(conn: sqlite3.Connection, job_id: str, result: dict[str, Any], *, request_key: str | None = None, capability: str | None = None) -> dict[str, Any]:
    allowed = {"repository", "number", "url", "state", "merged", "draft", "head_branch", "head_sha", "base_ref", "reason"}
    if not isinstance(result, dict) or set(result) != allowed or result.get("state") not in {"open", "closed", "unknown"} or not isinstance(result.get("merged"), bool) or not isinstance(result.get("draft"), bool) or not isinstance(result.get("reason"), str) or (result.get("head_branch") is not None and not isinstance(result.get("head_branch"), str)) or (result.get("head_sha") is not None and not re.fullmatch(r"[0-9a-f]{7,64}", result.get("head_sha"))) or (result.get("base_ref") is not None and not isinstance(result.get("base_ref"), str)):
        raise BridgeValidationError("inspection result is invalid")
    repository, number, url = _parse_pr_reference(result["repository"], result["url"])
    if result["number"] != number:
        raise BridgeValidationError("inspection result does not match request")
    stored_result = sanitize_detail(result)
    capability_expired = False
    prior_attempt_id = None
    with conn:
        request = conn.execute("SELECT * FROM factory_inspection_requests WHERE job_id=?", (job_id,)).fetchone()
        if not request or request_key != request["request_key"] or capability != request["capability"] or (request["repository"], request["number"], request["url"]) != (repository, number, url):
            raise BridgeValidationError("inspection result does not match a pending request")
        prior_attempt_id = request["prior_attempt_id"]
        # A lost response may be redelivered after the one-time capability has
        # expired. It is safe only when the stored canonical result is exactly
        # identical; an expired capability can never create or alter evidence.
        if request["result_json"]:
            if canonical_json(json.loads(request["result_json"])) != canonical_json(stored_result):
                raise BridgeConflict("inspection result is immutable")
            return get_pr_inspection(conn, repository, url, prior_attempt_id=prior_attempt_id) or {}
        try:
            capability_age = _inspection_age_seconds(request["created_at"], label="inspection capability")
        except BridgeValidationError as exc:
            raise BridgeValidationError("inspection capability timestamp is invalid") from exc
        if capability_age < 0 or capability_age > INSPECTION_CAPABILITY_TTL_SECONDS:
            _expire_incomplete_inspection_request(conn, request)
            capability_expired = True
            if prior_attempt_id and _effect_commit_receipt_needs_recovery(conn, prior_attempt_id):
                _refresh_effect_commit_inspection(conn, prior_attempt_id, url)
        else:
            job = get_job(conn, job_id)
            if job["requested_action"] != "inspect_draft_pr_state" or job["payload"].get("inspection_request_key") != request_key or job["payload"].get("inspection_capability") != capability or job["state"] not in {"accepted", "running", "completed"}:
                raise BridgeValidationError("inspection completion requires a running inspection job")
            digest = hashlib.sha256(canonical_json(stored_result).encode()).hexdigest(); observed = utc_now()
            if job["state"] != "completed":
                conn.execute("UPDATE bridge_jobs SET state='completed',reason_code=?,updated_at=? WHERE job_id=?", (sanitize_detail(stored_result["reason"], 120), observed, job_id))
                _append_event(conn, job_id=job_id, event_type="inspection_completed", state="completed", details={"reason": stored_result["reason"]})
            conn.execute("UPDATE factory_inspection_requests SET state='completed',result_json=?,result_digest=?,observed_at=?,reason=?,updated_at=? WHERE request_key=?", (canonical_json(stored_result), digest, observed, sanitize_detail(stored_result["reason"], 120), observed, request["request_key"]))
            if prior_attempt_id and not _recover_effect_commit_from_inspection(conn, prior_attempt_id, stored_result):
                if _effect_commit_receipt_needs_recovery(conn, prior_attempt_id):
                    _refresh_effect_commit_inspection(conn, prior_attempt_id, url)
    if capability_expired:
        raise BridgeValidationError("inspection capability is expired")
    return get_pr_inspection(conn, repository, url, prior_attempt_id=prior_attempt_id) or {}


record_pr_inspection = complete_pr_inspection


def _issue_control_held(conn: sqlite3.Connection, work_item_key: str | None) -> bool:
    # Fail closed for legacy aliases too; new reservations require canonical keys.
    key = work_item_key.strip().upper() if isinstance(work_item_key, str) else None
    row = conn.execute("SELECT held FROM factory_issue_controls WHERE work_item_key=?", (key,)).fetchone()
    return bool(row and row["held"])


def _require_issue_factory_control(conn: sqlite3.Connection, work_item_key: str | None) -> None:
    if _issue_control_held(conn, work_item_key):
        raise BridgeConflict("factory issue is under manual control")


def _job_work_item(job: dict[str, Any]) -> str | None:
    return job["payload"].get("work_item_id") or job["payload"].get("linear_issue")


def _issue_control_attempts(conn: sqlite3.Connection, work_item_key: str) -> list[dict[str, Any]]:
    # LIKE narrows legacy candidates; Python strip matches the hold lookup for
    # every whitespace alias, including ones SQLite's default TRIM misses.
    return [_row_to_factory_attempt(row) for row in conn.execute(
        "SELECT * FROM factory_attempts WHERE UPPER(work_item_key) LIKE ? ORDER BY generation DESC,attempt_id", (f"%{work_item_key}%",),
    ) if row["work_item_key"].strip().upper() == work_item_key]


def get_factory_issue_control(conn: sqlite3.Connection, work_item_key: str) -> dict[str, Any]:
    """Read ownership separately from workflow status; lease expiry is not settlement."""
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        return _factory_issue_control_projection(conn, work_item_key)


def _factory_issue_control_projection(conn: sqlite3.Connection, work_item_key: str) -> dict[str, Any]:
    if not isinstance(work_item_key, str) or not LINEAR_KEY_RE.fullmatch(work_item_key):
        raise BridgeValidationError("issue control requires a canonical Linear issue key")
    row = conn.execute("SELECT * FROM factory_issue_controls WHERE work_item_key=?", (work_item_key,)).fetchone()
    attempts = _issue_control_attempts(conn, work_item_key)
    evidence, blockers = [], []
    for attempt in attempts:
        lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        job = get_job(conn, attempt["job_id"]) if attempt.get("job_id") else None
        # Heartbeats alone do not invalidate a preview, but grants, cancellation,
        # receipts and ownership settlement must. Never derive safety from expiry.
        lease_evidence = {key: lease[key] for key in ("lease_id", "epoch", "effect_authorized_at", "released_at", "cancel_requested_at", "effect_observed_at")} if lease else None
        evidence.append({"attempt_id": attempt["attempt_id"], "status": attempt["status"], "generation": attempt["generation"], "lease": lease_evidence,
                         "job": {key: job[key] for key in ("job_id", "state", "branch", "commit_sha", "pr_url", "updated_at")} if job else None})
        if lease and lease["released_at"] is None:
            blocker = {"attempt_id": attempt["attempt_id"], "kind": "effect_in_flight" if lease["effect_authorized_at"] else "executor_unsettled"}
            observation = conn.execute(
                "SELECT details_json,created_at FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_receipt_observed' ORDER BY created_at DESC,event_id DESC LIMIT 1",
                (attempt["attempt_id"],),
            ).fetchone()
            if observation:
                detail = json.loads(observation["details_json"])
                blocker["recovery"] = {"state": detail.get("state"), "observed_at": observation["created_at"]}
            blockers.append(blocker)
        elif attempt["status"] in {"running", "effect_authorized", "effect_outcome_unknown", "cancellation_requested"}:
            blockers.append({"attempt_id": attempt["attempt_id"], "kind": "execution_outcome_unsettled"})
    untracked = conn.execute(
        "SELECT j.job_id,j.state,j.updated_at,json_extract(j.payload_json,'$.work_item_id') AS work_item_id,json_extract(j.payload_json,'$.linear_issue') AS linear_issue FROM bridge_jobs j LEFT JOIN factory_attempts a ON a.job_id=j.job_id "
        "WHERE a.attempt_id IS NULL AND j.requested_action IN ('create_draft_pr','repair_draft_pr') "
        "AND j.state IN ('accepted','running','effect_committed','verification_pending') "
        "AND (UPPER(json_extract(j.payload_json,'$.work_item_id')) LIKE ? OR UPPER(json_extract(j.payload_json,'$.linear_issue')) LIKE ?) "
        "AND COALESCE(json_extract(j.payload_json,'$.dry_run'),0)!=1 ORDER BY j.job_id", (f"%{work_item_key}%", f"%{work_item_key}%"),
    ).fetchall()
    untracked = [item for item in untracked if any(isinstance(item[key], str) and item[key].strip().upper() == work_item_key for key in ("work_item_id", "linear_issue"))]
    blockers.extend({"job_id": item["job_id"], "kind": "untracked_active_job"} for item in untracked)
    held = bool(row and row["held"])
    revision = row["revision"] if row else 0
    history = [dict(item) for item in conn.execute(
        "SELECT request_id,action,actor,reason,revision,attempt_id,created_at FROM factory_issue_control_commands "
        "WHERE work_item_key=? ORDER BY revision DESC LIMIT 100", (work_item_key,),
    )]
    return {
        "work_item_key": work_item_key, "held": held, "revision": revision,
        "mode": ("handoff_pending" if blockers else "manual") if held else "factory",
        "snapshot_digest": payload_hash({"control": dict(row) if row else None, "attempts": evidence, "untracked": [dict(item) for item in untracked]}),
        "actor": row["actor"] if row else None, "reason": row["reason"] if row else None,
        "updated_at": row["updated_at"] if row else None, "blockers": blockers,
        "automatic_intake_blocked": row is not None, "history": history,
    }


def _issue_control_command(
    conn: sqlite3.Connection, *, work_item_key: str, request_id: str, actor: str, reason: str,
    action: str, expected_revision: int, expected_snapshot_digest: str, extra: dict[str, Any] | None = None,
    command_context_digest: str | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    if not all(isinstance(value, str) and value.strip() and len(value) <= 200 for value in (request_id, actor)):
        raise BridgeValidationError("issue control command requires request_id and authenticated actor")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("issue control command requires a bounded reason")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0 or not isinstance(expected_snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_digest):
        raise BridgeValidationError("issue control command requires its preview revision and digest")
    if command_context_digest is not None and (not isinstance(command_context_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", command_context_digest)):
        raise BridgeValidationError("issue control command context digest is invalid")
    intent = {"work_item_key": work_item_key, "actor": actor, "reason": reason, "action": action,
              "expected_revision": expected_revision, "expected_snapshot_digest": expected_snapshot_digest, "extra": extra,
              "command_context_digest": command_context_digest}
    digest = payload_hash(intent)
    existing = conn.execute("SELECT * FROM factory_issue_control_commands WHERE request_id=?", (request_id,)).fetchone()
    if existing:
        if existing["intent_digest"] != digest:
            raise BridgeConflict("issue control request_id conflicts with existing intent")
        return get_factory_issue_control(conn, work_item_key), digest, dict(existing)
    control = get_factory_issue_control(conn, work_item_key)
    if control["revision"] != expected_revision or control["snapshot_digest"] != expected_snapshot_digest:
        raise BridgeConflict("issue control preview is stale; refresh before confirming")
    return control, digest, None


def _record_issue_control_command(
    conn: sqlite3.Connection, *, work_item_key: str, request_id: str, actor: str, reason: str,
    action: str, revision: int, digest: str, expected_revision: int,
    expected_snapshot_digest: str, attempt_id: str | None = None, command_context_digest: str | None = None,
) -> None:
    command_digest = payload_hash({"work_item_key": work_item_key, "actor": actor, "reason": reason, "action": action,
                                   "expected_revision": expected_revision, "expected_snapshot_digest": expected_snapshot_digest,
                                   "command_context_digest": command_context_digest})
    conn.execute(
        "INSERT INTO factory_issue_control_commands(request_id,work_item_key,action,intent_digest,command_digest,actor,reason,revision,attempt_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_id, work_item_key, action, digest, command_digest, sanitize_detail(actor, 200), sanitize_detail(reason.strip(), 320), revision, attempt_id, utc_now()),
    )


def get_factory_issue_control_command(
    conn: sqlite3.Connection, *, work_item_key: str, request_id: str, actor: str, reason: str,
    action: str, expected_revision: int, expected_snapshot_digest: str,
    command_context_digest: str | None = None,
) -> dict[str, Any] | None:
    """Recover a committed response before repeating external preview freshness checks."""
    row = conn.execute("SELECT * FROM factory_issue_control_commands WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        return None
    digest = payload_hash({"work_item_key": work_item_key, "actor": actor, "reason": reason, "action": action,
                           "expected_revision": expected_revision, "expected_snapshot_digest": expected_snapshot_digest,
                           "command_context_digest": command_context_digest})
    if row["command_digest"] != digest:
        raise BridgeConflict("issue control request_id conflicts with existing intent")
    control = get_factory_issue_control(conn, work_item_key)
    if row["attempt_id"]:
        attempt = get_factory_attempt(conn, row["attempt_id"])
        return {"control": control, "attempt": attempt, "job": get_job(conn, attempt["job_id"]), "created": False}
    return {"control": control, "created": False}


def take_factory_issue_control(
    conn: sqlite3.Connection, *, work_item_key: str, request_id: str, actor: str, reason: str,
    expected_revision: int, expected_snapshot_digest: str,
    command_context_digest: str | None = None,
) -> dict[str, Any]:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        control, digest, existing = _issue_control_command(
            conn, work_item_key=work_item_key, request_id=request_id, actor=actor, reason=reason,
            action="take_control", expected_revision=expected_revision, expected_snapshot_digest=expected_snapshot_digest,
            command_context_digest=command_context_digest,
        )
        if existing:
            return control
        if control["held"]:
            raise BridgeConflict("factory issue is already under manual control")
        now, revision = utc_now(), control["revision"] + 1
        conn.execute(
            "INSERT INTO factory_issue_controls(work_item_key,held,revision,actor,reason,updated_at) VALUES (?,1,?,?,?,?) "
            "ON CONFLICT(work_item_key) DO UPDATE SET held=1,revision=excluded.revision,actor=excluded.actor,reason=excluded.reason,updated_at=excluded.updated_at",
            (work_item_key, revision, sanitize_detail(actor, 200), sanitize_detail(reason.strip(), 320), now),
        )
        for attempt in _issue_control_attempts(conn, work_item_key):
            conn.execute("UPDATE factory_execution_leases SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE attempt_id=? AND released_at IS NULL", (now, attempt["attempt_id"]))
            cancel_factory_attempt(conn, attempt["attempt_id"], reason=reason, actor=actor)
            if attempt.get("job_id"):
                _close_verification_lease(conn, attempt["job_id"], "manual_takeover")
            conn.execute(
                "UPDATE factory_attempt_outbox SET state='suppressed',acknowledged_at=?,delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE attempt_id=? AND acknowledged_at IS NULL",
                (now, now, attempt["attempt_id"]),
            )
            if attempt.get("job_id"):
                _enqueue_execution_receipt_recovery(conn, get_factory_attempt(conn, attempt["attempt_id"]), get_job(conn, attempt["job_id"]))
        _record_issue_control_command(conn, work_item_key=work_item_key, request_id=request_id, actor=actor, reason=reason,
                                      action="take_control", revision=revision, digest=digest,
                                      expected_revision=expected_revision, expected_snapshot_digest=expected_snapshot_digest,
                                      command_context_digest=command_context_digest)
        return get_factory_issue_control(conn, work_item_key)


def return_factory_issue_control(
    conn: sqlite3.Connection, *, work_item_key: str, request_id: str, actor: str, reason: str,
    expected_revision: int, expected_snapshot_digest: str, payload: dict[str, Any], mode: str = "real",
    prior_attempt_id: str | None = None, supersede_pr_url: str | None = None,
    command_context_digest: str | None = None,
) -> dict[str, Any]:
    """Return by admitting one reviewed fresh attempt, never by replaying old events.

    The operator boundary must validate current Linear and GitHub artifacts using
    the same trusted preview and effect fence as fresh retry. Open-PR continuation
    and verification of manually changed heads need a separate adoption contract.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        control, digest, existing = _issue_control_command(
            conn, work_item_key=work_item_key, request_id=request_id, actor=actor, reason=reason,
            action="return_control", expected_revision=expected_revision, expected_snapshot_digest=expected_snapshot_digest,
            extra={"payload": payload, "mode": mode, "prior_attempt_id": prior_attempt_id, "supersede_pr_url": supersede_pr_url},
            command_context_digest=command_context_digest,
        )
        if existing:
            attempt = get_factory_attempt(conn, existing["attempt_id"])
            return {"control": control, "attempt": attempt, "job": get_job(conn, attempt["job_id"]), "created": False}
        if control["mode"] != "manual":
            raise BridgeConflict("return requires a settled manual handoff")
        if not isinstance(payload, dict) or not isinstance(payload.get("trusted_snapshot_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", payload["trusted_snapshot_digest"]):
            raise BridgeValidationError("return requires a trusted fresh-attempt snapshot")
        if prior_attempt_id:
            prior = get_factory_attempt(conn, prior_attempt_id)
            if prior["work_item_key"] != work_item_key:
                raise BridgeValidationError("return prior attempt belongs to another issue")
        now, revision = utc_now(), control["revision"] + 1
        conn.execute("UPDATE factory_issue_controls SET held=0,revision=?,actor=?,reason=?,updated_at=? WHERE work_item_key=?",
                     (revision, sanitize_detail(actor, 200), sanitize_detail(reason.strip(), 320), now, work_item_key))
        attempt, job, created = reserve_factory_retry(
            conn, work_item_key=work_item_key, request_id=f"issue-control:{request_id}", actor=actor, reason=reason,
            payload=payload, mode=mode, prior_attempt_id=prior_attempt_id, supersede_pr_url=supersede_pr_url,
        )
        if not created:
            raise BridgeConflict("return attempt request already exists outside this command")
        _record_issue_control_command(conn, work_item_key=work_item_key, request_id=request_id, actor=actor, reason=reason,
                                      action="return_control", revision=revision, digest=digest, attempt_id=attempt["attempt_id"],
                                      expected_revision=expected_revision, expected_snapshot_digest=expected_snapshot_digest,
                                      command_context_digest=command_context_digest)
        return {"control": get_factory_issue_control(conn, work_item_key), "attempt": attempt, "job": job, "created": True}


def _reserve_factory_attempt_locked(
    conn: sqlite3.Connection, *, work_item_key: str, mode: str, request_id: str,
    reason: str, actor: str, supersede_pr_url: str | None = None,
    artifact_fence: dict[str, Any] | None = None,
    trusted_snapshot_digest: str | None = None,
    reserved_event_type: str = "retry_reserved",
) -> tuple[dict[str, Any], bool]:
    if not all(isinstance(value, str) and value and len(value) <= 200 for value in (work_item_key, request_id, actor)) or not LINEAR_KEY_RE.fullmatch(work_item_key) or mode not in {"real", "dry-run"}:
        raise BridgeValidationError("factory retry request is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("factory retry reason is invalid")
    _require_issue_factory_control(conn, work_item_key)
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
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
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
        _record_factory_admission_evidence(conn, attempt["attempt_id"])
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
        _require_issue_factory_control(conn, work_item_key)
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
        _record_factory_admission_evidence(conn, attempt["attempt_id"])
        outbox_key = f"factory:accepted:{attempt['attempt_id']}"
        conn.execute("INSERT INTO factory_attempt_outbox (outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]}), "pending", utc_now(), utc_now()))
        _append_factory_attempt_event(conn, attempt["attempt_id"], "accepted_event_enqueued", {"outbox_key": outbox_key})
    return get_factory_attempt(conn, attempt["attempt_id"]), job, True


def _factory_auto_repair_max_generations() -> int:
    try:
        value = int(os.environ.get("HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS", "1"))
    except ValueError:
        value = 1
    return max(1, min(value, 10))


def _reserve_factory_verification_repair(
    conn: sqlite3.Connection,
    *,
    failed_job: dict[str, Any],
    factory_attempt: dict[str, Any],
    details: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    if _issue_control_held(conn, factory_attempt["work_item_key"]) or factory_attempt["status"] in {"canceled", "canceled_after_effect", "cancellation_requested", "superseded"}:
        return None
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
    _record_factory_admission_evidence(conn, attempt["attempt_id"])
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
    if not all(isinstance(value, str) and value and len(value) <= 200 for value in (work_item_key, request_id, actor)) or not LINEAR_KEY_RE.fullmatch(work_item_key) or mode not in {"real", "dry-run"}:
        raise BridgeValidationError("factory intake request is invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320:
        raise BridgeValidationError("factory intake reason is invalid")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # Ingress events have no control revision. Once explicitly controlled,
        # only a reviewed handback may admit work; a delayed pre-hold webhook
        # must never regain authority just because held becomes false.
        if conn.execute("SELECT 1 FROM factory_issue_controls WHERE work_item_key=?", (work_item_key,)).fetchone():
            raise BridgeConflict("controlled issue requires explicit Factory handback, not automatic intake")
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
            _append_factory_attempt_event_once(
                conn,
                attempt["attempt_id"],
                "intake_request_deduplicated",
                request_id,
            )
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
            _append_factory_attempt_event_once(
                conn,
                attempt["attempt_id"],
                "intake_active_job_reused",
                request_id,
            )
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
        _record_factory_admission_evidence(conn, attempt["attempt_id"])
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
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
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
        if _issue_control_held(conn, attempt["work_item_key"] if attempt else _job_work_item(get_job(conn, job_id))):
            return {"claimed": False, "reason": "factory_issue_manual_control", "attempt": attempt}
        if not attempt:
            job = get_job(conn, job_id)
            if job["requested_action"] == "create_draft_pr" and job["payload"].get("dry_run") is not True:
                return {"claimed": False, "reason": "factory_attempt_required", "attempt": None}
            return {"claimed": True, "attempt": None, "lease": None}
        if attempt["status"] in {"canceled", "canceled_after_effect", "superseded", "failed", "verified_ready", "completed", "effect_outcome_unknown", "effect_committed", "verification_pending"}:
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
        if attempt["status"] == "cancellation_requested":
            return {"claimed": False, "reason": "factory_cancellation_pending", "attempt": attempt}
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
    return not _issue_control_held(conn, _job_work_item(get_job(conn, job_id))) and _current_execution_lease(conn, job_id, claim) is not None


def _execution_route_details(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict) or set(details) != EXECUTION_ROUTE_FIELDS:
        raise BridgeValidationError("factory execution route is invalid")
    if details.get("analytics_schema_version") != 1:
        raise BridgeValidationError("factory execution route schema is invalid")
    for field in (
        "assignment_id", "experiment_id", "variant", "intended_backend",
        "intended_transport", "configured_provider", "configured_model",
    ):
        if (
            not isinstance(details.get(field), str)
            or not ANALYTICS_DIMENSION_RE.fullmatch(details[field])
            or _secret_shaped(details[field])
        ):
            raise BridgeValidationError(f"factory execution route {field} is invalid")
    if details.get("assignment_method") not in {"static", "deterministic_hash", "operator_override"}:
        raise BridgeValidationError("factory execution route assignment_method is invalid")
    if details.get("intended_transport") not in {"grpc_uds", "grpc_tcp", "local_command"}:
        raise BridgeValidationError("factory execution route intended_transport is invalid")
    if details.get("configured_auth_mode") not in {"chatgpt_codex_oauth", "api_key", "unknown"}:
        raise BridgeValidationError("factory execution route configured_auth_mode is invalid")
    for field in ("backend_version", "factory_release_version"):
        value = details.get(field)
        if value is not None and (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,119}", value)
            or _secret_shaped(value)
        ):
            raise BridgeValidationError(f"factory execution route {field} is invalid")
    release_sha = details.get("factory_release_sha")
    if release_sha is not None and (not isinstance(release_sha, str) or not RELEASE_SHA_RE.fullmatch(release_sha)):
        raise BridgeValidationError("factory execution route factory_release_sha is invalid")
    registry_version = details.get("routing_registry_version")
    if registry_version is not None and (
        not isinstance(registry_version, int)
        or isinstance(registry_version, bool)
        or not 1 <= registry_version <= 1_000_000
    ):
        raise BridgeValidationError("factory execution route routing_registry_version is invalid")
    policy_digest = details.get("verifier_policy_digest")
    if policy_digest is not None and (
        not isinstance(policy_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", policy_digest)
    ):
        raise BridgeValidationError("factory execution route verifier_policy_digest is invalid")
    if details.get("fallback_allowed") is not False:
        raise BridgeValidationError("factory execution route fallback is not supported")
    sanitized = sanitize_detail(details)
    if sanitized != details:
        raise BridgeValidationError("factory execution route contains disallowed material")
    return dict(details)


def record_factory_execution_route(
    conn: sqlite3.Connection,
    job_id: str,
    details: dict[str, Any],
) -> bool:
    """Record the immutable intent-to-treat route before executor preflight."""
    validated = _execution_route_details(details)
    serialized = canonical_json(validated)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_factory_attempt_for_job(conn, job_id)
        if not attempt:
            raise BridgeConflict("factory execution route requires an attempt")
        job = get_job(conn, job_id)
        if attempt["mode"] != "real" or job["requested_action"] not in DRAFT_PR_EFFECT_ACTIONS:
            raise BridgeValidationError("factory execution route requires a real draft PR attempt")
        if validated["assignment_id"] != f"route.{attempt['attempt_id']}":
            raise BridgeValidationError("factory execution route assignment does not match the attempt")
        rows = conn.execute(
            "SELECT details_json FROM factory_attempt_events "
            "WHERE attempt_id=? AND event_type='execution_route_selected'",
            (attempt["attempt_id"],),
        ).fetchall()
        for row in rows:
            if canonical_json(json.loads(row["details_json"])) == serialized:
                return False
            raise BridgeConflict("factory execution route is immutable")
        if attempt["status"] != "accepted" or job["state"] != "accepted":
            raise BridgeConflict("factory execution route must precede execution")
        _append_factory_attempt_event(conn, attempt["attempt_id"], "execution_route_selected", validated)
        admission = conn.execute("SELECT manifest_id,document_json FROM factory_execution_manifests WHERE attempt_id=? AND phase='admission'", (attempt["attempt_id"],)).fetchone()
        if admission:
            _insert_work_evidence(
                conn, attempt_id=attempt["attempt_id"], kind="execution", manifest_id=admission["manifest_id"],
                document=work_evidence.execution_manifest(json.loads(admission["document_json"]), validated),
            )
        return True


def _validate_execution_failure_boundary_fields(details: Mapping[str, Any]) -> None:
    transport_status = details.get("transport_status")
    if "transport_status" in details and transport_status is not None and (
        not isinstance(transport_status, str)
        or transport_status not in EXECUTION_TRANSPORT_STATUSES
    ):
        raise BridgeValidationError("factory failure classification transport_status is invalid")
    exit_code = details.get("process_exit_code")
    if "process_exit_code" in details and exit_code is not None and (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -(2**31) <= exit_code < 2**31
    ):
        raise BridgeValidationError("factory failure classification process_exit_code is invalid")
    process_signal = details.get("process_signal")
    if "process_signal" in details and process_signal is not None and (
        not isinstance(process_signal, str)
        or process_signal not in EXECUTION_PROCESS_SIGNALS
    ):
        raise BridgeValidationError("factory failure classification process_signal is invalid")
    for field in ("timed_out", "buffer_exceeded"):
        if field in details and details.get(field) is not None and not isinstance(details.get(field), bool):
            raise BridgeValidationError(f"factory failure classification {field} is invalid")


def _execution_analytics_details(event_type: str, details: Any) -> dict[str, Any]:
    common = set(EXECUTION_ANALYTICS_COMMON_FIELDS)
    failure_fields = {
        "failure_domain", "failure_stage", "reason_code", "retryable",
        "failure_disposition", "operator_intervention_required",
        "effect_outcome", "elapsed_ms",
    }
    receipt_fields = {
        "observed_pr_url", "observed_commit_sha", "observed_branch",
        "observed_hermes_session_id", "backend_outcome", "verification_context",
    }
    additions = {
        "execution_backend_invoked": set(),
        "execution_model_selected": {"model_source", "selected_model", "selected_provider"},
        "execution_backend_completed": {"backend_outcome", "elapsed_ms"},
        "execution_backend_failed": failure_fields,
        "execution_dispatch_failed": failure_fields,
        "execution_effect_commit_failed": failure_fields | receipt_fields,
    }
    if event_type not in additions or not isinstance(details, dict):
        raise BridgeValidationError("factory execution analytics event is invalid")
    required = common | additions[event_type]
    allowed = required | (EXECUTION_FAILURE_OPTIONAL_FIELDS if event_type in EXECUTION_FAILURE_EVENT_TYPES else set())
    if not required <= set(details) or not set(details) <= allowed:
        raise BridgeValidationError("factory execution analytics event is invalid")
    if details.get("analytics_schema_version") != 1:
        raise BridgeValidationError("factory execution analytics schema is invalid")
    invocation_id = details.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id or len(invocation_id) > 200:
        raise BridgeValidationError("factory execution analytics invocation_id is invalid")
    for field in ("execution_backend", "execution_transport"):
        if (
            not isinstance(details.get(field), str)
            or not ANALYTICS_DIMENSION_RE.fullmatch(details[field])
            or _secret_shaped(details[field])
        ):
            raise BridgeValidationError(f"factory execution analytics {field} is invalid")
    backend_version = details.get("backend_version")
    if backend_version is not None and (
        not isinstance(backend_version, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,119}", backend_version)
        or _secret_shaped(backend_version)
    ):
        raise BridgeValidationError("factory execution analytics backend_version is invalid")
    if details.get("configured_auth_mode") not in {"chatgpt_codex_oauth", "api_key", "unknown"}:
        raise BridgeValidationError("factory execution analytics configured_auth_mode is invalid")
    if event_type == "execution_model_selected":
        if details.get("model_source") != "executor_config":
            raise BridgeValidationError("factory model selection source is invalid")
        for field in ("selected_model", "selected_provider"):
            value = details.get(field)
            if (not isinstance(value, str) or len(value) > 120
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:/-]*", value)
                    or "://" in value or _secret_shaped(value) or value in {"auto", "unknown"}):
                raise BridgeValidationError(f"factory model selection {field} is invalid")
    if event_type == "execution_backend_completed" and details.get("backend_outcome") not in {"created", "reused", "unknown"}:
        raise BridgeValidationError("factory execution analytics backend_outcome is invalid")
    if event_type in {"execution_backend_failed", "execution_dispatch_failed", "execution_effect_commit_failed"}:
        if details.get("failure_domain") not in EXECUTION_FAILURE_DOMAINS:
            raise BridgeValidationError("factory execution analytics failure_domain is invalid")
        if details.get("failure_stage") not in EXECUTION_FAILURE_STAGES:
            raise BridgeValidationError("factory execution analytics failure_stage is invalid")
        if (
            not isinstance(details.get("reason_code"), str)
            or not ANALYTICS_DIMENSION_RE.fullmatch(details["reason_code"])
            or _secret_shaped(details["reason_code"])
        ):
            raise BridgeValidationError("factory execution analytics reason_code is invalid")
        for field in ("retryable", "operator_intervention_required"):
            if details.get(field) is not None and not isinstance(details.get(field), bool):
                raise BridgeValidationError(f"factory execution analytics {field} is invalid")
        if details.get("failure_disposition") not in {"retry", "terminal", "recovery_required"}:
            raise BridgeValidationError("factory execution analytics failure_disposition is invalid")
        if details.get("effect_outcome") not in {"none", "observed", "unknown"}:
            raise BridgeValidationError("factory execution analytics effect_outcome is invalid")
        _validate_execution_failure_boundary_fields(details)
    if event_type == "execution_effect_commit_failed":
        if details.get("failure_domain") != "factory" or details.get("failure_stage") != "effect_commit" or details.get("effect_outcome") != "observed":
            raise BridgeValidationError("factory execution effect commit classification is invalid")
        if not isinstance(details.get("observed_pr_url"), str) or not PR_URL_RE.fullmatch(details["observed_pr_url"]):
            raise BridgeValidationError("factory execution observed_pr_url is invalid")
        observed_sha = details.get("observed_commit_sha")
        if observed_sha is not None and (
            not isinstance(observed_sha, str)
            or not re.fullmatch(r"[0-9a-f]{7,64}", observed_sha)
        ):
            # The concrete PR URL and branch are still useful recovery
            # evidence when an untrusted backend supplies a malformed SHA.
            # Drop the raw value instead of rejecting the whole settlement;
            # the receipt remains effect-observed and exact PR inspection can
            # recover the canonical head without persisting attacker-controlled
            # text or authorizing another backend invocation.
            details = {**details, "observed_commit_sha": None}
        observed_branch = details.get("observed_branch")
        if not isinstance(observed_branch, str) or not observed_branch or len(observed_branch) > 255:
            raise BridgeValidationError("factory execution observed_branch is invalid")
        if details.get("backend_outcome") not in {"created", "reused"}:
            raise BridgeValidationError("factory execution observed backend_outcome is invalid")
        session_id = details.get("observed_hermes_session_id")
        if session_id is not None:
            raise BridgeValidationError("factory execution observed Hermes session is invalid")
        context = details.get("verification_context")
        if context is not None:
            # Verification metadata is secondary to the concrete effect
            # receipt. Discard malformed context so it cannot roll back the
            # observed marker or cause another backend invocation.
            try:
                details = {**details, "verification_context": _validate_verification_context(context)}
            except BridgeValidationError:
                details = {**details, "verification_context": None}
    if event_type not in {"execution_backend_invoked", "execution_model_selected"}:
        elapsed = details.get("elapsed_ms")
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or not 0 <= elapsed <= 604_800_000:
            raise BridgeValidationError("factory execution analytics elapsed_ms is invalid")
    return sanitize_detail(details)


def _validate_failure_classification(details: dict[str, Any]) -> None:
    fields = {
        "failure_domain", "failure_stage", "reason_code", "retryable",
        "failure_disposition", "operator_intervention_required",
    }
    if not ((fields - {"reason_code"}) | EXECUTION_FAILURE_OPTIONAL_FIELDS) & set(details):
        return
    if not fields <= set(details):
        raise BridgeValidationError("factory failure classification is incomplete")
    if details.get("failure_domain") not in EXECUTION_FAILURE_DOMAINS:
        raise BridgeValidationError("factory failure classification failure_domain is invalid")
    if details.get("failure_stage") not in EXECUTION_FAILURE_STAGES:
        raise BridgeValidationError("factory failure classification failure_stage is invalid")
    if (
        not isinstance(details.get("reason_code"), str)
        or not ANALYTICS_DIMENSION_RE.fullmatch(details["reason_code"])
        or _secret_shaped(details["reason_code"])
    ):
        raise BridgeValidationError("factory failure classification reason_code is invalid")
    for field in ("retryable", "operator_intervention_required"):
        if details.get(field) is not None and not isinstance(details.get(field), bool):
            raise BridgeValidationError(f"factory failure classification {field} is invalid")
    if details.get("failure_disposition") not in {"retry", "terminal", "recovery_required"}:
        raise BridgeValidationError("factory failure classification disposition is invalid")
    _validate_execution_failure_boundary_fields(details)


def _append_factory_execution_analytics_event(
    conn: sqlite3.Connection,
    attempt_id: str,
    event_type: str,
    details: dict[str, Any],
) -> bool:
    """Append once per invocation and phase without requiring a schema change."""
    serialized = canonical_json(details)
    invocation_registered = False
    duplicate = False
    for row in conn.execute(
        "SELECT event_type,details_json FROM factory_attempt_events WHERE attempt_id=? AND event_type IN ('execution_backend_invoked','execution_model_selected','execution_backend_completed','execution_backend_failed','execution_dispatch_failed','execution_effect_commit_failed')",
        (attempt_id,),
    ):
        existing = json.loads(row["details_json"])
        if existing.get("invocation_id") != details["invocation_id"]:
            if event_type == "execution_backend_invoked" and row["event_type"] == "execution_backend_invoked":
                raise BridgeConflict("factory execution backend invocation is already registered")
            continue
        if any(existing.get(field) != details.get(field) for field in EXECUTION_ANALYTICS_COMMON_FIELDS):
            raise BridgeConflict("factory execution analytics identity is immutable")
        if row["event_type"] == "execution_backend_invoked":
            invocation_registered = True
        if row["event_type"] == event_type and canonical_json(existing) == serialized:
            duplicate = True
            continue
        if row["event_type"] == event_type:
            raise BridgeConflict("factory execution analytics event is immutable")
        backend_terminal = {"execution_backend_completed", "execution_backend_failed"}
        if event_type in backend_terminal and row["event_type"] in backend_terminal:
            raise BridgeConflict("factory execution analytics outcome is immutable")
        if event_type == "execution_backend_invoked" and row["event_type"] in backend_terminal:
            raise BridgeConflict("factory execution analytics invocation is already terminal")
    if event_type in {
        "execution_model_selected",
        "execution_backend_completed",
        "execution_backend_failed",
        "execution_effect_commit_failed",
    } and not invocation_registered:
        raise BridgeConflict("factory execution backend invocation is not registered")
    if duplicate:
        return False
    _append_factory_attempt_event(conn, attempt_id, event_type, details)
    return True


def _validate_execution_route_identity(
    conn: sqlite3.Connection,
    attempt_id: str,
    details: Mapping[str, Any],
) -> None:
    row = conn.execute(
        "SELECT details_json FROM factory_attempt_events "
        "WHERE attempt_id=? AND event_type='execution_route_selected' "
        "ORDER BY created_at,event_id LIMIT 1",
        (attempt_id,),
    ).fetchone()
    if not row:
        # Historical or already-running attempts may predate route telemetry.
        # Coverage remains explicit in analytics; do not invent an assignment.
        return
    route = json.loads(row["details_json"])
    expected = {
        "execution_backend": route.get("intended_backend"),
        "execution_transport": route.get("intended_transport"),
        "configured_auth_mode": route.get("configured_auth_mode"),
        "backend_version": route.get("backend_version"),
    }
    if any(details.get(field) != value for field, value in expected.items()):
        raise BridgeConflict("factory execution identity does not match its selected route")
    if route.get("fallback_allowed") is not False:
        raise BridgeConflict("factory execution route fallback is not supported")


def record_factory_execution_analytics(
    conn: sqlite3.Connection,
    job_id: str,
    claim: dict[str, Any],
    *,
    event_type: str,
    details: dict[str, Any],
) -> bool:
    validated = _execution_analytics_details(event_type, details)
    with conn:
        row = _current_execution_lease(
            conn,
            job_id,
            claim,
            allow_cancel=True,
            require_live=event_type == "execution_backend_invoked",
            require_authorized=True,
        )
        if not row:
            raise BridgeConflict("factory execution analytics fence is stale or unauthorized")
        _validate_execution_route_identity(conn, row["attempt_id"], validated)
        return _append_factory_execution_analytics_event(conn, row["attempt_id"], event_type, validated)


def _validate_execution_receipt_binding(
    job: dict[str, Any],
    attempt: dict[str, Any],
    *,
    pr_url: Any,
    branch: Any,
    commit_sha: Any,
    allow_missing_commit_sha: bool = False,
) -> None:
    repository = job.get("payload", {}).get("repo")
    try:
        observed_repository, _, _ = _parse_pr_reference(repository, pr_url)
    except (TypeError, BridgeValidationError) as exc:
        raise BridgeValidationError("execution receipt does not match the execution job") from exc
    if (
        job.get("state") != "running"
        or attempt.get("job_id") != job.get("job_id")
        or observed_repository != repository
        or branch != job.get("branch")
    ):
        raise BridgeValidationError("execution receipt does not match the execution job")
    if commit_sha is None and allow_missing_commit_sha:
        return
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
        raise BridgeValidationError("execution receipt does not match the execution job")


def _validate_execution_commit_analytics_identity(
    conn: sqlite3.Connection,
    attempt_id: str,
    details: dict[str, Any],
) -> None:
    supplied = set(EXECUTION_ANALYTICS_COMMON_FIELDS) & set(details)
    if not supplied:
        return
    if supplied != set(EXECUTION_ANALYTICS_COMMON_FIELDS):
        raise BridgeValidationError("factory execution commit analytics identity is incomplete")
    identity = _execution_analytics_details(
        "execution_backend_invoked",
        {field: details.get(field) for field in EXECUTION_ANALYTICS_COMMON_FIELDS},
    )
    invocation = conn.execute(
        "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? "
        "AND event_type='execution_backend_invoked' ORDER BY created_at,event_id LIMIT 1",
        (attempt_id,),
    ).fetchone()
    if invocation:
        recorded = json.loads(invocation["details_json"])
        if any(recorded.get(field) != identity.get(field) for field in EXECUTION_ANALYTICS_COMMON_FIELDS):
            raise BridgeConflict("factory execution commit analytics identity does not match invocation")
    if "backend_outcome" in details and details.get("backend_outcome") not in {"created", "reused", "unknown"}:
        raise BridgeValidationError("factory execution commit backend_outcome is invalid")
    elapsed = details.get("backend_elapsed_ms")
    if "backend_elapsed_ms" in details and (
        not isinstance(elapsed, int)
        or isinstance(elapsed, bool)
        or not 0 <= elapsed <= 604_800_000
    ):
        raise BridgeValidationError("factory execution commit backend_elapsed_ms is invalid")


def _enqueue_factory_attempt_resume(
    conn: sqlite3.Connection,
    attempt: dict[str, Any],
    job: dict[str, Any],
    *,
    preserve_pending: bool = False,
    event_type: str = "execution_effect_commit_recovery_wake_enqueued",
    receipt_recovery: bool = False,
) -> bool:
    """Reuse the safe accepted-event path to resume after receipt recovery."""
    current = get_factory_attempt(conn, attempt["attempt_id"])
    if receipt_recovery and not _execution_receipt_recovery_eligible(conn, current):
        return False
    if not receipt_recovery and (_issue_control_held(conn, current["work_item_key"]) or current["status"] in {"canceled", "canceled_after_effect", "cancellation_requested", "superseded"}):
        return False
    outbox_key = f"factory:accepted:{attempt['attempt_id']}"
    payload = canonical_json({"job_id": job["job_id"], "idempotency_key": job["idempotency_key"]})
    row = conn.execute(
        "SELECT * FROM factory_attempt_outbox WHERE outbox_key=?",
        (outbox_key,),
    ).fetchone()
    if row and (
        row["attempt_id"] != attempt["attempt_id"]
        or row["job_id"] != job["job_id"]
        or row["event_type"] != "hermes_job_accepted"
        or row["payload_json"] != payload
    ):
        raise BridgeConflict("factory recovery wake conflicts with the accepted-event outbox")
    if preserve_pending and row and row["acknowledged_at"] is None:
        return False
    now = utc_now()
    if row:
        # Clear even an in-flight lease. Its stale acknowledgement is fenced by
        # delivery_lease_id, while this replay remains durably pending.
        conn.execute(
            "UPDATE factory_attempt_outbox SET state='pending',delivery_lease_id=NULL,"
            "delivery_lease_expires_at=NULL,acknowledged_at=NULL,updated_at=? WHERE outbox_key=?",
            (now, outbox_key),
        )
    else:
        conn.execute(
            "INSERT INTO factory_attempt_outbox "
            "(outbox_key,attempt_id,job_id,event_type,payload_json,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (outbox_key, attempt["attempt_id"], job["job_id"], "hermes_job_accepted", payload, "pending", now, now),
        )
    if preserve_pending or not conn.execute(
        "SELECT 1 FROM factory_attempt_events WHERE attempt_id=? "
        "AND event_type=?",
        (attempt["attempt_id"], event_type),
    ).fetchone():
        _append_factory_attempt_event(
            conn,
            attempt["attempt_id"],
            event_type,
            {"outbox_key": outbox_key},
        )
    return True


def _commit_observed_execution_receipt(
    conn: sqlite3.Connection,
    job_id: str,
    lease: sqlite3.Row,
    details: dict[str, Any],
) -> bool:
    commit_sha = details.get("observed_commit_sha")
    job = get_job(conn, job_id)
    attempt = get_factory_attempt(conn, lease["attempt_id"])
    _validate_execution_receipt_binding(
        job,
        attempt,
        pr_url=details.get("observed_pr_url"),
        branch=details.get("observed_branch"),
        commit_sha=commit_sha,
        allow_missing_commit_sha=True,
    )
    if commit_sha is None:
        return False
    commit_details = {
        "commit_sha": commit_sha,
        "pr_url": details["observed_pr_url"],
        "branch": details["observed_branch"],
        "hermes_session_id": details["observed_hermes_session_id"],
        "reason_code": "factory_effect_commit_recovered",
        "analytics_schema_version": details["analytics_schema_version"],
        "invocation_id": details["invocation_id"],
        "execution_backend": details["execution_backend"],
        "execution_transport": details["execution_transport"],
        "backend_version": details["backend_version"],
        "configured_auth_mode": details["configured_auth_mode"],
        "backend_outcome": details["backend_outcome"],
        "backend_elapsed_ms": details["elapsed_ms"],
    }
    job = transition_job(
        conn,
        job_id,
        state="effect_committed",
        details=commit_details,
        _factory_execution_commit=True,
    )
    context = details.get("verification_context")
    if isinstance(context, dict) and not _issue_control_held(conn, attempt["work_item_key"]) and get_factory_attempt(conn, attempt["attempt_id"])["status"] not in {"canceled", "canceled_after_effect", "cancellation_requested", "superseded"}:
        job = transition_job(
            conn,
            job["job_id"],
            state="verification_pending",
            details={
                "reason_code": "draft_pr_verification_pending",
                "verification_context": context,
            },
        )
    now = utc_now()
    conn.execute(
        "UPDATE factory_execution_leases SET released_at=?,effect_observed_at=COALESCE(effect_observed_at,?) WHERE attempt_id=? AND epoch=? AND fence_token=?",
        (now, now, lease["attempt_id"], lease["epoch"], lease["fence_token"]),
    )
    _append_factory_attempt_event(
        conn,
        lease["attempt_id"],
        "execution_effect_commit_recovered",
        {"invocation_id": details["invocation_id"], "job_id": job["job_id"]},
    )
    _enqueue_factory_attempt_resume(conn, attempt, job)
    return True


def authorize_factory_effect(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any]) -> bool:
    """Irrevocably assign the one external-effect authority for a generation."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _current_execution_lease(conn, job_id, claim)
        if not row:
            return False
        job = get_job(conn, job_id)
        attempt = get_factory_attempt(conn, row["attempt_id"])
        if _issue_control_held(conn, attempt["work_item_key"]):
            return False
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


def settle_factory_execution(
    conn: sqlite3.Connection,
    job_id: str,
    claim: dict[str, Any],
    *,
    effect_observed: bool,
    failure_details: dict[str, Any] | None = None,
    failure_event_type: str = "execution_backend_failed",
) -> None:
    if failure_event_type not in EXECUTION_FAILURE_EVENT_TYPES:
        raise BridgeValidationError("factory execution settlement failure event type is invalid")
    validated_failure = _execution_analytics_details(failure_event_type, failure_details) if failure_details is not None else None
    if effect_observed and validated_failure is not None:
        raise BridgeValidationError("factory execution settlement cannot combine success and failure evidence")
    with conn:
        row = _current_execution_lease(conn, job_id, claim, allow_cancel=True, require_live=False)
        if not row:
            return
        now = utc_now()
        if row["effect_authorized_at"]:
            # Authorization is absorbing. Observation is not a transfer or a
            # release: the only owner/fence remains able to commit the receipt.
            failure_observed = bool(validated_failure and validated_failure.get("effect_outcome") == "observed")
            conn.execute("UPDATE factory_execution_leases SET effect_observed_at=COALESCE(effect_observed_at,?) WHERE attempt_id=? AND epoch=? AND fence_token=?", (now if effect_observed or failure_observed else None, row["attempt_id"], row["epoch"], row["fence_token"]))
            if effect_observed:
                _append_factory_attempt_event(conn, row["attempt_id"], "effect_authorized_observed", {})
                return
            if validated_failure is not None:
                if failure_event_type == "execution_effect_commit_failed":
                    validated_failure = {
                        **validated_failure,
                        "verification_context": _bounded_verification_context_for_job(
                            get_job(conn, job_id),
                            validated_failure.get("verification_context"),
                        ),
                    }
                _append_factory_execution_analytics_event(conn, row["attempt_id"], failure_event_type, validated_failure)
                receipt_identity_valid = True
                if failure_event_type == "execution_effect_commit_failed":
                    try:
                        if _commit_observed_execution_receipt(conn, job_id, row, validated_failure):
                            return
                    except BridgeValidationError:
                        receipt_identity_valid = False
                        _append_factory_attempt_event(
                            conn,
                            row["attempt_id"],
                            "execution_effect_commit_receipt_rejected",
                            {
                                "reason_code": "factory_effect_commit_receipt_identity_invalid",
                                "operator_intervention_required": True,
                            },
                        )
            else:
                receipt_identity_valid = False
            conn.execute(
                "UPDATE factory_attempts SET status='effect_outcome_unknown',updated_at=? "
                "WHERE attempt_id=? AND status!='cancellation_requested'",
                (now, row["attempt_id"]),
            )
            _append_factory_attempt_event(conn, row["attempt_id"], "effect_authorized_outcome_unknown", validated_failure or {})
            if failure_event_type == "execution_effect_commit_failed" and validated_failure is not None and receipt_identity_valid:
                attempt = get_factory_attempt(conn, row["attempt_id"])
                _queue_pr_inspection(
                    conn,
                    attempt["work_item_key"],
                    validated_failure["observed_pr_url"],
                    attempt["attempt_id"],
                )
                inspection = get_pr_inspection(
                    conn,
                    get_job(conn, job_id)["payload"]["repo"],
                    validated_failure["observed_pr_url"],
                    prior_attempt_id=attempt["attempt_id"],
                )
                if (
                    inspection
                    and inspection.get("fresh") is True
                    and isinstance(inspection.get("result"), dict)
                ):
                    if not _recover_effect_commit_from_inspection(conn, attempt["attempt_id"], inspection["result"]):
                        _refresh_effect_commit_inspection(
                            conn,
                            attempt["attempt_id"],
                            validated_failure["observed_pr_url"],
                        )
            return
        conn.execute("UPDATE factory_execution_leases SET released_at=?, effect_observed_at=? WHERE attempt_id=? AND epoch=? AND fence_token=?", (now, now if effect_observed else None, row["attempt_id"], row["epoch"], row["fence_token"]))
        if validated_failure is not None:
            if failure_event_type != "execution_dispatch_failed":
                raise BridgeValidationError("pre-authorization settlement accepts dispatch failure evidence only")
            _append_factory_execution_analytics_event(
                conn, row["attempt_id"], failure_event_type, validated_failure
            )
        if row["cancel_requested_at"] and not effect_observed:
            conn.execute("UPDATE factory_attempts SET status='canceled', updated_at=? WHERE attempt_id=?", (now, row["attempt_id"]))
            _append_factory_attempt_event(conn, row["attempt_id"], "attempt_canceled_before_effect", {})


def commit_factory_execution(conn: sqlite3.Connection, job_id: str, claim: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """Fence the only post-provider effect commit for a retry generation."""
    if (
        not isinstance(details, dict)
        or not {"commit_sha", "pr_url", "branch"} <= set(details)
        or not set(details) <= EXECUTION_COMMIT_FIELDS
    ):
        raise BridgeValidationError("factory execution commit details are invalid")
    if details.get("hermes_session_id") is not None:
        raise BridgeValidationError("factory execution commit cannot retain Hermes session material")
    if "reason_code" in details and details.get("reason_code") not in {"hermes_created", "hermes_reused"}:
        raise BridgeValidationError("factory execution commit reason is invalid")
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = _current_execution_lease(conn, job_id, claim, allow_cancel=True, require_live=False, require_authorized=True)
        if not row:
            raise BridgeConflict("factory execution fence is stale or canceled")
        attempt = get_factory_attempt(conn, row["attempt_id"])
        if attempt["status"] == "superseded":
            raise BridgeConflict("factory execution attempt was superseded")
        job = get_job(conn, job_id)
        context = None
        if "verification_context" in details:
            context = _bounded_verification_context_for_job(job, details["verification_context"])
            if context is None:
                raise BridgeValidationError("execution receipt verification context does not match policy")
            details = {key: value for key, value in details.items() if key != "verification_context"}
        _validate_execution_receipt_binding(
            job,
            attempt,
            pr_url=details.get("pr_url"),
            branch=details.get("branch"),
            commit_sha=details.get("commit_sha"),
        )
        _validate_execution_commit_analytics_identity(
            conn,
            attempt["attempt_id"],
            details,
        )
        job = transition_job(
            conn,
            job_id,
            state="effect_committed",
            details=details,
            _factory_execution_commit=True,
        )
        # Commit the effect and its validation evidence in one transaction. A
        # concurrent verifier must never see a recovered PR without its context.
        if context is not None and not _issue_control_held(conn, attempt["work_item_key"]) and get_factory_attempt(conn, attempt["attempt_id"])["status"] != "canceled_after_effect":
            job = transition_job(conn, job_id, state="verification_pending", details={
                "reason_code": "draft_pr_verification_pending", "verification_context": context,
            })
        now = utc_now()
        conn.execute("UPDATE factory_execution_leases SET released_at=?,effect_observed_at=? WHERE attempt_id=? AND epoch=? AND fence_token=?", (now, now, row["attempt_id"], row["epoch"], row["fence_token"]))
        if get_factory_attempt(conn, row["attempt_id"])["status"] == "canceled_after_effect":
            conn.execute("UPDATE factory_attempt_outbox SET state='suppressed',acknowledged_at=?,delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE attempt_id=? AND acknowledged_at IS NULL", (now, now, row["attempt_id"]))
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
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM factory_attempt_outbox WHERE outbox_key=? AND acknowledged_at IS NULL", (outbox_key,)).fetchone()
        if not row: return None
        attempt = get_factory_attempt(conn, row["attempt_id"])
        receipt_recovery = _execution_receipt_recovery_eligible(conn, attempt)
        if not receipt_recovery and (_issue_control_held(conn, attempt["work_item_key"]) or attempt["status"] in {"canceled", "canceled_after_effect", "cancellation_requested", "superseded"}):
            return None
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        if row["delivery_lease_expires_at"] and row["delivery_lease_expires_at"] > now_text:
            return None
        lease_id = str(uuid.uuid4()); expires = (now + timedelta(seconds=60)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        conn.execute("UPDATE factory_attempt_outbox SET delivery_attempts=delivery_attempts+1, state='delivering', delivery_lease_id=?, delivery_lease_expires_at=?, updated_at=? WHERE outbox_key=?", (lease_id, expires, now_text, outbox_key))
    return next(item for item in list_factory_outbox(conn, 250) if item["outbox_key"] == outbox_key)


def acknowledge_factory_outbox(conn: sqlite3.Connection, outbox_key: str, lease_id: str) -> None:
    if not isinstance(lease_id, str) or not lease_id:
        raise BridgeValidationError("factory outbox acknowledgement requires its delivery lease")
    with conn:
        now = utc_now()
        conn.execute("UPDATE factory_attempt_outbox SET state='acknowledged', acknowledged_at=?, delivery_lease_id=NULL, delivery_lease_expires_at=NULL, updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL AND delivery_lease_id=?", (now, now, outbox_key, lease_id))


def fail_factory_outbox_delivery(conn: sqlite3.Connection, outbox_key: str, lease_id: str) -> None:
    with conn:
        conn.execute("UPDATE factory_attempt_outbox SET state='retrying', delivery_lease_id=NULL, delivery_lease_expires_at=NULL, updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL AND delivery_lease_id=?", (utc_now(), outbox_key, lease_id))


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


def acknowledge_factory_inspection_outbox(conn: sqlite3.Connection, outbox_key: str, lease_id: str) -> None:
    if not isinstance(lease_id, str) or not lease_id:
        raise BridgeValidationError("factory inspection outbox acknowledgement requires its delivery lease")
    with conn:
        now = utc_now()
        conn.execute("UPDATE factory_inspection_outbox SET state='acknowledged',acknowledged_at=?,delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL AND delivery_lease_id=?", (now, now, outbox_key, lease_id))


def fail_factory_inspection_outbox_delivery(conn: sqlite3.Connection, outbox_key: str, lease_id: str) -> None:
    with conn:
        conn.execute("UPDATE factory_inspection_outbox SET state='retrying',delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE outbox_key=? AND acknowledged_at IS NULL AND delivery_lease_id=?", (utc_now(), outbox_key, lease_id))


def _sync_factory_attempt_state(conn: sqlite3.Connection, job: dict[str, Any], state: str, details: dict[str, Any]) -> None:
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
        event_details = {"job_id": job["job_id"], "generation": row["generation"]}
        if event == "attempt_failed":
            event_details.update({
                key: details.get(key)
                for key in (
                    "failure_domain", "failure_stage", "reason_code", "retryable",
                    "failure_disposition", "operator_intervention_required",
                )
                if key in details
            })
        _append_factory_attempt_event(conn, row["attempt_id"], event, event_details)
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
               j.requested_action, j.idempotency_key, j.pr_url, j.reason_code, j.payload_json, NULL AS attempt_id, NULL AS generation
        FROM bridge_events e JOIN bridge_jobs j ON j.job_id = e.job_id
        UNION ALL
        SELECT ae.event_id, a.job_id, ae.event_type, a.status, ae.created_at,
               j.requested_action, j.idempotency_key, a.prior_pr_url, j.reason_code, j.payload_json, a.attempt_id, a.generation
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
            "requestedAction": row["requested_action"], "prUrl": row["pr_url"], "reasonCode": row["reason_code"],
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
                conn.execute("UPDATE reconciliation_findings SET details_json = ?, last_seen_at = ?, occurrence_count = occurrence_count + 1 WHERE finding_key = ?", (canonical_json(finding), now, key))
                existing.append(key)
            else:
                conn.execute(
                    "INSERT INTO reconciliation_findings (finding_key, job_id, subject, kind, details_json, first_seen_at, last_seen_at, occurrence_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (key, job_id, subject, kind, canonical_json(finding), now, now),
                )
                created.append(key)
    return created, existing


def reconciliation_findings_status(conn: sqlite3.Connection, *, active_within_minutes: int = 30) -> dict[str, Any]:
    if active_within_minutes < 1 or active_within_minutes > 1440:
        raise BridgeValidationError("active_within_minutes must be between 1 and 1440")
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=active_within_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    rows = conn.execute(
        """
        SELECT r.details_json, r.job_id, j.state
        FROM reconciliation_findings r
        LEFT JOIN bridge_jobs j ON j.job_id = r.job_id
        WHERE r.last_seen_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    severity = {"critical": 0, "warning": 0}
    actionable_severity = {"critical": 0, "warning": 0}
    scope = {"active_work": 0, "terminal_work": 0, "system": 0}
    for row in rows:
        value = json.loads(row["details_json"])
        level = value.get("severity")
        if level in severity:
            severity[level] += 1
        if row["job_id"] is None:
            finding_scope = "system"
        elif row["state"] in ACTIVE_STATES:
            finding_scope = "active_work"
        else:
            finding_scope = "terminal_work"
        scope[finding_scope] += 1
        if finding_scope != "terminal_work" and level in actionable_severity:
            actionable_severity[level] += 1
    total = int(conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0])
    return {
        "active_within_minutes": active_within_minutes,
        "current": len(rows),
        "historical": total - len(rows),
        "total": total,
        "severity": severity,
        "scope": scope,
        "actionable": scope["active_work"] + scope["system"],
        "actionable_severity": actionable_severity,
    }


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
        if not isinstance(command, dict) or set(command) != {"command", "exit_code"} or not isinstance(command["command"], str) or not command["command"].strip() or len(command["command"]) > 300 or not isinstance(command["exit_code"], int) or isinstance(command["exit_code"], bool) or not -(2**31) <= command["exit_code"] < 2**31:
            raise BridgeValidationError("verification_context.local_commands is malformed")
        normalized_commands.append({"command": command["command"].strip(), "exit_code": command["exit_code"]})
    if not isinstance(secret, dict) or set(secret) != {"command", "exit_code"} or not isinstance(secret.get("command"), str) or not secret["command"].strip() or len(secret["command"]) > 300 or not isinstance(secret.get("exit_code"), int) or isinstance(secret.get("exit_code"), bool) or not -(2**31) <= secret["exit_code"] < 2**31:
        raise BridgeValidationError("verification_context.secret_check is malformed")
    return {"local_commands": normalized_commands, "secret_check": {"command": secret["command"].strip(), "exit_code": secret["exit_code"]}}


def _bounded_verification_context_for_job(job: dict[str, Any], value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        context = _validate_verification_context(value)
        policy = _validate_payload_policy(job)
    except BridgeValidationError:
        return None
    expected = policy["required_local_commands"]
    observed = context["local_commands"]
    if len(observed) != len(expected) or [item["command"] for item in observed] != expected:
        return None
    if context["secret_check"]["command"] != policy["secret_check_command"]:
        return None
    return context


def _canonical_verifier_policy(repo: str) -> dict[str, Any]:
    configured = os.environ.get("HATCHET_VERIFIER_POLICY_PATH")
    if not configured:
        raise BridgeValidationError("HATCHET_VERIFIER_POLICY_PATH is required for real verification")
    if os.environ.get("HATCHET_FACTORY_RUNTIME") == "docker" and configured != DOCKER_VERIFIER_POLICY_PATH:
        raise BridgeValidationError(f"HATCHET_VERIFIER_POLICY_PATH must be {DOCKER_VERIFIER_POLICY_PATH} for Docker Factory")
    path = Path(configured)
    try:
        catalog = json.loads(path.read_text())
        version = catalog["version"]
        policies = [item for item in catalog["policies"] if isinstance(item, dict) and item.get("repo") == repo]
        if not isinstance(version, int) or version < 1 or len(policies) != 1:
            raise ValueError("catalog is invalid")
        policy = policies[0]
        required = {"id", "repo", "expected_base", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command"}
        if set(policy) != required or any(not isinstance(policy[key], str) or not policy[key].strip() for key in ("id", "repo", "expected_base", "secret_check_command")) or any(not isinstance(policy[key], list) or not policy[key] or any(not isinstance(item, str) or not item.strip() for item in policy[key]) for key in ("allowed_changed_paths", "forbidden_changed_paths", "required_local_commands")):
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
    if not _payload_policy_matches(supplied, canonical):
        raise BridgeValidationError("submitted verifier policy does not exactly match the checked-in policy")
    for key in ("expected_base", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command"):
        if job["payload"].get(key) != canonical[key]:
            raise BridgeValidationError("submitted verifier policy fields do not exactly match the checked-in policy")
    return canonical


def _payload_policy_matches(supplied: Any, canonical: dict[str, Any]) -> bool:
    if not isinstance(supplied, dict) or supplied.get("catalog_version") != canonical.get("catalog_version"):
        return False
    body_keys = ("id", "repo", "expected_base", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command")
    allowed_keys = {*body_keys, "catalog_version", "digest"}
    legacy_allowed_keys = {*allowed_keys, "required_github_checks"}
    supplied_keys = set(supplied)
    if supplied_keys != allowed_keys and supplied_keys != legacy_allowed_keys:
        return False
    if {key: supplied.get(key) for key in body_keys} != {key: canonical[key] for key in body_keys}:
        return False
    digest = supplied.get("digest")
    if digest == canonical["digest"]:
        return True
    legacy_checks = supplied.get("required_github_checks")
    if not isinstance(legacy_checks, list) or not legacy_checks or any(not isinstance(item, str) or not item.strip() for item in legacy_checks):
        return False
    legacy_body = {key: supplied[key] for key in body_keys}
    legacy_body["required_github_checks"] = legacy_checks
    return digest == hashlib.sha256(canonical_json(legacy_body).encode()).hexdigest()


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
    checks = value["checks"]
    if not isinstance(checks, dict) or set(checks) != {"status", "total_count"} or checks.get("status") != "SUCCESS" or not isinstance(checks.get("total_count"), int) or checks["total_count"] < 1 or checks["total_count"] > 512:
        raise BridgeValidationError("verification receipt GitHub check status is not successful")
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
    if state == "failed":
        _validate_failure_classification(details)

    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
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
        if not _factory_execution_commit and (_issue_control_held(conn, _job_work_item(job)) or (factory_attempt and factory_attempt["status"] in {"canceled", "canceled_after_effect", "cancellation_requested", "superseded"})):
            if _factory_verifier_completion and not _issue_control_held(conn, _job_work_item(job)):
                raise BridgeValidationError("verification requires the current eligible Factory attempt")
            raise BridgeConflict("issue control fences this attempt from further workflow transitions")
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
            if factory_attempt and factory_attempt["mode"] == "real" and factory_attempt["actor"] != "legacy-import":
                supplied_context = _bounded_verification_context_for_job(job, supplied_context)
                if supplied_context is None:
                    raise BridgeValidationError("verification_context does not match the checked-in verifier policy")
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
        _sync_factory_attempt_state(conn, job, state, details)
        _append_event(
            conn,
            job_id=job["job_id"],
            event_type=f"transition:{state}",
            state=state,
            details=details,
        )
        _record_factory_transition_checkpoint(conn, get_job(conn, job["job_id"]), verification_completion=verification_completion)
        if verification_completion and state == "failed" and factory_attempt:
            _reserve_factory_verification_repair(
                conn,
                failed_job=get_job(conn, job["job_id"]),
                factory_attempt=factory_attempt,
                details=details,
            )

    return get_job(conn, job["job_id"])
