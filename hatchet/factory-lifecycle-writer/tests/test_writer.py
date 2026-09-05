from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory_lifecycle_writer.cli import lifecycle_bind_host, status_health
from factory_lifecycle_writer.linear import LinearClient, LinearError
from factory_lifecycle_writer.outbox import Outbox
from factory_lifecycle_writer.writer import Config, LifecycleWriter, _canonical_timestamp

ISSUE_ID = "11111111-2222-4333-8444-555555555555"
EVENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeLinear:
    def __init__(self, *, marker: bool = False, fail: bool = False):
        self.marker, self.fail, self.created = marker, fail, []
    def verify_scope(self): return None
    def verify_issue_team(self, issue_id):
        if issue_id != ISSUE_ID: raise LinearError("linear_issue_team_mismatch")
    def marker_exists(self, issue_id, marker, *, heartbeat=None):
        if heartbeat and not heartbeat(): raise LinearError("lifecycle_lease_lost")
        return self.marker
    def create_comment(self, issue_id, body):
        if self.fail: raise LinearError("linear_api_timeout")
        self.created.append((issue_id, body))


def config() -> Config:
    return Config("http://bridge", "bridge-token", "http://ingress", "factory-token", "2000-01-01T00:00:00Z", frozenset({"Driftward-LLC/capykit"}))


def event(*, state="running", dry_run=False, issue_id=ISSUE_ID, pr_url=None, reason=None, created="2099-01-01T00:00:00Z", event_id=EVENT_ID, repo="Driftward-LLC/capykit", job_id="job-1"):
    return {"eventId": event_id, "jobId": job_id, "idempotencyKey": f"key-{job_id}", "state": state, "prUrl": pr_url,
            "reasonCode": reason, "createdAt": created,
            "payload": {"linear_issue_id": issue_id, "linear_issue": "ENG-52", "linear_issue_url": "https://linear.app/driftward/issue/ENG-52/test", "source_event_key": "linear:event:1", "source_delivery_id": "delivery-1", "repo": repo, "dry_run": dry_run}}


class PagedWriter(LifecycleWriter):
    def __init__(self, config, outbox, pages):
        super().__init__(config, outbox); self.pages = pages; self.calls = []; self.urls = []
    def _json(self, url, *, bridge):
        source = "bridge" if bridge else "ingress"
        self.urls.append(url)
        index = sum(1 for value in self.calls if value == source)
        self.calls.append(source)
        return self.pages[source][index] if index < len(self.pages[source]) else {"events" if bridge else "observations": []}


class LifecycleWriterTest(unittest.TestCase):
    def make(self, pages=None):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        outbox = Outbox(str(Path(directory.name) / "outbox.db")); self.addCleanup(outbox.close)
        writer = PagedWriter(config(), outbox, pages or {"bridge": [{"events": []}], "ingress": [{"observations": []}]})
        writer.linear = FakeLinear()
        return writer, outbox

    def test_event_stream_cursor_drains_over_250_without_loss(self):
        events = [event(state="failed", event_id=f"aaaaaaaa-bbbb-4ccc-8ddd-{index:012d}", job_id=f"job-{index}") for index in range(250)]
        final = event(state="failed", event_id="aaaaaaaa-bbbb-4ccc-8ddd-999999999999", job_id="job-final")
        writer, outbox = self.make({"bridge": [{"events": events}, {"events": [final]}], "ingress": [{"observations": []}]})
        writer.scan()
        self.assertEqual(outbox.status()["states"]["pending"], 251)
        self.assertIsNotNone(outbox.get_meta("cursor:bridge:v1"))
        bridge_urls = [url for url in writer.urls if url.startswith("http://bridge")]
        self.assertEqual({url.split("before_created_at=")[1].split("&")[0] for url in bridge_urls}, {bridge_urls[0].split("before_created_at=")[1].split("&")[0]})

    def test_snapshot_scans_more_than_one_hundred_pages_without_moving_tail(self):
        page = [event(state="accepted", event_id=f"aaaaaaaa-bbbb-4ccc-8ddd-{index:012d}", job_id=f"job-{index}") for index in range(250)]
        writer, _ = self.make({"bridge": [{"events": page}] * 101 + [{"events": []}], "ingress": [{"observations": []}]})
        writer.scan()
        self.assertGreaterEqual(len([url for url in writer.urls if url.startswith("http://bridge")]), 102)

    def test_source_failure_does_not_advance_cursor_and_delivery_still_drains(self):
        writer, outbox = self.make({"bridge": [{"events": [event(state="failed")]}], "ingress": [{"observations": "bad"}]})
        writer.scan()
        self.assertIn("ingress", writer.source_errors)
        self.assertEqual(writer.deliver()["delivered"], 1)
        self.assertIsNone(outbox.get_meta("cursor:ingress:v1"))

    def test_two_outboxes_cannot_claim_same_row_and_expired_lease_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "outbox.db")
            first, second = Outbox(path), Outbox(path)
            self.addCleanup(first.close); self.addCleanup(second.close)
            writer = LifecycleWriter(config(), first)
            first.record(writer._bridge_candidate(event(state="failed")))
            claim_a, claim_b = first.claim_one("a"), second.claim_one("b")
            self.assertIsNotNone(claim_a); self.assertIsNone(claim_b)
            first.conn.execute("UPDATE lifecycle_outbox SET lease_until = '2000-01-01T00:00:00Z' WHERE outbox_key = ?", (claim_a["outbox_key"],))
            self.assertIsNotNone(second.claim_one("b"))

    def test_expired_owner_is_fenced_before_a_slow_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(str(Path(directory) / "outbox.db")); self.addCleanup(outbox.close)
            outbox.record(LifecycleWriter(config(), outbox)._bridge_candidate(event(state="failed")))
            claimed = outbox.claim_one("slow", lease_seconds=1)
            outbox.conn.execute("UPDATE lifecycle_outbox SET lease_until = '2000-01-01T00:00:00.000000Z' WHERE outbox_key = ?", (claimed["outbox_key"],))
            self.assertFalse(outbox.renew(claimed["outbox_key"], "slow"))
            self.assertIsNotNone(outbox.claim_one("reclaimer"))

    def test_reclaim_during_slow_marker_pagination_fences_comment_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "outbox.db")
            primary, reclaimer = Outbox(path), Outbox(path)
            self.addCleanup(primary.close); self.addCleanup(reclaimer.close)
            writer = LifecycleWriter(config(), primary)
            candidate = writer._bridge_candidate(event(state="failed"))
            primary.record(candidate)
            test = self

            class SlowMarkerLinear(FakeLinear):
                def marker_exists(self, issue_id, marker, *, heartbeat=None):
                    test.assertTrue(heartbeat())
                    # Model a provider page outliving the lease: another process
                    # recovers the row before this stale process reaches create.
                    reclaimer.conn.execute(
                        "UPDATE lifecycle_outbox SET lease_until = ? WHERE outbox_key = ?",
                        ("2000-01-01T00:00:00.000000Z", candidate["outbox_key"]),
                    )
                    test.assertIsNotNone(reclaimer.claim_one("other-owner"))
                    return False

            writer.linear = SlowMarkerLinear()
            self.assertEqual(writer.deliver()["delivered"], 0)
            self.assertEqual(writer.linear.created, [])

    def test_legacy_outbox_migrates_before_lease_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "outbox.db")
            legacy = Outbox(path)
            legacy.conn.execute("DROP INDEX IF EXISTS idx_lifecycle_lease")
            legacy.conn.execute("ALTER TABLE lifecycle_outbox RENAME TO legacy")
            legacy.conn.execute("CREATE TABLE lifecycle_outbox AS SELECT outbox_key, phase, delivery_state, issue_id, identifier, issue_url, job_id, idempotency_key, source_event_key, source_delivery_id, repo, pr_url, reason_code, execution_mode, source_kind, source_recorded_at, marker, body, attempts, next_attempt_at, first_seen_at, last_seen_at, delivered_at, suppressed_at, last_error_code FROM legacy")
            legacy.close()
            migrated = Outbox(path); self.addCleanup(migrated.close)
            columns = {row[1] for row in migrated.conn.execute("PRAGMA table_info(lifecycle_outbox)")}
            self.assertTrue({"lease_owner", "lease_until"}.issubset(columns))

    def test_legacy_timestamps_and_cursor_are_canonicalized_before_comparison(self):
        writer, outbox = self.make()
        item = writer._bridge_candidate(event(state="failed", created="2099-01-01T02:00:00+02:00"))
        outbox.record(item)
        outbox.conn.execute(
            "UPDATE lifecycle_outbox SET source_recorded_at = ? WHERE outbox_key = ?",
            ("2099-01-01T02:00:00+02:00", item["outbox_key"]),
        )
        outbox.set_meta(
            "cursor:bridge:v1",
            '{"createdAt":"2099-01-01T02:00:00+02:00","eventId":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}',
        )
        outbox.close()
        migrated = Outbox(outbox.path)
        self.addCleanup(migrated.close)
        resumed = LifecycleWriter(config(), migrated)
        self.assertEqual(
            migrated.conn.execute(
                "SELECT source_recorded_at FROM lifecycle_outbox WHERE outbox_key = ?",
                (item["outbox_key"],),
            ).fetchone()[0],
            "2099-01-01T00:00:00.000000Z",
        )
        self.assertEqual(resumed._cursor("cursor:bridge:v1")["createdAt"], "2099-01-01T00:00:00.000000Z")
        self.assertIn(".000000Z", migrated.get_meta("cursor:bridge:v1"))

    def test_crash_after_create_recovers_by_marker(self):
        writer, outbox = self.make({"bridge": [{"events": [event(state="failed")]}], "ingress": [{"observations": []}]})
        writer.linear = FakeLinear(marker=True); writer.scan()
        self.assertEqual(writer.deliver()["recovered"], 1)
        self.assertEqual(outbox.status()["states"]["recovered"], 1)

    def test_draft_artifact_snapshot_never_generates_success_before_verified_ready(self):
        writer, outbox = self.make()
        first = writer._bridge_candidate(event(state="completed", dry_run=True, pr_url="https://github.com/Driftward-LLC/capykit/pull/1")); outbox.record(first)
        mutated = writer._bridge_candidate(event(state="completed", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/1")); outbox.record(mutated)
        self.assertEqual(outbox.status()["states"]["suppressed"], 1)
        self.assertEqual(writer.deliver()["delivered"], 0)

    def test_pending_is_visible_once_but_only_verified_ready_generates_success(self):
        writer, outbox = self.make()
        for state in ("effect_committed", "completed"):
            candidate = writer._bridge_candidate(event(state=state, dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/1", job_id=f"job-{state}"))
            outbox.record(candidate)
            self.assertEqual(candidate["delivery_state"], "suppressed")
        pending = writer._bridge_candidate(event(state="verification_pending", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/1", job_id="job-pending"))
        duplicate_pending = writer._bridge_candidate(event(state="verification_pending", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/1", job_id="job-pending"))
        outbox.record(pending); outbox.record(duplicate_pending)
        self.assertEqual(pending["phase"], "verification_pending")
        self.assertEqual(writer.deliver()["delivered"], 1)
        self.assertIn("Deterministic verification is pending", writer.linear.created[0][1])
        ready = writer._bridge_candidate(event(state="ready_for_human_review", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/2", job_id="job-ready"))
        outbox.record(ready)
        self.assertEqual(ready["phase"], "succeeded")
        self.assertEqual(writer.deliver()["delivered"], 1)
        self.assertIn("Factory deterministic draft-PR verification passed", writer.linear.created[1][1])

        attempt_ready = event(state="verified_ready", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/2")
        attempt_ready.update(sourceKind="factory_attempt", eventType="attempt_verified_ready", attemptId="attempt-1", generation=1)
        self.assertIsNone(writer._bridge_candidate(attempt_ready))
        self.assertEqual(writer.deliver()["delivered"], 0)

    def test_attempt_verified_ready_is_silent(self):
        writer, _ = self.make()
        candidate = event(state="verified_ready", dry_run=False, pr_url="https://github.com/Driftward-LLC/capykit/pull/2")
        candidate.update(sourceKind="factory_attempt", eventType="attempt_verified_ready", attemptId="attempt-1", generation=1)
        self.assertIsNone(writer._bridge_candidate(candidate))

    def test_bridge_running_is_silent_and_attempt_start_is_single_comment(self):
        writer, outbox = self.make()
        bridge_start = event(state="running", dry_run=False)
        attempt_start = event(state="running", dry_run=False)
        attempt_start.update({"sourceKind": "factory_attempt", "eventType": "attempt_started", "attemptId": "attempt-1", "generation": 1})

        self.assertIsNone(writer._bridge_candidate(bridge_start))
        outbox.record(writer._bridge_candidate(attempt_start))

        self.assertEqual(writer.deliver()["delivered"], 1)
        self.assertIn("Factory attempt 1 started", writer.linear.created[0][1])
        self.assertNotIn("Factory work started", writer.linear.created[0][1])
        self.assertNotIn("Factory retry attempt 1", writer.linear.created[0][1])

    def test_unknown_or_malformed_mode_dead_letters(self):
        writer, _ = self.make()
        for value in (None, "false", 0):
            candidate = event(state="failed"); candidate["payload"]["dry_run"] = value
            self.assertEqual(writer._bridge_candidate(candidate)["delivery_state"], "dead_letter")

    def test_status_health_keeps_historical_dead_letters_visible_but_nonfatal(self):
        self.assertEqual(
            status_health({}, {"retried": 0}, {"dead_letters": 1, "oldest_queue_age_seconds": None}),
            (True, None),
        )
        self.assertEqual(
            status_health({"bridge": "bridge_source_unavailable"}, {"retried": 0}, {"dead_letters": 0, "oldest_queue_age_seconds": None}),
            (False, "source_or_outbox_degraded"),
        )
        self.assertEqual(
            status_health({}, {"retried": 1}, {"dead_letters": 0, "oldest_queue_age_seconds": None}),
            (False, "source_or_outbox_degraded"),
        )
        self.assertEqual(
            status_health({}, {"retried": 0}, {"dead_letters": 0, "oldest_queue_age_seconds": 601}),
            (False, "source_or_outbox_degraded"),
        )

    def test_lifecycle_bind_host_defaults_to_loopback_and_allows_container_override(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(lifecycle_bind_host(), "127.0.0.1")
        with mock.patch.dict("os.environ", {"FACTORY_LIFECYCLE_HOST": "0.0.0.0"}, clear=True):
            self.assertEqual(lifecycle_bind_host(), "0.0.0.0")

    def test_config_from_env_reads_lifecycle_env_file_and_overrides_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "factory-lifecycle-writer.env"
            env_path.write_text(
                "\n".join(
                    [
                        "LINEAR_FACTORY_ACCESS_TOKEN=from-file",
                        "HATCHET_HERMES_BRIDGE_TOKEN=bridge-token",
                        "FACTORY_LIFECYCLE_ACTIVATION_WATERMARK=2026-08-25T00:00:00Z",
                        "FACTORY_LIFECYCLE_ALLOWED_REPOS=Driftward-LLC/factory",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "FACTORY_LIFECYCLE_ENV_FILE": str(env_path),
                    "LINEAR_FACTORY_ACCESS_TOKEN": "stale-process-token",
                },
                clear=True,
            ):
                config = Config.from_env()

        self.assertEqual(config.token, "from-file")
        self.assertEqual(config.bridge_token, "bridge-token")
        self.assertEqual(config.allowed_repos, frozenset({"Driftward-LLC/factory"}))

    def test_pre_activation_and_missing_source_time_suppress_or_dead_letter(self):
        writer, _ = self.make()
        self.assertEqual(writer._bridge_candidate(event(created="1999-01-01T00:00:00Z"))["delivery_state"], "suppressed")
        self.assertEqual(writer._bridge_candidate(event(created=None))["delivery_state"], "dead_letter")

    def test_failure_is_allowlisted_and_never_uses_raw_error(self):
        writer, _ = self.make()
        candidate = writer._bridge_candidate(event(state="failed", reason="raw command output"))
        self.assertEqual(candidate["reason_code"], "execution_failed_unclassified")
        self.assertNotIn("raw command", candidate["body"])

    def test_verification_policy_mismatch_failures_are_allowlisted(self):
        writer, _ = self.make()
        for reason in ("verification_local_command_policy_mismatch", "verification_secret_check_policy_mismatch"):
            with self.subTest(reason=reason):
                candidate = writer._bridge_candidate(event(state="failed", reason=reason))
                self.assertEqual(candidate["reason_code"], reason)
                self.assertIn(f"Reason: `{reason}`", candidate["body"])

    def test_generationed_attempt_events_are_exactly_once_and_replace_bridge_phrasing(self):
        writer, outbox = self.make()
        retry_payload = event(); retry_payload["payload"]["factory_retry"] = {"attempt_id": "attempt-2", "generation": 2}
        self.assertIsNone(writer._bridge_candidate(retry_payload))
        attempt = event(); attempt.update({"sourceKind": "factory_attempt", "eventType": "retry_requested", "attemptId": "attempt-2", "generation": 2})
        candidate = writer._bridge_candidate(attempt)
        self.assertEqual(candidate["phase"], "retry_requested")
        outbox.record(candidate); outbox.record(writer._bridge_candidate(attempt))
        self.assertEqual(writer.deliver()["delivered"], 1)
        self.assertIn("attempt 2", writer.linear.created[0][1])

        retry_start = event(); retry_start.update({"sourceKind": "factory_attempt", "eventType": "attempt_started", "attemptId": "attempt-2", "generation": 2})
        self.assertIn("Factory retry attempt 2 started", writer._bridge_candidate(retry_start)["body"])

        repair = event(pr_url="https://github.com/Driftward-LLC/capykit/pull/2")
        repair.update({"sourceKind": "factory_attempt", "eventType": "repair_requested", "attemptId": "attempt-3", "generation": 3})
        self.assertIn("Factory repair requested", writer._bridge_candidate(repair)["body"])

    def test_strict_issue_repo_and_pr_inputs_dead_letter(self):
        writer, _ = self.make()
        for change in (
            {"repo": "outside/repo"},
            {"linear_issue_url": "https://linear.app/driftward/issue/ENG-x/bad"},
            {"pr_url": "https://github.com/Driftward-LLC/capykit/pull/nope"},
        ):
            candidate = event(state="ready_for_human_review", pr_url="https://github.com/Driftward-LLC/capykit/pull/1")
            candidate["payload"].update({key: value for key, value in change.items() if key in candidate["payload"]})
            if "pr_url" in change: candidate["prUrl"] = change["pr_url"]
            self.assertEqual(writer._bridge_candidate(candidate)["delivery_state"], "dead_letter")
        for decorated in ("https://linear.app:443/driftward/issue/ENG-52/test", "https://user@linear.app/driftward/issue/ENG-52/test", "https://linear.app.evil/driftward/issue/ENG-52/test", "https://linear.app/driftward/issue/ENG-52/test?x=1"):
            candidate = event(); candidate["payload"]["linear_issue_url"] = decorated
            self.assertEqual(writer._bridge_candidate(candidate)["delivery_state"], "dead_letter")
        for decorated in ("https://github.com:443/Driftward-LLC/capykit/pull/1", "https://user@github.com/Driftward-LLC/capykit/pull/1", "https://github.com.evil/Driftward-LLC/capykit/pull/1", "https://github.com/Driftward-LLC/capykit/pull/1#x"):
            candidate = event(state="ready_for_human_review", pr_url=decorated)
            self.assertEqual(writer._bridge_candidate(candidate)["delivery_state"], "dead_letter")

    def test_timestamp_normalization_rejects_naive_and_preserves_offset_order(self):
        self.assertIsNone(_canonical_timestamp("2099-01-01T00:00:00"))
        self.assertIsNone(_canonical_timestamp("not-a-time"))
        self.assertEqual(_canonical_timestamp("2099-01-01T02:00:00+02:00"), "2099-01-01T00:00:00.000000Z")

    def test_routing_disabled_project_is_ignored(self):
        writer, _ = self.make()
        observation = {"eventId": EVENT_ID, "createdAt": "2099-01-01T00:00:00Z", "issueId": ISSUE_ID, "identifier": "ENG-52", "issueUrl": "https://linear.app/driftward/issue/ENG-52/test", "eventKey": "linear:event:2", "deliveryId": "d2", "reasonCode": "linear_issue_project_execution_disabled"}
        self.assertIsNone(writer._routing_candidate(observation))

    def test_canary_is_outbox_only_and_requires_valid_issue(self):
        writer, outbox = self.make(); writer.canary(issue_id=ISSUE_ID, identifier="ENG-52", issue_url="https://linear.app/driftward/issue/ENG-52/test")
        self.assertEqual(writer.deliver()["delivered"], 1); self.assertEqual(outbox.status()["states"]["delivered"], 1)


class LinearClientTest(unittest.TestCase):
    def test_marker_lookup_continues_beyond_one_thousand_comments(self):
        class Client(LinearClient):
            def __init__(self): super().__init__("token"); self.page = 0
            def _request(self, query, variables):
                self.page += 1
                marker = "needle" if self.page == 11 else "not-it"
                return {"issue": {"comments": {"nodes": [{"body": marker}] * 100, "pageInfo": {"hasNextPage": self.page < 11, "endCursor": str(self.page)}}}}
        self.assertTrue(Client().marker_exists(ISSUE_ID, "needle"))

    def test_identity_and_target_team_are_exact_ids(self):
        class Client(LinearClient):
            def _request(self, query, variables):
                if "FactoryIdentity" in query:
                    return {"viewer": {"id": "4945f724-8539-46c6-b024-e03b90257ac2", "name": "renamed", "organization": {"id": "aa6f0902-c91a-4aa3-a8cb-13f03df89bbf", "name": "elsewhere"}}, "teams": {"nodes": [{"id": "2e9999a3-4e98-4c3f-ba16-1de1406dfa89", "name": "x", "key": "x"}]}}
                return {"issue": {"id": ISSUE_ID, "team": {"id": "2e9999a3-4e98-4c3f-ba16-1de1406dfa89"}}}
        client = Client("token"); client.verify_scope(); client.verify_issue_team(ISSUE_ID)


class BoundaryTest(unittest.TestCase):
    def test_factory_credentials_are_not_referenced_by_workers_bridge_ingress_or_hermes(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ("workers", "bridge", "event-ingress"):
            for path in (root / relative).rglob("*"):
                if path.is_file() and "node_modules" not in path.parts and path.suffix in {".py", ".ts", ".sh", ".service"}:
                    self.assertNotIn("LINEAR_FACTORY_", path.read_text(encoding="utf-8"), str(path))
