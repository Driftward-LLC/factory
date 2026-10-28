import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import human_time, operator, store
from test_store import verifier_payload
from test_support import connect_migrate

ACTOR = "uid:0;pid:123;account:root"


class HumanTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bridge.db")
        self.conn = connect_migrate(self.path)
        self.addCleanup(self.conn.close)
        self.attempt, self.job, _ = store.reserve_factory_retry(
            self.conn, work_item_key="ENG-600", mode="real", request_id="fixture", reason="fixture", actor="test",
            payload=verifier_payload("Driftward-LLC/capykit"))
        self.aid = self.attempt["attempt_id"]

    def record(self, **changes):
        return human_time.record_human_time(self.conn, self.aid, **{
            "request_id": "session-1", "duration_ms": 300_000, "category": "intervention", "actor": ACTOR, **changes})

    def snapshot(self):
        tables = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: [tuple(row) for row in self.conn.execute(f'SELECT * FROM "{table}"')] for table in tables}

    def test_append_only_and_retry_preserve_state_and_original_actor(self):
        before = self.snapshot()
        first = self.record()
        repeated = self.record(actor="uid:0;pid:456;account:root")
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(first["event"], repeated["event"])
        self.assertEqual(first["event"]["details"], {
            "schema_version": 1, "request_id": "session-1", "duration_ms": 300_000,
            "category": "intervention", "actor": ACTOR})
        self.assertEqual(first["event"]["event_type"], "human_time_recorded")
        self.assertEqual(uuid.UUID(first["event"]["event_id"]).version, 5)
        after = self.snapshot()
        before_events, after_events = before.pop("factory_attempt_events"), after.pop("factory_attempt_events")
        self.assertEqual(after, before)
        self.assertEqual(after_events[:-1], before_events)
        self.assertEqual(len(after_events), len(before_events) + 1)
        for change in ({"duration_ms": 0}, {"category": "review"}):
            with self.subTest(change=change), self.assertRaises(store.BridgeConflict):
                self.record(**change)
        self.assertEqual(self.record()["event"], first["event"])

    def test_zero_and_maximum_duration_are_explicit_entries(self):
        for duration, category in ((0, "intervention"), (86_400_000, "review"), (1, "maintenance")):
            result = self.record(request_id=f"entry:{category}", duration_ms=duration, category=category)
            self.assertEqual(result["event"]["details"]["duration_ms"], duration)

    def test_invalid_and_private_fields_never_write_or_echo_input(self):
        invalid = [
            {"duration_ms": value} for value in (True, False, -1, 86_400_001, 1.0, float("nan"), float("inf"), "1", None)
        ] + [
            {"request_id": value} for value in (None, "", "x" * 129, "../private", "contains spaces", "line\nbreak",
                                                "sk-private-value", "ghp_private-value", "email@example.com", "https://example.com")
        ] + [{"category": value} for value in (None, [], "unknown")] + [
            {"actor": value} for value in ("uid:0", "uid:1000;pid:123;account:user", "uid:0;pid:1;account:root\n")
        ]
        before = self.snapshot()
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(store.BridgeValidationError) as caught:
                self.record(**changes)
            self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_missing_invalid_dry_run_and_legacy_attempts_are_rejected(self):
        kwargs = {"request_id": "entry", "duration_ms": 1, "category": "review", "actor": ACTOR}
        with self.assertRaises(store.BridgeNotFound):
            human_time.record_human_time(self.conn, str(uuid.uuid4()), **kwargs)
        with self.assertRaises(store.BridgeValidationError):
            human_time.record_human_time(self.conn, self.job["job_id"] + "/extra", **kwargs)
        for column, value in (("mode", "dry-run"), ("actor", "legacy-import"), ("status", "legacy_failed")):
            self.conn.execute("BEGIN")
            self.conn.execute(f"UPDATE factory_attempts SET {column}=? WHERE attempt_id=?", (value, self.aid))
            with self.subTest(column=column), self.assertRaises(store.BridgeValidationError):
                self.record()
            self.assertTrue(self.conn.in_transaction)
            self.conn.rollback()

    def test_enclosing_transaction_can_rollback_success_and_conflict(self):
        before = self.snapshot()
        self.conn.execute("BEGIN")
        self.record()
        self.assertTrue(self.conn.in_transaction)
        with self.assertRaises(store.BridgeConflict):
            self.record(duration_ms=1)
        self.assertTrue(self.conn.in_transaction)
        other = store.connect_runtime(self.path)
        try:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM factory_attempt_events WHERE event_type=?", (human_time.EVENT,)).fetchone()[0], 0)
        finally:
            other.close()
        self.conn.rollback()
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_replays_create_exactly_one_entry(self):
        gate = threading.Barrier(2)
        def record():
            conn = store.connect_runtime(self.path)
            try:
                gate.wait(timeout=5)
                return human_time.record_human_time(conn, self.aid, request_id="concurrent", duration_ms=5,
                                                    category="intervention", actor=ACTOR)
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: record(), range(2)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(results[0]["event"], results[1]["event"])

    @unittest.skipUnless(os.geteuid() == 0, "actual root Unix peer required")
    def test_operator_http_auth_schema_replay_conflict_and_missing_target(self):
        database = self.path
        class Handler(operator.OperatorHandler):
            def _paths(self): return database, "unused", "unused", "unused"
        socket_path = str(Path(self.tmp.name) / "operator.sock")
        server = operator._UnixHTTPServer(socket_path, Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close_server():
            server.shutdown(); worker.join(timeout=5); server.server_close()
        self.addCleanup(close_server)
        route = f"/v1/attempts/{self.aid}/human-time"
        body = {"request_id": "http-entry", "duration_ms": 300_000, "category": "review"}

        def post(value=body, *, path=route, token="fixture-only", raw=None):
            client = http.client.HTTPConnection("localhost", timeout=5)
            client.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.sock.settimeout(5); client.sock.connect(socket_path)
            try:
                client.request("POST", path, body=json.dumps(value) if raw is None else raw,
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()

        with patch.dict(os.environ, {"HATCHET_FACTORY_OPERATOR_TOKEN": "fixture-only"}):
            self.assertEqual(post(token="wrong")[0], 401)
            with patch.object(Handler, "_actor", return_value="uid:1000;pid:2;account:user"):
                self.assertEqual(post()[0], 400)
            status, first = post()
            self.assertEqual(status, 201)
            self.assertRegex(first["event"]["details"]["actor"], r"^uid:0;pid:[0-9]+;account:[A-Za-z0-9_-]+$")
            status, again = post()
            self.assertEqual(status, 200)
            self.assertEqual(first["event"], again["event"])
            self.assertEqual(post(dict(body, duration_ms=5))[0], 409)
            self.assertEqual(post(path=f"/v1/attempts/{uuid.uuid4()}/human-time")[0], 404)
            self.assertEqual(post(path="/v1/attempts/invalid/human-time")[0], 400)
            for invalid in ({}, [], dict(body, actor=ACTOR), dict(body, notes="private text"), dict(body, duration_ms=True)):
                self.assertEqual(post(invalid)[0], 400)
            for query in ("?", "?raw", "?raw=", "?days=1", "?actor=root"):
                self.assertEqual(post(path=route + query)[0], 400)
            self.assertEqual(post(raw="{")[0], 400)
            self.assertEqual(post(raw=b"\xff")[0], 400)
            self.assertEqual(post(raw="x" * 8193)[0], 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_events WHERE event_type=?", (human_time.EVENT,)).fetchone()[0], 1)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("curl"), "CLI requires jq and curl")
    def test_real_cli_records_once_and_reports_persisted_time(self):
        root = Path(self.tmp.name)
        env_file = root / "operator.env"
        env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture-local\n")
        env_file.chmod(0o600)
        socket_path = str(root / "operator.sock")
        class Handler(operator.OperatorHandler):
            def _actor(self):
                # Nonroot CI simulates only root identity; curl, HTTP and storage stay real.
                return super()._actor() if os.geteuid() == 0 else ACTOR
        server = operator._UnixHTTPServer(socket_path, Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close_server():
            server.shutdown(); worker.join(timeout=5); server.server_close()
        self.addCleanup(close_server)
        env = {
            "HATCHET_FACTORY_OPERATOR_TOKEN": "fixture-local",
            "FACTORY_OPERATOR_ENV": str(env_file), "FACTORY_OPERATOR_SOCKET": socket_path,
            "HATCHET_FACTORY_OPERATOR_BRIDGE_DB": self.path,
            "HATCHET_FACTORY_OPERATOR_INGRESS_DB": str(root / "absent-ingress.db"),
            "HATCHET_FACTORY_OPERATOR_LIFECYCLE_DB": str(root / "absent-lifecycle.db"),
            "HATCHET_FACTORY_OPERATOR_RUNTIME": "systemd",
            "HATCHET_VERIFIER_POLICY_PATH": str(Path(__file__).resolve().parents[2] / "workers/verifier-policy.json"),
        }
        if os.geteuid() != 0:
            bin_dir = root / "bin"; bin_dir.mkdir()
            identity = bin_dir / "id"
            identity.write_text("#!/bin/sh\n[ \"$*\" = '-u' ] || exit 1\nprintf '0\\n'\n")
            identity.chmod(0o700)
            env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        cli = str(Path(__file__).resolve().parents[3] / "factory/factory")
        def run(*args):
            result = subprocess.run([cli, "analytics", *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        command = ("record-time", self.aid.upper(), "--minutes", "1.125", "--request-id", "cli-session-1")
        with patch.dict(os.environ, env):
            first = run(*command)
            again = run(*command)
            report = run("--json")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["event"], again["event"])
        self.assertEqual(first["event"]["attempt_id"], self.aid)
        self.assertEqual(first["event"]["details"]["duration_ms"], 67_500)
        human = report["effort"]["human"]
        self.assertEqual(human["entries"], 1)
        self.assertEqual(human["recorded_ms"], 67_500)
        self.assertEqual(human["recorded_interventions"], 1)
        self.assertEqual(human["by_category_ms"]["intervention"], 67_500)
        self.assertEqual(human["completeness"], "self_reported_not_attested")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM factory_attempt_events WHERE event_type=?", (human_time.EVENT,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
