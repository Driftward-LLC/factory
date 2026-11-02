#!/usr/bin/env python3
"""Rehearse offline naming metadata migration on synthetic disk state; no Docker."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import naming_migration as naming

ATTEMPT = "11111111-2222-4333-8444-555555555555"
INFO = {"identity_label": "io.synthetic.installation", "tenant_label": "io.synthetic.tenant"}


def configuration():
    return {"format_version": 1, "source_identity_label": INFO["identity_label"],
            "source_tenant_label": INFO["tenant_label"], "source_environment_prefix": "SYNTHETIC_",
            "environment_keys": {"SYNTHETIC_HOME": "FACTORY_HOME", "SYNTHETIC_HERMES_CREATE_DRAFT_PR_ENABLED": "FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED"},
            "environment_values": {"/synthetic/executor": "/factory/executor"},
            "agent_trace_seed": "synthetic-history:trace:attempt:{attempt_id}", "gateway_policy": "unbound"}


def seed(path, volume):
    """Small cold source schemas, including immutable historical UUIDs and payloads."""
    with sqlite3.connect(path) as conn:
        if volume == "bridge-data":
            conn.executescript("""
                CREATE TABLE bridge_schema_meta (schema_version INTEGER);
                INSERT INTO bridge_schema_meta VALUES (10);
                CREATE TABLE factory_attempts (attempt_id TEXT PRIMARY KEY,status TEXT);
                CREATE TABLE factory_execution_leases (released_at TEXT);
                INSERT INTO factory_execution_leases VALUES ('2026-01-01T00:00:00Z');
                CREATE TABLE bridge_jobs (state TEXT);
                INSERT INTO bridge_jobs VALUES ('ready_for_human_review');
                CREATE TABLE verification_leases (closed_at TEXT);
                INSERT INTO verification_leases VALUES ('2026-01-01T00:00:00Z');
                CREATE TABLE factory_attempt_outbox (acknowledged_at TEXT);
                INSERT INTO factory_attempt_outbox VALUES ('2026-01-01T00:00:00Z');
                CREATE TABLE factory_inspection_outbox (acknowledged_at TEXT);
                INSERT INTO factory_inspection_outbox VALUES ('2026-01-01T00:00:00Z');
                CREATE TABLE factory_inspection_requests (state TEXT);
                INSERT INTO factory_inspection_requests VALUES ('completed');
                CREATE TABLE factory_independent_reviews (state TEXT);
                INSERT INTO factory_independent_reviews VALUES ('changes_requested');
            """)
            conn.execute("INSERT INTO factory_attempts VALUES (?, 'verified_ready')", (ATTEMPT,))
        elif volume == "ingress-data":
            conn.executescript("""
                CREATE TABLE ingress_schema_meta (schema_version INTEGER);
                INSERT INTO ingress_schema_meta VALUES (3);
                CREATE TABLE ingress_events (hatchet_published INTEGER,publish_lease_id TEXT);
                INSERT INTO ingress_events VALUES (1,NULL);
            """)
        elif volume == "lifecycle-data":
            conn.executescript("""
                CREATE TABLE lifecycle_outbox (delivery_state TEXT,lease_owner TEXT);
                INSERT INTO lifecycle_outbox VALUES ('delivered',NULL);
            """)
        else:
            raise ValueError("unknown synthetic volume")
        conn.executescript("""
            CREATE TABLE immutable_history (event_id TEXT PRIMARY KEY,details_json TEXT NOT NULL);
            CREATE TRIGGER history_no_update BEFORE UPDATE ON immutable_history BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER history_no_delete BEFORE DELETE ON immutable_history BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER history_no_replace BEFORE INSERT ON immutable_history WHEN EXISTS (SELECT 1 FROM immutable_history WHERE event_id=NEW.event_id) BEGIN SELECT RAISE(ABORT,'append-only'); END;
        """)
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "synthetic-history:event:" + volume))
        conn.execute("INSERT INTO immutable_history VALUES (?,?)", (event_id, '{"source_key":"synthetic:original:1","path":"/synthetic/executor"}'))


def rows(conn):
    return {name: list(conn.execute(f'SELECT * FROM "{name}"')) for name, in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}


def hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def archive_rows(archive, filename):
    with tempfile.TemporaryDirectory(prefix="factory-naming-read-") as temporary:
        path = Path(temporary) / filename
        with tarfile.open(archive) as handle:
            path.write_bytes(handle.extractfile("./" + filename).read())
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
            return rows(conn), list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"))


def rehearse(root):
    """Backup, transform new archives, and restore the original bytes for rollback."""
    config = naming.validate_mapping(configuration(), INFO)
    source, snapshot, clone, rollback = (root / name for name in ("source", "snapshot", "clone", "rollback"))
    for directory in (source, snapshot, clone, rollback):
        directory.mkdir(mode=0o700)
    for directory in (snapshot / "volumes", clone / "volumes"):
        directory.mkdir(mode=0o700)
    bootstrap._create_private(source / ".env", bootstrap.env_text({"SYNTHETIC_HOME": "/synthetic/executor", "SYNTHETIC_HERMES_CREATE_DRAFT_PR_ENABLED": "1"}))
    (source / "gateway-journal.json").write_text(json.dumps({"attempt_id": ATTEMPT, "binding": "synthetic-source-only"}))
    for volume, filename in naming.SQLITE.items():
        folder = source / volume
        folder.mkdir(mode=0o700)
        seed(folder / filename, volume)
        (folder / "retained.json").write_text('{"historical_path":"/synthetic/executor"}\n')
        with tarfile.open(snapshot / "volumes" / (volume + ".tar"), "x") as handle:
            for path in sorted(folder.iterdir()):
                handle.add(path, arcname="./" + path.name)
    for name in (".env", "gateway-journal.json"):
        shutil.copyfile(source / name, snapshot / name)
        (snapshot / name).chmod(0o600)
    original_source, original_snapshot = hashes(source), hashes(snapshot)
    results = {}
    for volume, filename in naming.SQLITE.items():
        old, new = (folder / "volumes" / (volume + ".tar") for folder in (snapshot, clone))
        before, old_schema = archive_rows(old, filename)
        counts = naming.sqlite_archive(old, volume)
        assert naming.sqlite_archive(old, volume, output=new, config=config) == counts
        after, new_schema = archive_rows(new, filename)
        assert {name: after[name] for name in before} == before
        assert all(entry in new_schema for entry in old_schema)
        added = {"factory_naming_generation"} | ({"factory_imported_agent_traces"} if volume == "bridge-data" else set())
        assert set(after) - set(before) == added
        assert after["factory_naming_generation"] == [(bootstrap.NAMING_GENERATION,)]
        if volume == "bridge-data":
            expected = str(uuid.uuid5(uuid.NAMESPACE_URL, config["agent_trace_seed"].replace("{attempt_id}", ATTEMPT)))
            assert after["factory_imported_agent_traces"] == [(ATTEMPT, expected)]
        with tarfile.open(old) as old_archive, tarfile.open(new) as new_archive:
            assert old_archive.extractfile("./retained.json").read() == new_archive.extractfile("./retained.json").read()
            for member in old_archive:
                current = new_archive.getmember(member.name)
                assert (member.mode, member.uid, member.gid, member.mtime, member.type) == (current.mode, current.uid, current.gid, current.mtime, current.type)
            restored = rollback / volume
            restored.mkdir(mode=0o700)
            for member in old_archive:
                assert member.isfile()
                (restored / Path(member.name).name).write_bytes(old_archive.extractfile(member).read())
        results[volume] = {"original_rows_preserved": sum(map(len, before.values())), "integrity": "ok"}
    target_env = naming.environment(bootstrap.read_env(snapshot / ".env"), config)
    (clone / ".env").write_text(bootstrap.env_text(target_env))
    assert target_env["FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED"] == "0"
    assert config["gateway_policy"] == "unbound" and not (clone / "gateway-journal.json").exists()
    for name in (".env", "gateway-journal.json"):
        shutil.copyfile(snapshot / name, rollback / name)
    assert hashes(source) == original_source and hashes(snapshot) == original_snapshot
    assert hashes(rollback) == original_source
    report = {"stores": results, "source_unchanged": True, "snapshot_unchanged": True,
              "rollback_byte_identical": True, "gateway_journal": "UNBOUND", "services_started": 0,
              "source_sha256": original_source, "snapshot_sha256": original_snapshot}
    (root / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="new private directory for retained synthetic rehearsal evidence")
    args = parser.parse_args()
    if args.directory:
        args.directory.mkdir(mode=0o700)
        report = rehearse(args.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="factory-naming-rehearsal-") as temporary:
            report = rehearse(Path(temporary))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
