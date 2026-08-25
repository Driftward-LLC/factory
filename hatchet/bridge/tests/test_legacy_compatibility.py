import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from hatchet_hermes_bridge.legacy_artifacts import deterministic_branch, reconcile_legacy_active_row, routing_repositories
from hatchet_hermes_bridge.operator import OperatorError, _load_route, preview_retry
from test_support import connect_migrate as connect
from hatchet_hermes_bridge.store import init_db


ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / "hatchet/bridge/predeploy-check.py"
ROUTES = ROOT / "hatchet/workers/routing-registry.json"
POLICY = ROOT / "hatchet/workers/verifier-policy.json"
CAPYKIT = "Driftward-LLC/capykit"
HOST = "Driftward-LLC/driftward-host"
CAPYKIT_PROJECT = "d779c532-620d-458c-8abd-05e37f5ca4c1"
HOST_PROJECT = "c670f223-bcaf-4e48-9209-6cca65da4568"
NOW = "2026-08-17T00:00:00Z"
SENTINEL = "dry-run://github/pr/01234567-89ab-4cde-8fab-0123456789ab"


def legacy_key(issue: str, mode: str) -> str:
    return f"hatchet:work-item:linear:{issue}:create_draft_pr:{mode}:active"


def legacy_row(issue="ENG-8", mode="real", repository=CAPYKIT, job_id="legacy-job", state="completed", pr_url=None):
    key = legacy_key(issue, mode)
    return {
        "job_id": job_id,
        "idempotency_key": key,
        "payload_json": json.dumps({"linear_issue": issue, "repo": repository, "dry_run": mode == "dry-run"}),
        "branch": deterministic_branch(key),
        "pr_url": pr_url,
        "state": state,
        "requested_action": "create_draft_pr",
        "created_at": NOW,
        "updated_at": NOW,
    }


def legacy_attempt(row, **overrides):
    payload = json.loads(row["payload_json"])
    mode = "dry-run" if payload["dry_run"] else "real"
    value = {
        "attempt_id": f"legacy-{row['job_id']}",
        "work_item_key": payload["linear_issue"],
        "action": "create_draft_pr",
        "mode": mode,
        "generation": 1,
        "request_id": f"legacy:{row['job_id']}",
        "idempotency_key": row["idempotency_key"],
        "branch": deterministic_branch(row["idempotency_key"]),
        "status": f"legacy_{row['state']}",
        "job_id": row["job_id"],
        "prior_pr_url": row["pr_url"] if mode == "real" else None,
        "prior_attempt_id": None,
        "prior_job_id": None,
        "supersede_pr_url": None,
        "artifact_fence_json": None,
        "trusted_snapshot_digest": None,
        "reason": None,
        "actor": "legacy-import",
    }
    value.update(overrides)
    return value


def artifact_sources(row, *extra):
    return [("bridge", {"pr_url": row["pr_url"], "branch": row["branch"]}), *extra]


def import_event(attempt):
    return {
        "attempt_id": attempt["attempt_id"],
        "event_type": "legacy_imported",
        "details_json": json.dumps({"job_id": attempt["job_id"]}),
    }


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = str(Path(self.work.name) / "bridge.db")
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.routed, self.enabled = routing_repositories(ROUTES.read_text())

    def decision(self, row, *, sources=None, attempts=(), conflicts=(), leases=(), events=(), outbox=()):
        return reconcile_legacy_active_row(
            row,
            artifact_sources=artifact_sources(row) if sources is None else sources,
            attempts=attempts,
            import_conflicts=conflicts,
            execution_leases=leases,
            attempt_events=events,
            attempt_outbox=outbox,
            routed_repositories=self.routed,
            enabled_repositories=self.enabled,
        )

    def insert(self, row, *, event=None):
        self.conn.execute(
            "INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["job_id"], row["idempotency_key"], row["state"], row["requested_action"], row["payload_json"], "fixture", row["branch"], row["pr_url"], NOW, NOW),
        )
        if event is not None:
            self.conn.execute(
                "INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (f"event-{row['job_id']}", row["job_id"], "transition:completed", row["state"], json.dumps(event), NOW),
            )
        self.conn.commit()

    def insert_attempt(self, row, **overrides):
        attempt = legacy_attempt(row, **overrides)
        self.conn.execute(
            "INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,prior_pr_url,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt["attempt_id"], attempt["work_item_key"], attempt["action"], attempt["mode"], attempt["generation"], attempt["request_id"], attempt["idempotency_key"], attempt["branch"], attempt["status"], attempt["job_id"], attempt["prior_pr_url"], attempt["actor"], NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
            (f"receipt-{attempt['attempt_id']}", attempt["attempt_id"], "legacy_imported", json.dumps({"job_id": row["job_id"]}), NOW),
        )
        self.conn.commit()
        return attempt

    def predeploy(self):
        return subprocess.run(
            ["python3", str(CHECK), "--db", self.db, "--routing-registry", str(ROUTES), "--validate-legacy-import"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_live_shaped_imported_dry_run_reconciles_without_new_effects(self):
        row = legacy_row(issue="ENG-41", mode="dry-run", repository=HOST, job_id="dry-history", pr_url=SENTINEL)
        self.insert(row)
        init_db(self.conn)
        before = tuple(self.conn.execute(query).fetchone()[0] for query in ("SELECT COUNT(*) FROM factory_attempts", "SELECT COUNT(*) FROM bridge_jobs", "SELECT COUNT(*) FROM factory_attempt_events"))
        init_db(self.conn)
        after = tuple(self.conn.execute(query).fetchone()[0] for query in ("SELECT COUNT(*) FROM factory_attempts", "SELECT COUNT(*) FROM bridge_jobs", "SELECT COUNT(*) FROM factory_attempt_events"))
        self.assertEqual((before, after), ((1, 1, 1), (1, 1, 1)))
        self.assertEqual(self.predeploy().returncode, 0)

    def test_imported_real_disabled_route_reconciles_but_operator_route_is_ineligible(self):
        pr_url = f"https://github.com/{HOST}/pull/7"
        row = legacy_row(issue="ENG-7", repository=HOST, job_id="eng-7", pr_url=pr_url)
        self.insert(row, event={"effect": {"pr_url": pr_url, "head_branch": row["branch"]}})
        self.insert_attempt(row)
        self.assertEqual(self.predeploy().returncode, 0)
        with self.assertRaises(OperatorError):
            _load_route(HOST_PROJECT, str(ROUTES))

    def test_eng8_generation_one_receipt_reconciles_and_preview_still_requires_operator_gates(self):
        pr_url = f"https://github.com/{CAPYKIT}/pull/20"
        row = legacy_row(issue="ENG-8", job_id="eng-8", pr_url=pr_url)
        self.insert(row, event={"effect": {"pr_url": pr_url, "head_branch": row["branch"]}})
        init_db(self.conn)
        self.assertEqual(self.predeploy().returncode, 0)
        ingress = str(Path(self.work.name) / "ingress.db")
        sqlite3.connect(ingress).close()
        with self.assertRaisesRegex(OperatorError, "trusted_ingress"):
            preview_retry(bridge_db=self.db, ingress_db=ingress, registry_path=str(ROUTES), policy_path=str(POLICY), issue="ENG-8", mode="real")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0], 1)

    def test_failed_unlineaged_active_key_without_artifact_is_not_legacy(self):
        row = legacy_row(issue="ENG-9", job_id="eng-9-failed", state="failed", pr_url=None)
        self.assertEqual(self.decision(row).action, "not_legacy")
        dry_row = legacy_row(issue="ENG-9", mode="dry-run", job_id="eng-9-dry-run", state="failed", pr_url=None)
        self.assertEqual(self.decision(row, attempts=[legacy_attempt(dry_row, status="canceled")]).action, "not_legacy")
        self.assertEqual(self.decision(row, sources=artifact_sources(row, ("event", {"branch": row["branch"]}))).action, "not_legacy")
        self.insert(row)
        result = self.predeploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("legacy import validation errors: 0", result.stdout)

    def test_failed_active_key_with_side_effect_evidence_stays_blocked(self):
        row = legacy_row(issue="ENG-9", job_id="eng-9-failed", state="failed", pr_url=None)
        attempt = legacy_attempt(row, status="failed", actor="intake-worker")
        with self.assertRaisesRegex(ValueError, "approved terminal receipt"):
            self.decision(row, attempts=[attempt])
        with self.assertRaisesRegex(ValueError, "approved terminal receipt"):
            self.decision(row, sources=artifact_sources(row, ("event", {"effect": {"pr_url": f"https://github.com/{CAPYKIT}/pull/20", "head_branch": row["branch"]}})))
        with self.assertRaisesRegex(ValueError, "approved terminal receipt"):
            self.decision({**row, "pr_url": f"https://github.com/{CAPYKIT}/pull/20"})

    def test_forged_receipts_fail_closed(self):
        pr_url = f"https://github.com/{CAPYKIT}/pull/20"
        row = legacy_row(pr_url=pr_url)
        base = legacy_attempt(row)
        forged = {
            "unknown-repository": ({**row, "payload_json": json.dumps({"linear_issue": "ENG-8", "repo": "Unknown/repo", "dry_run": False})}, base),
            "cross-repository": (row, legacy_attempt(row, prior_pr_url=f"https://github.com/{HOST}/pull/20")),
            "missing-artifact": ({**row, "pr_url": None}, legacy_attempt({**row, "pr_url": None})),
            "prior-mismatch": (row, legacy_attempt(row, prior_pr_url=f"https://github.com/{CAPYKIT}/pull/21")),
            "non-legacy-actor": (row, legacy_attempt(row, actor="operator")),
            "arbitrary-status": (row, legacy_attempt(row, status="legacy_permitted")),
            "malformed-source-url": ({**row, "pr_url": "https://github.com/Driftward-LLC/capykit/pull/not-a-number"}, base),
            "malformed-source-branch": ({**row, "branch": "codex/not-deterministic"}, base),
        }
        for name, (source, attempt) in forged.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.decision(source, attempts=[attempt], events=[import_event(attempt)])

    def test_other_generation_action_lease_or_pending_attempt_cannot_hide_behind_receipt(self):
        row = legacy_row(pr_url=f"https://github.com/{CAPYKIT}/pull/20")
        base = legacy_attempt(row)
        gen_two = {**base, "attempt_id": "generation-two", "generation": 2, "job_id": None, "idempotency_key": "attempt:2", "branch": deterministic_branch("attempt:2"), "status": "failed"}
        non_create = {**base, "attempt_id": "inspect", "action": "inspect_draft_pr_state", "job_id": None, "idempotency_key": "inspect:1", "branch": deterministic_branch("inspect:1"), "status": "failed"}
        pending = {**gen_two, "status": "running"}
        for name, attempts, leases in (
            ("generation-two", [base, gen_two], []),
            ("non-create", [base, non_create], []),
            ("pending-other-generation", [base, pending], []),
            ("active-lease-other-generation", [base, gen_two], [{"attempt_id": "generation-two", "released_at": None, "effect_authorized_at": None, "effect_observed_at": None}]),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.decision(row, attempts=attempts, events=[import_event(base)], leases=leases)

    def test_unimported_dry_run_sentinel_is_exact_and_real_rows_stay_strict(self):
        dry = legacy_row(issue="ENG-41", mode="dry-run", repository=HOST, pr_url=SENTINEL)
        self.assertEqual(self.decision(dry).action, "import")
        malformed = {**dry, "pr_url": "dry-run://github/pr/not-a-uuid"}
        with self.assertRaises(ValueError):
            self.decision(malformed)
        real = legacy_row(issue="ENG-7", repository=HOST, pr_url=f"https://github.com/{HOST}/pull/7")
        with self.assertRaises(ValueError):
            self.decision(real)

    def test_unrelated_nested_urls_and_branches_are_not_artifact_evidence(self):
        row = legacy_row(pr_url=None)
        nested = ("event", {"metadata": {"url": f"https://github.com/{CAPYKIT}/pull/20", "branch": row["branch"]}})
        with self.assertRaises(ValueError):
            self.decision(row, sources=artifact_sources(row, nested))
        direct = ("bridge", {"pr_url": f"https://github.com/{CAPYKIT}/pull/20", "branch": row["branch"]})
        event = ("event", {"effect": {"pr_url": f"https://github.com/{CAPYKIT}/pull/20", "head_branch": row["branch"]}})
        self.assertEqual(self.decision(row, sources=[direct, event]).action, "import")

    def test_full_generated_live_shaped_matrix_reconciles_without_writes_or_effects(self):
        rows = []
        for number in range(1, 22):
            rows.append(legacy_row(issue=f"ENG-{100 + number}", mode="dry-run", repository=HOST, job_id=f"host-dry-{number}"))
        for number in range(1, 15):
            rows.append(legacy_row(issue=f"ENG-{200 + number}", mode="dry-run", repository=CAPYKIT, job_id=f"capykit-dry-{number}", pr_url=SENTINEL.replace("0123456789ab", f"0123456789{number:02x}")))
        rows.extend((
            legacy_row(issue="ENG-7", repository=HOST, job_id="eng-7", pr_url=f"https://github.com/{HOST}/pull/7"),
            legacy_row(issue="ENG-8", repository=CAPYKIT, job_id="eng-8", pr_url=f"https://github.com/{CAPYKIT}/pull/20"),
            legacy_row(issue="ENG-60", repository=CAPYKIT, job_id="eng-60", pr_url=f"https://github.com/{CAPYKIT}/pull/60"),
        ))
        self.assertEqual(len(rows), 38)
        for row in rows:
            self.insert(row)
            self.insert_attempt(row)
        before = tuple(self.conn.execute(query).fetchone()[0] for query in ("SELECT COUNT(*) FROM bridge_jobs", "SELECT COUNT(*) FROM factory_attempts", "SELECT COUNT(*) FROM factory_attempt_events", "SELECT COUNT(*) FROM factory_execution_leases"))
        result = self.predeploy()
        after = tuple(self.conn.execute(query).fetchone()[0] for query in ("SELECT COUNT(*) FROM bridge_jobs", "SELECT COUNT(*) FROM factory_attempts", "SELECT COUNT(*) FROM factory_attempt_events", "SELECT COUNT(*) FROM factory_execution_leases"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("legacy import validation errors: 0", result.stdout)
        self.assertEqual(before, after)

    def test_importer_and_predeploy_share_fixture_decisions(self):
        cases = (
            ("exact-dry", legacy_row(issue="ENG-41", mode="dry-run", repository=HOST, pr_url=SENTINEL), True),
            ("disabled-real-unimported", legacy_row(issue="ENG-7", repository=HOST, pr_url=f"https://github.com/{HOST}/pull/7"), False),
            ("malformed-real", legacy_row(issue="ENG-9", repository=CAPYKIT, pr_url="https://example.invalid/pull/9"), False),
        )
        for name, row, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "bridge.db")
                conn = connect(path)
                conn.execute("INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (row["job_id"], row["idempotency_key"], row["state"], row["requested_action"], row["payload_json"], "fixture", row["branch"], row["pr_url"], NOW, NOW))
                conn.commit()
                result = subprocess.run(["python3", str(CHECK), "--db", path, "--routing-registry", str(ROUTES), "--validate-legacy-import"], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode == 0, expected)
                init_db(conn)
                conflicts = conn.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0]
                attempts = conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0]
                self.assertEqual((conflicts == 0 and attempts == 1), expected)
                conn.close()
