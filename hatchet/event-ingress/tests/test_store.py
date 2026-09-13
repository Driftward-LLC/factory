from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hatchet_event_ingress.normalizer import normalize_github_pull_request, normalize_linear_issue
from hatchet_event_ingress.store import IngressConflict, claim_pending_publication, connect, latest_observation, list_ingress_receipts, list_lifecycle_observations, list_pending_publications, list_reconciliation_observations, mark_publication_failed, mark_published, publication_status, record_event


class IngressStoreTest(unittest.TestCase):
    def test_record_event_deduplicates_same_payload(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                first, first_created = record_event(conn, normalized=normalized, raw_payload=payload)
                second, second_created = record_event(conn, normalized=normalized, raw_payload=payload)
                receipts = list_ingress_receipts(conn)
                retained_raw = conn.execute(
                    "SELECT raw_payload_json FROM ingress_events WHERE event_key = ?",
                    (first["event_key"],),
                ).fetchone()["raw_payload_json"]
                schema_version = conn.execute(
                    "SELECT schema_version FROM ingress_schema_meta WHERE singleton = 1"
                ).fetchone()["schema_version"]
            finally:
                conn.close()

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["event_key"], second["event_key"])
        self.assertCountEqual([receipt["disposition"] for receipt in receipts], ["accepted", "duplicate"])
        self.assertEqual({receipt["event_key"] for receipt in receipts}, {first["event_key"]})
        self.assertNotIn("raw_payload", receipts[0])
        self.assertNotIn("normalized", receipts[0])
        self.assertNotIn("raw_payload", first)
        self.assertEqual(retained_raw, "{}")
        self.assertEqual(schema_version, 3)

    def test_digest_replay_retains_legacy_delivery_key_and_receipt_foreign_key(self) -> None:
        payload = _payload()
        current = normalize_github_pull_request(payload, delivery_id="current-delivery")
        legacy = copy.deepcopy(current)
        legacy["eventKey"] = "github:Driftward-LLC/capykit:pr:11:closed:legacy-delivery"
        legacy["deliveryId"] = "legacy-delivery"

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                retained, _ = record_event(conn, normalized=legacy, raw_payload=payload)
                mark_published(conn, retained["event_key"], "hatchet-legacy")
                replayed, created = record_event(
                    conn,
                    normalized=current,
                    raw_payload=payload,
                    signature_verified=True,
                )
                rows = conn.execute(
                    "SELECT event_key FROM ingress_events ORDER BY event_key"
                ).fetchall()
                receipts = list_ingress_receipts(conn)
            finally:
                conn.close()

        self.assertFalse(created)
        self.assertEqual(replayed["event_key"], legacy["eventKey"])
        self.assertTrue(replayed["hatchet_published"])
        self.assertEqual([row["event_key"] for row in rows], [legacy["eventKey"]])
        self.assertCountEqual(
            [receipt["disposition"] for receipt in receipts],
            ["accepted", "duplicate"],
        )
        self.assertEqual(
            {receipt["event_key"] for receipt in receipts},
            {legacy["eventKey"]},
        )
        self.assertEqual(
            {receipt["delivery_id"] for receipt in receipts},
            {"legacy-delivery", "current-delivery"},
        )

    def test_newer_schema_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ingress.db"
            future = sqlite3.connect(path)
            try:
                future.execute(
                    """
                    CREATE TABLE ingress_schema_meta (
                        singleton INTEGER PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        migrated_at TEXT NOT NULL
                    )
                    """
                )
                future.execute(
                    "INSERT INTO ingress_schema_meta VALUES (1, 4, 'future')"
                )
                future.commit()
            finally:
                future.close()
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "newer than this runtime"):
                connect(str(path))

            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(f"{path}-wal").exists())
            self.assertFalse(Path(f"{path}-shm").exists())
            verification = sqlite3.connect(path)
            try:
                tables = verification.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall()
                version = verification.execute(
                    "SELECT schema_version, migrated_at FROM ingress_schema_meta"
                ).fetchone()
                journal_mode = verification.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                verification.close()

        self.assertEqual(tables, [("ingress_schema_meta",)])
        self.assertEqual(version, (4, "future"))
        self.assertEqual(journal_mode, "delete")

    def test_record_event_conflicts_on_changed_payload(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")
        changed = _payload()
        changed["pull_request"]["title"] = "ENG-7 changed"

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/ingress.db"
            conn = connect(path)
            try:
                record_event(conn, normalized=normalized, raw_payload=payload)
                with self.assertRaises(IngressConflict):
                    record_event(conn, normalized=normalized, raw_payload=changed)
            finally:
                conn.close()
            reopened = connect(path)
            try:
                receipts = list_ingress_receipts(reopened)
            finally:
                reopened.close()

        self.assertCountEqual([receipt["disposition"] for receipt in receipts], ["accepted", "payload_conflict"])
        self.assertNotEqual(receipts[0]["payload_hash"], receipts[1]["payload_hash"])

    def test_ingress_receipts_are_database_enforced_append_only(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                record_event(conn, normalized=normalized, raw_payload=payload)
                receipt_id = list_ingress_receipts(conn)[0]["receipt_id"]
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute(
                        "UPDATE ingress_receipts SET disposition = 'duplicate' WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                conn.rollback()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    conn.execute("DELETE FROM ingress_receipts WHERE receipt_id = ?", (receipt_id,))
                conn.rollback()
                receipts = list_ingress_receipts(conn)
            finally:
                conn.close()

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["disposition"], "accepted")

    def test_mark_published_records_hatchet_event(self) -> None:
        payload = _payload()
        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                event, _ = record_event(conn, normalized=normalized, raw_payload=payload)
                published = mark_published(conn, event["event_key"], "hatchet-event-1")
                first_published_at = published["published_at"]
                published_again = mark_published(conn, event["event_key"], "hatchet-event-1")
            finally:
                conn.close()

        self.assertTrue(published["hatchet_published"])
        self.assertEqual(published["hatchet_event_id"], "hatchet-event-1")
        self.assertIsNotNone(first_published_at)
        self.assertEqual(published_again["published_at"], first_published_at)

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
            for index, delivery in enumerate(("one", "two"), 1):
                payload = _linear_payload()
                payload["webhookTimestamp"] += index
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
                provenance = conn.execute(
                    "SELECT provider_updated_at_us, provider_timestamp_source "
                    "FROM ingress_events"
                ).fetchone()
            finally:
                conn.close()
        self.assertNotIn("providerUpdatedAt", observation)
        self.assertIsInstance(provenance["provider_updated_at_us"], int)
        self.assertEqual(provenance["provider_timestamp_source"], "receipt_fallback")

    def test_latest_observation_uses_provider_time_not_delivery_time(self) -> None:
        newer = _linear_payload()
        newer["data"]["updatedAt"] = "2026-08-12T12:00:00Z"
        older_delayed = _linear_payload()
        older_delayed["data"]["updatedAt"] = "2026-08-12T11:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(f"{tmp}/ingress.db")
            try:
                newer_normalized = normalize_linear_issue(newer, delivery_id="newer")
                record_event(conn, normalized=newer_normalized, raw_payload=newer)
                record_event(conn, normalized=normalize_linear_issue(older_delayed, delivery_id="older-delayed"), raw_payload=older_delayed)
                observation = latest_observation(conn, source="linear", subject="ENG-40")
                inventory = list_reconciliation_observations(conn)
            finally:
                conn.close()
        self.assertEqual(observation["eventKey"], newer_normalized["eventKey"])
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
                with patch(
                    "hatchet_event_ingress.store.uuid.uuid4",
                    side_effect=["event-a", "receipt-a", "event-b", "receipt-b", "event-c", "receipt-c"],
                ):
                    record_event(conn, normalized=normalize_linear_issue(first, delivery_id="first"), raw_payload=first)
                    equivalent_normalized = normalize_linear_issue(equivalent, delivery_id="equivalent")
                    record_event(conn, normalized=equivalent_normalized, raw_payload=equivalent)
                    equal_instant = latest_observation(conn, source="linear", subject="ENG-40")
                    later_normalized = normalize_linear_issue(later, delivery_id="later")
                    record_event(conn, normalized=later_normalized, raw_payload=later)
                    latest = latest_observation(conn, source="linear", subject="ENG-40")
                    inventory = list_reconciliation_observations(conn)
            finally:
                conn.close()

        self.assertEqual(equal_instant["eventKey"], equivalent_normalized["eventKey"])
        self.assertEqual(equal_instant["providerUpdatedAt"], "2026-08-12T10:00:00.000000Z")
        self.assertEqual(latest["eventKey"], later_normalized["eventKey"])
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
                    "SELECT provider_updated_at_us, provider_timestamp_source "
                    "FROM ingress_events WHERE event_id = ?",
                    ("legacy-event",),
                ).fetchone()
                observation = latest_observation(conn, source="linear", subject="ENG-40")
            finally:
                conn.close()

        self.assertIsInstance(row["provider_updated_at_us"], int)
        self.assertEqual(observation["providerUpdatedAt"], "2026-08-12T10:00:00.000000Z")
        self.assertEqual(row["provider_timestamp_source"], "provider")


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
