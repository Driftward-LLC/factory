from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hatchet_event_ingress.normalizer import normalize_github_pull_request, normalize_linear_issue
from hatchet_event_ingress.store import IngressConflict, claim_pending_publication, connect, latest_observation, list_lifecycle_observations, list_pending_publications, list_reconciliation_observations, mark_publication_failed, mark_published, publication_status, record_event


class IngressStoreTest(unittest.TestCase):
    def test_record_event_deduplicates_same_payload(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                first, first_created = record_event(conn, normalized=normalized, raw_payload=payload)
                second, second_created = record_event(conn, normalized=normalized, raw_payload=payload)
            finally:
                conn.close()

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["event_key"], second["event_key"])

    def test_record_event_conflicts_on_changed_payload(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")
        changed = _payload()
        changed["pull_request"]["title"] = "ENG-7 changed"

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalized, raw_payload=payload)
                with self.assertRaises(IngressConflict):
                    record_event(conn, normalized=normalized, raw_payload=changed)
            finally:
                conn.close()

    def test_mark_published_records_hatchet_event(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                event, _ = record_event(conn, normalized=normalized, raw_payload=payload)
                published = mark_published(conn, event["event_key"], "hatchet-event-1")
            finally:
                conn.close()

        self.assertTrue(published["hatchet_published"])
        self.assertEqual(published["hatchet_event_id"], "hatchet-event-1")

    def test_pending_publication_is_leased_retried_and_visible_in_health_state(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-lease")
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                event, _ = record_event(conn, normalized=normalized, raw_payload=payload)
                claimed = claim_pending_publication(conn, event["event_key"])
                self.assertIsNotNone(claimed)
                self.assertIsNone(claim_pending_publication(conn, event["event_key"]))
                with self.assertRaises(IngressConflict):
                    mark_published(conn, event["event_key"], "wrong-owner", lease_id="wrong-lease")
                mark_publication_failed(conn, event["event_key"], claimed["publish_lease_id"])
                retried = claim_pending_publication(conn, event["event_key"])
                published = mark_published(conn, event["event_key"], "hatchet-event-2", lease_id=retried["publish_lease_id"])
                status = publication_status(conn)
                pending = list_pending_publications(conn)
            finally:
                conn.close()

        self.assertEqual(published["publish_attempts"], 2)
        self.assertEqual(status["unpublished"], 0)
        self.assertEqual(pending, [])

    def test_record_event_accepts_generic_subject(self) -> None:
        payload = _linear_payload()
        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                event, created = record_event(conn, normalized=normalized, raw_payload=payload)
            finally:
                conn.close()

        self.assertTrue(created)
        self.assertEqual(event["subject"], "ENG-40")
        self.assertEqual(event["hatchet_event_name"], "driftward.linear.issue.ready_for_implementation")

    def test_latest_observation_is_sanitized(self) -> None:
        payload = _linear_payload()
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalize_linear_issue(payload, delivery_id="linear-observation-1"), raw_payload=payload)
                observation = latest_observation(conn, source="linear", subject="ENG-40")
            finally:
                conn.close()
        self.assertEqual(observation["linear"]["status"], "Ready")
        self.assertNotIn("description", observation["linear"])

    def test_lifecycle_projection_has_uuid_correlation_but_no_raw_body(self) -> None:
        payload = _linear_payload()
        payload["data"]["description"] = "secret-like implementation prose"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalize_linear_issue(payload, delivery_id="lifecycle-1"), raw_payload=payload)
                observations = list_lifecycle_observations(conn)
            finally:
                conn.close()
        self.assertEqual(observations[0]["issueId"], "issue-uuid")
        self.assertEqual(observations[0]["identifier"], "ENG-40")
        self.assertEqual(observations[0]["deliveryId"], "lifecycle-1")
        self.assertNotIn("description", observations[0])
        self.assertNotIn("raw_payload", observations[0])

    def test_lifecycle_projection_cursor_is_ascending_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conn = connect(f"{directory}/events.db")
            self.addCleanup(conn.close)
            for delivery in ("one", "two"):
                payload = _linear_payload()
                record_event(conn, normalized=normalize_linear_issue(payload, delivery_id=delivery), raw_payload=payload)
            first = list_lifecycle_observations(conn, limit=1)
            frozen = list_lifecycle_observations(
                conn,
                before_created_at=first[0]["createdAt"],
                before_event_id=first[0]["eventId"],
                limit=10,
            )
            second = list_lifecycle_observations(conn, limit=10, after_created_at=first[0]["createdAt"], after_event_id=first[0]["eventId"])
            self.assertEqual(len(first), 1)
            self.assertEqual(len(frozen), 1)
            self.assertEqual(len(second), 1)

    def test_sanitized_observation_omits_absent_provider_timestamp(self) -> None:
        payload = _linear_payload()
        del payload["data"]["updatedAt"]
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalize_linear_issue(payload, delivery_id="without-provider-time"), raw_payload=payload)
                observation = latest_observation(conn, source="linear", subject="ENG-40")
            finally:
                conn.close()
        self.assertNotIn("providerUpdatedAt", observation)

    def test_latest_observation_uses_provider_time_not_delivery_time(self) -> None:
        newer = _linear_payload()
        newer["data"]["updatedAt"] = "2026-08-12T12:00:00Z"
        older_delayed = _linear_payload()
        older_delayed["data"]["updatedAt"] = "2026-08-12T11:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalize_linear_issue(newer, delivery_id="newer"), raw_payload=newer)
                record_event(conn, normalized=normalize_linear_issue(older_delayed, delivery_id="older-delayed"), raw_payload=older_delayed)
                observation = latest_observation(conn, source="linear", subject="ENG-40")
                inventory = list_reconciliation_observations(conn)
            finally:
                conn.close()
        self.assertEqual(observation["eventKey"], "linear:issue:ENG-40:update:newer")
        self.assertEqual(observation["providerUpdatedAt"], "2026-08-12T12:00:00.000000Z")
        self.assertTrue(inventory[0]["hatchetPublished"] is False)
        self.assertNotIn("raw_payload", inventory[0])

    def test_provider_order_normalizes_offsets_and_breaks_equal_instants_by_event_id(self) -> None:
        first = _linear_payload()
        first["data"]["updatedAt"] = "2026-08-12T10:00:00Z"
        equivalent = copy.deepcopy(first)
        equivalent["data"]["updatedAt"] = "2026-08-12T12:00:00+02:00"
        later = copy.deepcopy(first)
        later["data"]["updatedAt"] = "2026-08-12T05:00:01-05:00"

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                with patch("hatchet_event_ingress.store.uuid.uuid4", side_effect=["event-a", "event-b", "event-c"]):
                    record_event(conn, normalized=normalize_linear_issue(first, delivery_id="first"), raw_payload=first)
                    record_event(conn, normalized=normalize_linear_issue(equivalent, delivery_id="equivalent"), raw_payload=equivalent)
                    equal_instant = latest_observation(conn, source="linear", subject="ENG-40")
                    record_event(conn, normalized=normalize_linear_issue(later, delivery_id="later"), raw_payload=later)
                    latest = latest_observation(conn, source="linear", subject="ENG-40")
                    inventory = list_reconciliation_observations(conn)
            finally:
                conn.close()

        self.assertEqual(equal_instant["eventKey"], "linear:issue:ENG-40:update:equivalent")
        self.assertEqual(equal_instant["providerUpdatedAt"], "2026-08-12T10:00:00.000000Z")
        self.assertEqual(latest["eventKey"], "linear:issue:ENG-40:update:later")
        self.assertEqual(latest["providerUpdatedAt"], "2026-08-12T10:00:01.000000Z")
        self.assertEqual(inventory[0]["eventKey"], latest["eventKey"])

    def test_migrates_legacy_rows_to_numeric_provider_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/ingress.db"
            legacy = sqlite3.connect(path)
            try:
                legacy.execute(
                    """
                    CREATE TABLE ingress_events (
                        event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                        source TEXT NOT NULL, provider_event TEXT NOT NULL, action TEXT NOT NULL,
                        delivery_id TEXT, subject TEXT NOT NULL, payload_hash TEXT NOT NULL,
                        raw_payload_json TEXT NOT NULL, normalized_json TEXT NOT NULL,
                        hatchet_event_name TEXT NOT NULL, hatchet_event_id TEXT,
                        hatchet_published INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL,
                        reason_code TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                    """
                )
                legacy.execute(
                    """
                    INSERT INTO ingress_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-event", "linear:issue:ENG-40:update:legacy", "linear", "Issue", "update",
                        "legacy", "ENG-40", "hash", "{}", json.dumps({"updatedAt": "2026-08-12T12:00:00+02:00"}),
                        "driftward.linear.issue.observed", None, 0, "observe", "not_ready",
                        "2026-08-12T10:01:00Z", "2026-08-12T10:01:00Z",
                    ),
                )
                legacy.commit()
            finally:
                legacy.close()

            conn = connect(path)
            try:
                row = conn.execute(
                    "SELECT provider_updated_at_us FROM ingress_events WHERE event_id = ?",
                    ("legacy-event",),
                ).fetchone()
                observation = latest_observation(conn, source="linear", subject="ENG-40")
            finally:
                conn.close()

        self.assertIsInstance(row["provider_updated_at_us"], int)
        self.assertEqual(observation["providerUpdatedAt"], "2026-08-12T10:00:00.000000Z")


def _payload() -> dict:
    return {
        "action": "closed",
        "repository": {"full_name": "Driftward-LLC/capykit"},
        "pull_request": {
            "number": 11,
            "html_url": "https://github.com/Driftward-LLC/capykit/pull/11",
            "title": "ENG-7: implement Capykit discovery CLI commands",
            "body": "Refs ENG-7",
            "state": "closed",
            "merged": False,
            "closed_at": "2026-08-11T08:59:43Z",
            "head": {"ref": "codex/eng-7-implement-capykit-discovery-cli-commands"},
            "base": {"ref": "main"},
        },
    }


def _linear_payload() -> dict:
    return {
        "action": "update",
        "type": "Issue",
        "createdAt": "2026-08-11T17:00:00.000Z",
        "url": "https://linear.app/driftward/issue/ENG-40/example",
        "webhookTimestamp": 1797008400000,
        "data": {
            "id": "issue-uuid",
            "identifier": "ENG-40",
            "title": "Build a narrow Linear ingress test",
            "updatedAt": "2026-08-11T17:00:00.000Z",
            "team": {"name": "Engineering", "key": "ENG"},
            "project": {
                "id": "d779c532-620d-458c-8abd-05e37f5ca4c1",
                "name": "Capykit",
            },
            "state": {"name": "Ready", "type": "unstarted"},
            "labels": [{"name": "Feature"}],
            "relations": {"blockedBy": []},
        },
    }


if __name__ == "__main__":
    unittest.main()
