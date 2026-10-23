import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store
from test_server import _PostHandler
from test_support import connect_migrate


class ReconciliationScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(lambda: self.conn.close())

    def job(self, index, state="accepted", *, payload=None, created_at="2026-09-01T00:00:00.000000Z", **columns):
        job_id = str(uuid.UUID(int=index + 1))
        serialized = json.dumps(payload or {"dry_run": True}, sort_keys=True)
        data = dict(job_id=job_id, idempotency_key=f"scan:{index}", state=state,
                    requested_action="create_draft_pr", payload_json=serialized,
                    payload_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                    created_at=created_at, updated_at=created_at, **columns)
        with self.conn:
            self.conn.execute(f"INSERT INTO bridge_jobs ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
        return job_id

    def scan(self, cohort="active", limit=250):
        return store.reconciliation_scan(self.conn, cohort=cohort, limit=limit)

    def ack(self, page):
        store.acknowledge_reconciliation_scan(self.conn, cohort=page["cohort"], token=page["token"])

    def test_active_and_history_rotate_independently_without_oldest_or_newest_starvation(self):
        active = {self.job(index) for index in range(300)}
        history = {self.job(index, "completed") for index in range(300, 620)}
        first = self.scan()
        self.assertEqual(first["total"], 300)
        self.assertEqual(len(first["jobs"]), 250)
        self.assertFalse(first["complete"])
        self.assertFalse(first["sweep_complete"])
        self.ack(first)
        second = self.scan()
        self.assertEqual({job["job_id"] for job in first["jobs"] + second["jobs"]}, active)
        self.assertTrue(second["sweep_complete"])
        self.assertFalse(second["complete"])
        self.ack(second)
        self.assertEqual([job["job_id"] for job in self.scan()["jobs"]], [job["job_id"] for job in first["jobs"]])
        first_history = self.scan("history")
        self.ack(first_history)
        second_history = self.scan("history")
        self.assertEqual({job["job_id"] for job in first_history["jobs"] + second_history["jobs"]}, history)
        self.assertFalse(second_history["complete"])

    def test_new_arrivals_do_not_extend_a_sweep_and_are_visited_next_sweep(self):
        for index in range(3):
            self.job(index)
        first = self.scan(limit=2)
        self.ack(first)
        added = self.job(4, created_at="2026-09-02T00:00:00.000000Z")
        tail = self.scan(limit=2)
        self.assertEqual(len(tail["jobs"]), 1)
        self.assertTrue(tail["sweep_complete"])
        self.assertFalse(tail["complete"])
        self.ack(tail)
        next_start = self.scan(limit=2)
        self.ack(next_start)
        self.assertIn(added, [job["job_id"] for job in self.scan(limit=2)["jobs"]])

    def test_restart_and_parallel_readers_replay_pending_page_with_live_job_state(self):
        ids = [self.job(index) for index in range(3)]
        page = self.scan(limit=2)
        self.assertFalse(page["replayed"])
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET state='failed' WHERE job_id=?", (ids[0],))
        other = store.connect_runtime(self.path)
        try:
            repeated = store.reconciliation_scan(other, cohort="active", limit=1)
        finally:
            other.close()
        self.assertEqual(repeated["token"], page["token"])
        self.assertTrue(repeated["replayed"])
        self.assertEqual([job["job_id"] for job in repeated["jobs"]], ids[:2])
        self.assertEqual(repeated["jobs"][0]["state"], "failed")
        self.conn.close()
        self.conn = store.connect_runtime(self.path)
        self.assertEqual(self.scan()["token"], page["token"])
        self.ack(page)
        next_page = self.scan()
        self.assertFalse(next_page["replayed"])
        self.assertEqual([job["job_id"] for job in next_page["jobs"]], ids[2:])

    def test_duplicate_ack_is_idempotent_and_stale_ack_cannot_advance_new_page(self):
        for index in range(3):
            self.job(index)
        first = self.scan(limit=1)
        self.ack(first)
        second = self.scan(limit=1)
        self.ack(first)
        self.assertEqual(self.scan()["token"], second["token"])
        self.ack(second)
        third = self.scan(limit=1)
        with self.assertRaises(store.BridgeConflict):
            self.ack(first)
        self.assertEqual(self.scan()["token"], third["token"])
        with self.assertRaises(store.BridgeConflict):
            store.acknowledge_reconciliation_scan(self.conn, cohort="history", token=third["token"])

    def test_completeness_requires_all_current_ids_not_just_matching_counts(self):
        old = self.job(0)
        page = self.scan()
        self.assertTrue(page["complete"])
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET state='completed' WHERE job_id=?", (old,))
        self.job(1)
        replay = self.scan()
        self.assertEqual(replay["total"], len(page["jobs"]))
        self.assertFalse(replay["complete"])
        self.ack(replay)
        self.assertTrue(self.scan()["complete"])

    def test_empty_pages_ack_and_state_changes_behind_cursor_return_next_sweep(self):
        old = self.job(0, "completed")
        self.job(1)
        self.job(2)
        first = self.scan(limit=1)
        self.ack(first)
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET state='accepted' WHERE job_id=?", (old,))
            self.conn.execute("UPDATE bridge_jobs SET state='completed' WHERE job_id=?", (str(uuid.UUID(int=3)),))
        empty = self.scan(limit=1)
        self.assertEqual(empty["jobs"], [])
        self.assertTrue(empty["sweep_complete"])
        self.assertFalse(empty["complete"])
        self.ack(empty)
        self.assertEqual(self.scan(limit=1)["jobs"][0]["job_id"], old)
        other = self.scan("history")
        self.assertTrue(other["complete"])

    def test_duplicates_are_found_outside_page_and_failed_owners_are_ignored(self):
        job = self.job(0, branch="shared", pr_url="https://github.com/org/repo/pull/1")
        self.job(1, "failed", branch="shared", pr_url="https://github.com/org/repo/pull/1")
        owner = self.job(2, "completed", branch="shared", pr_url="https://github.com/org/repo/pull/1")
        page = self.scan(limit=1)
        self.assertEqual(page["jobs"][0]["job_id"], job)
        self.assertEqual(page["jobs"][0]["reconciliation_duplicate_refs"], {"branch_job_id": owner, "pr_job_id": owner})
        self.assertNotIn("reconciliation_execution_backend", page["jobs"][0])

    def test_invalid_scans_and_acks_do_not_change_progress(self):
        for cohort in (None, [], "unknown"):
            with self.assertRaises(store.BridgeValidationError):
                self.scan(cohort)
        for limit in (0, 251, True, 1.5, "2"):
            with self.assertRaises(store.BridgeValidationError):
                self.scan(limit=limit)
        for token in (None, [], "unknown", "a" * 500):
            with self.assertRaises(store.BridgeValidationError):
                store.acknowledge_reconciliation_scan(self.conn, cohort="active", token=token)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM reconciliation_scan_state").fetchone()[0], 0)

    def test_correlations_use_the_whole_ledger_and_only_true_legacy_fallback(self):
        self.job(0, "completed", payload={"linear_issue": "ENG-1", "source_event_key": "event-1", "source_event_keys": ["event-1", "event-2"]}, hatchet_workflow_run_id="workflow", hatchet_step_run_id="step")
        self.job(1, payload={"linear_issue": "ENG-2"})
        self.job(2, payload={"work_item_id": "ENG-3", "source_event_keys": []})
        self.job(3, payload={"linear_issue": "ENG-4", "source_event_keys": ["durable"]})
        request = {"source_event_keys": ["event-1", "event-2", "absent"], "legacy_work_items": ["ENG-1", "ENG-2", "ENG-3", "ENG-4"], "hatchet_run_ids": ["workflow", "step", "absent"]}
        self.scan(limit=1)
        before = list(self.conn.iterdump())
        readonly = store.connect_readonly(self.path)
        try:
            matches = store.reconciliation_correlations(readonly, request)
        finally:
            readonly.close()
        self.assertEqual(matches, {"source_event_keys": ["event-1", "event-2"], "legacy_work_items": ["ENG-2", "ENG-3"], "hatchet_run_ids": ["workflow", "step"]})
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("BEGIN")
        store.reconciliation_correlations(self.conn, request)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_correlations_reject_unbounded_or_malformed_requests(self):
        empty = {"source_event_keys": [], "legacy_work_items": [], "hatchet_run_ids": []}
        self.assertEqual(store.reconciliation_correlations(self.conn, empty), empty)
        for request in ({}, {**empty, "extra": []}, {**empty, "source_event_keys": ["x"] * 251},
                        {**empty, "legacy_work_items": [False]}, {**empty, "hatchet_run_ids": ["x" * 501]},
                        {**empty, "hatchet_run_ids": "x"}, {**empty, "hatchet_run_ids": [""]}):
            with self.assertRaises(store.BridgeValidationError):
                store.reconciliation_correlations(self.conn, request)

    def test_correlation_work_does_not_rescan_the_ledger_per_requested_key(self):
        for index in range(1000):
            self.job(index, payload={"linear_issue": f"ENG-{index}", "source_event_keys": [f"event-{index}"]})
        request = {key: [f"absent-{index}" for index in range(250)] for key in ("source_event_keys", "legacy_work_items", "hatchet_run_ids")}
        remaining = 1000

        def work_budget():
            nonlocal remaining
            remaining -= 1
            return remaining <= 0

        # SQLite instructions, not wall-clock timing: the old per-key full
        # ledger scan exhausts this generous budget; one set per source does not.
        self.conn.set_progress_handler(work_budget, 1000)
        try:
            self.assertEqual(store.reconciliation_correlations(self.conn, request), {key: [] for key in request})
        finally:
            self.conn.set_progress_handler(None, 0)

    def test_http_routes_validate_exact_bodies_and_ack_conflicts(self):
        self.job(0)
        handler = _PostHandler(self.conn, {"cohort": "active", "limit": 1}, "/reconciliation/scan")
        handler.do_POST()
        self.assertEqual(handler.response[0], 200)
        page = handler.response[1]
        ack = _PostHandler(self.conn, {"cohort": "active", "token": page["token"]}, "/reconciliation/scan/ack")
        ack.do_POST()
        self.assertEqual(ack.response, (200, {"ok": True}))
        wrong = _PostHandler(self.conn, {"cohort": "active", "token": str(uuid.uuid4())}, "/reconciliation/scan/ack")
        wrong.do_POST()
        self.assertEqual(wrong.response[0], 409)
        for path, body in (("/reconciliation/scan", {"cohort": "active", "limit": 1, "cursor": "bad"}),
                           ("/reconciliation/scan/ack", {"cohort": "active", "token": page["token"], "extra": True}),
                           ("/reconciliation/correlations", {"source_event_keys": []})):
            invalid = _PostHandler(self.conn, body, path)
            invalid.do_POST()
            self.assertEqual(invalid.response[0], 400)

    def test_schema6_upgrade_is_transactional_and_runtime_does_not_migrate(self):
        self.conn.execute("DROP TABLE reconciliation_scan_state")
        self.conn.execute("UPDATE bridge_schema_meta SET schema_version=6")
        self.conn.commit()
        with self.assertRaisesRegex(store.BridgeValidationError, "migration"):
            store.connect_runtime(self.path)
        with patch.dict(os.environ, {"HATCHET_BRIDGE_MIGRATION_FAIL_AT": "before-commit"}):
            with self.assertRaisesRegex(store.BridgeError, "injected migration failure"):
                store.init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], 6)
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='reconciliation_scan_state'").fetchone())
        store.init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT schema_version FROM bridge_schema_meta").fetchone()[0], store.SCHEMA_VERSION)
        self.assertTrue(self.scan()["complete"])
        with self.assertRaises(sqlite3.IntegrityError), self.conn:
            self.conn.execute("INSERT INTO reconciliation_scan_state(cohort) VALUES ('arbitrary')")


if __name__ == "__main__":
    unittest.main()
