import tempfile
import unittest
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge.store import (
    BridgeConflict,
    BridgeValidationError,
    deterministic_branch,
    list_events,
    list_lifecycle_events,
    count_events,
    record_job_correlation,
    submit_job,
    transition_job,
    record_reconciliation_findings,
    reconciliation_findings_status,
    acquire_verification_lease,
    claim_verification_lease,
    claim_factory_execution,
    authorize_factory_effect,
    commit_factory_execution,
    get_job,
    record_verification_observation,
    record_factory_execution_route,
    record_factory_execution_analytics,
    reserve_factory_attempt,
    reserve_factory_intake,
    list_active_factory_attempts,
    list_recent_factory_attempts,
    list_factory_attempts,
    list_factory_attempt_events,
    reserve_factory_retry,
    sanitize_detail,
    cancel_factory_attempt,
    get_factory_attempt,
    get_factory_lifecycle,
    _canonical_verifier_policy,
    _execution_analytics_details,
)
from test_support import connect_migrate as connect


def canonical_verifier_policy(repository="Driftward-LLC/driftward-host"):
    catalog = json.loads((Path(__file__).resolve().parents[2] / "workers" / "verifier-policy.json").read_text())
    policy = next(item for item in catalog["policies"] if item["repo"] == repository)
    return {"catalog_version": catalog["version"], "digest": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), **policy}


def verifier_payload(repository="Driftward-LLC/driftward-host"):
    canonical = canonical_verifier_policy(repository)
    policy = canonical
    return {"repo": policy["repo"], "dry_run": False, "verification_policy": canonical, "expected_base": policy["expected_base"], "allowed_changed_paths": policy["allowed_changed_paths"], "forbidden_changed_paths": policy["forbidden_changed_paths"], "required_local_commands": policy["required_local_commands"], "secret_check_command": policy["secret_check_command"]}


def legacy_verifier_payload(repository="Driftward-LLC/driftward-host"):
    payload = verifier_payload(repository)
    legacy_checks = ["legacy-check", "repo-specific-check"]
    policy = {key: payload["verification_policy"][key] for key in ("id", "repo", "expected_base", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command")}
    policy["required_github_checks"] = legacy_checks
    legacy = {"catalog_version": payload["verification_policy"]["catalog_version"], "digest": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), **policy}
    return {**payload, "verification_policy": legacy, "required_github_checks": legacy_checks}


def verification_receipt(job, commit_sha):
    changed_file = "src/example.ts" if job["payload"]["repo"] == "Driftward-LLC/capykit" else "hatchet/bridge/tests/test_store.py"
    policy = canonical_verifier_policy(job["payload"]["repo"])
    return {
        "version": "draft-pr-verification-v1", "verifier_version": "bridge-executor-v2", "verified_at": "2026-08-16T00:00:00Z",
        "repo": job["payload"]["repo"], "pr_url": job["pr_url"], "branch": job["branch"], "commit_sha": commit_sha,
        "policy_id": policy["id"], "policy_catalog_version": policy["catalog_version"], "policy_digest": policy["digest"],
        "checks": {"status": "SUCCESS", "total_count": 2},
        "pr_identity": {"base": job["payload"]["expected_base"], "draft": True, "author": "factory-hermes[bot]", "linear_reference": f"Refs Linear {job['payload'].get('linear_issue')}"},
        "file_policy": {"allowed_changed_paths": job["payload"]["allowed_changed_paths"], "forbidden_changed_paths": job["payload"]["forbidden_changed_paths"], "files": [changed_file]},
        "local_policy": {"required_commands": job["payload"]["required_local_commands"], "results": [{"command": command, "exit_code": 0} for command in job["payload"]["required_local_commands"]]},
        "secret_policy": {"required_command": job["payload"]["secret_check_command"], "result": {"command": job["payload"]["secret_check_command"], "exit_code": 0}},
    }


def original_context(job):
    return {
        "local_commands": [{"command": command, "exit_code": 0} for command in job["payload"]["required_local_commands"]],
        "secret_check": {"command": job["payload"]["secret_check_command"], "exit_code": 0},
    }


class BridgeStoreTests(unittest.TestCase):
    def setUp(self):
        os.environ["HATCHET_VERIFIER_POLICY_PATH"] = str(Path(__file__).resolve().parents[2] / "workers" / "verifier-policy.json")
        self.tmp = tempfile.NamedTemporaryFile()
        self.conn = connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        self.tmp.close()

    def test_secret_sanitizer_redacts_jwt_and_long_opaque_oauth_shapes(self):
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE"
        opaque = ("Ab7_" * 50) + ".xY9"
        sanitized = sanitize_detail(
            f"failure {jwt} and {opaque} and v1_sk-proj-FAKESECRET"
        )
        self.assertNotIn("eyJ", sanitized)
        self.assertNotIn("Ab7_", sanitized)
        self.assertNotIn("sk-proj", sanitized)
        self.assertEqual(
            sanitized,
            "failure [redacted] and [redacted] and v1_[redacted]",
        )

    def test_submit_is_idempotent_for_same_intent(self):
        request = {
            "idempotency_key": "hatchet:run-1:create-pr",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host", "linear_issue": "ENG-35"},
        }

        first, first_created = submit_job(self.conn, request)
        second, second_created = submit_job(self.conn, request)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["branch"], deterministic_branch(request["idempotency_key"]))

        events = list_events(self.conn, first["job_id"])
        self.assertEqual([event["event_type"] for event in events], ["accepted", "submit_deduplicated"])

    def test_execution_failure_boundary_fields_are_optional_bounded_dimensions(self):
        required = {
            "analytics_schema_version": 1,
            "invocation_id": "invocation-1",
            "execution_backend": "hermes",
            "execution_transport": "grpc_uds",
            "backend_version": None,
            "configured_auth_mode": "unknown",
            "failure_domain": "gateway_transport",
            "failure_stage": "gateway_connect",
            "reason_code": "hermes_grpc_gateway_unavailable",
            "retryable": True,
            "failure_disposition": "recovery_required",
            "operator_intervention_required": True,
            "effect_outcome": "unknown",
            "elapsed_ms": 25,
        }
        self.assertEqual(
            _execution_analytics_details("execution_backend_failed", required),
            required,
        )
        bounded = {
            **required,
            "transport_status": "grpc_unavailable",
            "process_exit_code": 17,
            "process_signal": "SIGTERM",
            "timed_out": False,
            "buffer_exceeded": False,
        }
        self.assertEqual(
            _execution_analytics_details("execution_backend_failed", bounded),
            bounded,
        )
        for field, value in (
            ("failure_domain", "made_up"),
            ("failure_stage", "made_up"),
            ("transport_status", "UPPERCASE"),
            ("transport_status", "sk-proj-fake_transport_value"),
            ("process_exit_code", True),
            ("process_signal", "x" * 41),
            ("process_signal", "sk-proj-FAKE_SIGNAL_VALUE"),
            ("timed_out", "false"),
        ):
            with self.subTest(field=field), self.assertRaises(BridgeValidationError):
                _execution_analytics_details("execution_backend_failed", {**bounded, field: value})

    def test_docker_runtime_rejects_non_image_verifier_policy_path(self):
        old_runtime = os.environ.get("HATCHET_FACTORY_RUNTIME")
        old_policy = os.environ.get("HATCHET_VERIFIER_POLICY_PATH")
        try:
            os.environ["HATCHET_FACTORY_RUNTIME"] = "docker"
            os.environ["HATCHET_VERIFIER_POLICY_PATH"] = "/etc/driftward/hatchet-verifier-policy.json"
            with self.assertRaisesRegex(BridgeValidationError, "must be /app/hatchet/workers/verifier-policy.json"):
                _canonical_verifier_policy("Driftward-LLC/capykit")
        finally:
            if old_runtime is None:
                os.environ.pop("HATCHET_FACTORY_RUNTIME", None)
            else:
                os.environ["HATCHET_FACTORY_RUNTIME"] = old_runtime
            if old_policy is None:
                os.environ.pop("HATCHET_VERIFIER_POLICY_PATH", None)
            else:
                os.environ["HATCHET_VERIFIER_POLICY_PATH"] = old_policy

    def test_submit_conflicts_for_same_key_different_payload(self):
        request = {
            "idempotency_key": "hatchet:run-1:create-pr",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        }
        submit_job(self.conn, request)

        with self.assertRaises(BridgeConflict):
            submit_job(
                self.conn,
                {
                    "idempotency_key": "hatchet:run-1:create-pr",
                    "requested_action": "create_draft_pr",
                    "payload": {"repo": "Driftward-LLC/other"},
                },
            )

    def test_transition_records_external_refs_and_blocks_terminal_changes(self):
        job, _ = submit_job(
            self.conn,
            {
                "idempotency_key": "hatchet:run-2:create-pr",
                "requested_action": "create_draft_pr",
                "payload": verifier_payload(),
            },
        )

        running = transition_job(self.conn, job["job_id"], state="running")
        self.assertEqual(running["state"], "running")

        committed = transition_job(
            self.conn,
            job["job_id"],
            state="effect_committed",
            details={
                "commit_sha": "abc123",
                "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/95",
            },
        )
        self.assertEqual(committed["state"], "effect_committed")
        self.assertEqual(committed["commit_sha"], "abc123")
        self.assertEqual(committed["pr_url"], "https://github.com/Driftward-LLC/driftward-host/pull/95")

        pending = transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="verified-attempt")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="verified-attempt"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="verified-attempt", receipt=verification_receipt(pending, "abc123"))
        self.assertEqual(pending["state"], "verification_pending")

        ready = transition_job(
            self.conn,
            job["job_id"],
            state="ready_for_human_review",
            details={"reason_code": "draft_pr_verified", "verification_attempt_id": "verified-attempt", "verification_receipt": verification_receipt(pending, "abc123")},
        )
        self.assertEqual(ready["state"], "ready_for_human_review")
        self.assertEqual(ready["verification_receipt"]["commit_sha"], "abc123")
        self.assertTrue(ready["verification_digest"])

        with self.assertRaises(BridgeConflict):
            transition_job(self.conn, job["job_id"], state="failed")

    def test_verification_receipt_accepts_github_app_author_slug(self):
        job, _ = submit_job(
            self.conn,
            {
                "idempotency_key": "hatchet:run-app-author:create-pr",
                "requested_action": "create_draft_pr",
                "payload": verifier_payload(),
            },
        )
        transition_job(self.conn, job["job_id"], state="running")
        transition_job(
            self.conn,
            job["job_id"],
            state="effect_committed",
            details={
                "commit_sha": "abc123",
                "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/96",
            },
        )
        pending = transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        receipt = verification_receipt(pending, "abc123")
        receipt["pr_identity"]["author"] = "app/factory-hermes"

        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="verified-app-author")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="verified-app-author"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="verified-app-author", receipt=receipt)
        ready = transition_job(
            self.conn,
            job["job_id"],
            state="ready_for_human_review",
            details={"reason_code": "draft_pr_verified", "verification_attempt_id": "verified-app-author", "verification_receipt": receipt},
        )

        self.assertEqual(ready["state"], "ready_for_human_review")
        self.assertEqual(ready["verification_receipt"]["pr_identity"]["author"], "app/factory-hermes")

    def test_verification_accepts_legacy_payload_policy_check_digest(self):
        job, _ = submit_job(
            self.conn,
            {
                "idempotency_key": "hatchet:run-legacy-policy:create-pr",
                "requested_action": "create_draft_pr",
                "payload": legacy_verifier_payload(),
            },
        )
        transition_job(self.conn, job["job_id"], state="running")
        transition_job(
            self.conn,
            job["job_id"],
            state="effect_committed",
            details={
                "commit_sha": "abc123",
                "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/97",
            },
        )
        pending = transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        receipt = verification_receipt(pending, "abc123")
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="legacy-policy")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="legacy-policy"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="legacy-policy", receipt=receipt)
        ready = transition_job(
            self.conn,
            job["job_id"],
            state="ready_for_human_review",
            details={"reason_code": "draft_pr_verified", "verification_attempt_id": "legacy-policy", "verification_receipt": receipt},
        )

        self.assertEqual(ready["state"], "ready_for_human_review")
        self.assertEqual(ready["verification_receipt"]["checks"], {"status": "SUCCESS", "total_count": 2})

    def test_verified_ready_requires_a_pr_and_verification_reason(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:verified-ready:validation",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        transition_job(self.conn, job["job_id"], state="running")
        transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/96"})
        transition_job(self.conn, job["job_id"], state="verification_pending")
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="ready_for_human_review")

    def test_ready_transition_rejects_abusive_or_detached_receipts(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:receipt:validation", "requested_action": "create_draft_pr",
            "payload": verifier_payload(),
        })
        transition_job(self.conn, job["job_id"], state="running")
        transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/97"})
        pending = transition_job(self.conn, job["job_id"], state="verification_pending")
        receipt = verification_receipt(pending, "abc123")
        receipt["pr_identity"] = {"passed": False}
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "commit_sha": "abc123", "verification_receipt": receipt})
        receipt = verification_receipt(pending, "other")
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "commit_sha": "abc123", "verification_receipt": receipt})

    def test_real_jobs_cannot_take_legacy_completed_transition(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:real:completed", "requested_action": "create_draft_pr", "payload": {"repo": "Driftward-LLC/driftward-host", "dry_run": False},
        })
        transition_job(self.conn, job["job_id"], state="running")
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, job["job_id"], state="completed")
        dry, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:dry:completed", "requested_action": "create_draft_pr", "payload": {"repo": "Driftward-LLC/driftward-host", "dry_run": True},
        })
        transition_job(self.conn, dry["job_id"], state="running")
        self.assertEqual(transition_job(self.conn, dry["job_id"], state="completed")["state"], "completed")

    def test_verification_lease_collapses_redelivery_expires_and_closes_terminal_work(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:lease:test", "requested_action": "create_draft_pr", "payload": verifier_payload()})
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/98"})
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        # Separate connections model concurrent worker/reconciler processes.
        second_conn = connect(self.tmp.name)
        self.addCleanup(second_conn.close)
        first = acquire_verification_lease(self.conn, pending["job_id"], attempt_id="worker-a")
        duplicate = acquire_verification_lease(second_conn, pending["job_id"], attempt_id="worker-b")
        self.assertTrue(first["acquired"]); self.assertFalse(duplicate["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="worker-a"))
        self.assertFalse(claim_verification_lease(self.conn, pending["job_id"], attempt_id="worker-a"))
        self.conn.execute("UPDATE verification_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE job_id = ?", (pending["job_id"],)); self.conn.commit()
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="worker-c")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="worker-c"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="worker-c", receipt=verification_receipt(pending, "abc123"))
        ready = transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": "worker-c", "verification_receipt": verification_receipt(pending, "abc123")})
        lease = self.conn.execute("SELECT closed_at, outcome FROM verification_leases WHERE job_id = ?", (ready["job_id"],)).fetchone()
        self.assertIsNotNone(lease["closed_at"]); self.assertEqual(lease["outcome"], "verified")
        with self.assertRaises(BridgeValidationError):
            acquire_verification_lease(self.conn, pending["job_id"], attempt_id="worker-after-terminal")

    def test_failed_verification_closes_the_active_lease_in_its_transition_transaction(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:lease:failure", "requested_action": "create_draft_pr", "payload": verifier_payload()})
        transition_job(self.conn, job["job_id"], state="running")
        transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/99"})
        pending = transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="crashed-worker")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="crashed-worker"))
        failed = transition_job(self.conn, pending["job_id"], state="failed", details={"reason_code": "github_checks_failed", "verification_attempt_id": "crashed-worker", "error": "sanitized"})
        lease = self.conn.execute("SELECT closed_at, outcome FROM verification_leases WHERE job_id = ?", (failed["job_id"],)).fetchone()
        self.assertIsNotNone(lease["closed_at"]); self.assertEqual(lease["outcome"], "failed")

    def test_failed_docs_only_check_does_not_queue_repair_without_explicit_opt_in(self):
        setting = "HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS"
        for index, value in enumerate((None, "", "invalid", "0", "-1", "1")):
            with self.subTest(value=value), patch.dict(os.environ):
                os.environ.pop(setting, None)
                if value is not None:
                    os.environ[setting] = value
                attempt, job, _ = reserve_factory_intake(
                    self.conn, work_item_key=f"ENG-{120 + index}", mode="real", request_id="docs-canary",
                    reason="Documentation-only canary", actor="intake-worker",
                    payload={**verifier_payload("Driftward-LLC/capykit"), "implementation_prompt": "Documentation-only: update README.md; do not change dependencies."},
                )
                transition_job(self.conn, job["job_id"], state="running")
                pr_url = f"https://github.com/Driftward-LLC/capykit/pull/{120 + index}"
                transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": pr_url}, _factory_execution_commit=True)
                transition_job(self.conn, job["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
                acquire_verification_lease(self.conn, job["job_id"], attempt_id="docs-verifier")
                claim_verification_lease(self.conn, job["job_id"], attempt_id="docs-verifier")
                outbox_count = self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0]

                failed = transition_job(
                    self.conn, job["job_id"], state="failed",
                    details={"reason_code": "verification_github_check_failed", "verification_attempt_id": "docs-verifier", "error": "GitHub dependency audit check failed"},
                    _factory_verifier_completion=True,
                )

                self.assertEqual((failed["state"], failed["pr_url"]), ("failed", pr_url))
                self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "failed")
                self.assertEqual(len(list_factory_attempts(self.conn, attempt["work_item_key"])), 1)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0], outbox_count)
                self.assertNotIn("repair_queued", [event["event_type"] for event in list_factory_attempt_events(self.conn, attempt["attempt_id"])])

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_failed_github_check_queues_bounded_repair_for_existing_pr(self):
        attempt, job, _ = reserve_factory_intake(
            self.conn, work_item_key="ENG-11", mode="real", request_id="linear:issue:ENG-11:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-11"},
        )
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/capykit/pull/111"}, _factory_execution_commit=True)
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="verifier-repair")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="verifier-repair"))

        failed = transition_job(
            self.conn,
            pending["job_id"],
            state="failed",
            details={"reason_code": "verification_github_check_failed", "verification_attempt_id": "verifier-repair", "error": "GitHub check markdown lint did not succeed; observed FAILURE"},
            _factory_verifier_completion=True,
        )

        attempts = list_factory_attempts(self.conn, "ENG-11")
        repair = attempts[0]
        repair_job = get_job(self.conn, repair["job_id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "failed")
        self.assertEqual(repair["generation"], 2)
        self.assertEqual(repair["status"], "accepted")
        self.assertEqual(repair["prior_attempt_id"], attempt["attempt_id"])
        self.assertEqual(repair["prior_pr_url"], "https://github.com/Driftward-LLC/capykit/pull/111")
        self.assertEqual(repair_job["requested_action"], "repair_draft_pr")
        self.assertEqual(repair_job["branch"], failed["branch"])
        self.assertEqual(repair_job["payload"]["factory_repair"]["pr_url"], failed["pr_url"])
        self.assertEqual(repair_job["payload"]["factory_repair"]["reason_code"], "verification_github_check_failed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox WHERE job_id=?", (repair_job["job_id"],)).fetchone()[0], 1)
        self.assertIn("repair_queued", [event["event_type"] for event in list_factory_attempt_events(self.conn, attempt["attempt_id"])])
        self.assertEqual([event["event_type"] for event in list_factory_attempt_events(self.conn, repair["attempt_id"])][:2], ["repair_reserved", "repair_requested"])

    @patch.dict(os.environ, {"HATCHET_FACTORY_AUTO_REPAIR_MAX_GENERATIONS": "2"})
    def test_non_repairable_verifier_failures_do_not_queue_repair(self):
        attempt, job, _ = reserve_factory_intake(
            self.conn, work_item_key="ENG-12", mode="real", request_id="linear:issue:ENG-12:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-12"},
        )
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/capykit/pull/112"}, _factory_execution_commit=True)
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="verifier-unsafe")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="verifier-unsafe"))

        transition_job(
            self.conn,
            pending["job_id"],
            state="failed",
            details={"reason_code": "verification_pr_author_mismatch", "verification_attempt_id": "verifier-unsafe", "error": "unexpected PR author"},
            _factory_verifier_completion=True,
        )

        self.assertEqual(len(list_factory_attempts(self.conn, "ENG-12")), 1)
        self.assertEqual(get_factory_attempt(self.conn, attempt["attempt_id"])["status"], "failed")

    def test_legacy_context_receipt_rows_remain_readable_without_allowing_new_legacy_completion(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:legacy:receipt", "requested_action": "create_draft_pr", "payload": verifier_payload()})
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/100"})
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="legacy-owner")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="legacy-owner"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="legacy-owner", receipt=verification_receipt(pending, "abc123"))
        ready = transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": "legacy-owner", "verification_receipt": verification_receipt(pending, "abc123")})
        self.conn.execute("UPDATE bridge_jobs SET verification_original_context_json = NULL, verification_context_json = verification_receipt_json WHERE job_id = ?", (ready["job_id"],)); self.conn.commit()
        readable = get_job(self.conn, ready["job_id"])
        self.assertEqual(readable["verification_receipt"]["commit_sha"], "abc123")
        self.assertEqual(readable["verification_context"]["commit_sha"], "abc123")

    def test_completion_rejects_lease_bypass_non_owner_expiry_and_forged_evidence(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:claimed:receipt", "requested_action": "create_draft_pr", "payload": verifier_payload()})
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/101"})
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        good = verification_receipt(pending, "abc123")
        for attempt in (None, "wrong-owner"):
            with self.subTest(attempt=attempt), self.assertRaises(BridgeValidationError):
                transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": attempt, "verification_receipt": good})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="owner")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="owner"))
        for field, mutate in (
            ("author", lambda receipt: receipt["pr_identity"].__setitem__("author", "human")),
            ("unexpected_pr_identity_field", lambda receipt: receipt["pr_identity"].__setitem__("labels", ["bug"])),
            ("checks", lambda receipt: receipt.__setitem__("checks", [])),
            ("files", lambda receipt: receipt["file_policy"].__setitem__("files", ["../escape"])),
            ("commands", lambda receipt: receipt["local_policy"].__setitem__("results", [])),
        ):
            forged = json.loads(json.dumps(good)); mutate(forged)
            with self.subTest(field=field), self.assertRaises(BridgeValidationError):
                transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": "owner", "verification_receipt": forged})
        self.conn.execute("UPDATE verification_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE job_id = ?", (pending["job_id"],)); self.conn.commit()
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": "owner", "verification_receipt": good})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="fresh-owner")["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="fresh-owner"))
        record_verification_observation(self.conn, pending["job_id"], attempt_id="fresh-owner", receipt=good)
        self.conn.execute("UPDATE bridge_jobs SET verification_observed_digest = 'tampered' WHERE job_id = ?", (pending["job_id"],)); self.conn.commit()
        with self.assertRaises(BridgeValidationError):
            get_job(self.conn, pending["job_id"])
        self.conn.execute("UPDATE bridge_jobs SET verification_observed_digest = ? WHERE job_id = ?", (hashlib.sha256(json.dumps(good, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), pending["job_id"])); self.conn.commit()
        ready = transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code": "draft_pr_verified", "verification_attempt_id": "fresh-owner", "verification_receipt": good})
        self.assertEqual(ready["verification_context"], original_context(job))

    def test_verification_context_is_write_once_on_effect_to_pending_only(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:context:immutable", "requested_action": "create_draft_pr", "payload": verifier_payload()})
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/102"})
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        replacement = {"local_commands": [], "secret_check": {"command": "wrong", "exit_code": 1}}
        with self.assertRaises(BridgeConflict):
            transition_job(self.conn, pending["job_id"], state="verification_pending", details={"verification_context": replacement})
        with self.assertRaises(BridgeValidationError):
            transition_job(self.conn, pending["job_id"], state="failed", details={"verification_context": replacement})
        self.assertEqual(get_job(self.conn, pending["job_id"])["verification_context"], original_context(job))

    def test_verification_terminal_failure_reason_codes_close_only_the_claimed_lease(self):
        for reason in ("github_checks_failed", "verification_github_checks_timeout", "verification_evidence_unavailable", "verification_pr_author_mismatch"):
            with self.subTest(reason=reason):
                job, _ = submit_job(self.conn, {"idempotency_key": f"hatchet:terminal:{reason}", "requested_action": "create_draft_pr", "payload": verifier_payload()})
                transition_job(self.conn, job["job_id"], state="running")
                committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha": "abc123", "pr_url": f"https://github.com/Driftward-LLC/driftward-host/pull/{reason}"})
                pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
                self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id=reason)["acquired"])
                self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id=reason))
                failed = transition_job(self.conn, pending["job_id"], state="failed", details={"reason_code": reason, "verification_attempt_id": reason, "error": "sanitized"})
                self.assertEqual(failed["state"], "failed")
                self.assertEqual(self.conn.execute("SELECT outcome FROM verification_leases WHERE job_id = ?", (pending["job_id"],)).fetchone()[0], "failed")

    def test_lifecycle_event_cursor_exposes_sanitized_history_in_order(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:lifecycle:cursor", "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/capykit", "linear_issue_id": "11111111-2222-4333-8444-555555555555", "linear_issue": "ENG-52", "linear_issue_url": "https://linear.app/driftward/issue/ENG-52/test", "dry_run": False},
        })
        transition_job(self.conn, job["job_id"], state="running")
        first = list_lifecycle_events(self.conn, after_created_at=None, after_event_id=None, limit=1)
        # A scan upper bound freezes its snapshot even when a later event arrives.
        frozen = list_lifecycle_events(
            self.conn,
            after_created_at=None,
            after_event_id=None,
            before_created_at=first[0]["createdAt"],
            before_event_id=first[0]["eventId"],
            limit=10,
        )
        second = list_lifecycle_events(self.conn, after_created_at=first[0]["createdAt"], after_event_id=first[0]["eventId"], limit=10)
        self.assertEqual(first[0]["state"], "accepted")
        self.assertEqual(first[0]["requestedAction"], "create_draft_pr")
        self.assertEqual([row["state"] for row in frozen], ["accepted"])
        self.assertEqual(second[0]["state"], "running")
        self.assertNotIn("error", first[0])

    def test_reconciliation_finding_retries_are_deduplicated(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:reconciliation:test",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        finding = {
            "finding_key": "reconciliation:v1:github_pr_branch_mismatch:test",
            "job_id": job["job_id"], "subject": f"bridge:{job['job_id']}",
            "kind": "github_pr_branch_mismatch", "severity": "warning",
            "details": {"expected": {"branch": "codex/expected"}, "observed": {"headRefName": "codex/observed"}},
        }
        created, existing = record_reconciliation_findings(self.conn, [finding])
        retry_created, retry_existing = record_reconciliation_findings(self.conn, [finding])
        self.assertEqual(created, ["reconciliation:v1:github_pr_branch_mismatch:test"])
        self.assertEqual(existing, [])
        self.assertEqual(retry_created, [])
        self.assertEqual(retry_existing, ["reconciliation:v1:github_pr_branch_mismatch:test"])
        status = reconciliation_findings_status(self.conn, active_within_minutes=30)
        self.assertEqual(status["current"], 1)
        self.assertEqual(status["historical"], 0)
        self.assertEqual(status["severity"], {"critical": 0, "warning": 1})
        self.assertEqual(status["scope"], {"active_work": 1, "terminal_work": 0, "system": 0})
        self.assertEqual(status["actionable"], 1)
        self.assertEqual(status["actionable_severity"], {"critical": 0, "warning": 1})
        record_reconciliation_findings(self.conn, [{**finding, "severity": "critical"}])
        self.assertEqual(reconciliation_findings_status(self.conn, active_within_minutes=30)["severity"], {"critical": 1, "warning": 0})

    def test_reconciliation_status_separates_terminal_debt_from_actionable_findings(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:reconciliation:scope",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        finding = {
            "finding_key": "reconciliation:v1:github_pr_branch_mismatch:scope",
            "job_id": job["job_id"], "subject": f"bridge:{job['job_id']}",
            "kind": "github_pr_branch_mismatch", "severity": "warning",
            "details": {"expected": {"branch": "codex/a"}, "observed": {"headRefName": "codex/b"}},
        }
        record_reconciliation_findings(self.conn, [finding])
        transition_job(self.conn, job["job_id"], state="failed", details={"reason_code": "test"})
        record_reconciliation_findings(self.conn, [{
            "finding_key": "reconciliation:v1:ingress_inventory_unavailable:scope",
            "job_id": None, "subject": "ingress", "kind": "ingress_inventory_unavailable",
            "severity": "critical", "details": {"expected": {"available": True}, "observed": {"available": False}},
        }])

        status = reconciliation_findings_status(self.conn)
        self.assertEqual(status["scope"], {"active_work": 0, "terminal_work": 1, "system": 1})
        self.assertEqual(status["actionable"], 1)
        self.assertEqual(status["actionable_severity"], {"critical": 1, "warning": 0})

    def test_reconciliation_findings_require_known_kind_existing_job_and_sanitized_details(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:reconciliation:validation",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        base = {
            "finding_key": "reconciliation:v1:github_pr_branch_mismatch:validation",
            "job_id": job["job_id"], "subject": f"bridge:{job['job_id']}",
            "kind": "github_pr_branch_mismatch", "severity": "warning",
            "details": {"expected": {"branch": "codex/a"}, "observed": {"headRefName": "codex/b"}},
        }
        with self.assertRaises(BridgeValidationError):
            record_reconciliation_findings(self.conn, [{**base, "kind": "not_a_contract_kind"}])
        with self.assertRaises(BridgeValidationError):
            record_reconciliation_findings(self.conn, [{**base, "job_id": "00000000-0000-0000-0000-000000000000"}])
        with self.assertRaises(BridgeValidationError):
            record_reconciliation_findings(self.conn, [{**base, "details": {"raw_payload": "secret"}}])

    def test_reconciliation_finding_can_use_non_job_sanitized_subject(self):
        finding = {
            "finding_key": "reconciliation:v1:ingress_stored_not_published:ingress",
            "job_id": None, "subject": "ingress:linear:issue:ENG-39:update:delivery-1",
            "kind": "ingress_stored_not_published", "severity": "critical",
            "details": {"expected": {"published": True}, "observed": {"published": False}},
        }
        created, _ = record_reconciliation_findings(self.conn, [finding])
        self.assertEqual(created, [finding["finding_key"]])

    def test_reconciliation_findings_reject_extra_raw_and_non_string_severity_without_mutating_ledger(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:reconciliation:strict-schema",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        base = {
            "finding_key": "reconciliation:v1:github_pr_branch_mismatch:strict",
            "job_id": job["job_id"], "subject": f"bridge:{job['job_id']}",
            "kind": "github_pr_branch_mismatch", "severity": "warning",
            "details": {"expected": {"branch": "codex/a"}, "observed": {"headRefName": "codex/b"}},
        }
        before = self.conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0]
        for invalid in (
            {**base, "extra": "rejected"},
            {**base, "raw_payload": {"body": "rejected"}},
            {**base, "severity": "info"},
            {**base, "details": {"expected": {}, "observed": {}, "body": "rejected"}},
            {**base, "details": {"expected": {"raw_payload": "rejected"}, "observed": {}}},
        ):
            with self.assertRaises(BridgeValidationError):
                record_reconciliation_findings(self.conn, [invalid])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0], before)

        for severity in ([], {}, None, 0, 1.5):
            with self.subTest(severity=severity):
                with self.assertRaises(BridgeValidationError):
                    record_reconciliation_findings(self.conn, [{**base, "severity": severity}])
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0], before)

    def test_reconciliation_validation_is_atomic_and_persists_constructed_record_only(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:reconciliation:atomic",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        good = {
            "finding_key": "reconciliation:v1:github_pr_branch_mismatch:atomic-good",
            "job_id": job["job_id"], "subject": f"bridge:{job['job_id']}",
            "kind": "github_pr_branch_mismatch", "severity": "warning",
            "details": {"expected": {"branch": "codex/a"}, "observed": {"headRefName": "codex/b"}},
        }
        with self.assertRaises(BridgeValidationError):
            record_reconciliation_findings(self.conn, [good, {**good, "finding_key": "bad", "severity": "info"}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reconciliation_findings").fetchone()[0], 0)
        record_reconciliation_findings(self.conn, [good])
        stored = self.conn.execute("SELECT details_json FROM reconciliation_findings WHERE finding_key = ?", (good["finding_key"],)).fetchone()[0]
        self.assertNotIn("reason", stored)
        self.assertNotIn("raw_payload", stored)

    def test_submit_preserves_event_level_correlation_across_retries(self):
        base = {
            "idempotency_key": "hatchet:correlation:ENG-39",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host", "linear_issue": "ENG-39", "source_event_key": "linear:issue:ENG-39:update:first", "source_delivery_id": "first"},
        }
        first, created = submit_job(self.conn, base)
        self.assertTrue(created)
        retry, created = submit_job(self.conn, {**base, "payload": {**base["payload"], "source_event_key": "linear:issue:ENG-39:update:second", "source_delivery_id": "second"}})
        self.assertFalse(created)
        self.assertEqual(first["job_id"], retry["job_id"])
        self.assertEqual(retry["payload"]["source_event_key"], "linear:issue:ENG-39:update:first")
        self.assertEqual(retry["payload"]["source_event_keys"], ["linear:issue:ENG-39:update:first", "linear:issue:ENG-39:update:second"])

    def test_correlation_endpoint_preserves_new_delivery_when_effect_payload_changes(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:correlation:changed-payload",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host", "linear_issue": "ENG-39", "implementation_prompt": "first", "source_event_key": "linear:issue:ENG-39:update:first"},
        })
        correlated = record_job_correlation(self.conn, job["idempotency_key"], {
            "source_event_key": "linear:issue:ENG-39:update:second", "source_delivery_id": "second",
        })
        self.assertEqual(correlated["payload"]["source_event_keys"], ["linear:issue:ENG-39:update:first", "linear:issue:ENG-39:update:second"])
        self.assertEqual(correlated["payload"]["implementation_prompt"], "first")
        with self.assertRaises(BridgeValidationError):
            record_job_correlation(self.conn, job["job_id"], {"raw_payload": "rejected"})

    def test_event_history_is_bounded_and_counted(self):
        job, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:event-limit",
            "requested_action": "create_draft_pr",
            "payload": {"repo": "Driftward-LLC/driftward-host"},
        })
        for _ in range(3):
            submit_job(self.conn, {
                "idempotency_key": "hatchet:event-limit",
                "requested_action": "create_draft_pr",
                "payload": {"repo": "Driftward-LLC/driftward-host"},
            })
        self.assertEqual(len(list_events(self.conn, job["job_id"], 2)), 2)
        self.assertEqual(count_events(self.conn, job["job_id"]), 4)
        with self.assertRaises(BridgeValidationError):
            list_events(self.conn, job["job_id"], 251)

    def test_operator_retry_generations_are_transactional_and_idempotent(self):
        first, created = reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real", request_id="retry-001", reason="red CI", actor="root")
        self.assertTrue(created); self.assertEqual(first["generation"], 1)
        self.assertEqual(first["idempotency_key"], "hatchet:work-item:linear:ENG-8:create_draft_pr:real:active")
        replay, created = reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real", request_id="retry-001", reason="red CI", actor="root")
        self.assertFalse(created); self.assertEqual(replay["attempt_id"], first["attempt_id"])
        with self.assertRaises(BridgeConflict):
            reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real", request_id="retry-001", reason="different", actor="root")
        self.conn.execute("UPDATE factory_attempts SET status='failed' WHERE attempt_id=?", (first["attempt_id"],)); self.conn.commit()
        second, created = reserve_factory_attempt(self.conn, work_item_key="ENG-8", mode="real", request_id="retry-002", reason="retry after failure", actor="root")
        self.assertTrue(created); self.assertEqual(second["generation"], 2)
        self.assertEqual(second["branch"], deterministic_branch(second["idempotency_key"]))
        self.assertEqual(len(list_factory_attempts(self.conn, "ENG-8")), 2)

    def test_generationed_retry_tracks_lifecycle_and_cancel_is_non_destructive(self):
        attempt, job, created = reserve_factory_retry(
            self.conn, work_item_key="ENG-8", mode="real", request_id="atomic-1", reason="failed CI", actor="uid:0",
            payload=verifier_payload(),
        )
        self.assertTrue(created); self.assertEqual(attempt["status"], "accepted")
        canceled = cancel_factory_attempt(self.conn, attempt["attempt_id"], reason="operator stop", actor="uid:0")
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(get_job(self.conn, job["job_id"])["state"], "accepted")
        events = [event["event_type"] for event in list_factory_attempt_events(self.conn, attempt["attempt_id"])]
        self.assertEqual(events, ["retry_reserved", "retry_requested", "attempt_accepted", "accepted_event_enqueued", "cancellation_requested"])

    def test_intake_reserves_attempt_lineage_and_dry_run_does_not_block_real(self):
        dry_attempt, dry_job, created = reserve_factory_intake(
            self.conn, work_item_key="ENG-9", mode="dry-run", request_id="linear:issue:ENG-9:update:dry",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-9:update:dry"},
            hatchet_workflow_run_id="wf-dry", hatchet_step_run_id="step-dry",
        )
        self.assertTrue(created)
        self.assertEqual(dry_attempt["status"], "accepted")
        self.assertEqual(dry_job["payload"]["factory_retry"]["attempt_id"], dry_attempt["attempt_id"])
        self.assertEqual(dry_job["hatchet_workflow_run_id"], "wf-dry")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox WHERE job_id=?", (dry_job["job_id"],)).fetchone()[0], 1)

        transition_job(self.conn, dry_job["job_id"], state="running")
        transition_job(self.conn, dry_job["job_id"], state="completed", details={"reason_code": "dry_run_completed"})
        self.assertEqual(get_factory_attempt(self.conn, dry_attempt["attempt_id"])["status"], "completed")

        real_attempt, real_job, real_created = reserve_factory_intake(
            self.conn, work_item_key="ENG-9", mode="real", request_id="linear:issue:ENG-9:update:real",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-9:update:real"},
            hatchet_workflow_run_id="wf-real", hatchet_step_run_id="step-real",
        )
        self.assertTrue(real_created)
        self.assertEqual(real_attempt["mode"], "real")
        self.assertEqual(real_job["idempotency_key"], "hatchet:work-item:linear:ENG-9:create_draft_pr:real:active")
        self.assertEqual(real_job["payload"]["dry_run"], False)
        self.assertIsNotNone(real_job["payload"]["factory_retry"]["attempt_id"])
        self.assertTrue(claim_factory_execution(self.conn, real_job["job_id"], owner="executor")["claimed"])

    def test_intake_skips_orphaned_active_bridge_key(self):
        orphan, _ = submit_job(self.conn, {
            "idempotency_key": "hatchet:work-item:linear:ENG-9:create_draft_pr:real:active",
            "requested_action": "create_draft_pr",
            "payload": {
                **verifier_payload("Driftward-LLC/capykit"),
                "linear_issue": "ENG-9",
                "dry_run": False,
                "source_event_key": "linear:issue:ENG-9:update:old",
            },
        })
        self.conn.execute(
            "UPDATE bridge_jobs SET state='failed', reason_code='factory_attempt_required', error=? WHERE job_id=?",
            ("real create_draft_pr requires a valid factory attempt lineage", orphan["job_id"]),
        )
        self.conn.commit()

        attempt, job, created = reserve_factory_intake(
            self.conn, work_item_key="ENG-9", mode="real", request_id="linear:issue:ENG-9:update:new",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={
                **verifier_payload("Driftward-LLC/capykit"),
                "source_event_key": "linear:issue:ENG-9:update:new",
                "linear_issue": "ENG-9",
            },
        )
        self.assertTrue(created)
        self.assertEqual(attempt["generation"], 2)
        self.assertEqual(attempt["idempotency_key"], "hatchet:work-item:linear:ENG-9:create_draft_pr:real:attempt:2")
        self.assertEqual(job["idempotency_key"], attempt["idempotency_key"])
        self.assertNotEqual(job["job_id"], orphan["job_id"])
        self.assertEqual(get_job(self.conn, orphan["job_id"])["state"], "failed")

    def test_intake_reuses_active_attempt_and_records_event_correlation(self):
        first_attempt, first_job, created = reserve_factory_intake(
            self.conn, work_item_key="ENG-10", mode="real", request_id="linear:issue:ENG-10:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-10:update:first"},
        )
        self.assertTrue(created)
        second_attempt, second_job, second_created = reserve_factory_intake(
            self.conn, work_item_key="ENG-10", mode="real", request_id="linear:issue:ENG-10:update:second",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-10:update:second"},
        )
        self.assertFalse(second_created)
        self.assertEqual(second_attempt["attempt_id"], first_attempt["attempt_id"])
        self.assertEqual(second_job["job_id"], first_job["job_id"])
        self.assertEqual(second_job["payload"]["source_event_keys"], ["linear:issue:ENG-10:update:first", "linear:issue:ENG-10:update:second"])

        _replayed_attempt, _replayed_job, replayed_created = reserve_factory_intake(
            self.conn, work_item_key="ENG-10", mode="real", request_id="linear:issue:ENG-10:update:second",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-10:update:second"},
        )
        self.assertFalse(replayed_created)
        reuse_events = [
            event for event in list_factory_attempt_events(self.conn, first_attempt["attempt_id"])
            if event["event_type"] == "intake_active_job_reused"
        ]
        self.assertEqual(len(reuse_events), 1)
        self.assertEqual(reuse_events[0]["details"], {})

    def test_intake_exact_replay_records_one_payload_free_deduplication(self):
        request = {
            "work_item_key": "ENG-11", "mode": "real",
            "request_id": "linear:issue:ENG-11:update:first",
            "reason": "Linear Ready intake reserved a factory attempt", "actor": "intake-worker",
            "payload": {**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-11:update:first"},
        }
        attempt, _job, created = reserve_factory_intake(self.conn, **request)
        self.assertTrue(created)
        self.assertFalse(reserve_factory_intake(self.conn, **request)[2])
        self.assertFalse(reserve_factory_intake(self.conn, **request)[2])
        events = [
            event for event in list_factory_attempt_events(self.conn, attempt["attempt_id"])
            if event["event_type"] == "intake_request_deduplicated"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"], {})

    def test_execution_route_is_recorded_once_before_any_execution_lease(self):
        attempt, job, created = reserve_factory_intake(
            self.conn, work_item_key="ENG-40", mode="real", request_id="linear:issue:ENG-40:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={
                **verifier_payload("Driftward-LLC/capykit"),
                "source_event_key": "linear:issue:ENG-40:update:first",
                "routing": {"registry_version": 2},
            },
        )
        self.assertTrue(created)
        details = {
            "analytics_schema_version": 1,
            "assignment_id": f"route.{attempt['attempt_id']}",
            "experiment_id": "factory-default-v1",
            "variant": "hermes",
            "assignment_method": "static",
            "intended_backend": "hermes",
            "intended_transport": "grpc_uds",
            "configured_provider": "openai_codex",
            "configured_model": "gpt-5.5",
            "configured_auth_mode": "chatgpt_codex_oauth",
            "backend_version": "0.17.0",
            "factory_release_sha": "a" * 40,
            "factory_release_version": "2026.09.13",
            "routing_registry_version": 2,
            "verifier_policy_digest": job["payload"]["verification_policy"]["digest"],
            "fallback_allowed": False,
        }
        self.assertTrue(record_factory_execution_route(self.conn, job["job_id"], details))
        self.assertFalse(record_factory_execution_route(self.conn, job["job_id"], details))
        with self.assertRaises(BridgeValidationError):
            record_factory_execution_route(
                self.conn, job["job_id"], {**details, "fallback_allowed": True}
            )
        with self.assertRaises(BridgeConflict):
            record_factory_execution_route(self.conn, job["job_id"], {**details, "variant": "codex"})
        with self.assertRaises(BridgeValidationError):
            record_factory_execution_route(self.conn, job["job_id"], {**details, "raw_prompt": "forbidden"})
        with self.assertRaises(BridgeValidationError):
            record_factory_execution_route(self.conn, job["job_id"], {**details, "backend_version": "github_pat_secret"})
        for field, value in (
            ("configured_provider", "sk-proj-fake_provider_value"),
            ("configured_model", "gsk_fake_model_value"),
            ("backend_version", "v1+sk-ant-fake-version-value"),
            ("factory_release_version", "v1+github_pat_fake_version_value"),
            ("backend_version", "v1_sk-proj-FAKESECRET"),
            ("backend_version", "eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE"),
            ("factory_release_version", "v1+eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE"),
        ):
            with self.subTest(field=field), self.assertRaises(BridgeValidationError):
                record_factory_execution_route(self.conn, job["job_id"], {**details, field: value})
        _other_attempt, other_job, _ = reserve_factory_intake(
            self.conn, work_item_key="ENG-41", mode="real", request_id="linear:issue:ENG-41:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "source_event_key": "linear:issue:ENG-41:update:first"},
        )
        with self.assertRaises(BridgeValidationError):
            record_factory_execution_route(
                self.conn,
                other_job["job_id"],
                {**details, "assignment_id": f"route.{attempt['attempt_id']}"},
            )
        events = [
            event for event in list_factory_attempt_events(self.conn, attempt["attempt_id"])
            if event["event_type"] == "execution_route_selected"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"], details)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM factory_execution_leases WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0], 0)

        transition_job(self.conn, job["job_id"], state="running")
        execution = claim_factory_execution(self.conn, job["job_id"], owner="worker-route")
        self.assertTrue(execution["claimed"])
        self.assertTrue(authorize_factory_effect(self.conn, job["job_id"], execution["lease"]))
        receipt = {
            "commit_sha": "abcdef1",
            "pr_url": "https://github.com/Driftward-LLC/capykit/pull/40",
            "branch": job["branch"],
        }
        for forbidden in (
            {"summary": "raw model output"},
            {"hermes_session_id": "sk-proj-fake_session_value"},
            {"validation": {"model_output": "raw"}},
        ):
            with self.subTest(forbidden=next(iter(forbidden))), self.assertRaises(BridgeValidationError):
                commit_factory_execution(self.conn, job["job_id"], execution["lease"], {**receipt, **forbidden})
        identity = {
            "analytics_schema_version": 1,
            "invocation_id": "route-invocation-1",
            "execution_backend": "hermes",
            "execution_transport": "grpc_uds",
            "backend_version": "0.17.0",
            "configured_auth_mode": "chatgpt_codex_oauth",
        }
        self.assertTrue(record_factory_execution_analytics(
            self.conn, job["job_id"], execution["lease"],
            event_type="execution_backend_invoked", details=identity,
        ))
        with self.assertRaises(BridgeValidationError):
            _execution_analytics_details(
                "execution_backend_invoked",
                {**identity, "backend_version": "sk-proj-fake_version_value"},
            )
        with self.assertRaises(BridgeValidationError):
            _execution_analytics_details(
                "execution_backend_invoked",
                {
                    **identity,
                    "backend_version": "eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE",
                },
            )
        with self.assertRaises(BridgeConflict):
            record_factory_execution_analytics(
                self.conn, job["job_id"], execution["lease"],
                event_type="execution_backend_completed",
                details={
                    **identity,
                    "execution_transport": "local_command",
                    "backend_outcome": "created",
                    "elapsed_ms": 1,
                },
            )

    def test_active_attempts_include_sanitized_job_and_lease_summary(self):
        attempt, job, _ = reserve_factory_intake(
            self.conn, work_item_key="ENG-21", mode="real", request_id="linear:issue:ENG-21:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-21", "implementation_prompt": "keep this out of lifecycle summaries"},
        )
        transition_job(self.conn, job["job_id"], state="running")
        claim_factory_execution(self.conn, job["job_id"], owner="executor-secret-owner")

        active = list_active_factory_attempts(self.conn)
        recent = list_recent_factory_attempts(self.conn, 1)

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["attempt"]["attempt_id"], attempt["attempt_id"])
        self.assertEqual(active[0]["job"]["job_id"], job["job_id"])
        self.assertEqual(active[0]["job"]["payload"]["repo"], "Driftward-LLC/capykit")
        self.assertEqual(active[0]["job"]["payload"]["linear_issue"], "ENG-21")
        self.assertNotIn("implementation_prompt", active[0]["job"]["payload"])
        self.assertEqual(active[0]["execution_lease"]["status"], "active")
        self.assertTrue(active[0]["execution_lease"]["currently_live"])
        self.assertTrue(active[0]["execution_lease"]["fence_token_present"])
        self.assertNotIn("fence_token", active[0]["execution_lease"])
        self.assertEqual(recent[0]["attempt"]["attempt_id"], attempt["attempt_id"])
        with self.assertRaises(BridgeValidationError):
            list_recent_factory_attempts(self.conn, 0)

    def test_factory_lifecycle_stitches_attempt_job_and_lease_events(self):
        attempt, job, _ = reserve_factory_intake(
            self.conn, work_item_key="ENG-22", mode="real", request_id="linear:issue:ENG-22:update:first",
            reason="Linear Ready intake reserved a factory attempt", actor="intake-worker",
            payload={**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-22"},
        )
        transition_job(self.conn, job["job_id"], state="running")
        claim_factory_execution(self.conn, job["job_id"], owner="executor")

        lifecycle = get_factory_lifecycle(self.conn, "ENG-22")

        self.assertEqual(lifecycle["work_item_key"], "ENG-22")
        self.assertEqual(lifecycle["attempts"][0]["attempt"]["attempt_id"], attempt["attempt_id"])
        self.assertEqual(lifecycle["active_attempts"][0]["attempt"]["attempt_id"], attempt["attempt_id"])
        source_kinds = {event["source_kind"] for event in lifecycle["timeline"]}
        self.assertIn("factory_attempt", source_kinds)
        self.assertIn("bridge_job", source_kinds)
        self.assertIn("factory_execution_lease", source_kinds)
        event_types = [event["event_type"] for event in lifecycle["timeline"]]
        self.assertIn("attempt_execution_claimed", event_types)
        self.assertIn("transition:running", event_types)
        self.assertIn("execution_lease_acquired", event_types)
        self.assertFalse(lifecycle["timeline_truncated"])

        limited = get_factory_lifecycle(self.conn, "ENG-22", limit=1)
        self.assertEqual(len(limited["timeline"]), 1)
        self.assertTrue(limited["timeline_truncated"])
        self.assertEqual(limited["timeline"][0]["event_id"], lifecycle["timeline"][-1]["event_id"])
        with self.assertRaises(BridgeValidationError):
            get_factory_lifecycle(self.conn, "ENG-22", limit=0)

    def test_legacy_real_import_cannot_claim_new_verified_ready_path(self):
        job, _ = submit_job(self.conn, {"idempotency_key": "hatchet:work-item:linear:ENG-91:create_draft_pr:real:active", "requested_action": "create_draft_pr", "payload": {**verifier_payload("Driftward-LLC/capykit"), "linear_issue": "ENG-91"}})
        # Reopen migrations after a complete legacy artifact exists, then prove
        # the imported generation-one lineage cannot use the fresh verifier path.
        self.conn.execute("DELETE FROM factory_attempts"); self.conn.commit()
        self.conn.execute("UPDATE bridge_jobs SET state='completed',pr_url=? WHERE job_id=?", ("https://github.com/Driftward-LLC/capykit/pull/91", job["job_id"]))
        self.conn.execute("INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES ('legacy-eng91',?,'effect_committed','effect_committed',?,'2026-08-17T00:00:00Z')", (job["job_id"], json.dumps({"pr_url":"https://github.com/Driftward-LLC/capykit/pull/91", "head_branch":job["branch"]})))
        self.conn.commit()
        from hatchet_hermes_bridge.store import init_db
        init_db(self.conn)
        self.conn.execute("UPDATE bridge_jobs SET state='accepted',pr_url=NULL WHERE job_id=?", (job["job_id"],)); self.conn.commit()
        transition_job(self.conn, job["job_id"], state="running")
        committed = transition_job(self.conn, job["job_id"], state="effect_committed", details={"commit_sha":"abc", "pr_url":"https://github.com/Driftward-LLC/driftward-host/pull/91"})
        pending = transition_job(self.conn, committed["job_id"], state="verification_pending", details={"verification_context": original_context(job)})
        self.assertTrue(acquire_verification_lease(self.conn, pending["job_id"], attempt_id="legacy") ["acquired"])
        self.assertTrue(claim_verification_lease(self.conn, pending["job_id"], attempt_id="legacy"))
        receipt = verification_receipt(pending, "abc")
        record_verification_observation(self.conn, pending["job_id"], attempt_id="legacy", receipt=receipt)
        with self.assertRaisesRegex(BridgeValidationError, "legacy generation-one"):
            transition_job(self.conn, pending["job_id"], state="ready_for_human_review", details={"reason_code":"draft_pr_verified", "verification_attempt_id":"legacy", "verification_receipt":receipt})


if __name__ == "__main__":
    unittest.main()
