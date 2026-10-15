import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hatchet_hermes_bridge.operator import OperatorError, preview_retry, reserve_retry
from hatchet_hermes_bridge.store import (
    BridgeValidationError, BridgeConflict, acknowledge_factory_outbox, claim_factory_execution,
    claim_factory_outbox_delivery, connect_readonly, fail_factory_outbox_delivery, get_factory_attempt,
    list_factory_outbox, list_factory_attempt_events, reserve_factory_retry, cancel_factory_attempt, submit_job,
    complete_pr_inspection, get_job, recover_factory_execution,
)
from test_support import connect_migrate as connect

ROOT = Path(__file__).resolve().parents[3]
ROUTES = str(ROOT / "hatchet/workers/routing-registry.json")
POLICY = str(ROOT / "hatchet/workers/verifier-policy.json")
CAPYKIT = "d779c532-620d-458c-8abd-05e37f5ca4c1"


class RetryReviewFencingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "bridge.db"); connect(self.db).close()

    def _reserve(self):
        conn = connect(self.db)
        try:
            return reserve_factory_retry(conn, work_item_key="ENG-62", mode="real", request_id="review-1", reason="retry", actor="uid:0;pid:1;account:root", payload={"repo":"Driftward-LLC/capykit"})
        finally: conn.close()

    def test_cancel_claim_and_recovery_never_open_second_execution_path(self):
        attempt, job, _ = self._reserve()
        conn = connect(self.db)
        first = claim_factory_execution(conn, job["job_id"], owner="one")
        self.assertTrue(first["claimed"])
        self.assertEqual(cancel_factory_attempt(conn, attempt["attempt_id"], reason="stop", actor="uid:0" )["status"], "cancellation_requested")
        self.assertFalse(claim_factory_execution(conn, job["job_id"], owner="two")["claimed"])
        # The claimed executor observes the cancellation and settles before an
        # effect; a later worker still cannot resurrect this generation.
        from hatchet_hermes_bridge.store import settle_factory_execution
        settle_factory_execution(conn, job["job_id"], first["lease"], effect_observed=False)
        self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "canceled")
        self.assertFalse(claim_factory_execution(conn, job["job_id"], owner="three")["claimed"])
        conn.close()

    def test_real_job_without_attempt_is_never_claimable(self):
        conn = connect(self.db)
        job, _ = submit_job(conn, {"idempotency_key":"hatchet:unimportable:real","requested_action":"create_draft_pr","payload":{"repo":"Driftward-LLC/capykit","dry_run":False}})
        self.assertEqual(claim_factory_execution(conn, job["job_id"], owner="executor")["reason"], "factory_attempt_required")
        self.assertEqual(get_job(conn, job["job_id"])["state"], "accepted")
        conn.close()

    def test_outbox_is_single_row_replayable_and_ack_is_idempotent(self):
        _, _, _ = self._reserve()
        conn = connect(self.db)
        row = list_factory_outbox(conn)[0]
        claimed = claim_factory_outbox_delivery(conn, row["outbox_key"])
        self.assertIsNotNone(claimed)
        self.assertIsNone(claim_factory_outbox_delivery(conn, row["outbox_key"]))
        fail_factory_outbox_delivery(conn, row["outbox_key"], claimed["delivery_lease_id"])
        second_claim = claim_factory_outbox_delivery(conn, row["outbox_key"])
        self.assertIsNotNone(second_claim)
        # A crash before acknowledgement leaves the same immutable event for a
        # lease-expiry replay, never a second attempt/job/event row.
        conn.execute("UPDATE factory_attempt_outbox SET delivery_lease_expires_at='2000-01-01T00:00:00.000000Z' WHERE outbox_key=?", (row["outbox_key"],)); conn.commit()
        replay = claim_factory_outbox_delivery(conn, row["outbox_key"])
        self.assertEqual(replay["payload"], row["payload"])
        acknowledge_factory_outbox(conn, row["outbox_key"], second_claim["delivery_lease_id"])
        self.assertEqual(len(list_factory_outbox(conn)), 1)
        acknowledge_factory_outbox(conn, row["outbox_key"], replay["delivery_lease_id"])
        acknowledge_factory_outbox(conn, row["outbox_key"], replay["delivery_lease_id"])
        self.assertEqual(list_factory_outbox(conn), [])
        conn.close()

    def test_readonly_access_never_migrates_or_changes_database_bytes(self):
        before = Path(self.db).read_bytes(); mtime = Path(self.db).stat().st_mtime_ns
        conn = connect_readonly(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 0); conn.close()
        self.assertEqual(Path(self.db).read_bytes(), before)
        self.assertEqual(Path(self.db).stat().st_mtime_ns, mtime)

    def test_malformed_legacy_payload_records_conflict_and_readonly_requires_predeploy(self):
        conn = connect(self.db)
        conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
        conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,created_at,updated_at) VALUES ('legacy','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active','completed','create_draft_pr','{', 'x','branch','2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z')")
        conn.commit(); conn.close()
        # Reopening invokes the write-time import/backfill and keeps the problem
        # visible rather than fabricating an attempt.
        conn = connect(self.db)
        self.assertEqual(conn.execute("SELECT reason FROM factory_import_conflicts").fetchone()[0], "legacy_payload_malformed")
        conn.close()

    def test_eng8_legacy_event_pr20_is_imported_as_a_blocking_artifact(self):
        conn = connect(self.db)
        conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
        payload = json.dumps({"linear_issue": "ENG-8", "work_item_id": "ENG-8", "repo": "Driftward-LLC/capykit", "dry_run": False})
        branch = "codex/hatchet-hermes-d648b143d1aa45d1"
        conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,created_at,updated_at) VALUES ('eng8-legacy','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active','completed','create_draft_pr',?,?, ?,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z')", (payload, "fixture", branch))
        conn.execute("INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES ('00000000-0000-4000-8000-000000000020','eng8-legacy','effect_committed','effect_committed',?, '2026-01-01T00:01:00.000000Z')", (json.dumps({"pr_url": "https://github.com/Driftward-LLC/capykit/pull/20"}),))
        conn.commit(); conn.close()
        conn = connect(self.db)
        imported = conn.execute("SELECT prior_pr_url,status FROM factory_attempts WHERE work_item_key='ENG-8'").fetchone()
        self.assertEqual((imported[0], imported[1]), ("https://github.com/Driftward-LLC/capykit/pull/20", "legacy_completed"))
        self.assertIsNotNone(conn.execute("SELECT request_key FROM factory_inspection_requests WHERE number=20").fetchone())
        conn.close()

    def test_legacy_cross_repository_event_is_recorded_as_an_import_conflict(self):
        conn = connect(self.db)
        conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
        payload = json.dumps({"linear_issue": "ENG-8", "repo": "Driftward-LLC/capykit", "dry_run": False})
        conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,created_at,updated_at) VALUES ('wrong-repo','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active','completed','create_draft_pr',?,?,NULL,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z')", (payload, "fixture"))
        conn.execute("INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES ('00000000-0000-4000-8000-000000000021','wrong-repo','effect_committed','effect_committed',?, '2026-01-01T00:01:00.000000Z')", (json.dumps({"pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/143"}),))
        conn.commit(); conn.close()
        conn = connect(self.db)
        self.assertIsNone(conn.execute("SELECT attempt_id FROM factory_attempts WHERE job_id='wrong-repo'").fetchone())
        self.assertEqual(conn.execute("SELECT reason FROM factory_import_conflicts WHERE job_id='wrong-repo'").fetchone()[0], "legacy_artifact_reference_conflict")
        conn.close()

    def test_importer_rejects_present_malformed_artifact_fields_in_every_source(self):
        cases = (
            ("payload", {"pr_url": 20}),
            ("direct", {"branch": "not-a-deterministic-branch"}),
            ("event", {"url": ["https://github.com/Driftward-LLC/capykit/pull/20"]}),
            ("inspection", {"headRefName": {"bad": True}}),
        )
        for source, malformed in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                db = str(Path(directory) / "bridge.db")
                conn = connect(db)
                conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
                payload = {"linear_issue":"ENG-8", "repo":"Driftward-LLC/capykit", "dry_run":False}
                if source == "payload": payload.update(malformed)
                branch = malformed["branch"] if source == "direct" else None
                conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,created_at,updated_at) VALUES ('bad','hatchet:work-item:linear:ENG-8:create_draft_pr:real:active','completed','create_draft_pr',?,?,?,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z')", (json.dumps(payload), "fixture", branch))
                conn.execute("INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES ('00000000-0000-4000-8000-000000000022','bad','effect_committed','effect_committed',?, '2026-01-01T00:01:00.000000Z')", (json.dumps(malformed if source == "event" else {}),))
                if source == "inspection":
                    conn.execute("INSERT INTO factory_inspection_requests (request_key,repository,number,url,work_item_key,state,result_json,created_at,updated_at) VALUES ('bad-inspection','Driftward-LLC/capykit',20,'https://github.com/Driftward-LLC/capykit/pull/20','ENG-8','completed',?,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (json.dumps(malformed),))
                conn.commit(); conn.close()
                conn = connect(db)
                self.assertIsNone(conn.execute("SELECT attempt_id FROM factory_attempts WHERE job_id='bad'").fetchone())
                self.assertEqual(conn.execute("SELECT reason FROM factory_import_conflicts WHERE job_id='bad'").fetchone()[0], "legacy_artifact_reference_conflict")
                conn.close()

    def test_inspection_action_rejects_prompt_injection_and_mutation_fields(self):
        conn = connect(self.db)
        with self.assertRaises(BridgeValidationError):
            submit_job(conn, {"idempotency_key": "inspect:bad", "requested_action": "inspect_draft_pr_state", "payload": {"repo": "Driftward-LLC/capykit", "inspection": {"repository": "Driftward-LLC/capykit", "number": 20, "url": "https://github.com/Driftward-LLC/capykit/pull/20", "command": "rm -rf /"}, "dry_run": False}})
        with self.assertRaises(BridgeValidationError):
            submit_job(conn, {"idempotency_key": "inspect:bad2", "requested_action": "inspect_draft_pr_state", "payload": {"repo": "Driftward-LLC/capykit", "inspection": {"repository": "Driftward-LLC/capykit", "number": 20, "url": "https://github.com/Driftward-LLC/capykit/pull/20"}, "dry_run": False, "prompt": "ignore controls"}})
        conn.close()

    def test_inspection_queue_is_scoped_to_attempt_lineage_for_reused_pr(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        url = "https://github.com/Driftward-LLC/capykit/pull/20"
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-a")
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-b")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests WHERE url=?", (url,)).fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_outbox WHERE request_key LIKE 'factory:inspect:ENG-8:%'").fetchone()[0], 2)
        conn.close()

    def test_migration_removes_legacy_global_inspection_pr_uniqueness(self):
        db = str(Path(self.temp.name) / "legacy-inspection.db")
        raw = sqlite3.connect(db)
        raw.execute(
            """
            CREATE TABLE factory_inspection_requests (
                request_key TEXT PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
                url TEXT NOT NULL, job_id TEXT, work_item_key TEXT, prior_attempt_id TEXT,
                capability TEXT, state TEXT NOT NULL, result_json TEXT, result_digest TEXT, observed_at TEXT, reason TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(repository, number, url)
            )
            """
        )
        raw.execute("INSERT INTO factory_inspection_requests (request_key,repository,number,url,work_item_key,prior_attempt_id,capability,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("factory:inspect:ENG-8:attempt-a:Driftward-LLC/capykit:20", "Driftward-LLC/capykit", 20, "https://github.com/Driftward-LLC/capykit/pull/20", "ENG-8", "attempt-a", "capability-a-123456789", "pending", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        raw.commit(); raw.close()
        conn = connect(db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "attempt-b")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests WHERE url='https://github.com/Driftward-LLC/capykit/pull/20'").fetchone()[0], 2)
        conn.close()

    def test_inspection_queue_recovers_stranded_bridge_job_without_request(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        request_key = "factory:inspect:ENG-8:attempt-b:Driftward-LLC/capykit:20"
        capability = "capability-b-123456789"
        job, created = submit_job(conn, {
            "idempotency_key": request_key,
            "requested_action": "inspect_draft_pr_state",
            "payload": {
                "repo": "Driftward-LLC/capykit",
                "inspection": {"repository": "Driftward-LLC/capykit", "number": 20, "url": "https://github.com/Driftward-LLC/capykit/pull/20"},
                "inspection_request_key": request_key,
                "inspection_capability": capability,
                "dry_run": False,
            },
        }, internal_inspection=True)
        self.assertTrue(created)
        self.assertIsNone(conn.execute("SELECT request_key FROM factory_inspection_requests WHERE request_key=?", (request_key,)).fetchone())
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "attempt-b")
        request = conn.execute("SELECT * FROM factory_inspection_requests WHERE request_key=?", (request_key,)).fetchone()
        self.assertEqual(request["job_id"], job["job_id"])
        self.assertEqual(request["capability"], capability)
        self.assertEqual(conn.execute("SELECT state FROM factory_inspection_outbox WHERE request_key=?", (request_key,)).fetchone()[0], "pending")
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "attempt-b")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_inspection_requests WHERE request_key=?", (request_key,)).fetchone()[0], 1)
        conn.close()

    def test_inspection_queue_refreshes_expired_pending_request(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection, transition_job
        url = "https://github.com/Driftward-LLC/capykit/pull/20"
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-refresh")
        first = conn.execute("SELECT * FROM factory_inspection_requests WHERE url=?", (url,)).fetchone()
        transition_job(conn, first["job_id"], state="running", details={})
        conn.execute("UPDATE factory_inspection_requests SET created_at='2000-01-01T00:00:00.000000Z',updated_at='2000-01-01T00:00:00.000000Z' WHERE request_key=?", (first["request_key"],))
        conn.execute("UPDATE factory_inspection_outbox SET state='acknowledged',acknowledged_at='2000-01-01T00:00:00.000000Z' WHERE request_key=?", (first["request_key"],)); conn.commit()
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-refresh")
        rows = conn.execute("SELECT * FROM factory_inspection_requests WHERE url=? ORDER BY created_at,request_key", (url,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["state"], "expired")
        self.assertEqual(get_job(conn, first["job_id"])["state"], "failed")
        self.assertEqual(get_job(conn, first["job_id"])["reason_code"], "inspection_capability_expired")
        self.assertTrue(rows[1]["request_key"].endswith(":refresh:2"))
        self.assertEqual(rows[1]["state"], "pending")
        self.assertNotEqual(rows[1]["capability"], first["capability"])
        self.assertEqual(conn.execute("SELECT state FROM factory_inspection_outbox WHERE request_key=?", (rows[1]["request_key"],)).fetchone()[0], "pending")
        conn.close()

    def test_inspection_queue_refreshes_stale_completed_result(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        url = "https://github.com/Driftward-LLC/capykit/pull/20"
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-stale")
        first = conn.execute("SELECT * FROM factory_inspection_requests WHERE url=?", (url,)).fetchone()
        result = {"repository":"Driftward-LLC/capykit","number":20,"url":url,"state":"closed","merged":False,"draft":True,"head_branch":"codex/hatchet-hermes-abc","head_sha":"abcdef1","base_ref":"main","reason":"ok"}
        complete_pr_inspection(conn, first["job_id"], result, request_key=first["request_key"], capability=first["capability"])
        conn.execute("UPDATE factory_inspection_requests SET observed_at='2000-01-01T00:00:00.000000Z',updated_at='2000-01-01T00:00:00.000000Z' WHERE request_key=?", (first["request_key"],)); conn.commit()
        _queue_pr_inspection(conn, "ENG-8", url, "attempt-stale")
        rows = conn.execute("SELECT request_key,state,result_json FROM factory_inspection_requests WHERE url=? ORDER BY created_at,request_key", (url,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["state"], "completed")
        self.assertIsNotNone(rows[0]["result_json"])
        self.assertTrue(rows[1]["request_key"].endswith(":refresh:2"))
        self.assertEqual(rows[1]["state"], "pending")
        self.assertIsNone(rows[1]["result_json"])
        conn.close()

    def test_legacy_bridge_init_refreshes_latest_attempt_expired_inspection(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        attempt, _job, _ = reserve_factory_retry(conn, work_item_key="ENG-62", mode="real", request_id="refresh-on-init", reason="retry", actor="uid:0", payload={"repo":"Driftward-LLC/capykit"})
        url = "https://github.com/Driftward-LLC/capykit/pull/22"
        conn.execute("UPDATE factory_attempts SET status='failed', prior_pr_url=?, updated_at=? WHERE attempt_id=?", (url, "2000-01-01T00:00:00.000000Z", attempt["attempt_id"]))
        _queue_pr_inspection(conn, "ENG-62", url, attempt["attempt_id"])
        conn.execute("UPDATE bridge_schema_meta SET schema_version=7")
        conn.execute("UPDATE factory_inspection_requests SET created_at='2000-01-01T00:00:00.000000Z',updated_at='2000-01-01T00:00:00.000000Z' WHERE prior_attempt_id=?", (attempt["attempt_id"],)); conn.commit(); conn.close()
        conn = connect(self.db)
        rows = conn.execute("SELECT request_key,state FROM factory_inspection_requests WHERE prior_attempt_id=? ORDER BY created_at,request_key", (attempt["attempt_id"],)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["state"], "expired")
        self.assertTrue(rows[1]["request_key"].endswith(":refresh:2"))
        self.assertEqual(rows[1]["state"], "pending")
        conn.close()

    def test_inspection_completion_is_transactional_idempotent_and_terminal(self):
        conn = connect(self.db)
        # Only the internal operator lineage creator can create this job.
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "legacy-test")
        request = conn.execute("SELECT * FROM factory_inspection_requests").fetchone()
        from hatchet_hermes_bridge.store import transition_job
        transition_job(conn, request["job_id"], state="running", details={})
        result = {"repository":"Driftward-LLC/capykit","number":20,"url":"https://github.com/Driftward-LLC/capykit/pull/20","state":"unknown","merged":False,"draft":False,"head_branch":None,"head_sha":None,"base_ref":None,"reason":"inspect_unavailable"}
        first = complete_pr_inspection(conn, request["job_id"], result, request_key=request["request_key"], capability=request["capability"])
        second = complete_pr_inspection(conn, request["job_id"], result, request_key=request["request_key"], capability=request["capability"])
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(get_job(conn, request["job_id"])["state"], "completed")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM bridge_events WHERE job_id=? AND event_type='inspection_completed'", (request["job_id"],)).fetchone()[0], 1)
        conn.close()

    def test_tampered_or_future_inspection_receipt_is_rejected_on_read(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection, get_pr_inspection
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "legacy-tamper")
        request = conn.execute("SELECT * FROM factory_inspection_requests").fetchone()
        result = {"repository":"Driftward-LLC/capykit","number":20,"url":"https://github.com/Driftward-LLC/capykit/pull/20","state":"closed","merged":False,"draft":True,"head_branch":"codex/hatchet-hermes-abc","head_sha":"abcdef1","base_ref":"main","reason":"ok"}
        complete_pr_inspection(conn, request["job_id"], result, request_key=request["request_key"], capability=request["capability"])
        conn.execute("UPDATE factory_inspection_requests SET result_json=? WHERE request_key=?", (json.dumps({**result, "head_sha":"badbeef"}), request["request_key"])); conn.commit()
        with self.assertRaisesRegex(BridgeValidationError, "digest"):
            get_pr_inspection(conn, "Driftward-LLC/capykit", result["url"], prior_attempt_id="legacy-tamper")
        conn.close()

    def test_inspection_digest_uses_sanitized_value_and_capability_expires(self):
        conn = connect(self.db)
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/20", "legacy-sanitize")
        request = conn.execute("SELECT * FROM factory_inspection_requests").fetchone()
        result = {"repository":"Driftward-LLC/capykit","number":20,"url":"https://github.com/Driftward-LLC/capykit/pull/20","state":"closed","merged":False,"draft":True,"head_branch":"codex/hatchet-hermes-abc","head_sha":"abcdef1","base_ref":"main","reason":"Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\n" + "x" * 400}
        first = complete_pr_inspection(conn, request["job_id"], result, request_key=request["request_key"], capability=request["capability"])
        self.assertNotIn("ghp_", first["result"]["reason"])
        self.assertEqual(first["result_digest"], hashlib.sha256(json.dumps(first["result"], sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(first["result_digest"], complete_pr_inspection(conn, request["job_id"], {**result, "reason": result["reason"].replace("\n", " ")}, request_key=request["request_key"], capability=request["capability"])["result_digest"])
        conn.execute("UPDATE factory_inspection_requests SET created_at='2000-01-01T00:00:00.000000Z' WHERE request_key=?", (request["request_key"],)); conn.commit()
        self.assertEqual(first["result_digest"], complete_pr_inspection(conn, request["job_id"], result, request_key=request["request_key"], capability=request["capability"])["result_digest"])
        with self.assertRaises(BridgeConflict):
            complete_pr_inspection(conn, request["job_id"], {**result, "reason":"different"}, request_key=request["request_key"], capability=request["capability"])
        _queue_pr_inspection(conn, "ENG-8", "https://github.com/Driftward-LLC/capykit/pull/21", "legacy-expired")
        expired = conn.execute("SELECT * FROM factory_inspection_requests WHERE number=21").fetchone()
        conn.execute("UPDATE factory_inspection_requests SET created_at='2000-01-01T00:00:00.000000Z' WHERE request_key=?", (expired["request_key"],)); conn.commit()
        with self.assertRaisesRegex(BridgeValidationError, "capability is expired"):
            complete_pr_inspection(conn, expired["job_id"], {**result, "number":21, "url":"https://github.com/Driftward-LLC/capykit/pull/21"}, request_key=expired["request_key"], capability=expired["capability"])
        self.assertEqual(conn.execute("SELECT state FROM factory_inspection_requests WHERE request_key=?", (expired["request_key"],)).fetchone()[0], "expired")
        self.assertEqual(get_job(conn, expired["job_id"])["state"], "failed")
        conn.close()

    def test_expired_lease_is_permanently_fenced_even_with_recovery_evidence(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        lease = claim_factory_execution(conn, job["job_id"], owner="crashed")["lease"]
        conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00.000000Z' WHERE lease_id=?", (lease["lease_id"],)); conn.commit()
        self.assertEqual(claim_factory_execution(conn, job["job_id"], owner="next")["reason"], "factory_execution_recovery_required")
        self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "effect_outcome_unknown")
        from hatchet_hermes_bridge.store import _queue_pr_inspection
        _queue_pr_inspection(conn, "ENG-62", "https://github.com/Driftward-LLC/capykit/pull/1", attempt["attempt_id"])
        inspection = conn.execute("SELECT * FROM factory_inspection_requests WHERE prior_attempt_id=?", (attempt["attempt_id"],)).fetchone()
        complete_pr_inspection(conn, inspection["job_id"], {"repository":"Driftward-LLC/capykit","number":1,"url":"https://github.com/Driftward-LLC/capykit/pull/1","state":"closed","merged":False,"draft":True,"head_branch":attempt["branch"],"head_sha":"abcdef1","base_ref":"main","reason":"ok"}, request_key=inspection["request_key"], capability=inspection["capability"])
        evidence = {"inspection_request_key":inspection["request_key"],"inspection_result_digest":conn.execute("SELECT result_digest FROM factory_inspection_requests WHERE request_key=?", (inspection["request_key"],)).fetchone()[0],"repository":"Driftward-LLC/capykit","observed_at":conn.execute("SELECT observed_at FROM factory_inspection_requests WHERE request_key=?", (inspection["request_key"],)).fetchone()[0],"number":1,"state":"closed","pr_url":"https://github.com/Driftward-LLC/capykit/pull/1","head_branch":attempt["branch"],"head_sha":"abcdef1","base_ref":"main","merged":False,"draft":True}
        self.assertEqual(recover_factory_execution(conn, job["job_id"], request_key="recovery-1", evidence=evidence, actor="recovery-token")["status"], "effect_outcome_unknown")
        self.assertEqual(recover_factory_execution(conn, job["job_id"], request_key="recovery-1", evidence=evidence, actor="recovery-token")["status"], "effect_outcome_unknown")
        self.assertFalse(claim_factory_execution(conn, job["job_id"], owner="next")["claimed"])
        conn.close()

    def test_stalled_executor_cannot_commit_and_recovery_cannot_authorize_b(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import execution_may_effect, commit_factory_execution, BridgeConflict
        from hatchet_hermes_bridge.store import transition_job
        transition_job(conn, job["job_id"], state="running", details={})
        a = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        self.assertTrue(execution_may_effect(conn, job["job_id"], a))  # A crossed its final pre-Hermes gate.
        conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00.000000Z' WHERE lease_id=?", (a["lease_id"],)); conn.commit()
        self.assertEqual(claim_factory_execution(conn, job["job_id"], owner="executor-b")["reason"], "factory_execution_recovery_required")
        self.assertFalse(execution_may_effect(conn, job["job_id"], a))
        with self.assertRaises(BridgeConflict):
            commit_factory_execution(conn, job["job_id"], a, {"pr_url":"https://github.com/Driftward-LLC/capykit/pull/9", "commit_sha":"abcdef1", "branch":job["branch"]})
        self.assertEqual(get_job(conn, job["job_id"])["state"], "running")
        self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "effect_outcome_unknown")
        conn.close()

    def test_effect_authorization_survives_pause_cancel_and_expiry_without_second_owner(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import authorize_factory_effect, commit_factory_execution, transition_job
        transition_job(conn, job["job_id"], state="running", details={})
        a = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        self.assertTrue(authorize_factory_effect(conn, job["job_id"], a))
        self.assertEqual(cancel_factory_attempt(conn, attempt["attempt_id"], reason="too late", actor="uid:0")["status"], "cancellation_requested")
        conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00.000000Z' WHERE lease_id=?", (a["lease_id"],)); conn.commit()
        self.assertEqual(claim_factory_execution(conn, job["job_id"], owner="executor-b")["reason"], "factory_execution_recovery_required")
        committed = commit_factory_execution(conn, job["job_id"], a, {"pr_url":"https://github.com/Driftward-LLC/capykit/pull/9", "commit_sha":"abcdef1", "branch":job["branch"]})
        self.assertEqual(committed["state"], "effect_committed")
        self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "canceled_after_effect")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_execution_leases WHERE effect_authorized_at IS NOT NULL").fetchone()[0], 1)
        conn.close()

    def test_observed_authorization_is_absorbing_until_owner_commits(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import authorize_factory_effect, settle_factory_execution, commit_factory_execution, transition_job
        transition_job(conn, job["job_id"], state="running", details={})
        a = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        self.assertTrue(authorize_factory_effect(conn, job["job_id"], a))
        settle_factory_execution(conn, job["job_id"], a, effect_observed=True)
        lease = conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        self.assertIsNone(lease["released_at"])
        self.assertEqual(claim_factory_execution(conn, job["job_id"], owner="executor-b")["reason"], "factory_execution_in_progress")
        committed = commit_factory_execution(conn, job["job_id"], a, {"pr_url":"https://github.com/Driftward-LLC/capykit/pull/10", "commit_sha":"abcdef1", "branch":job["branch"]})
        self.assertEqual(committed["state"], "effect_committed")
        self.assertFalse(claim_factory_execution(conn, job["job_id"], owner="executor-b")["claimed"])
        conn.close()

    def test_direct_transition_cannot_bypass_active_execution_fence(self):
        """The root CLI calls the store directly, so the store owns this fence."""
        _, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import authorize_factory_effect, transition_job
        transition_job(conn, job["job_id"], state="running", details={})
        a = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        with self.assertRaises(BridgeValidationError):
            transition_job(conn, job["job_id"], state="failed", details={"reason_code":"manual"})
        self.assertTrue(authorize_factory_effect(conn, job["job_id"], a))
        self.assertEqual(get_job(conn, job["job_id"])["state"], "running")
        conn.close()

    def test_post_commit_generic_terminals_are_blocked_but_verifier_failure_is_allowed(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import authorize_factory_effect, commit_factory_execution, transition_job, acquire_verification_lease, claim_verification_lease
        transition_job(conn, job["job_id"], state="running", details={})
        claim = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        self.assertTrue(authorize_factory_effect(conn, job["job_id"], claim))
        commit_factory_execution(conn, job["job_id"], claim, {"pr_url":"https://github.com/Driftward-LLC/capykit/pull/11", "commit_sha":"abcdef1", "branch":job["branch"]})
        for state in ("failed", "completed", "canceled"):
            with self.assertRaises(BridgeValidationError):
                transition_job(conn, job["job_id"], state=state, details={})
        transition_job(conn, job["job_id"], state="verification_pending", details={})
        verification = acquire_verification_lease(conn, job["job_id"], attempt_id="verify-a")
        self.assertTrue(verification["acquired"])
        self.assertTrue(claim_verification_lease(conn, job["job_id"], attempt_id="verify-a"))
        failed = transition_job(conn, job["job_id"], state="failed", details={"verification_attempt_id":"verify-a"}, _factory_verifier_completion=True)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(get_factory_attempt(conn, attempt["attempt_id"])["status"], "failed")
        self.assertEqual([event["event_type"] for event in list_factory_attempt_events(conn, attempt["attempt_id"])].count("attempt_failed"), 1)
        conn.close()

    def test_cancellation_before_effect_authorization_prevents_external_authority(self):
        attempt, job, _ = self._reserve(); conn = connect(self.db)
        from hatchet_hermes_bridge.store import authorize_factory_effect
        a = claim_factory_execution(conn, job["job_id"], owner="executor-a")["lease"]
        cancel_factory_attempt(conn, attempt["attempt_id"], reason="stop", actor="uid:0")
        self.assertFalse(authorize_factory_effect(conn, job["job_id"], a))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM factory_execution_leases WHERE effect_authorized_at IS NOT NULL").fetchone()[0], 0)
        conn.close()
