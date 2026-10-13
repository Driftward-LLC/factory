"""Maintenance retirement preserves supersession, successor and ownership."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hatchet_hermes_bridge import interrupted_resolution, store
from test_store import verifier_payload
from test_support import connect_migrate

SCRIPT = Path(__file__).resolve().parents[3] / "tools/factory-retire-superseded.py"
spec = importlib.util.spec_from_file_location("superseded_retirement", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
ACTOR = "uid:0;pid:123;account:root"


class SupersededRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge.db"
        self.conn = connect_migrate(str(self.path))
        self.addCleanup(self.conn.close)
        payload = verifier_payload("Driftward-LLC/capykit")
        old, self.job, _ = store.reserve_factory_retry(self.conn, work_item_key="ENG-600", mode="real",
            request_id="old-fixture", reason="test", actor="test", payload=payload)
        self.aid = old["attempt_id"]
        store.transition_job(self.conn, self.job["job_id"], state="running")
        self.lease = store.claim_factory_execution(self.conn, self.job["job_id"], owner="executor")["lease"]
        store.authorize_factory_effect(self.conn, self.job["job_id"], self.lease)
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00Z' WHERE attempt_id=?", (self.aid,))
        store.claim_factory_execution(self.conn, self.job["job_id"], owner="second")
        row = self.conn.execute("SELECT * FROM factory_execution_leases WHERE attempt_id=?", (self.aid,)).fetchone()
        absence = {"kind": "github_branch_pr_absence_v1", "repository": payload["repo"], "head_branch": old["branch"],
                   "branch_exists": False, "pull_requests": [], "observed_at": store.utc_now()}
        fence = {"kind": absence["kind"], "prior_attempt_id": self.aid, "prior_job_id": self.job["job_id"],
                 "repository": payload["repo"], "head_branch": old["branch"], "lease_id": row["lease_id"], "lease_epoch": row["epoch"],
                 "effect_authorized_at": row["effect_authorized_at"], "lease_expires_at": row["expires_at"],
                 "branch_exists": False, "pull_request_count": 0, "observed_at": absence["observed_at"], "evidence_digest": store.payload_hash(absence)}
        self.successor, self.successor_job, _ = store.reserve_factory_absence_retry(self.conn, work_item_key="ENG-600", mode="real",
            request_id="successor-fixture", reason="reviewed absence", actor="test", prior_attempt_id=self.aid,
            payload={**payload, "factory_artifact_fence": fence}, absence_evidence=absence)
        # Fixture for the already verified newer generation; no live execution.
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET status='verified_ready' WHERE attempt_id=?", (self.successor["attempt_id"],))
            self.conn.execute("UPDATE bridge_jobs SET state='ready_for_human_review',pr_url=?,commit_sha=? WHERE job_id=?",
                              ("https://github.com/Driftward-LLC/capykit/pull/600", "a" * 40, self.successor_job["job_id"]))
        self.evidence = {"schema": interrupted_resolution.SCHEMA, "attempt_id": self.aid, "job_id": self.job["job_id"],
                         "lease_id": self.lease["lease_id"], "lease_epoch": self.lease["epoch"], "observed_at": store.utc_now(),
                         "runtime_ref": "test/retired-executor", "containment_method": "isolated_runtime_retired",
                         "evidence_sha256": "b" * 64, "workspace_snapshot_sha256": "c" * 64,
                         "checks": {key: True for key in interrupted_resolution.CHECKS}}
        self.absence = {**absence, "observed_at": store.utc_now()}

    def values(self):
        return {"successor_attempt_id": self.successor["attempt_id"], "attestation": self.evidence,
                "absence_evidence": self.absence, "reason": "reviewed containment", "actor": ACTOR}

    def preview(self, **changes):
        return recovery.preview(self.conn, self.aid, **{**self.values(), **changes})

    def command(self):
        return {**self.values(), "request_id": "retire-fixture", "snapshot_digest": self.preview()["snapshot_digest"]}

    def rows(self, table):
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_preview_is_readonly_and_never_exposes_fence_token(self):
        before = self.conn.total_changes
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            preview = recovery.preview(conn, self.aid, **self.values())
        self.assertTrue(preview["eligible"])
        self.assertEqual(self.conn.total_changes, before)
        self.assertNotIn(self.lease["fence_token"], json.dumps(preview))

    def test_retires_only_old_job_lease_delivery_and_appends_audit(self):
        preserved = {name: self.rows(name) for name in ("factory_attempts", "factory_issue_controls", "factory_issue_control_commands",
                     "factory_execution_manifests", "factory_work_checkpoints")}
        other_outbox = self.rows("factory_attempt_outbox")[1]
        newer_job = store.get_job(self.conn, self.successor_job["job_id"])
        result = recovery.confirm(self.conn, self.aid, **self.command())
        self.assertEqual(result["effect_outcome"], "unknown")
        for table, rows in preserved.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertEqual(store.get_job(self.conn, newer_job["job_id"]), newer_job)
        self.assertEqual(self.rows("factory_attempt_outbox")[1], other_outbox)
        old_job = store.get_job(self.conn, self.job["job_id"])
        self.assertEqual((old_job["state"], old_job["reason_code"]), ("failed", "operator_interrupted_outcome_unknown"))
        lease = self.rows("factory_execution_leases")[0]
        self.assertIsNotNone(lease["released_at"])
        self.assertIsNone(lease["effect_observed_at"])
        self.assertIsNone(lease["cancel_requested_at"])
        self.assertEqual(self.rows("factory_attempt_outbox")[0]["state"], "suppressed")
        self.assertEqual(len(self.rows("factory_recovery_requests")), 1)
        self.assertEqual(self.rows("factory_attempt_events")[-1]["event_type"], interrupted_resolution.EVENT)
        self.assertEqual(self.rows("bridge_events")[-1]["event_type"], interrupted_resolution.EVENT)
        self.assertFalse(store.claim_factory_execution(self.conn, self.job["job_id"], owner="replay")["claimed"])
        self.assertFalse(store.heartbeat_factory_execution(self.conn, self.job["job_id"], self.lease))
        self.assertIsNone(store.claim_factory_outbox_delivery(self.conn, self.rows("factory_attempt_outbox")[0]["outbox_key"]))
        with self.assertRaises(store.BridgeConflict):
            store.commit_factory_execution(self.conn, self.job["job_id"], self.lease,
                {"pr_url": newer_job["pr_url"], "commit_sha": "d" * 40, "branch": self.job["branch"]})

    def test_replay_after_expiry_returns_original_and_changed_intent_rejected(self):
        command = self.command()
        first = recovery.confirm(self.conn, self.aid, **command)
        with patch.object(interrupted_resolution, "_age", return_value=100000):
            replay = recovery.confirm(self.conn, self.aid, **{**command, "actor": "uid:0;pid:999;account:root"})
        self.assertEqual(replay, {**first, "created": False})
        for change in ({"reason": "changed"}, {"successor_attempt_id": self.aid}, {"snapshot_digest": "f" * 64},
                       {"absence_evidence": {**self.absence, "head_branch": "wrong"}}):
            with self.subTest(change=change), self.assertRaises(store.BridgeConflict):
                recovery.confirm(self.conn, self.aid, **{**command, **change})

    def test_wrong_state_receipt_lease_and_lineage_are_rejected_without_writes(self):
        cases = [("factory_attempts", "status", "failed", "attempt_id", self.aid),
                 ("factory_attempts", "mode", "dry-run", "attempt_id", self.aid),
                 ("bridge_jobs", "state", "completed", "job_id", self.job["job_id"]),
                 ("bridge_jobs", "pr_url", "https://github.com/Driftward-LLC/capykit/pull/999", "job_id", self.job["job_id"]),
                 ("bridge_jobs", "commit_sha", "d" * 40, "job_id", self.job["job_id"]),
                 ("factory_execution_leases", "effect_observed_at", store.utc_now(), "attempt_id", self.aid),
                 ("factory_execution_leases", "cancel_requested_at", store.utc_now(), "attempt_id", self.aid),
                 ("factory_execution_leases", "released_at", store.utc_now(), "attempt_id", self.aid),
                 ("factory_execution_leases", "effect_authorized_at", None, "attempt_id", self.aid),
                 ("factory_execution_leases", "expires_at", "2099-01-01T00:00:00Z", "attempt_id", self.aid),
                 ("factory_attempts", "status", "running", "attempt_id", self.successor["attempt_id"]),
                 ("factory_attempts", "prior_attempt_id", None, "attempt_id", self.successor["attempt_id"]),
                 ("factory_attempts", "artifact_fence_json", "{}", "attempt_id", self.successor["attempt_id"])]
        for table, column, value, key, identity in cases:
            original = self.conn.execute(f"SELECT {column} FROM {table} WHERE {key}=?", (identity,)).fetchone()[0]
            with self.conn:
                self.conn.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, identity))
            before = self.conn.total_changes
            with self.subTest(table=table, column=column), self.assertRaises(store.BridgeConflict):
                self.preview()
            self.assertEqual(self.conn.total_changes, before)
            with self.conn:
                self.conn.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (original, identity))

    def test_bad_evidence_root_and_cross_generation_identity_block(self):
        for change in ({"actor": "uid:1000;pid:1;account:user"}, {"successor_attempt_id": self.aid},
                       {"attestation": {**self.evidence, "job_id": self.successor_job["job_id"]}},
                       {"attestation": {**self.evidence, "lease_epoch": 2}},
                       {"attestation": {**self.evidence, "checks": {**self.evidence["checks"], "restart_and_replay_blocked": False}}},
                       {"attestation": {**self.evidence, "observed_at": "2000-01-01T00:00:00Z"}},
                       {"absence_evidence": {**self.absence, "observed_at": "2000-01-01T00:00:00Z"}},
                       {"absence_evidence": {**self.absence, "head_branch": self.successor["branch"]}},
                       {"absence_evidence": {**self.absence, "branch_exists": True}},
                       {"absence_evidence": {**self.absence, "pull_requests": [{"state": "closed"}]}}):
            with self.subTest(change=change), self.assertRaises((store.BridgeConflict, store.BridgeValidationError)):
                self.preview(**change)

    def test_changed_rows_or_receipt_after_preview_block_and_audit_failure_rolls_back(self):
        command = self.command()
        store._append_factory_attempt_event(self.conn, self.aid, "new_observation", {})
        self.conn.commit()
        with self.assertRaisesRegex(store.BridgeConflict, "preview changed"):
            recovery.confirm(self.conn, self.aid, **command)
        command = self.command()
        before = {table: self.rows(table) for table in ("factory_execution_leases", "bridge_jobs", "factory_attempt_outbox",
                                                      "factory_attempts", "factory_attempt_events", "bridge_events", "factory_recovery_requests")}
        with patch.object(store, "_append_factory_attempt_event", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                recovery.confirm(self.conn, self.aid, **command)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        store._append_factory_attempt_event(self.conn, self.aid, "execution_receipt_observed", {"state": "completed"})
        self.conn.commit()
        with self.assertRaisesRegex(store.BridgeConflict, "receipt recovery"):
            recovery.confirm(self.conn, self.aid, **command)

    def test_newer_generation_or_untracked_job_blocks(self):
        other, other_job, _ = store.reserve_factory_retry(self.conn, work_item_key="ENG-601", mode="real", request_id="other",
            reason="test", actor="test", payload=verifier_payload("Driftward-LLC/capykit"))
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET work_item_key='ENG-600',generation=3 WHERE attempt_id=?", (other["attempt_id"],))
        with self.assertRaises(store.BridgeConflict):
            self.preview()
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET work_item_key='ENG-601' WHERE attempt_id=?", (other["attempt_id"],))
        store.submit_job(self.conn, {"idempotency_key": "untracked", "requested_action": "create_draft_pr",
                                    "payload": {"repo": "Driftward-LLC/capykit", "linear_issue": "ENG-600", "dry_run": False}})
        with self.assertRaisesRegex(store.BridgeConflict, "another execution"):
            self.preview()

    @unittest.skipUnless(os.geteuid() == 0, "root-only evidence/CLI test")
    def test_private_files_and_real_cli_preview_confirm(self):
        evidence = Path(self.tmp.name) / "attestation.json"
        absence = Path(self.tmp.name) / "absence.json"
        evidence.write_text(json.dumps(self.evidence)); evidence.chmod(0o600)
        absence.write_text(json.dumps(self.absence)); absence.chmod(0o600)
        args = ["--database", str(self.path), "--attempt-id", self.aid, "--successor-attempt-id", self.successor["attempt_id"],
                "--attestation", str(evidence), "--absence-evidence", str(absence), "--reason", "reviewed containment"]
        inspected = subprocess.run([sys.executable, str(SCRIPT), "preview", *args], text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        digest = json.loads(inspected.stdout)["snapshot_digest"]
        for extras, successful in (([], False), (["--yes", "--request-id", "cli-test", "--snapshot-digest", digest], True)):
            run = subprocess.run([sys.executable, str(SCRIPT), "confirm", *args, *extras], text=True, capture_output=True)
            self.assertEqual(run.returncode == 0, successful, run.stderr)
        evidence.chmod(0o644)
        with self.assertRaises(store.BridgeValidationError):
            recovery.private_file(evidence, evidence=True)
        link = Path(self.tmp.name) / "link.json"; link.symlink_to(absence)
        with self.assertRaises(store.BridgeValidationError):
            recovery.private_file(link, evidence=True)
        with patch.object(recovery.os, "geteuid", return_value=1000), patch.object(sys, "argv", [str(SCRIPT), "preview", *args]):
            self.assertEqual(recovery.main(), 1)


if __name__ == "__main__":
    unittest.main()
