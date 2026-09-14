import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge.analytics import (
    _github_observation_order,
    _github_within_horizon,
    _ready_correlation,
    _ready_work_item_conversion,
    factory_analytics,
)
from hatchet_hermes_bridge.store import (
    BridgeConflict,
    BridgeValidationError,
    acknowledge_factory_outbox,
    authorize_factory_effect,
    cancel_factory_attempt,
    claim_factory_outbox_delivery,
    claim_factory_execution,
    commit_factory_execution,
    complete_pr_inspection,
    get_factory_attempt,
    get_job,
    record_factory_execution_analytics,
    refresh_factory_effect_receipt_inspection,
    reserve_factory_intake,
    reserve_factory_retry,
    settle_factory_execution,
    sweep_factory_effect_receipt_inspections,
    transition_job,
    _queue_pr_inspection,
)
from test_support import connect_migrate as connect


ROOT = Path(__file__).resolve().parents[2]


def verifier_payload(repository: str = "Driftward-LLC/capykit") -> dict:
    catalog = json.loads((ROOT / "workers/verifier-policy.json").read_text())
    policy = next(item for item in catalog["policies"] if item["repo"] == repository)
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    canonical = {"catalog_version": catalog["version"], "digest": digest, **policy}
    return {
        "repo": repository,
        "dry_run": False,
        "verification_policy": canonical,
        "expected_base": policy["expected_base"],
        "allowed_changed_paths": policy["allowed_changed_paths"],
        "forbidden_changed_paths": policy["forbidden_changed_paths"],
        "required_local_commands": policy["required_local_commands"],
        "secret_check_command": policy["secret_check_command"],
    }


def analytics_identity(invocation_id: str) -> dict:
    return {
        "analytics_schema_version": 1,
        "invocation_id": invocation_id,
        "execution_backend": "hermes",
        "execution_transport": "grpc_uds",
        "backend_version": None,
        "configured_auth_mode": "chatgpt_codex_oauth",
    }


def route_identity(
    attempt_id: str,
    *,
    variant: str = "hermes",
    backend: str = "hermes",
    transport: str = "grpc_uds",
) -> dict:
    return {
        "analytics_schema_version": 1,
        "assignment_id": f"route:{attempt_id}",
        "experiment_id": "factory-default-v1",
        "variant": variant,
        "assignment_method": "static",
        "intended_backend": backend,
        "intended_transport": transport,
        "configured_provider": "openai_codex",
        "configured_model": "gpt-5.5",
        "configured_auth_mode": "chatgpt_codex_oauth",
        "backend_version": None,
        "factory_release_sha": None,
        "factory_release_version": None,
        "routing_registry_version": 1,
        "verifier_policy_digest": "0" * 64,
        "fallback_allowed": False,
    }


class FactoryAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Store-created records must stay inside the fixed September report window.
        clock_patch = patch("hatchet_hermes_bridge.store.datetime", wraps=datetime)
        store_clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        # Preserve write order instead of breaking timestamp ties with random IDs.
        store_clock.now.side_effect = (
            datetime(2026, 9, 11, tzinfo=timezone.utc) + timedelta(microseconds=tick)
            for tick in count()
        )
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        root = Path(self.work.name)
        self.bridge_db = str(root / "bridge.db")
        self.ingress_db = str(root / "ingress.db")
        self.lifecycle_db = str(root / "lifecycle.db")
        self.conn = connect(self.bridge_db)
        self.addCleanup(self.conn.close)
        self._init_optional_ledgers()

    def _reserve(self, issue: str):
        return reserve_factory_intake(
            self.conn,
            work_item_key=issue,
            mode="real",
            request_id=f"linear:issue:{issue}:update:one",
            reason="Linear Ready intake reserved a factory attempt",
            actor="intake-worker",
            payload={**verifier_payload(), "linear_issue": issue, "source_event_key": f"linear:issue:{issue}:update:one"},
        )

    def _register_invocation(self, job: dict, lease: dict, invocation_id: str) -> dict:
        identity = analytics_identity(invocation_id)
        self.assertTrue(record_factory_execution_analytics(
            self.conn,
            job["job_id"],
            lease,
            event_type="execution_backend_invoked",
            details=identity,
        ))
        return identity

    def _init_optional_ledgers(self) -> None:
        conn = sqlite3.connect(self.ingress_db)
        conn.execute(
            """CREATE TABLE ingress_events (
                event_id TEXT, source TEXT, subject TEXT, normalized_json TEXT, decision TEXT,
                reason_code TEXT, hatchet_published INTEGER, created_at TEXT,
                provider_updated_at_us INTEGER
            )"""
        )
        conn.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            ("linear-1", "linear", "ENG-40", "{}", "ready_for_implementation", "linear_issue_ready_for_implementation", 1, "2026-09-10T00:00:00Z", 1),
        )
        conn.commit(); conn.close()

        conn = sqlite3.connect(self.lifecycle_db)
        conn.execute(
            "CREATE TABLE lifecycle_outbox ("
            "job_id TEXT, delivery_state TEXT, reason_code TEXT, first_seen_at TEXT, last_error_code TEXT)"
        )
        conn.commit(); conn.close()

    def _record_ingress_provenance(
        self,
        conn: sqlite3.Connection,
        event_keys: dict[str, str],
        *,
        signature_verified: bool = True,
        fallback_timestamp_events: set[str] | None = None,
    ) -> None:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ingress_events)")
        }
        if "event_key" not in columns:
            conn.execute("ALTER TABLE ingress_events ADD COLUMN event_key TEXT")
        if "provider_timestamp_source" not in columns:
            conn.execute(
                "ALTER TABLE ingress_events ADD COLUMN provider_timestamp_source TEXT"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingress_receipts ("
            "receipt_id TEXT PRIMARY KEY,event_key TEXT,source TEXT,provider_event TEXT,"
            "delivery_id TEXT,payload_hash TEXT,signature_verified INTEGER,"
            "disposition TEXT,received_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ingress_schema_meta ("
            "singleton INTEGER PRIMARY KEY,schema_version INTEGER,migrated_at TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingress_schema_meta VALUES (1,3,?)",
            ("2026-09-10T00:00:00Z",),
        )
        fallback_timestamp_events = fallback_timestamp_events or set()
        for event_id, event_key in event_keys.items():
            row = conn.execute(
                "SELECT source,created_at FROM ingress_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            source, received_at = row
            conn.execute(
                "UPDATE ingress_events SET event_key=?,provider_timestamp_source=? "
                "WHERE event_id=?",
                (
                    event_key,
                    "receipt_fallback" if event_id in fallback_timestamp_events else "provider",
                    event_id,
                ),
            )
            conn.execute(
                "INSERT INTO ingress_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), event_key, source,
                    "issue" if source == "linear" else "pull_request",
                    None, f"payload:{event_id}", int(signature_verified),
                    "accepted", received_at,
                ),
            )

    def test_horizon_outcome_uses_latest_state_after_reopen(self) -> None:
        origin = datetime(2026, 9, 10, tzinfo=timezone.utc)
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/80"
        observations = {
            pr_url: [
                {
                    "normalized": {"state": "closed", "merged": False},
                    "provider_timestamp_source": "provider",
                    "provider_updated_at_us": int(
                        (origin + timedelta(hours=2)).timestamp() * 1_000_000
                    ),
                    "created_at": "2026-09-10T02:00:00Z",
                    "event_id": "closed",
                },
                {
                    "normalized": {"state": "open", "merged": False},
                    "provider_timestamp_source": "provider",
                    "provider_updated_at_us": int(
                        (origin + timedelta(hours=3)).timestamp() * 1_000_000
                    ),
                    "created_at": "2026-09-10T03:00:00Z",
                    "event_id": "reopened",
                },
            ],
        }
        result = _github_within_horizon(
            observations, {pr_url: origin}, {pr_url}, timedelta(hours=24),
            origin + timedelta(days=2),
        )
        self.assertEqual(result["by_outcome"], {"insufficient_horizon_evidence": 1})
        self.assertFalse(result["closed_unmerged_within_horizon"]["available"])
        self.assertIsNone(result["closed_unmerged_within_horizon"]["observed"])
        self.assertIsNone(result["merged_within_horizon"]["rate"])
        self.assertEqual(result["right_censored_prs"], 1)

    def test_horizon_without_observation_is_unknown_not_measured_failure(self) -> None:
        origin = datetime(2026, 9, 10, tzinfo=timezone.utc)
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/81"

        result = _github_within_horizon(
            {}, {pr_url: origin}, {pr_url}, timedelta(hours=24),
            origin + timedelta(days=2),
        )

        self.assertEqual(result["by_outcome"], {"unobserved_within_horizon": 1})
        self.assertEqual(
            result["observation_evidence"],
            {"observed": 0, "eligible": 1, "rate": 0.0, "complete": False},
        )
        self.assertEqual(
            result["merged_within_horizon"],
            {"observed": 0, "eligible": 1, "rate": None, "complete": False},
        )
        self.assertEqual(result["right_censored_prs"], 1)

    def test_horizon_uses_post_boundary_state_as_nonmerge_evidence(self) -> None:
        origin = datetime(2026, 9, 10, tzinfo=timezone.utc)
        cutoff = origin + timedelta(hours=24)
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/83"
        observations = {
            pr_url: [
                {
                    "normalized": {"state": "open", "merged": False},
                    "provider_timestamp_source": "provider",
                    "provider_updated_at_us": int(
                        (origin + timedelta(hours=1)).timestamp() * 1_000_000
                    ),
                    "created_at": (origin + timedelta(hours=1)).isoformat(),
                    "event_id": "early-open",
                },
                {
                    "normalized": {"state": "open", "merged": False},
                    "provider_timestamp_source": "provider",
                    "provider_updated_at_us": int(
                        (cutoff + timedelta(hours=1)).timestamp() * 1_000_000
                    ),
                    "created_at": (cutoff + timedelta(hours=1)).isoformat(),
                    "event_id": "later-open",
                },
            ],
        }

        result = _github_within_horizon(
            observations, {pr_url: origin}, {pr_url}, timedelta(hours=24),
            origin + timedelta(days=2),
        )

        self.assertEqual(result["by_outcome"], {"not_merged_within_horizon": 1})
        self.assertEqual(
            result["merged_within_horizon"],
            {"observed": 0, "eligible": 1, "rate": 0.0, "complete": True},
        )
        self.assertFalse(result["closed_unmerged_within_horizon"]["available"])
        self.assertEqual(result["right_censored_prs"], 0)

    def test_fallback_receipt_time_can_outrank_older_provider_snapshot(self) -> None:
        provider_open = {
            "normalized": {"state": "open", "merged": False},
            "provider_timestamp_source": "provider",
            "provider_updated_at_us": int(
                datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp() * 1_000_000
            ),
            "created_at": "2026-09-12T00:00:00Z",
            "event_id": "provider-open",
        }
        fallback_merge = {
            "normalized": {"state": "closed", "merged": True},
            "provider_timestamp_source": "receipt_fallback",
            "provider_updated_at_us": None,
            "created_at": "2026-09-11T00:00:00Z",
            "event_id": "fallback-merge",
        }

        self.assertIs(
            max((provider_open, fallback_merge), key=_github_observation_order),
            fallback_merge,
        )

    def test_ready_merge_requires_pr_association_no_later_than_merge(self) -> None:
        ready_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        merge_at = ready_at + timedelta(days=1)
        association_at = ready_at + timedelta(days=2)
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/84"
        attempt = {
            "attempt_id": "attempt-post-merge",
            "job_id": "job-post-merge",
            "work_item_key": "ENG-84",
            "request_id": "linear:issue:ENG-84:update:one",
            "job_payload_json": "{}",
            "mode": "real",
            "actor": "intake-worker",
            "status": "completed",
            "created_at": association_at.isoformat(),
            "updated_at": association_at.isoformat(),
            "pr_url": pr_url,
            "prior_pr_url": None,
            "job_updated_at": association_at.isoformat(),
        }
        events = {
            attempt["attempt_id"]: [{
                "event_type": "attempt_effect_committed",
                "created_at": association_at.isoformat(),
            }],
        }
        observation = {
            "normalized": {"state": "closed", "merged": True},
            "provider_timestamp_source": "provider",
            "provider_updated_at_us": int(merge_at.timestamp() * 1_000_000),
            "created_at": merge_at.isoformat(),
            "event_id": "merge-before-association",
        }
        result = _ready_correlation(
            [{
                "event_key": attempt["request_id"],
                "event_id": "linear-ready-84",
                "subject": "ENG-84",
                "created_at": ready_at.isoformat(),
                "provider_timestamp_source": "provider",
                "provider_updated_at_us": int(ready_at.timestamp() * 1_000_000),
            }],
            [attempt], events, {}, {pr_url: merge_at}, {pr_url: "merged"},
            {pr_url: [observation]}, ready_at + timedelta(days=10),
        )

        conversion = result["work_item_conversion"]["all"]
        self.assertEqual(conversion["merged"]["observed"], 0)
        self.assertIsNone(conversion["merged"]["rate"])
        self.assertEqual(conversion["merge_outcome_evidence"]["observed"], 0)
        self.assertEqual(conversion["right_censored_work_items"], 1)
        self.assertEqual(result["latency_ms"]["ready_received_to_merged"]["samples"], 0)
        self.assertEqual(result["latency_ms"]["ready_received_to_merged"]["eligible"], 1)
        self.assertEqual(
            result["latency_ms"]["ready_received_to_merged"]["coverage_rate"],
            0.0,
        )

    def test_ready_multi_pr_outcomes_are_exclusive_and_evidence_bounded(self) -> None:
        origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
        closed_url = "https://github.com/Driftward-LLC/capykit/pull/90"
        unobserved_url = "https://github.com/Driftward-LLC/capykit/pull/91"
        merged_url = "https://github.com/Driftward-LLC/capykit/pull/92"
        attempts = {
            "ENG-MIX": [
                {"attempt_id": "mix-closed", "job_id": "job-mix-closed", "status": "completed", "pr_url": closed_url},
                {"attempt_id": "mix-missing", "job_id": "job-mix-missing", "status": "completed", "pr_url": unobserved_url},
            ],
            "ENG-MERGED": [
                {"attempt_id": "merged-active", "job_id": "job-merged-active", "status": "running", "pr_url": merged_url},
            ],
        }
        effect_at = (origin + timedelta(hours=1)).isoformat()
        events = {
            attempt["attempt_id"]: [
                {"event_type": "attempt_effect_committed", "created_at": effect_at}
            ]
            for lineage in attempts.values()
            for attempt in lineage
        }
        closed_observation = {
            "normalized": {"state": "closed", "merged": False},
            "provider_timestamp_source": "provider",
            "provider_updated_at_us": int(
                (origin + timedelta(hours=2)).timestamp() * 1_000_000
            ),
            "created_at": (origin + timedelta(hours=2)).isoformat(),
            "event_id": "closed",
        }
        merged_at = origin + timedelta(hours=3)
        merged_observation = {
            "normalized": {"state": "closed", "merged": True},
            "provider_timestamp_source": "provider",
            "provider_updated_at_us": int(merged_at.timestamp() * 1_000_000),
            "created_at": merged_at.isoformat(),
            "event_id": "merged",
        }

        result = _ready_work_item_conversion(
            {
                "ENG-MIX": {"created_at": origin.isoformat()},
                "ENG-MERGED": {"created_at": origin.isoformat()},
            },
            attempts,
            events,
            {},
            {merged_url: merged_at},
            {closed_url: "closed_unmerged", merged_url: "merged"},
            {closed_url: [closed_observation], merged_url: [merged_observation]},
            datetime(2026, 9, 13, tzinfo=timezone.utc),
        )

        all_outcomes = result["all"]
        self.assertEqual(all_outcomes["merged"]["observed"], 1)
        self.assertEqual(all_outcomes["github_closed_unmerged_work_items"], 0)
        self.assertEqual(all_outcomes["github_unobserved_work_items"], 1)
        self.assertEqual(all_outcomes["right_censored_work_items"], 1)
        within = result["maturity"]["outcome_within_horizon"]["within_24h"]
        self.assertEqual(
            within["merge_outcome_evidence"],
            {"observed": 1, "eligible": 2, "rate": 0.5, "complete": False},
        )
        self.assertEqual(
            within["merged_within_horizon"],
            {"observed": 1, "eligible": 2, "rate": None, "complete": False},
        )
        self.assertEqual(within["right_censored_work_items"], 1)

    def test_check_suite_summary_counts_every_authenticated_observation(self) -> None:
        _attempt, job, _ = self._reserve("ENG-80")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/80"
        self.conn.execute(
            "UPDATE bridge_jobs SET pr_url=?,commit_sha='abc0080',"
            "updated_at='2026-09-11T00:00:00Z' WHERE job_id=?",
            (pr_url, job["job_id"]),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        for index, conclusion in enumerate(("failure", "success"), 1):
            ingress.execute(
                "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"github-check-80-{index}", "github",
                    "Driftward-LLC/capykit#80",
                    json.dumps({
                        "prUrl": pr_url,
                        "headSha": "abc0080",
                        "checkConclusion": conclusion,
                    }),
                    "observe_only", "github_checks_completed", 1,
                    f"2026-09-12T0{index}:00:00Z",
                    int(
                        datetime(
                            2026, 9, 12, index, tzinfo=timezone.utc
                        ).timestamp() * 1_000_000
                    ),
                ),
            )
        self._record_ingress_provenance(
            ingress,
            {
                "github-check-80-1": "github:check_suite:80:one",
                "github-check-80-2": "github:check_suite:80:two",
            },
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        checks = report["github"]["check_suite_observations"]
        self.assertEqual(checks["coverage"]["observed"], 1)
        self.assertEqual(checks["observations"], 2)
        self.assertEqual(checks["by_conclusion"], {"failure": 1, "success": 1})
        self.assertEqual(checks["successful_observations"], 1)
        self.assertEqual(checks["head_sha_binding"]["eligible"], 2)
        self.assertEqual(checks["head_sha_binding"]["observed"], 2)

    def test_report_projects_funnel_failures_backend_and_optional_ledgers(self) -> None:
        successful, success_job, _ = self._reserve("ENG-40")
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (
                str(uuid.uuid4()), successful["attempt_id"], "execution_route_selected",
                json.dumps(route_identity(successful["attempt_id"])),
                "2026-09-11T00:00:00Z",
            ),
        )
        self.conn.commit()
        transition_job(self.conn, success_job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, success_job["job_id"], owner="executor-a")["lease"]
        self.assertTrue(authorize_factory_effect(self.conn, success_job["job_id"], lease))
        identity = analytics_identity("dispatch-success")
        self.assertTrue(record_factory_execution_analytics(
            self.conn, success_job["job_id"], lease,
            event_type="execution_backend_invoked", details=identity,
        ))
        self.assertFalse(record_factory_execution_analytics(
            self.conn, success_job["job_id"], lease,
            event_type="execution_backend_invoked", details=identity,
        ))
        with self.assertRaises(BridgeConflict):
            record_factory_execution_analytics(
                self.conn,
                success_job["job_id"],
                lease,
                event_type="execution_backend_invoked",
                details=analytics_identity("second-provider-call"),
            )
        completed = {**identity, "backend_outcome": "created", "elapsed_ms": 1234}
        with self.assertRaises(BridgeConflict):
            record_factory_execution_analytics(
                self.conn,
                success_job["job_id"],
                lease,
                event_type="execution_backend_completed",
                details={**completed, "execution_transport": "local_command"},
            )
        record_factory_execution_analytics(
            self.conn, success_job["job_id"], lease,
            event_type="execution_backend_completed", details=completed,
        )
        commit_factory_execution(self.conn, success_job["job_id"], lease, {
            "commit_sha": "abc1234",
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/40",
            "branch": success_job["branch"],
            "reason_code": "hermes_created",
            **identity,
            "backend_outcome": "created",
            "backend_elapsed_ms": 1234,
        })
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-11T12:00:00Z' "
            "WHERE attempt_id=? AND event_type='attempt_effect_committed'",
            (successful["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_events SET created_at='2026-09-11T12:00:00Z' "
            "WHERE job_id=? AND event_type='transition:effect_committed'",
            (success_job["job_id"],),
        )
        self.conn.execute("UPDATE factory_attempts SET status='verified_ready' WHERE attempt_id=?", (successful["attempt_id"],))
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), successful["attempt_id"], "attempt_verified_ready", "{}", "2026-09-12T00:00:00Z"),
        )
        self.conn.commit()

        _failed, failed_job, _ = self._reserve("ENG-41")
        transition_job(self.conn, failed_job["job_id"], state="running")
        transition_job(self.conn, failed_job["job_id"], state="failed", details={
            "reason_code": "artifact_fence_changed",
            "failure_domain": "github",
            "failure_stage": "artifact_fence",
            "retryable": False,
            "failure_disposition": "terminal",
            "operator_intervention_required": True,
        })
        self.conn.commit()

        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-1", "github", "Driftward-LLC/capykit#40",
                json.dumps({"prUrl": "https://github.com/Driftward-LLC/capykit/pull/40", "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1, "2026-09-12T12:00:00Z",
                int(datetime(2026, 9, 12, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-stale", "github", "Driftward-LLC/capykit#40",
                json.dumps({"prUrl": "https://github.com/Driftward-LLC/capykit/pull/40", "state": "open", "merged": False}),
                "observe_only", "github_pr_open", 1, "2026-09-13T12:00:00Z",
                int(datetime(2026, 9, 11, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-tied-open", "github", "Driftward-LLC/capykit#40",
                json.dumps({"prUrl": "https://github.com/Driftward-LLC/capykit/pull/40", "state": "open", "merged": False}),
                "observe_only", "github_pr_open", 1, "2026-09-13T13:00:00Z",
                int(datetime(2026, 9, 12, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-checks", "github", "Driftward-LLC/capykit#40",
                json.dumps({"prUrl": "https://github.com/Driftward-LLC/capykit/pull/40", "checkConclusion": "success"}),
                "observe_only", "github_checks_completed", 1, "2026-09-12T13:00:00Z",
                int(datetime(2026, 9, 12, 13, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-review", "github", "Driftward-LLC/capykit#40",
                json.dumps({
                    "prUrl": "https://github.com/Driftward-LLC/capykit/pull/40",
                    "reviewState": "changes_requested", "actorKind": "human",
                }),
                "observe_only", "github_review_submitted", 1, "2026-09-12T14:00:00Z",
                int(datetime(2026, 9, 12, 14, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(ingress, {
            "linear-1": "linear:issue:ENG-40:update:one",
            "github-1": "github:pull_request:40:merged",
            "github-tied-open": "github:pull_request:40:open:tied",
            "github-checks": "github:check_suite:40:completed",
            "github-review": "github:pull_request_review:40:submitted",
        })
        self._record_ingress_provenance(
            ingress,
            {"github-stale": "github:pull_request:40:open:stale"},
            signature_verified=False,
        )
        ingress.commit(); ingress.close()
        lifecycle = sqlite3.connect(self.lifecycle_db)
        lifecycle.execute(
            "INSERT INTO lifecycle_outbox VALUES (?,?,?,?,?)",
            (success_job["job_id"], "delivered", "draft_pr_verified", "2026-09-12T00:00:00Z", None),
        )
        lifecycle.execute(
            "INSERT INTO lifecycle_outbox VALUES (?,?,?,?,?)",
            ("outside-cohort", "dead_letter", "draft_pr_verified", "2026-09-12T00:00:00Z", "linear_api_unavailable"),
        )
        lifecycle.commit(); lifecycle.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["schema"], "factory-analytics-v1")
        self.assertEqual(report["coverage"]["cohort_attempts"], 2)
        self.assertEqual(report["funnel"]["stages"]["attempted"]["count"], 2)
        self.assertEqual(report["funnel"]["stages"]["effect_committed"]["count"], 1)
        self.assertEqual(report["funnel"]["stages"]["verified_ready"]["count"], 1)
        self.assertEqual(report["outcomes"]["merged_prs"], 1)
        self.assertEqual(report["outcomes"]["right_censored_prs"], 0)
        self.assertEqual(report["github"]["closed_unmerged_rate"], 0.0)
        self.assertEqual(
            report["github"]["closed_unmerged_rate_denominator"],
            "authenticated_terminal_prs",
        )
        self.assertEqual(
            report["github"]["closed_unmerged_rates"]["terminal_prs"],
            {
                "numerator": 0,
                "denominator": 1,
                "rate": 0.0,
                "denominator_definition": (
                    "authenticated_prs_with_latest_state_merged_or_closed_unmerged"
                ),
            },
        )
        self.assertEqual(report["github"]["check_suite_observations"]["successful_observations"], 1)
        self.assertEqual(report["github"]["check_suite_observations"]["head_sha_binding"]["observed"], 0)
        self.assertFalse(report["github"]["first_pass_checks"]["available"])
        self.assertIsNone(report["github"]["first_pass_checks"]["success_rate"])
        self.assertEqual(report["github"]["reviews"]["changes_requested_prs"], 1)
        self.assertEqual(report["github"]["reviews"]["by_actor_kind"], {"human": 1})
        self.assertEqual(report["github"]["reviews"]["changes_requested_all_eligible_pr_rate"], 1.0)
        self.assertEqual(
            report["github"]["unverified_observations"],
            {
                "authority": "observation_only_not_used_in_canonical_metrics",
                "matching_events": 1,
                "unique_prs": 1,
                "by_kind": {"pull_request_state": 1},
                "pull_request_state_events": {"open": 1},
            },
        )
        self.assertEqual(report["github"]["authenticity"]["event_coverage"]["observed"], 4)
        self.assertEqual(report["github"]["authenticity"]["event_coverage"]["eligible"], 5)
        self.assertEqual(report["sources"]["ingress"]["by_source"]["github"]["row_count"], 5)
        self.assertEqual(report["sources"]["ingress"]["schema_version"], 3)
        self.assertEqual(
            report["sources"]["ingress"]["schema_version_source"],
            "ingress_schema_meta",
        )
        self.assertEqual(report["failures"]["by_classification"][0], {
            "failure_domain": "github", "failure_stage": "artifact_fence",
            "reason_code": "artifact_fence_changed", "count": 1,
        })
        self.assertEqual(report["backends"][0]["execution_backend"], "hermes")
        self.assertEqual(report["backends"][0]["configured_auth_mode"], "chatgpt_codex_oauth")
        self.assertEqual(report["backends"][0]["completed_invocations"], 1)
        self.assertEqual(report["backends"][0]["backend_outcomes"], {"created": 1})
        self.assertEqual(report["backend_summary"]["backend_outcomes"], {"created": 1})
        self.assertEqual(report["routes"]["assignment_coverage"]["observed"], 1)
        self.assertEqual(report["routes"]["actual_identity_coverage"]["observed"], 1)
        self.assertEqual(report["routes"]["identity_mismatches"], 0)
        self.assertFalse(report["routes"]["groups"][0]["fallback_allowed"])
        self.assertEqual(report["lifecycle"]["by_state"], {"delivered": 1})
        self.assertEqual(report["lifecycle"]["dead_letter_event_reasons"], {"draft_pr_verified": 1})
        self.assertEqual(report["lifecycle"]["dead_letter_delivery_errors"], {"linear_api_unavailable": 1})
        self.assertIsNone(report["safety"]["duplicate_effects_detected"])

    def test_effect_commit_uses_durable_bridge_transition_as_compatibility_evidence(self) -> None:
        attempt, job, _ = self._reserve("ENG-44")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-c")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        commit_factory_execution(self.conn, job["job_id"], lease, {
            "commit_sha": "abc1245",
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/44",
            "branch": job["branch"],
            "reason_code": "hermes_created",
            **analytics_identity("dispatch-compatibility"),
            "backend_outcome": "created",
            "backend_elapsed_ms": 432,
        })
        self.conn.execute(
            "DELETE FROM factory_attempt_events WHERE attempt_id=? AND event_type='attempt_effect_committed'",
            (attempt["attempt_id"],),
        )
        self.conn.commit()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["funnel"]["stages"]["effect_committed"]["count"], 1)
        self.assertEqual(report["latency_ms"]["executor_started_to_effect_committed"]["samples"], 1)
        self.assertEqual(report["coverage"]["duplicate_effect_observation"]["eligible"], 1)
        self.assertEqual(report["backends"][0]["invocations"], 1)
        self.assertEqual(report["backends"][0]["completed_invocations"], 1)
        self.assertEqual(report["backends"][0]["completed_invocation_latency_ms"]["p50"], 432)

    def test_invocation_ids_are_scoped_to_attempts_in_analytics(self) -> None:
        for index, issue in enumerate(("ENG-64", "ENG-65"), start=1):
            _attempt, job, _ = self._reserve(issue)
            transition_job(self.conn, job["job_id"], state="running")
            lease = claim_factory_execution(
                self.conn, job["job_id"], owner=f"executor-collision-{index}"
            )["lease"]
            authorize_factory_effect(self.conn, job["job_id"], lease)
            identity = self._register_invocation(job, lease, "shared-dispatch-id")
            settle_factory_execution(
                self.conn,
                job["job_id"],
                lease,
                effect_observed=False,
                failure_details={
                    **identity,
                    "failure_domain": "executor_backend",
                    "failure_stage": "backend_execution",
                    "reason_code": "hermes_gateway_failed",
                    "retryable": None,
                    "failure_disposition": "recovery_required",
                    "operator_intervention_required": True,
                    "effect_outcome": "unknown",
                    "elapsed_ms": index * 10,
                },
            )

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["coverage"]["cohort_attempts"], 2)
        self.assertEqual(report["backends"][0]["invocations"], 2)
        self.assertEqual(report["backends"][0]["failed_invocations"], 2)
        self.assertEqual(report["failures"]["backend_invocations"]["failed"], 2)
        self.assertEqual(
            report["failures"]["backend_invocations"]["by_classification"][0]["count"],
            2,
        )

    def test_primary_effect_commit_binds_repository_branch_and_git_sha(self) -> None:
        _attempt, job, _ = self._reserve("ENG-57")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(
            self.conn, job["job_id"], owner="executor-primary-commit"
        )["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "primary-bound-receipt")
        receipt = {
            "commit_sha": "abcdef1",
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/57",
            "branch": job["branch"],
        }
        for mutation in (
            {"pr_url": "https://github.com/Other-Org/other/pull/57"},
            {"branch": "codex/foreign-branch"},
            {"commit_sha": "not-a-git-sha"},
            {"branch": None},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(BridgeValidationError):
                commit_factory_execution(
                    self.conn,
                    job["job_id"],
                    lease,
                    {**receipt, **mutation},
                )
            self.assertEqual(get_job(self.conn, job["job_id"])["state"], "running")

        with self.assertRaises(BridgeConflict):
            commit_factory_execution(
                self.conn,
                job["job_id"],
                lease,
                {
                    **receipt,
                    **identity,
                    "execution_transport": "local_command",
                    "backend_outcome": "created",
                    "backend_elapsed_ms": 10,
                },
            )
        committed = commit_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            {
                **receipt,
                **identity,
                "backend_outcome": "created",
                "backend_elapsed_ms": 10,
            },
        )
        self.assertEqual(committed["state"], "effect_committed")
        self.assertEqual(committed["commit_sha"], "abcdef1")

    def test_backend_failure_is_fenced_idempotent_and_preserves_classification(self) -> None:
        attempt, job, _ = self._reserve("ENG-42")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-b")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        record_factory_execution_analytics(
            self.conn,
            job["job_id"],
            lease,
            event_type="execution_backend_invoked",
            details=analytics_identity("dispatch-failure"),
        )
        failure = {
            **analytics_identity("dispatch-failure"),
            "failure_domain": "executor_backend",
            "failure_stage": "backend_execution",
            "reason_code": "hermes_grpc_gateway_failed",
            "retryable": False,
            "failure_disposition": "recovery_required",
            "operator_intervention_required": True,
            "effect_outcome": "unknown",
            "elapsed_ms": 250,
            "transport_status": None,
            "process_exit_code": 17,
            "process_signal": "SIGTERM",
            "timed_out": False,
            "buffer_exceeded": False,
        }
        settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False, failure_details=failure)
        settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False, failure_details=failure)
        rows = self.conn.execute(
            "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_backend_failed'",
            (attempt["attempt_id"],),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        persisted = json.loads(rows[0]["details_json"])
        self.assertEqual(persisted["failure_domain"], "executor_backend")
        self.assertEqual(persisted["process_exit_code"], 17)
        self.assertEqual(persisted["process_signal"], "SIGTERM")

        self.conn.execute(
            "UPDATE factory_attempts SET status='superseded' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.commit()
        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["backends"][0]["unknown_effect_attempts"], 1)

        changed = {**failure, "reason_code": "different"}
        with self.assertRaises(BridgeConflict):
            settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False, failure_details=changed)

        self.conn.execute(
            "UPDATE factory_attempts SET status='verified_ready',updated_at='2026-09-13T23:00:00Z' "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (
                str(uuid.uuid4()), attempt["attempt_id"], "attempt_verified_ready", "{}",
                "2026-09-13T23:00:00Z",
            ),
        )
        self.conn.commit()
        recovered = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(recovered["failures"]["attempts_with_failure_evidence"], 1)
        self.assertEqual(recovered["recovery"]["failed_attempts_eventually_verified"], 1)
        self.assertEqual(
            recovered["recovery"]["failure_boundaries_recovered_in_place_to_verified"],
            1,
        )

    def test_backend_invocation_requires_a_live_lease_but_completion_can_arrive_late(self) -> None:
        _attempt, job, _ = self._reserve("ENG-56")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(
            self.conn, job["job_id"], owner="executor-delayed"
        )["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = analytics_identity("delayed-invocation")
        self.conn.execute(
            "UPDATE factory_execution_leases SET expires_at='2026-01-01T00:00:00.000000Z' "
            "WHERE lease_id=?",
            (lease["lease_id"],),
        )
        self.conn.commit()
        with self.assertRaises(BridgeConflict):
            record_factory_execution_analytics(
                self.conn,
                job["job_id"],
                lease,
                event_type="execution_backend_invoked",
                details=identity,
            )

        # A backend that began while its lease was live must still be able to
        # append its terminal telemetry after a long provider call.
        self.conn.execute(
            "UPDATE factory_execution_leases SET expires_at='2999-01-01T00:00:00.000000Z' "
            "WHERE lease_id=?",
            (lease["lease_id"],),
        )
        self.conn.commit()
        self.assertTrue(record_factory_execution_analytics(
            self.conn,
            job["job_id"],
            lease,
            event_type="execution_backend_invoked",
            details=identity,
        ))
        self.conn.execute(
            "UPDATE factory_execution_leases SET expires_at='2026-01-01T00:00:00.000000Z' "
            "WHERE lease_id=?",
            (lease["lease_id"],),
        )
        self.conn.commit()
        self.assertTrue(record_factory_execution_analytics(
            self.conn,
            job["job_id"],
            lease,
            event_type="execution_backend_completed",
            details={**identity, "backend_outcome": "created", "elapsed_ms": 901_000},
        ))
        with self.assertRaises(BridgeValidationError):
            settle_factory_execution(
                self.conn,
                job["job_id"],
                lease,
                effect_observed=False,
                failure_event_type="execution_backend_completed",
                failure_details={
                    **identity,
                    "backend_outcome": "created",
                    "elapsed_ms": 901_000,
                },
            )

    def test_dispatch_failure_is_not_counted_as_a_backend_invocation(self) -> None:
        _attempt, job, _ = self._reserve("ENG-45")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-dispatch")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        failure = {
            **analytics_identity("dispatch-boundary"),
            "failure_domain": "executor",
            "failure_stage": "receipt_dispatch",
            "reason_code": "factory_execution_recovery_required",
            "retryable": None,
            "failure_disposition": "recovery_required",
            "operator_intervention_required": True,
            "effect_outcome": "unknown",
            "elapsed_ms": 25,
        }
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_details=failure,
            failure_event_type="execution_dispatch_failed",
        )

        _preauth_attempt, preauth_job, _ = self._reserve("ENG-46")
        transition_job(self.conn, preauth_job["job_id"], state="running")
        preauth_lease = claim_factory_execution(
            self.conn, preauth_job["job_id"], owner="executor-preauth"
        )["lease"]
        settle_factory_execution(
            self.conn,
            preauth_job["job_id"],
            preauth_lease,
            effect_observed=False,
            failure_details={**failure, "invocation_id": "preauth-boundary"},
            failure_event_type="execution_dispatch_failed",
        )

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["coverage"]["execution_claimed_attempts"], 2)
        self.assertEqual(report["coverage"]["effect_authorized_attempts"], 1)
        self.assertEqual(report["coverage"]["backend_attribution"]["eligible"], 1)
        self.assertEqual(report["coverage"]["backend_attribution"]["observed"], 0)
        self.assertEqual(report["failures"]["backend_invocations"]["failed"], 0)
        self.assertEqual(report["failures"]["dispatch_boundaries"]["failed"], 2)
        self.assertEqual(report["backends"], [])

    def test_backend_completion_and_effect_commit_failure_are_distinct_durable_phases(self) -> None:
        attempt, job, _ = self._reserve("ENG-47")
        accepted_outbox_key = f"factory:accepted:{attempt['attempt_id']}"
        accepted_delivery = claim_factory_outbox_delivery(self.conn, accepted_outbox_key)
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-commit")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = analytics_identity("commit-boundary")
        record_factory_execution_analytics(
            self.conn, job["job_id"], lease,
            event_type="execution_backend_invoked", details=identity,
        )
        record_factory_execution_analytics(
            self.conn, job["job_id"], lease,
            event_type="execution_backend_completed",
            details={**identity, "backend_outcome": "created", "elapsed_ms": 500},
        )
        commit_failure = {
            **identity,
            "failure_domain": "factory",
            "failure_stage": "effect_commit",
            "reason_code": "factory_effect_commit_failed",
            "retryable": None,
            "failure_disposition": "recovery_required",
            "operator_intervention_required": True,
            "effect_outcome": "observed",
            "elapsed_ms": 510,
            "observed_pr_url": "https://github.com/Driftward-LLC/capykit/pull/47",
            "observed_commit_sha": None,
            "observed_branch": job["branch"],
            "observed_hermes_session_id": None,
            "backend_outcome": "created",
            "verification_context": None,
        }
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_details=commit_failure,
            failure_event_type="execution_effect_commit_failed",
        )
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_details=commit_failure,
            failure_event_type="execution_effect_commit_failed",
        )
        events = self.conn.execute(
            "SELECT event_type, details_json FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type IN ('execution_backend_completed','execution_effect_commit_failed') "
            "ORDER BY created_at, event_id",
            (attempt["attempt_id"],),
        ).fetchall()
        self.assertEqual({row["event_type"] for row in events}, {
            "execution_backend_completed", "execution_effect_commit_failed",
        })
        details_by_type = {row["event_type"]: json.loads(row["details_json"]) for row in events}
        self.assertEqual(
            details_by_type["execution_effect_commit_failed"]["observed_pr_url"],
            "https://github.com/Driftward-LLC/capykit/pull/47",
        )
        observed_at = self.conn.execute(
            "SELECT effect_observed_at FROM factory_execution_leases WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()["effect_observed_at"]
        self.assertIsNotNone(observed_at)
        unresolved = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(unresolved["safety"]["observed_receipt_commit_recovery_incidents"], 1)
        self.assertEqual(unresolved["safety"]["unresolved_observed_receipts"], 1)
        self.assertEqual(
            unresolved["delivery"]["factory_inspection_outbox"]["cohort_records"],
            1,
        )

        inspection = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertIsNotNone(inspection)
        complete_pr_inspection(
            self.conn,
            inspection["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 47,
                "url": "https://github.com/Driftward-LLC/capykit/pull/47",
                "state": "unknown",
                "merged": False,
                "draft": False,
                "head_branch": None,
                "head_sha": None,
                "base_ref": None,
                "reason": "inspect_unavailable",
            },
            request_key=inspection["request_key"],
            capability=inspection["capability"],
        )
        refreshed = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? AND state='pending'",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertIsNotNone(refreshed)
        self.assertNotEqual(refreshed["request_key"], inspection["request_key"])
        complete_pr_inspection(
            self.conn,
            refreshed["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 47,
                "url": "https://github.com/Driftward-LLC/capykit/pull/47",
                "state": "open",
                "merged": False,
                "draft": True,
                "head_branch": job["branch"],
                "head_sha": "abcdef1",
                "base_ref": "main",
                "reason": "ok",
            },
            request_key=refreshed["request_key"],
            capability=refreshed["capability"],
        )
        self.assertEqual(get_job(self.conn, job["job_id"])["state"], "effect_committed")
        self.assertIsNone(get_job(self.conn, job["job_id"])["hermes_session_id"])
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "effect_committed")
        recovered_lease = self.conn.execute(
            "SELECT released_at,effect_observed_at FROM factory_execution_leases WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertIsNotNone(recovered_lease["released_at"])
        self.assertIsNotNone(recovered_lease["effect_observed_at"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? "
                "AND event_type='execution_effect_commit_recovered'",
                (attempt["attempt_id"],),
            ).fetchone()[0],
            1,
        )
        # A late ACK from the pre-recovery delivery cannot consume the rearmed
        # wake because acknowledgements are bound to the delivery lease.
        acknowledge_factory_outbox(
            self.conn, accepted_outbox_key, accepted_delivery["delivery_lease_id"]
        )
        wake = self.conn.execute(
            "SELECT state,acknowledged_at FROM factory_attempt_outbox WHERE outbox_key=?",
            (accepted_outbox_key,),
        ).fetchone()
        self.assertEqual(wake["state"], "pending")
        self.assertIsNone(wake["acknowledged_at"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? "
                "AND event_type='execution_effect_commit_recovery_wake_enqueued'",
                (attempt["attempt_id"],),
            ).fetchone()[0],
            1,
        )

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["failures"]["backend_invocations"]["failed"], 0)
        self.assertEqual(report["failures"]["effect_commit_boundaries"]["failed"], 1)
        self.assertEqual(report["safety"]["observed_receipt_commit_recovery_incidents"], 1)
        self.assertEqual(report["safety"]["unresolved_observed_receipts"], 0)
        self.assertEqual(report["safety"]["observed_receipt_recovery_exhausted"], 0)
        self.assertEqual(report["safety"]["unknown_effect_outcomes"], 0)
        self.assertEqual(report["backends"][0]["unknown_effect_attempts"], 0)
        self.assertEqual(report["backends"][0]["observed_receipt_recovery_attempts"], 1)
        self.assertEqual(report["backends"][0]["completed_invocation_latency_ms"]["samples"], 1)

        self.conn.execute(
            "DELETE FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_backend_completed'",
            (attempt["attempt_id"],),
        )
        self.conn.commit()
        fallback = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(fallback["backends"][0]["completed_invocations"], 1)
        self.assertEqual(fallback["backends"][0]["completed_invocation_latency_ms"]["p50"], 510)

    def test_effect_commit_receipt_survives_malformed_verification_context(self) -> None:
        attempt, job, _ = self._reserve("ENG-48")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-commit")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-with-sha")
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": "https://github.com/Driftward-LLC/capykit/pull/48",
                "observed_commit_sha": "abcdef1",
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": {"untrusted": "shape"},
            },
        )
        recovered = get_job(self.conn, job["job_id"])
        self.assertEqual(recovered["state"], "effect_committed")
        self.assertEqual(recovered["commit_sha"], "abcdef1")
        self.assertIsNone(recovered["hermes_session_id"])
        stored = self.conn.execute(
            "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type='execution_effect_commit_failed'",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertIsNone(json.loads(stored["details_json"])["verification_context"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0],
            0,
        )

    def test_effect_commit_recovery_rejects_unbound_receipts_and_bounds_bad_sha(self) -> None:
        mutations = (
            ("ENG-52", {"observed_branch": "codex/foreign-branch"}),
            ("ENG-53", {"observed_pr_url": "https://github.com/Other-Org/other/pull/53"}),
            ("ENG-54", {"observed_commit_sha": "not-a-git-sha"}),
            ("ENG-55", {
                "observed_pr_url": "https://github.com/Other-Org/other/pull/55",
                "observed_commit_sha": None,
            }),
        )
        for issue, mutation in mutations:
            with self.subTest(mutation=mutation):
                attempt, job, _ = self._reserve(issue)
                transition_job(self.conn, job["job_id"], state="running")
                lease = claim_factory_execution(
                    self.conn, job["job_id"], owner="executor-commit"
                )["lease"]
                authorize_factory_effect(self.conn, job["job_id"], lease)
                identity = self._register_invocation(
                    job, lease, f"unbound-receipt-{issue}"
                )
                receipt = {
                    **identity,
                    "failure_domain": "factory",
                    "failure_stage": "effect_commit",
                    "reason_code": "factory_effect_commit_failed",
                    "retryable": None,
                    "failure_disposition": "recovery_required",
                    "operator_intervention_required": True,
                    "effect_outcome": "observed",
                    "elapsed_ms": 25,
                    "observed_pr_url": (
                        f"https://github.com/Driftward-LLC/capykit/pull/{issue[4:]}"
                    ),
                    "observed_commit_sha": "abcdef1",
                    "observed_branch": job["branch"],
                    "observed_hermes_session_id": None,
                    "backend_outcome": "created",
                    "verification_context": None,
                }
                settle_factory_execution(
                    self.conn,
                    job["job_id"],
                    lease,
                    effect_observed=False,
                    failure_event_type="execution_effect_commit_failed",
                    failure_details={**receipt, **mutation},
                )
                self.assertEqual(get_job(self.conn, job["job_id"])["state"], "running")
                self.assertIsNone(get_job(self.conn, job["job_id"])["commit_sha"])
                self.assertEqual(
                    get_factory_attempt(self.conn, attempt["attempt_id"])["status"],
                    "effect_outcome_unknown",
                )
                stored_lease = self.conn.execute(
                    "SELECT released_at,effect_observed_at FROM factory_execution_leases "
                    "WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                ).fetchone()
                self.assertIsNone(stored_lease["released_at"])
                self.assertIsNotNone(stored_lease["effect_observed_at"])
                event_counts = dict(self.conn.execute(
                    "SELECT event_type,COUNT(*) AS count FROM factory_attempt_events "
                    "WHERE attempt_id=? AND event_type IN "
                    "('execution_effect_commit_failed','execution_effect_commit_receipt_rejected') "
                    "GROUP BY event_type",
                    (attempt["attempt_id"],),
                ))
                expected_events = {"execution_effect_commit_failed": 1}
                if issue != "ENG-54":
                    expected_events["execution_effect_commit_receipt_rejected"] = 1
                self.assertEqual(event_counts, expected_events)
                inspection_count = self.conn.execute(
                    "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
                    (attempt["attempt_id"],),
                ).fetchone()[0]
                self.assertEqual(inspection_count, 1 if issue == "ENG-54" else 0)
                if issue == "ENG-54":
                    stored_failure = self.conn.execute(
                        "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? "
                        "AND event_type='execution_effect_commit_failed'",
                        (attempt["attempt_id"],),
                    ).fetchone()
                    self.assertIsNone(
                        json.loads(stored_failure["details_json"])["observed_commit_sha"]
                    )

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["safety"]["rejected_observed_receipts"], 3)
        self.assertEqual(report["safety"]["unresolved_observed_receipts"], 4)

    def test_effect_commit_inspection_refresh_is_bounded_and_escalated(self) -> None:
        attempt, job, _ = self._reserve("ENG-49")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-commit")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-needs-inspection")
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": "https://github.com/Driftward-LLC/capykit/pull/49",
                "observed_commit_sha": None,
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        unavailable = {
            "repository": "Driftward-LLC/capykit",
            "number": 49,
            "url": "https://github.com/Driftward-LLC/capykit/pull/49",
            "state": "unknown",
            "merged": False,
            "draft": False,
            "head_branch": None,
            "head_sha": None,
            "base_ref": None,
            "reason": "inspect_unavailable",
        }
        for _ in range(3):
            request = self.conn.execute(
                "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? "
                "AND state='pending' ORDER BY created_at,request_key LIMIT 1",
                (attempt["attempt_id"],),
            ).fetchone()
            self.assertIsNotNone(request)
            complete_pr_inspection(
                self.conn,
                request["job_id"],
                unavailable,
                request_key=request["request_key"],
                capability=request["capability"],
            )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM factory_inspection_requests WHERE prior_attempt_id=? AND state='pending'",
            (attempt["attempt_id"],),
        ).fetchone())
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "effect_outcome_unknown")
        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["safety"]["unresolved_observed_receipts"], 1)
        self.assertEqual(report["safety"]["observed_receipt_recovery_exhausted"], 1)
        self.assertEqual(report["human"]["exception_intervention_attempts"], 1)
        refreshed = refresh_factory_effect_receipt_inspection(
            self.conn,
            attempt["attempt_id"],
            reason="operator reviewed exhausted inspection evidence",
            actor="uid:0",
        )
        self.assertTrue(refreshed["created"])
        request = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? AND state='pending'",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(refreshed["inspection"], {
            "request_key": request["request_key"],
            "state": "pending",
        })
        refresh_event = self.conn.execute(
            "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type='execution_effect_commit_recovery_refreshed'",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(
            json.loads(refresh_event["details_json"])["inspection_request_key"],
            request["request_key"],
        )
        complete_pr_inspection(
            self.conn,
            request["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 49,
                "url": "https://github.com/Driftward-LLC/capykit/pull/49",
                "state": "open",
                "merged": False,
                "draft": True,
                "head_branch": job["branch"],
                "head_sha": "abcdef1",
                "base_ref": "main",
                "reason": "ok",
            },
            request_key=request["request_key"],
            capability=request["capability"],
        )
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "effect_committed")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type='execution_effect_commit_recovery_refreshed'",
            (attempt["attempt_id"],),
        ).fetchone()[0], 1)

    def test_effect_commit_inspection_timeouts_refresh_then_exhaust(self) -> None:
        attempt, job, _ = self._reserve("ENG-59")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(
            self.conn, job["job_id"], owner="executor-inspection-timeout"
        )["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-inspection-timeout")
        url = "https://github.com/Driftward-LLC/capykit/pull/59"
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": url,
                "observed_commit_sha": None,
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        irrelevant, _irrelevant_job, _ = self._reserve("ENG-60")
        # More than one dispatcher batch of older, unrelated inspections must
        # not starve the eligible observed-receipt timeout.
        for number in range(100, 151):
            self.conn.execute(
                "INSERT INTO factory_inspection_requests "
                "(request_key,repository,number,url,job_id,work_item_key,prior_attempt_id,capability,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"irrelevant-{number}", "Driftward-LLC/capykit", number,
                    f"https://github.com/Driftward-LLC/capykit/pull/{number}", None,
                    irrelevant["work_item_key"], irrelevant["attempt_id"], str(uuid.uuid4()),
                    "pending", "2019-01-01T00:00:00.000000Z", "2019-01-01T00:00:00.000000Z",
                ),
            )
        self.conn.commit()
        summaries = []
        for _ in range(3):
            request = self.conn.execute(
                "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? "
                "AND state='pending' ORDER BY created_at DESC,request_key DESC LIMIT 1",
                (attempt["attempt_id"],),
            ).fetchone()
            self.assertIsNotNone(request)
            self.conn.execute(
                "UPDATE factory_inspection_requests SET created_at='2020-01-01T00:00:00.000000Z' "
                "WHERE request_key=?",
                (request["request_key"],),
            )
            self.conn.commit()
            summaries.append(sweep_factory_effect_receipt_inspections(self.conn, 50))

        self.assertEqual(summaries, [
            {"expired": 1, "refreshed": 1, "exhausted": 0},
            {"expired": 1, "refreshed": 1, "exhausted": 0},
            {"expired": 1, "refreshed": 0, "exhausted": 1},
        ])

        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0], 3)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM factory_inspection_requests WHERE prior_attempt_id=? AND state='pending'",
            (attempt["attempt_id"],),
        ).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_attempt_events WHERE attempt_id=? "
            "AND event_type='execution_effect_commit_recovery_exhausted'",
            (attempt["attempt_id"],),
        ).fetchone()[0], 1)

    def test_effect_commit_settlement_consumes_fresh_cached_inspection(self) -> None:
        attempt, job, _ = self._reserve("ENG-50")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-commit")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-cached-inspection")
        url = "https://github.com/Driftward-LLC/capykit/pull/50"
        _queue_pr_inspection(self.conn, "ENG-50", url, attempt["attempt_id"])
        cached = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        complete_pr_inspection(
            self.conn,
            cached["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 50,
                "url": url,
                "state": "open",
                "merged": False,
                "draft": True,
                "head_branch": job["branch"],
                "head_sha": "abcdef1",
                "base_ref": "main",
                "reason": "ok",
            },
            request_key=cached["request_key"],
            capability=cached["capability"],
        )
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": url,
                "observed_commit_sha": None,
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        recovered = get_job(self.conn, job["job_id"])
        self.assertEqual(recovered["state"], "effect_committed")
        self.assertEqual(recovered["commit_sha"], "abcdef1")
        self.assertIsNone(recovered["hermes_session_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0], 1)

    def test_effect_commit_settlement_never_consumes_stale_cached_inspection(self) -> None:
        attempt, job, _ = self._reserve("ENG-58")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(
            self.conn, job["job_id"], owner="executor-stale-inspection"
        )["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-stale-inspection")
        url = "https://github.com/Driftward-LLC/capykit/pull/58"
        _queue_pr_inspection(self.conn, "ENG-58", url, attempt["attempt_id"])
        cached = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        complete_pr_inspection(
            self.conn,
            cached["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 58,
                "url": url,
                "state": "open",
                "merged": False,
                "draft": True,
                "head_branch": job["branch"],
                "head_sha": "abcdef1",
                "base_ref": "main",
                "reason": "ok",
            },
            request_key=cached["request_key"],
            capability=cached["capability"],
        )
        self.conn.execute(
            "UPDATE factory_inspection_requests SET observed_at='2020-01-01T00:00:00.000000Z' "
            "WHERE request_key=?",
            (cached["request_key"],),
        )
        self.conn.commit()
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": url,
                "observed_commit_sha": None,
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        self.assertEqual(get_job(self.conn, job["job_id"])["state"], "running")
        self.assertEqual(
            get_factory_attempt(self.conn, attempt["attempt_id"])["status"],
            "effect_outcome_unknown",
        )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0], 2)
        newest = self.conn.execute(
            "SELECT state FROM factory_inspection_requests WHERE prior_attempt_id=? "
            "ORDER BY created_at DESC,request_key DESC LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        self.assertEqual(newest["state"], "pending")

    def test_observed_receipt_recovery_preserves_post_authorization_cancellation(self) -> None:
        attempt, job, _ = self._reserve("ENG-51")
        transition_job(self.conn, job["job_id"], state="running")
        lease = claim_factory_execution(self.conn, job["job_id"], owner="executor-commit")["lease"]
        authorize_factory_effect(self.conn, job["job_id"], lease)
        identity = self._register_invocation(job, lease, "commit-after-cancel")
        cancel_factory_attempt(
            self.conn,
            attempt["attempt_id"],
            reason="operator cancellation during execution",
            actor="uid:0",
        )
        settle_factory_execution(
            self.conn,
            job["job_id"],
            lease,
            effect_observed=False,
            failure_event_type="execution_effect_commit_failed",
            failure_details={
                **identity,
                "failure_domain": "factory",
                "failure_stage": "effect_commit",
                "reason_code": "factory_effect_commit_failed",
                "retryable": None,
                "failure_disposition": "recovery_required",
                "operator_intervention_required": True,
                "effect_outcome": "observed",
                "elapsed_ms": 25,
                "observed_pr_url": "https://github.com/Driftward-LLC/capykit/pull/51",
                "observed_commit_sha": None,
                "observed_branch": job["branch"],
                "observed_hermes_session_id": None,
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "cancellation_requested")
        request = self.conn.execute(
            "SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=? AND state='pending'",
            (attempt["attempt_id"],),
        ).fetchone()
        complete_pr_inspection(
            self.conn,
            request["job_id"],
            {
                "repository": "Driftward-LLC/capykit",
                "number": 51,
                "url": "https://github.com/Driftward-LLC/capykit/pull/51",
                "state": "open",
                "merged": False,
                "draft": True,
                "head_branch": job["branch"],
                "head_sha": "abcdef1",
                "base_ref": "main",
                "reason": "ok",
            },
            request_key=request["request_key"],
            capability=request["capability"],
        )
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "canceled_after_effect")
        self.assertEqual(get_job(self.conn, job["job_id"])["state"], "effect_committed")

    def test_ready_events_correlate_exactly_to_created_and_reused_attempts(self) -> None:
        created_attempt, _created_job, _ = self._reserve("ENG-70")
        reused_attempt, _reused_job, _ = self._reserve("ENG-71")
        reused_key = "linear:issue:ENG-71:update:two"
        replayed, _job, created = reserve_factory_intake(
            self.conn,
            work_item_key="ENG-71",
            mode="real",
            request_id=reused_key,
            reason="Linear Ready intake reserved a factory attempt",
            actor="intake-worker",
            payload={
                **verifier_payload(),
                "linear_issue": "ENG-71",
                "source_event_key": reused_key,
            },
        )
        self.assertFalse(created)
        self.assertEqual(replayed["attempt_id"], reused_attempt["attempt_id"])
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-10T01:00:00Z' WHERE attempt_id=?",
            (created_attempt["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-11T01:00:00Z' WHERE attempt_id=?",
            (reused_attempt["attempt_id"],),
        )
        self.conn.commit()

        ingress = sqlite3.connect(self.ingress_db)
        for definition in (
            "event_key TEXT", "provider_event TEXT", "action TEXT", "updated_at TEXT",
            "published_at TEXT", "publish_attempts INTEGER", "publish_last_error TEXT",
        ):
            ingress.execute(f"ALTER TABLE ingress_events ADD COLUMN {definition}")
        created_key = "linear:issue:ENG-70:update:one"
        provider_at = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp() * 1_000_000)
        ingress.execute(
            "UPDATE ingress_events SET event_key=?,subject='ENG-70',provider_event='issue',action='update',"
            "updated_at=created_at,published_at=created_at,publish_attempts=1,provider_updated_at_us=? "
            "WHERE event_id='linear-1'",
            (created_key, provider_at),
        )
        ingress.execute(
            "INSERT INTO ingress_events "
            "(event_id,event_key,source,provider_event,action,subject,normalized_json,decision,reason_code,"
            "hatchet_published,created_at,updated_at,published_at,publish_attempts,provider_updated_at_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "linear-2", reused_key, "linear", "issue", "update", "ENG-71", "{}",
                "ready_for_implementation", "linear_issue_ready_for_implementation", 1,
                "2026-09-12T00:00:00Z", "2026-09-12T00:00:01Z",
                "2026-09-12T00:00:01Z", 2,
                int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events "
            "(event_id,event_key,source,provider_event,action,subject,normalized_json,decision,reason_code,"
            "hatchet_published,created_at,updated_at,published_at,publish_attempts,provider_updated_at_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "linear-unlinked", "linear:issue:ENG-71:update:unlinked", "linear",
                "issue", "update", "ENG-71", "{}", "ready_for_implementation",
                "linear_issue_ready_for_implementation", 1, "2026-09-11T12:00:00Z",
                "2026-09-11T12:00:01Z", "2026-09-11T12:00:01Z", 1,
                int(datetime(2026, 9, 11, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events "
            "(event_id,event_key,source,provider_event,action,subject,normalized_json,decision,reason_code,"
            "hatchet_published,created_at,updated_at,published_at,publish_attempts,provider_updated_at_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "linear-missing-attempt", "linear:issue:ENG-404:update:one", "linear",
                "issue", "update", "ENG-404", "{}", "ready_for_implementation",
                "linear_issue_ready_for_implementation", 1, "2026-09-12T12:00:00Z",
                "2026-09-12T12:00:01Z", "2026-09-12T12:00:01Z", 1,
                int(datetime(2026, 9, 12, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {
                "linear-1": created_key,
                "linear-2": reused_key,
                "linear-unlinked": "linear:issue:ENG-71:update:unlinked",
                "linear-missing-attempt": "linear:issue:ENG-404:update:one",
            },
            fallback_timestamp_events={"linear-1"},
        )
        ingress.execute(
            "INSERT INTO ingress_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "receipt-duplicate", created_key, "linear", "issue", None,
                "payload:linear-1", 1, "duplicate", "2026-09-10T00:00:01Z",
            ),
        )
        ingress.commit(); ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        correlation = report["intake"]["correlation"]
        self.assertEqual(correlation["by_outcome"], {
            "active_attempt_reused": 1,
            "real_attempt_created": 1,
            "unlinked": 2,
        })
        self.assertEqual(correlation["eligible_to_real_attempt"]["rate"], 0.5)
        self.assertEqual(correlation["eligible_work_item_to_real_attempt"]["rate"], 0.6667)
        self.assertEqual(report["intake"]["authenticated_repeated_eligible_events"], 1)
        self.assertEqual(report["intake"]["repeated_eligible_event_rate"], 0.25)
        self.assertEqual(correlation["active_attempt_reuse_rate"], 0.25)
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["real_attempt"],
            {"observed": 2, "eligible": 3, "rate": 0.6667, "complete": False},
        )
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["right_censored_work_items"],
            3,
        )
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["merge_outcome_evidence"],
            {"observed": 0, "eligible": 3, "rate": 0.0, "complete": False},
        )
        self.assertIsNone(
            correlation["work_item_conversion"]["all"]["merged"]["rate"]
        )
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["missing_correlation_evidence_work_items"],
            1,
        )
        self.assertEqual(
            correlation["latency_ms"]["ready_received_to_attempt_reserved"]["samples"],
            1,
        )
        self.assertEqual(
            correlation["latency_ms"]["provider_ready_to_attempt_reserved"],
            {"samples": 0, "p50": None, "p95": None, "eligible": 1, "coverage_rate": 0.0},
        )
        self.assertIsNone(report["intake"]["receipts"]["redelivery_rate"])
        self.assertEqual(report["intake"]["receipts"]["observed_receipt_redelivery_rate"], 0.2)
        self.assertFalse(report["intake"]["receipts"]["full_window_rate_available"])
        self.assertEqual(
            report["intake"]["receipts"]["event_receipt_coverage"],
            {"observed": 4, "eligible": 4, "rate": 1.0, "complete": True},
        )
        self.assertEqual(
            report["intake"]["receipts"]["authenticated_event_coverage"],
            {"observed": 4, "eligible": 4, "rate": 1.0, "complete": True},
        )
        self.assertEqual(report["intake"]["unverified_eligible_events"], 0)
        self.assertEqual(report["delivery"]["ingress_publication"]["retried_events"], 1)
        self.assertEqual(report["sources"]["ingress"]["schema_contract"], "ingress-schema-v3")
        self.assertEqual(
            correlation["work_item_conversion"]["maturity"]["basis"],
            "age_qualified_cohort_with_current_outcome_observed_through_report_cutoff",
        )

    def test_retry_lineage_recovers_a_failed_ancestor(self) -> None:
        failed_attempt, failed_job, _ = self._reserve("ENG-72")
        transition_job(self.conn, failed_job["job_id"], state="running")
        transition_job(self.conn, failed_job["job_id"], state="failed", details={
            "reason_code": "provider_temporarily_unavailable",
            "failure_domain": "provider",
            "failure_stage": "model_request",
            "retryable": True,
            "failure_disposition": "terminal",
            "operator_intervention_required": False,
        })
        retry, retry_job, created = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-72",
            mode="real",
            request_id="operator:retry:ENG-72:one",
            reason="retry after provider failure",
            actor="uid:0",
            payload=verifier_payload(),
            prior_attempt_id=failed_attempt["attempt_id"],
        )
        self.assertTrue(created)
        self.conn.executemany(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (
                (
                    str(uuid.uuid4()), failed_attempt["attempt_id"],
                    "execution_route_selected",
                    json.dumps(route_identity(failed_attempt["attempt_id"])),
                    "2026-09-10T01:00:00Z",
                ),
                (
                    str(uuid.uuid4()), retry["attempt_id"], "execution_route_selected",
                    json.dumps(route_identity(
                        retry["attempt_id"], variant="codex_canary",
                        backend="codex_cli", transport="local_command",
                    )),
                    "2026-09-11T01:00:00Z",
                ),
            ),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-10T00:00:00Z' WHERE attempt_id=?",
            (failed_attempt["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-11T00:00:00Z',"
            "status='verified_ready',updated_at='2026-09-12T00:00:00Z' WHERE attempt_id=?",
            (retry["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_jobs SET pr_url='https://github.com/Driftward-LLC/capykit/pull/72',"
            "updated_at='2026-09-11T01:00:00Z' "
            "WHERE job_id=?",
            (retry_job["job_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_events SET created_at='2026-09-10T12:00:00Z' "
            "WHERE job_id=? AND event_type='transition:failed'",
            (failed_job["job_id"],),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), retry["attempt_id"], "attempt_verified_ready", "{}", "2026-09-12T00:00:00Z"),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-merged-72", "github", "Driftward-LLC/capykit#72",
                json.dumps({
                    "prUrl": "https://github.com/Driftward-LLC/capykit/pull/72",
                    "state": "closed", "merged": True,
                }),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T12:00:00Z",
                int(datetime(2026, 9, 12, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {"github-merged-72": "github:pull_request:72:merged"},
        )
        ingress.commit(); ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["failures"]["recovered_failure_attempts"], 1)
        self.assertEqual(report["recovery"]["failed_attempts_eventually_verified"], 1)
        self.assertEqual(report["recovery"]["unrecovered_failed_attempts"], 0)
        self.assertEqual(report["recovery"]["attempt_depth_to_verified"]["p50"], 1)
        self.assertEqual(report["recovery"]["failure_to_verified_ready_ms"]["samples"], 1)
        self.assertEqual(report["routes"]["lineage"]["route_pair_coverage"]["observed"], 1)
        self.assertEqual(report["routes"]["lineage"]["variant_crossovers"], 1)
        self.assertEqual(report["routes"]["lineage"]["verified_after_variant_crossover"], 1)
        self.assertEqual(report["routes"]["lineage"]["by_variant_transition"], [{
            "from_variant": "hermes", "to_variant": "codex_canary", "count": 1,
        }])
        repair_efficiency = report["recovery"]["repair_efficiency_on_authenticated_merged_prs"]
        self.assertEqual(
            repair_efficiency["grain"],
            "unique_authenticated_current_merged_pr_url",
        )
        self.assertEqual(repair_efficiency["observed_merged_prs"], 1)
        self.assertEqual(
            repair_efficiency["merged_pr_lineage_coverage"],
            {"observed": 1, "eligible": 1, "rate": 1.0, "complete": True},
        )
        self.assertEqual(repair_efficiency["observed_merged_work_items"], 1)
        self.assertEqual(repair_efficiency["lineage_attempts"], 2)
        self.assertEqual(repair_efficiency["attempts_per_merged_pr"], 2.0)
        self.assertEqual(repair_efficiency["retry_and_repair_attempts_per_merged_pr"], 1.0)
        self.assertEqual(report["github"]["maturity_origin"]["coverage"]["observed"], 1)
        self.assertEqual(report["github"]["maturity"]["at_24h"]["eligible_prs"], 1)

    def test_recovery_rate_uses_age_qualified_fixed_horizon(self) -> None:
        classification = {
            "reason_code": "provider_temporarily_unavailable",
            "failure_domain": "provider",
            "failure_stage": "model_request",
            "retryable": True,
            "failure_disposition": "terminal",
            "operator_intervention_required": False,
        }

        def failed_attempt(issue: str):
            attempt, job, _created = self._reserve(issue)
            transition_job(self.conn, job["job_id"], state="running")
            transition_job(
                self.conn, job["job_id"], state="failed", details=classification
            )
            return attempt, job

        recovered_source, _ = failed_attempt("ENG-RECOVERED")
        terminal_source, _ = failed_attempt("ENG-TERMINAL")
        active_source, _ = failed_attempt("ENG-ACTIVE")
        recovered_retry, _job, _created = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-RECOVERED",
            mode="real",
            request_id="operator:retry:ENG-RECOVERED:one",
            reason="retry recovered inside seven days",
            actor="uid:0",
            payload=verifier_payload(),
            prior_attempt_id=recovered_source["attempt_id"],
        )
        active_retry, _job, _created = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-ACTIVE",
            mode="real",
            request_id="operator:retry:ENG-ACTIVE:one",
            reason="retry remains active",
            actor="uid:0",
            payload=verifier_payload(),
            prior_attempt_id=active_source["attempt_id"],
        )
        source_ids = (
            recovered_source["attempt_id"], terminal_source["attempt_id"],
            active_source["attempt_id"],
        )
        self.conn.executemany(
            "UPDATE factory_attempts SET created_at='2026-09-09T00:00:00Z',"
            "updated_at='2026-09-10T00:00:00Z' WHERE attempt_id=?",
            ((attempt_id,) for attempt_id in source_ids),
        )
        self.conn.executemany(
            "UPDATE factory_attempt_events SET created_at='2026-09-09T00:00:00Z' "
            "WHERE attempt_id=?",
            ((attempt_id,) for attempt_id in source_ids),
        )
        self.conn.executemany(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE attempt_id=? AND event_type='attempt_failed'",
            ((attempt_id,) for attempt_id in source_ids),
        )
        self.conn.executemany(
            "UPDATE bridge_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE job_id=? AND event_type='transition:failed'",
            (
                (attempt["job_id"],)
                for attempt in (recovered_source, terminal_source, active_source)
            ),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-10T01:00:00Z',"
            "updated_at='2026-09-11T00:00:00Z',status='verified_ready' "
            "WHERE attempt_id=?",
            (recovered_retry["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T01:00:00Z' "
            "WHERE attempt_id=?",
            (recovered_retry["attempt_id"],),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (
                str(uuid.uuid4()), recovered_retry["attempt_id"],
                "attempt_verified_ready", "{}", "2026-09-11T00:00:00Z",
            ),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-09-10T01:00:00Z',"
            "updated_at='2026-09-10T01:00:00Z' WHERE attempt_id=?",
            (active_retry["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T01:00:00Z' "
            "WHERE attempt_id=?",
            (active_retry["attempt_id"],),
        )
        self.conn.commit()

        immature = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )["recovery"]
        self.assertEqual(immature["failed_attempts_eventually_verified"], 1)
        self.assertEqual(immature["failed_attempts_verified_within_horizon"], 0)
        self.assertEqual(immature["settled_failed_attempts"], 0)
        self.assertIsNone(immature["failed_attempt_recovery_rate"])
        self.assertEqual(immature["right_censored_failed_attempts"], 3)
        self.assertEqual(immature["active_recovery_lineages"], 1)

        mature = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 18, 23, 59, tzinfo=timezone.utc),
        )["recovery"]
        self.assertEqual(mature["settled_failed_attempts"], 3)
        self.assertEqual(mature["failed_attempts_verified_within_horizon"], 1)
        self.assertEqual(mature["failed_attempt_recovery_rate"], 0.3333)
        self.assertEqual(
            mature["failed_attempt_recovery_outcome_coverage"],
            {"observed": 3, "eligible": 3, "rate": 1.0, "complete": True},
        )
        self.assertEqual(mature["right_censored_failed_attempts"], 0)
        self.assertEqual(mature["active_recovery_lineages"], 1)
        self.assertEqual(mature["failed_attempts_not_recovered_within_horizon"], 2)

    def test_activity_includes_events_for_attempts_created_before_the_cohort(self) -> None:
        attempt, _job, _ = self._reserve("ENG-73")
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-01-01T00:00:00Z',"
            "status='verified_ready',updated_at='2026-09-12T00:00:00Z' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-01-01T00:00:00Z' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), attempt["attempt_id"], "attempt_verified_ready", "{}", "2026-09-12T00:00:00Z"),
        )
        self.conn.commit()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["coverage"]["cohort_attempts"], 0)
        self.assertTrue(report["activity"]["independent_of_attempt_creation_cohort"])
        day = next(item for item in report["activity"]["daily"] if item["date"] == "2026-09-12")
        self.assertEqual(day["verified_ready"], 1)

    def test_activity_counts_each_active_intake_reuse_event(self) -> None:
        self._reserve("ENG-75")
        for suffix in ("two", "three"):
            reserve_factory_intake(
                self.conn,
                work_item_key="ENG-75",
                mode="real",
                request_id=f"linear:issue:ENG-75:update:{suffix}",
                reason="Linear Ready intake reserved a factory attempt",
                actor="intake-worker",
                payload={
                    **verifier_payload(),
                    "linear_issue": "ENG-75",
                    "source_event_key": f"linear:issue:ENG-75:update:{suffix}",
                },
            )

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        events = sum(
            int(day.get("active_attempt_reuses", 0))
            for day in report["activity"]["daily"]
        )
        self.assertEqual(events, 2)

    def test_ready_reuse_observes_pr_from_pre_window_attempt(self) -> None:
        attempt, job, _ = self._reserve("ENG-82")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/82"
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='2026-07-01T00:00:00Z',"
            "updated_at='2026-07-02T00:00:00Z' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-07-01T00:00:00Z' "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_jobs SET pr_url=?,updated_at='2026-07-02T00:00:00Z' "
            "WHERE job_id=?",
            (pr_url, job["job_id"]),
        )
        self.conn.commit()

        reused_key = "linear:issue:ENG-82:update:two"
        reused, _job, created = reserve_factory_intake(
            self.conn,
            work_item_key="ENG-82",
            mode="real",
            request_id=reused_key,
            reason="Linear Ready intake reserved a factory attempt",
            actor="intake-worker",
            payload={
                **verifier_payload(),
                "linear_issue": "ENG-82",
                "source_event_key": reused_key,
            },
        )
        self.assertFalse(created)
        self.assertEqual(reused["attempt_id"], attempt["attempt_id"])
        self.conn.execute(
            "UPDATE bridge_jobs SET updated_at='2026-07-02T00:00:00Z' WHERE job_id=?",
            (job["job_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=? WHERE attempt_id=?",
            (pr_url, attempt["attempt_id"]),
        )
        self.conn.commit()

        transition_job(self.conn, job["job_id"], state="running")
        transition_job(self.conn, job["job_id"], state="failed", details={
            "reason_code": "verification_evidence_unavailable",
            "failure_domain": "github",
            "failure_stage": "verification_checks",
            "retryable": True,
            "failure_disposition": "terminal",
            "operator_intervention_required": False,
        })
        late_repair, _repair_job, _created = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-82",
            mode="real",
            request_id="repair:ENG-82:after-merge",
            reason="repair associated after the already observed merge",
            actor="verification-repair",
            payload=verifier_payload(),
            prior_attempt_id=attempt["attempt_id"],
        )
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-09-13T00:00:00Z',"
            "updated_at='2026-09-13T00:00:00Z' WHERE attempt_id=?",
            (pr_url, late_repair["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-13T00:00:00Z' "
            "WHERE attempt_id=?",
            (late_repair["attempt_id"],),
        )
        self.conn.commit()

        ingress = sqlite3.connect(self.ingress_db)
        ready_at = datetime(2026, 9, 12, tzinfo=timezone.utc)
        merge_at = ready_at + timedelta(hours=2)
        ingress.execute(
            "UPDATE ingress_events SET subject='ENG-82',created_at=?,"
            "provider_updated_at_us=? WHERE event_id='linear-1'",
            (
                ready_at.isoformat().replace("+00:00", "Z"),
                int(ready_at.timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-merged-82", "github", "Driftward-LLC/capykit#82",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                merge_at.isoformat().replace("+00:00", "Z"),
                int(merge_at.timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {
                "linear-1": reused_key,
                "github-merged-82": "github:pull_request:82:merged",
            },
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["coverage"]["cohort_attempts"], 1)
        self.assertEqual(report["outcomes"]["merged_prs"], 0)
        self.assertEqual(report["github"]["pre_association_observations_excluded"], 1)
        correlation = report["intake"]["correlation"]
        self.assertEqual(correlation["by_outcome"], {"active_attempt_reused": 1})
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["merged"],
            {"observed": 1, "eligible": 1, "rate": 1.0, "complete": True},
        )
        self.assertEqual(
            correlation["work_item_conversion"]["all"]["github_unobserved_work_items"],
            0,
        )
        self.assertEqual(
            correlation["latency_ms"]["ready_received_to_merged"]["samples"],
            1,
        )

    def test_inherited_repair_pr_is_included_when_source_predates_cohort(self) -> None:
        source, source_job, _ = self._reserve("ENG-76")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/76"
        self.conn.execute(
            "UPDATE factory_attempts SET status='failed',created_at='2026-07-01T00:00:00Z',"
            "updated_at='2026-07-03T00:00:00Z' WHERE attempt_id=?",
            (source["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_jobs SET state='failed',pr_url=?,commit_sha='abc0076' WHERE job_id=?",
            (pr_url, source_job["job_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-07-01T00:00:00Z' "
            "WHERE attempt_id=?",
            (source["attempt_id"],),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)",
            (
                str(uuid.uuid4()), source["attempt_id"], "attempt_effect_committed",
                "{}", "2026-07-02T00:00:00Z",
            ),
        )
        self.conn.commit()
        repair, repair_job, _ = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-76",
            mode="real",
            request_id="repair:ENG-76:one",
            reason="repair existing draft",
            actor="verification-repair",
            payload={**verifier_payload(), "linear_issue": "ENG-76"},
            prior_attempt_id=source["attempt_id"],
        )
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-09-10T00:00:00Z',"
            "updated_at='2026-09-10T00:00:00Z' WHERE attempt_id=?",
            (pr_url, repair["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE attempt_id=?",
            (repair["attempt_id"],),
        )
        self.conn.commit()

        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-merged-76", "github", "Driftward-LLC/capykit#76",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z",
                int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {"github-merged-76": "github:pull_request:76:merged"},
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["coverage"]["cohort_attempts"], 1)
        self.assertEqual(report["funnel"]["stages"]["draft_pr_available"]["count"], 1)
        self.assertEqual(report["outcomes"]["merged_prs"], 1)
        self.assertEqual(report["github"]["maturity_origin"]["coverage"]["observed"], 1)
        efficiency = report["recovery"]["repair_efficiency_on_authenticated_merged_prs"]
        self.assertEqual(efficiency["work_items_with_pr"], 1)
        self.assertEqual(efficiency["lineage_attempts"], 2)
        self.assertEqual(efficiency["retry_and_repair_attempts"], 1)

    def test_post_merge_pr_association_does_not_seed_repair_efficiency(self) -> None:
        source, source_job, _ = self._reserve("ENG-81")
        old_unrelated, _old_job, _ = self._reserve("ENG-OLD-81")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/81"
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-07-01T00:00:00Z',"
            "updated_at='2026-07-01T00:00:00Z' WHERE attempt_id=?",
            (pr_url, old_unrelated["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-07-01T00:00:00Z' "
            "WHERE attempt_id=?",
            (old_unrelated["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempts SET status='failed',created_at='2026-09-10T00:00:00Z',"
            "updated_at='2026-09-10T01:00:00Z' WHERE attempt_id=?",
            (source["attempt_id"],),
        )
        self.conn.execute(
            "UPDATE bridge_jobs SET state='failed' WHERE job_id=?",
            (source_job["job_id"],),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE attempt_id=?",
            (source["attempt_id"],),
        )
        self.conn.commit()
        later, _later_job, _ = reserve_factory_retry(
            self.conn,
            work_item_key="ENG-81",
            mode="real",
            request_id="repair:ENG-81:after-merge",
            reason="late association",
            actor="verification-repair",
            payload={**verifier_payload(), "linear_issue": "ENG-81"},
            prior_attempt_id=source["attempt_id"],
        )
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-09-13T00:00:00Z',"
            "updated_at='2026-09-13T00:00:00Z' WHERE attempt_id=?",
            (pr_url, later["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-13T00:00:00Z' "
            "WHERE attempt_id=?",
            (later["attempt_id"],),
        )
        self.conn.commit()

        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-merged-81", "github", "Driftward-LLC/capykit#81",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z",
                int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {"github-merged-81": "github:pull_request:81:merged"},
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        efficiency = report["recovery"]["repair_efficiency_on_authenticated_merged_prs"]
        self.assertEqual(efficiency["observed_merged_prs"], 0)
        self.assertEqual(report["github"]["pre_association_observations_excluded"], 1)
        self.assertEqual(
            efficiency["merged_pr_lineage_coverage"],
            {"observed": 0, "eligible": 0, "rate": None, "complete": True},
        )
        self.assertEqual(efficiency["lineage_attempts"], 0)
        self.assertIsNone(efficiency["attempts_per_merged_pr"])
        self.assertIsNone(efficiency["repair_attempts_per_merged_pr"])

    def test_unjoinable_ingress_authenticity_makes_metrics_null_not_zero(self) -> None:
        _attempt, job, _ = self._reserve("ENG-77")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/77"
        self.conn.execute(
            "UPDATE bridge_jobs SET pr_url=? WHERE job_id=?",
            (pr_url, job["job_id"]),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-merged-77", "github", "Driftward-LLC/capykit#77",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z",
                int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "CREATE TABLE ingress_receipts ("
            "receipt_id TEXT PRIMARY KEY,event_key TEXT,source TEXT,provider_event TEXT,"
            "delivery_id TEXT,payload_hash TEXT,signature_verified INTEGER,"
            "disposition TEXT,received_at TEXT)"
        )
        ingress.execute(
            "INSERT INTO ingress_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "receipt-77", "legacy-unjoinable-key", "github", "pull_request",
                "delivery-77", "payload-77", 1, "accepted",
                "2026-09-12T00:00:00Z",
            ),
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertFalse(report["github"]["authenticated_outcomes_available"])
        self.assertTrue(report["intake"]["receipts"]["signature_provenance_available"])
        self.assertFalse(report["intake"]["receipts"]["event_key_join_available"])
        self.assertFalse(report["intake"]["receipts"]["canonical_authenticity_available"])
        self.assertIsNone(report["intake"]["authenticated_eligible_events"])
        self.assertIsNone(report["intake"]["eligible_event_authenticity"])
        self.assertIsNone(report["outcomes"]["merged_prs"])
        self.assertIsNone(report["outcomes"]["github_observation_coverage"])
        self.assertIsNone(report["github"]["reviews"]["observations"])
        self.assertIsNone(report["github"]["check_suite_observations"]["coverage"])
        self.assertEqual(
            report["github"]["unverified_observations"]["pull_request_state_events"],
            {"merged": 1},
        )

    def test_source_health_counts_malformed_and_future_primary_timestamps(self) -> None:
        ingress = sqlite3.connect(self.ingress_db)
        ingress.executemany(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    "invalid-time", "linear", "ENG-78", "{}", "observe_only",
                    "linear_issue_not_ready", 0, "not-a-time", 1,
                ),
                (
                    "future-time", "linear", "ENG-79", "{}", "observe_only",
                    "linear_issue_not_ready", 0, "2099-01-01T00:00:00Z", 1,
                ),
            ],
        )
        ingress.commit()
        ingress.close()
        lifecycle = sqlite3.connect(self.lifecycle_db)
        lifecycle.executemany(
            "INSERT INTO lifecycle_outbox VALUES (?,?,?,?,?)",
            [
                ("invalid-job", "pending", "draft_pr_verified", "not-a-time", None),
                (
                    "future-job", "pending", "draft_pr_verified",
                    "2099-01-01T00:00:00Z", None,
                ),
            ],
        )
        lifecycle.commit()
        lifecycle.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["intake"]["linear_events"], 1)
        self.assertEqual(report["sources"]["ingress"]["row_count"], 3)
        self.assertEqual(report["sources"]["ingress"]["invalid_timestamp_rows"], 1)
        self.assertEqual(report["sources"]["ingress"]["future_timestamp_rows"], 1)
        self.assertEqual(report["sources"]["lifecycle"]["row_count"], 2)
        self.assertEqual(report["sources"]["lifecycle"]["invalid_timestamp_rows"], 1)
        self.assertEqual(report["sources"]["lifecycle"]["future_timestamp_rows"], 1)

    def test_bridge_source_health_covers_attempt_cohort_timestamp(self) -> None:
        attempt, _job, _created = self._reserve("ENG-BAD-TIME")
        self.conn.execute(
            "UPDATE factory_attempts SET created_at='!',"
            "updated_at='2026-09-12T00:00:00Z' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.commit()

        report = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["coverage"]["cohort_attempts"], 0)
        self.assertGreaterEqual(report["sources"]["bridge"]["invalid_timestamp_rows"], 1)
        self.assertEqual(
            report["sources"]["bridge"]["timestamp_health_by_field"][
                "factory_attempts.created_at"
            ]["invalid"],
            1,
        )

    def test_invalid_receipt_time_cannot_authenticate_provider_event(self) -> None:
        attempt, _job, _created = self._reserve("ENG-86")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/86"
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-09-10T00:00:00Z',"
            "updated_at='2026-09-10T00:00:00Z' WHERE attempt_id=?",
            (pr_url, attempt["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-invalid-receipt-86", "github", "Driftward-LLC/capykit#86",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z",
                int(datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {"github-invalid-receipt-86": "github:pull_request:86:merged"},
        )
        ingress.execute(
            "UPDATE ingress_receipts SET received_at='!' "
            "WHERE event_key='github:pull_request:86:merged'"
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["outcomes"]["merged_prs"], 0)
        self.assertEqual(report["outcomes"]["unobserved_prs"], 1)
        self.assertEqual(report["intake"]["receipts"]["invalid_timestamp_rows"], 1)
        self.assertEqual(report["intake"]["receipts"]["signature_verified_event_keys"], 0)
        self.assertEqual(report["sources"]["ingress"]["invalid_receipt_timestamp_rows"], 1)

    def test_future_provider_timestamp_is_excluded_from_canonical_outcome(self) -> None:
        attempt, _job, _created = self._reserve("ENG-85")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/85"
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at='2026-09-10T00:00:00Z',"
            "updated_at='2026-09-10T00:00:00Z' WHERE attempt_id=?",
            (pr_url, attempt["attempt_id"]),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at='2026-09-10T00:00:00Z' "
            "WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-future-85", "github", "Driftward-LLC/capykit#85",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z",
                int(datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000),
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {"github-future-85": "github:pull_request:85:merged"},
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["outcomes"]["merged_prs"], 0)
        self.assertEqual(report["outcomes"]["unobserved_prs"], 1)
        self.assertEqual(report["github"]["future_observations_excluded"], 1)
        self.assertEqual(report["sources"]["ingress"]["future_provider_timestamp_rows"], 1)
        self.assertEqual(
            report["sources"]["ingress"]["by_source"]["github"][
                "future_provider_timestamp_rows"
            ],
            1,
        )

    def test_receipt_fallback_merge_is_state_only_not_temporal_attribution(self) -> None:
        attempt, _job, _created = self._reserve("ENG-87")
        pr_url = "https://github.com/Driftward-LLC/capykit/pull/87"
        ready_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
        self.conn.execute(
            "UPDATE factory_attempts SET prior_pr_url=?,created_at=?,updated_at=?,"
            "status='completed' WHERE attempt_id=?",
            (
                pr_url, ready_at.isoformat(), ready_at.isoformat(),
                attempt["attempt_id"],
            ),
        )
        self.conn.execute(
            "UPDATE factory_attempt_events SET created_at=? WHERE attempt_id=?",
            (ready_at.isoformat(), attempt["attempt_id"]),
        )
        self.conn.commit()
        ingress = sqlite3.connect(self.ingress_db)
        ingress.execute(
            "UPDATE ingress_events SET subject='ENG-87',created_at=?,"
            "provider_updated_at_us=? WHERE event_id='linear-1'",
            (
                ready_at.isoformat(), int(ready_at.timestamp() * 1_000_000),
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-fallback-87", "github", "Driftward-LLC/capykit#87",
                json.dumps({"prUrl": pr_url, "state": "closed", "merged": True}),
                "observe_only", "github_pr_closed_merged", 1,
                "2026-09-12T00:00:00Z", 0,
            ),
        )
        self._record_ingress_provenance(
            ingress,
            {
                "linear-1": attempt["request_id"],
                "github-fallback-87": "github:pull_request:87:merged",
            },
            fallback_timestamp_events={"github-fallback-87"},
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db, self.ingress_db, self.lifecycle_db, days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertEqual(report["outcomes"]["merged_prs"], 1)
        self.assertEqual(
            report["github"]["merge_timestamp_coverage"],
            {"observed": 0, "eligible": 1, "rate": 0.0, "complete": False},
        )
        correlation = report["intake"]["correlation"]
        self.assertEqual(correlation["work_item_conversion"]["all"]["merged"]["observed"], 0)
        self.assertIsNone(correlation["work_item_conversion"]["all"]["merged"]["rate"])
        self.assertEqual(correlation["latency_ms"]["ready_received_to_merged"]["samples"], 0)
        efficiency = report["recovery"]["repair_efficiency_on_authenticated_merged_prs"]
        self.assertEqual(efficiency["observed_merged_prs"], 1)
        self.assertEqual(efficiency["provider_timed_merged_prs"], 0)
        self.assertFalse(efficiency["canonical_efficiency_available"])
        self.assertIsNone(efficiency["attempts_per_merged_pr"])

    def test_missing_optional_ledgers_are_reported_not_guessed(self) -> None:
        report = factory_analytics(
            self.bridge_db,
            str(Path(self.work.name) / "missing-ingress.db"),
            str(Path(self.work.name) / "missing-lifecycle.db"),
            days=30,
            now=datetime.now(timezone.utc),
        )
        self.assertFalse(report["intake"]["available"])
        self.assertIsNone(report["outcomes"]["merged_prs"])
        self.assertFalse(report["lifecycle"]["available"])
        repair_efficiency = report["recovery"][
            "repair_efficiency_on_authenticated_merged_prs"
        ]
        self.assertFalse(repair_efficiency["available"])
        self.assertIsNone(repair_efficiency["observed_merged_prs"])
        self.assertIsNone(repair_efficiency["merged_pr_lineage_coverage"])

    def test_future_ingress_schema_is_unavailable_not_misinterpreted(self) -> None:
        ingress = sqlite3.connect(self.ingress_db)
        self._record_ingress_provenance(
            ingress,
            {"linear-1": "linear:issue:ENG-40:update:one"},
        )
        ingress.execute(
            "UPDATE ingress_schema_meta SET schema_version=4 WHERE singleton=1"
        )
        ingress.commit()
        ingress.close()

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )

        self.assertFalse(report["intake"]["available"])
        self.assertIsNone(report["outcomes"]["merged_prs"])
        self.assertFalse(report["coverage"]["ingress_available"])
        self.assertEqual(
            report["sources"]["ingress"]["schema_contract"],
            "unsupported-forward-schema",
        )
        self.assertEqual(report["sources"]["ingress"]["schema_version"], 4)
        self.assertEqual(
            report["sources"]["ingress"]["schema_version_source"],
            "ingress_schema_meta",
        )
        self.assertIn(
            "intake_and_github_outcomes: ingress ledger is unavailable or incompatible",
            report["unavailable_metrics"],
        )

    def test_partial_failure_classification_is_rejected(self) -> None:
        _attempt, job, _ = self._reserve("ENG-43")
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="failed", details={
                "reason_code": "bad_partial_classification",
                "failure_domain": "factory",
            })
        complete = {
            "reason_code": "bad_classification_dimension",
            "failure_domain": "factory",
            "failure_stage": "preflight",
            "retryable": False,
            "failure_disposition": "terminal",
            "operator_intervention_required": True,
        }
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="failed", details={**complete, "failure_domain": "made_up"})
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="failed", details={**complete, "failure_stage": "made_up"})
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="failed", details={**complete, "process_exit_code": True})

    def test_stale_bridge_schema_is_rejected(self) -> None:
        stale_path = str(Path(self.work.name) / "stale.db")
        stale = connect(stale_path)
        stale.execute("UPDATE bridge_schema_meta SET schema_version=4 WHERE singleton=1")
        stale.commit()
        stale.close()

        with self.assertRaises(BridgeValidationError):
            factory_analytics(
                stale_path,
                self.ingress_db,
                self.lifecycle_db,
                days=30,
                now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
