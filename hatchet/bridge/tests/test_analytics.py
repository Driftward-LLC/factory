import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hatchet_hermes_bridge.analytics import factory_analytics
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


class FactoryAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def test_report_projects_funnel_failures_backend_and_optional_ledgers(self) -> None:
        successful, success_job, _ = self._reserve("ENG-40")
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
            **completed,
        })
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
                "observe_only", "github_pr_closed_merged", 1, "2026-09-12T12:00:00Z", 2,
            ),
        )
        ingress.execute(
            "INSERT INTO ingress_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "github-stale", "github", "Driftward-LLC/capykit#40",
                json.dumps({"prUrl": "https://github.com/Driftward-LLC/capykit/pull/40", "state": "open", "merged": False}),
                "observe_only", "github_pr_open", 1, "2026-09-13T12:00:00Z", 1,
            ),
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
        self.assertEqual(report["failures"]["by_classification"][0], {
            "failure_domain": "github", "failure_stage": "artifact_fence",
            "reason_code": "artifact_fence_changed", "count": 1,
        })
        self.assertEqual(report["backends"][0]["execution_backend"], "hermes")
        self.assertEqual(report["backends"][0]["configured_auth_mode"], "chatgpt_codex_oauth")
        self.assertEqual(report["backends"][0]["completed_invocations"], 1)
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
        }
        settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False, failure_details=failure)
        settle_factory_execution(self.conn, job["job_id"], lease, effect_observed=False, failure_details=failure)
        rows = self.conn.execute(
            "SELECT details_json FROM factory_attempt_events WHERE attempt_id=? AND event_type='execution_backend_failed'",
            (attempt["attempt_id"],),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["details_json"])["failure_domain"], "executor_backend")

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
            "observed_hermes_session_id": "hermes-session-47",
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
        self.assertEqual(get_job(self.conn, job["job_id"])["hermes_session_id"], "hermes-session-47")
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
                "observed_hermes_session_id": "hermes-session-48",
                "backend_outcome": "created",
                "verification_context": {"untrusted": "shape"},
            },
        )
        recovered = get_job(self.conn, job["job_id"])
        self.assertEqual(recovered["state"], "effect_committed")
        self.assertEqual(recovered["commit_sha"], "abcdef1")
        self.assertEqual(recovered["hermes_session_id"], "hermes-session-48")
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

    def test_effect_commit_recovery_rejects_receipts_not_bound_to_the_job(self) -> None:
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
                self.assertEqual(event_counts, {
                    "execution_effect_commit_failed": 1,
                    "execution_effect_commit_receipt_rejected": 1,
                })
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM factory_inspection_requests WHERE prior_attempt_id=?",
                    (attempt["attempt_id"],),
                ).fetchone()[0], 0)

        report = factory_analytics(
            self.bridge_db,
            self.ingress_db,
            self.lifecycle_db,
            days=30,
            now=datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(report["safety"]["rejected_observed_receipts"], 4)
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
                "observed_hermes_session_id": "hermes-session-50",
                "backend_outcome": "created",
                "verification_context": None,
            },
        )
        recovered = get_job(self.conn, job["job_id"])
        self.assertEqual(recovered["state"], "effect_committed")
        self.assertEqual(recovered["commit_sha"], "abcdef1")
        self.assertEqual(recovered["hermes_session_id"], "hermes-session-50")
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

    def test_partial_failure_classification_is_rejected(self) -> None:
        _attempt, job, _ = self._reserve("ENG-43")
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="failed", details={
                "reason_code": "bad_partial_classification",
                "failure_domain": "factory",
            })

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
