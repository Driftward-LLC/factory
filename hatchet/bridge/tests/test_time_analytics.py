import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hatchet_hermes_bridge.analytics import factory_analytics
from test_support import connect_migrate


class FactoryTimeAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        root = Path(self.work.name)
        self.bridge = str(root / "bridge.db")
        self.conn = connect_migrate(self.bridge)
        self.addCleanup(self.conn.close)
        self.now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.sequence = 0
        self.attempt_times = {}

    def _attempt(self, name, *, issue=None, days_ago=2, status="failed", mode="real", actor="intake-worker", prior=None):
        at = self.now - timedelta(days=days_ago)
        timestamp = at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.attempt_times[name] = at
        issue = issue or name
        generation = self.conn.execute(
            "SELECT count(*) + 1 FROM factory_attempts WHERE work_item_key=? AND mode=?", (issue, mode)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, name, status, "create_draft_pr", '{}', "fixture", timestamp, timestamp),
        )
        self.conn.execute(
            "INSERT INTO factory_attempts "
            "(attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,prior_attempt_id,actor,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, issue, "create_draft_pr", mode, generation, name, name, name, status, name, prior, actor, timestamp, timestamp),
        )
        self._event(name, "intake_reserved" if not prior else "retry_reserved")
        return name

    def _event(self, attempt, kind, details=None, *, seconds=0, bridge=False):
        self.sequence += 1
        timestamp = (self.attempt_times[attempt] + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        values = (f"event-{self.sequence:04}", attempt, kind, json.dumps(details or {}), timestamp)
        if bridge:
            self.conn.execute(
                "INSERT INTO bridge_events (event_id,job_id,event_type,details_json,created_at,state) VALUES (?,?,?,?,?,'effect_committed')", values
            )
        else:
            self.conn.execute("INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)", values)

    def _invocation(self, attempt, duration, *, invocation="run", kind="execution_backend_completed"):
        identity = {"invocation_id": invocation, "execution_backend": "fixture", "execution_transport": "local"}
        self._event(attempt, "execution_backend_invoked", identity)
        self._event(attempt, kind, {**identity, "elapsed_ms": duration}, seconds=3)

    def _human(self, attempt, duration, *, request="time-1", category="intervention", seconds=0):
        self._event(attempt, "human_time_recorded", {
            "schema_version": 1, "request_id": request, "duration_ms": duration,
            "category": category, "actor": "private-operator-identity",
        }, seconds=seconds)

    def _report(self):
        self.conn.commit()
        return factory_analytics(self.bridge, self.bridge + ".missing-ingress", self.bridge + ".missing-lifecycle", days=7, now=self.now)

    def test_effort_includes_prior_failed_retry_repair_and_unsuccessful_work(self):
        self._attempt("initial", issue="ENG-1", days_ago=29)
        self._invocation("initial", 60_000, kind="execution_backend_failed")
        self._human("initial", 1_000)
        self._attempt("retry", issue="ENG-1", days_ago=6, prior="initial")
        self._invocation("retry", 20_000, kind="execution_backend_failed")
        self._human("retry", 2_000)
        self._attempt("repair", issue="ENG-1", days_ago=5, status="verified_ready", prior="retry")
        self._event("repair", "repair_reserved")
        self._invocation("repair", 30_000)
        self._event("repair", "attempt_verified_ready", seconds=10)
        self._human("repair", 3_000, category="review")
        self._attempt("unsuccessful", issue="ENG-2")
        self._invocation("unsuccessful", 90_000, kind="execution_backend_failed")
        self._human("unsuccessful", 4_000, category="maintenance")
        self._attempt("outside", days_ago=29)
        self._invocation("outside", 999_999)

        effort = self._report()["effort"]
        self.assertEqual((effort["work_items"], effort["verified_work_items"], effort["attempts"]), (2, 1, 4))
        self.assertEqual(effort["active_attempts"], 0)
        self.assertEqual(effort["execution"]["total_ms"], 200_000)
        self.assertEqual(effort["execution"]["ms_per_verified_work_item"], 200_000)
        self.assertEqual(effort["execution"]["coverage"], {"observed": 4, "eligible": 4, "rate": 1.0, "complete": True})
        self.assertEqual(effort["human"]["recorded_ms"], 10_000)
        self.assertEqual(effort["human"]["recorded_ms_per_verified_work_item"], 10_000)
        self.assertEqual(effort["human"]["recorded_interventions"], 2)
        self.assertEqual(effort["human"]["by_category_ms"], {"intervention": 3_000, "review": 3_000, "maintenance": 4_000})

    def test_invocations_and_mirrored_receipts_are_counted_once(self):
        self._attempt("one", status="verified_ready")
        self._event("one", "attempt_verified_ready")
        self._invocation("one", 100, invocation="primary")
        self._event("one", "execution_backend_completed", {"invocation_id": "primary", "elapsed_ms": 100}, bridge=True)
        self._event("one", "transition:effect_committed", {"invocation_id": "primary", "execution_backend": "fixture", "backend_elapsed_ms": 999}, bridge=True)
        self._invocation("one", 200, invocation="failed", kind="execution_backend_failed")
        self._event("one", "execution_effect_commit_failed", {"invocation_id": "commit", "elapsed_ms": 300})
        self._event("one", "transition:effect_committed", {"invocation_id": "commit", "execution_backend": "fixture", "backend_elapsed_ms": 300}, bridge=True)
        self._event("one", "transition:effect_committed", {"invocation_id": "bridge", "execution_backend": "fixture", "backend_elapsed_ms": 400}, bridge=True)
        self._attempt("two", status="verified_ready")
        self._event("two", "attempt_verified_ready")
        self._invocation("two", 500, invocation="primary")

        execution = self._report()["effort"]["execution"]
        self.assertEqual(execution["total_ms"], 1_500)
        self.assertEqual(execution["ms_per_verified_work_item"], 750)
        self.assertEqual(execution["coverage"]["eligible"], 5)
        self.assertEqual(execution["active_invocations"], 0)

    def test_duration_fallback_uses_completion_not_late_settlement_timestamps(self):
        self._attempt("fallback")
        self._event("fallback", "execution_backend_invoked", {"invocation_id": "timestamp"}, seconds=1)
        self._event("fallback", "execution_backend_completed", {"invocation_id": "timestamp"}, seconds=3)
        self._event("fallback", "execution_backend_invoked", {"invocation_id": "receipt"})
        self._event("fallback", "execution_backend_completed", {"invocation_id": "receipt"}, seconds=10)
        self._event("fallback", "transition:effect_committed", {"invocation_id": "receipt", "execution_backend": "fixture", "backend_elapsed_ms": 250}, seconds=100, bridge=True)
        self._event("fallback", "execution_backend_invoked", {"invocation_id": "unmeasured"})
        self._event("fallback", "execution_effect_commit_failed", {"invocation_id": "unmeasured"}, seconds=100)

        execution = self._report()["effort"]["execution"]
        self.assertEqual(execution["observed_ms"], 2_250)
        self.assertEqual(execution["coverage"], {"observed": 2, "eligible": 3, "rate": 0.6667, "complete": False})
        self.assertIsNone(execution["total_ms"])

    def test_missing_active_and_historical_execution_evidence_prevents_total(self):
        self._attempt("complete", status="verified_ready")
        self._event("complete", "attempt_verified_ready")
        self._invocation("complete", 500)
        self._attempt("active", status="running")
        self._event("active", "execution_backend_invoked", {"invocation_id": "running"})
        self._attempt("historical")
        self._event("historical", "attempt_started")
        self._attempt("authorized")
        self._event("authorized", "effect_authorized")
        self._attempt("invalid-duration")
        self._event("invalid-duration", "execution_backend_failed", {"invocation_id": "bad", "elapsed_ms": True})
        self._attempt("missing-identity")
        self._event("missing-identity", "execution_backend_completed", {"elapsed_ms": 500})

        report = self._report()
        effort = report["effort"]
        self.assertEqual(effort["active_attempts"], 1)
        execution = effort["execution"]
        self.assertEqual(execution["observed_ms"], 500)
        self.assertEqual(execution["coverage"], {"observed": 1, "eligible": 6, "rate": 0.1667, "complete": False})
        self.assertEqual(execution["attempts_without_invocation_identity"], 3)
        self.assertEqual(execution["active_invocations"], 1)
        self.assertIsNone(execution["total_ms"])
        self.assertIsNone(execution["ms_per_verified_work_item"])
        self.assertTrue(any(value.startswith("total_execution_time:") for value in report["unavailable_metrics"]))

    def test_dispatch_before_invocation_is_not_machine_time_but_ambiguous_dispatch_is_gap(self):
        self._attempt("dispatch")
        self._event("dispatch", "attempt_started")
        self._event("dispatch", "effect_authorized")
        self._event("dispatch", "execution_dispatch_failed", {"invocation_id": "never-ran", "elapsed_ms": 125, "effect_outcome": "none"})
        execution = self._report()["effort"]["execution"]
        self.assertEqual(execution["total_ms"], 0)
        self.assertEqual(execution["coverage"]["eligible"], 0)

        self._attempt("ambiguous")
        self._event("ambiguous", "effect_authorized")
        self._event("ambiguous", "execution_dispatch_failed", {"invocation_id": "maybe-ran", "elapsed_ms": 125, "effect_outcome": "unknown"})
        execution = self._report()["effort"]["execution"]
        self.assertIsNone(execution["total_ms"])
        self.assertEqual(execution["coverage"]["eligible"], 1)

        # An earlier failed dispatch cannot explain an unmeasured later execution.
        self._event("dispatch", "effect_authorized", seconds=10)
        execution = self._report()["effort"]["execution"]
        self.assertEqual(execution["coverage"]["eligible"], 2)

    def test_zero_verified_no_human_and_explicit_zero_are_distinct(self):
        self._attempt("zero")
        self._invocation("zero", 0)
        effort = self._report()["effort"]
        self.assertEqual(effort["execution"]["total_ms"], 0)
        self.assertIsNone(effort["execution"]["ms_per_verified_work_item"])
        self.assertIsNone(effort["human"]["recorded_ms"])
        self.assertIsNone(effort["human"]["recorded_interventions"])
        self.assertIsNone(effort["human"]["by_category_ms"]["intervention"])
        self.assertFalse(effort["human"]["attempt_recording_coverage"]["complete"])

        self._human("zero", 0)
        human = self._report()["effort"]["human"]
        self.assertEqual((human["recorded_ms"], human["recorded_interventions"], human["entries"]), (0, 0, 1))
        self.assertIsNone(human["recorded_ms_per_verified_work_item"])
        self.assertTrue(human["attempt_recording_coverage"]["complete"])
        self.assertEqual(human["completeness"], "self_reported_not_attested")

    def test_human_entries_are_additive_deduplicated_and_not_exported(self):
        self._attempt("private-attempt")
        self._human("private-attempt", 100, request="private-request")
        self._human("private-attempt", 100, request="private-request")
        self._human("private-attempt", 200, request="different-request", category="review")
        self._human("private-attempt", True, request="malformed")
        self._human("private-attempt", 86_400_001, request="too-long")
        self._human("private-attempt", 5, request="bad-category", category={})
        self._event("private-attempt", "human_time_recorded", {"schema_version": True, "duration_ms": 5, "request_id": "bad-version", "category": "intervention"})
        report = self._report()
        human = report["effort"]["human"]
        self.assertEqual((human["recorded_ms"], human["entries"], human["recorded_interventions"]), (300, 2, 1))
        self.assertIsNone(human["by_category_ms"]["maintenance"])
        self.assertIsNone(report["human"]["actual_interventions"])
        for private in ("private-attempt", "private-request", "private-operator-identity"):
            self.assertNotIn(private, json.dumps(report["effort"]))

    def test_review_record_does_not_claim_zero_interventions(self):
        self._attempt("review")
        self._human("review", 100, category="review")
        human = self._report()["effort"]["human"]
        self.assertEqual(human["recorded_ms"], 100)
        self.assertIsNone(human["recorded_interventions"])
        self.assertIsNone(human["by_category_ms"]["intervention"])

    def test_cutoff_dry_run_legacy_and_durable_verification_boundaries(self):
        self._attempt("real", issue="ENG-1", status="verified_ready")
        self._invocation("real", 100)
        # Current mutable status must not invent a durable verification at cutoff.
        self._event("real", "attempt_verified_ready", seconds=3 * 86_400)
        self._human("real", 900, seconds=3 * 86_400)
        self._attempt("older", issue="ENG-1", days_ago=29, status="verified_ready")
        self._event("older", "attempt_verified_ready")
        self._invocation("older", 200)
        self._attempt("dry", issue="ENG-1", mode="dry-run")
        self._invocation("dry", 999)
        self._human("dry", 999)
        self._attempt("legacy", issue="ENG-1", actor="legacy-import")
        self._invocation("legacy", 999)
        self._attempt("future", issue="ENG-1", days_ago=-1)
        self._invocation("future", 999)
        self._attempt("future-verification", status="verified_ready")
        self._event("future-verification", "attempt_verified_ready", seconds=3 * 86_400)
        self._invocation("future-verification", 300)

        effort = self._report()["effort"]
        self.assertEqual((effort["work_items"], effort["attempts"], effort["verified_work_items"]), (2, 3, 1))
        self.assertEqual(effort["execution"]["total_ms"], 600)
        self.assertIsNone(effort["human"]["recorded_ms"])

    def test_projection_does_not_write_or_backfill_the_ledger(self):
        self._attempt("readonly")
        self._invocation("readonly", 123)
        self._human("readonly", 456)
        self.conn.commit()
        before = list(self.conn.iterdump())
        first = self._report()
        self.assertEqual(first, self._report())
        self.assertEqual(before, list(self.conn.iterdump()))


if __name__ == "__main__":
    unittest.main()
