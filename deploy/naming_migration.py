"""Bounded markerless-source adapter for the offline profile migration.

All prior namespace strings are explicit private deployment input. This module
never opens a live database or changes the source installation.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
import uuid

import bootstrap
import migration_postgres as pg
import portable_state as state

require = pg.require
SQLITE = {"bridge-data": "bridge.db", "ingress-data": "ingress.db", "lifecycle-data": "outbox.db"}


def source_info(bundle, directory):
    # Use the preserved validator, never weaken the target bootstrap guard.
    script = """import importlib.util,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import bootstrap,portable_state
spec=importlib.util.spec_from_file_location('source_production',Path(sys.argv[1])/'factory-production.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
m,v=bootstrap.validate_directory(Path(sys.argv[2]))
print(json.dumps({'metadata':m,'values':v,'files':sorted(portable_state.config_files(m['profile'],m)),
 'identity_label':portable_state.IDENTITY_LABEL,'tenant_label':p.TENANT_LABEL,
 'volumes':list(portable_state.VOLUMES),'services':list(p.SERVICES)}))
"""
    import sys
    return json.loads(pg.run([sys.executable, "-c", script, str(bundle), str(directory)]))


def mapping(path, info):
    value = json.loads(bootstrap._read_private(state._path(path)))
    return validate_mapping(value, info)


def validate_mapping(value, info):
    require(isinstance(value, dict) and set(value) == {"format_version", "source_identity_label", "source_tenant_label",
            "source_environment_prefix", "environment_keys", "environment_values", "agent_trace_seed", "gateway_policy"}
            and value["format_version"] == 1, "invalid naming mapping format")
    for key in ("source_identity_label", "source_tenant_label"):
        require(value[key] == info[key.removeprefix("source_")] and re.fullmatch(r"[a-z][a-z0-9.-]{1,127}", value[key]),
                "source ownership labels differ from the preserved bundle")
    prefix = value["source_environment_prefix"]
    require(isinstance(prefix, str) and re.fullmatch(r"[A-Z][A-Z0-9]*_", prefix)
            and prefix not in ("FACTORY_", "HATCHET_", "LINEAR_"), "invalid source environment namespace")
    keys = value["environment_keys"]
    require(isinstance(keys, dict) and all(isinstance(k, str) and k.startswith(prefix)
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", k) and isinstance(v, str)
            and re.fullmatch(r"FACTORY_[A-Z0-9_]+", v) for k, v in keys.items())
            and len(set(keys.values())) == len(keys), "invalid or ambiguous environment key mapping")
    replacements = value["environment_values"]
    require(isinstance(replacements, dict) and all(isinstance(k, str) and k and isinstance(v, str)
            and k.startswith(("/", "unix:///")) and v.startswith(("/", "unix:///"))
            and v and not any(ord(c) < 32 for c in k + v) for k, v in replacements.items()), "invalid environment value mapping")
    seed = value["agent_trace_seed"]
    require(isinstance(seed, str) and 1 <= len(seed) <= 200 and seed.count("{attempt_id}") == 1
            and "{" not in seed.replace("{attempt_id}", "") and "}" not in seed.replace("{attempt_id}", "")
            and all(32 <= ord(c) < 127 for c in seed), "invalid historical agent trace seed")
    require(value["gateway_policy"] == "unbound", "naming migration must leave the executor gateway unbound")
    return value


def environment(values, config):
    output = {}
    for key, content in values.items():
        if key.startswith(config["source_environment_prefix"]):
            require(key in config["environment_keys"], "source environment key has no reviewed mapping")
        target = config["environment_keys"].get(key, key)
        require(target not in output, "environment mapping collides with an existing key")
        output[target] = config["environment_values"].get(content, content)
    # Preparation can never retain or introduce a grant to execute.
    for key in ("FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED", "FACTORY_REVIEWER_ENABLED"):
        if key in output:
            output[key] = "0"
    for key in ("FACTORY_HERMES_MODEL", "FACTORY_HERMES_PROVIDER"):
        output.pop(key, None)
    return output


def quiescent(conn, volume):
    require(not conn.execute("PRAGMA foreign_key_check").fetchall()
            and conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    require("factory_naming_generation" not in tables, "source SQLite already has a naming generation")
    checks = {
        "bridge-data": (
            ("bridge_schema_meta", "schema_version != 10 OR schema_version IS NULL"),
            ("factory_attempts", "status NOT IN ('completed','failed','verified_ready','canceled','canceled_after_effect','superseded','legacy_completed','legacy_failed','legacy_ready_for_human_review') OR status IS NULL"),
            ("factory_execution_leases", "released_at IS NULL"),
            ("bridge_jobs", "state NOT IN ('completed','ready_for_human_review','failed') OR state IS NULL"),
            ("verification_leases", "closed_at IS NULL"),
            ("factory_attempt_outbox", "acknowledged_at IS NULL"),
            ("factory_inspection_outbox", "acknowledged_at IS NULL"),
            ("factory_inspection_requests", "state NOT IN ('completed','expired') OR state IS NULL"),
            ("factory_independent_reviews", "state NOT IN ('passed','changes_requested','needs_operator') OR state IS NULL"),
        ),
        "ingress-data": (
            ("ingress_schema_meta", "schema_version != 3 OR schema_version IS NULL"),
            ("ingress_events", "hatchet_published != 1 OR hatchet_published IS NULL OR publish_lease_id IS NOT NULL"),
        ),
        "lifecycle-data": (
            ("lifecycle_outbox", "delivery_state NOT IN ('delivered','suppressed','archived') OR delivery_state IS NULL OR lease_owner IS NOT NULL"),
        ),
    }
    counts, pending = {}, {}
    for table, predicate in checks[volume]:
        require(table in tables, "unsupported or incomplete source SQLite schema")
        count = conn.execute(f'SELECT count(*) FROM "{table}" WHERE {predicate}').fetchone()[0]
        if count:
            pending[table] = count
        counts[table] = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    require(not pending, "source SQLite contains pending work, leases, or an unsupported schema: "
            + volume + " " + json.dumps(pending, sort_keys=True))
    if volume in ("bridge-data", "ingress-data"):
        table = "bridge_schema_meta" if volume == "bridge-data" else "ingress_schema_meta"
        require(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1, "source schema metadata is incomplete")
    return counts


def sqlite_archive(path, volume, *, output=None, config=None):
    """Check cold state; when requested add metadata only to a new archive."""
    state._archive_check(path)
    filename = SQLITE[volume]
    with tempfile.TemporaryDirectory(prefix="factory-naming-sqlite-") as temporary:
        root = Path(temporary)
        with tarfile.open(path) as archive:
            members = list(archive)
            for member in members:
                name = member.name.removeprefix("./")
                if name in (filename, filename + "-wal", filename + "-shm"):
                    require(member.isfile(), "SQLite state is not a regular file")
                    (root / name).write_bytes(archive.extractfile(member).read())
        require((root / filename).is_file(), "SQLite database missing from source backup")
        with sqlite3.connect(root / filename) as conn:
            counts = quiescent(conn, volume)
            if output is not None:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in tables), "unsupported SQLite table name")
                before = {name: list(conn.execute(f'SELECT * FROM "{name}"')) for name in tables}
                conn.execute("CREATE TABLE factory_naming_generation (generation TEXT NOT NULL)")
                conn.execute("INSERT INTO factory_naming_generation VALUES (?)", (bootstrap.NAMING_GENERATION,))
                if volume == "bridge-data":
                    conn.execute("CREATE TABLE factory_imported_agent_traces (attempt_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL)")
                    rows = [(r[0], str(uuid.uuid5(uuid.NAMESPACE_URL, config["agent_trace_seed"].replace("{attempt_id}", r[0]))))
                            for r in conn.execute("SELECT attempt_id FROM factory_attempts")]
                    conn.executemany("INSERT INTO factory_imported_agent_traces VALUES (?,?)", rows)
                require(all(list(conn.execute(f'SELECT * FROM "{name}"')) == rows for name, rows in before.items()),
                        "migration changed original SQLite rows")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if output is not None:
            # Drop checkpointed transient WAL/SHM entries, retain every other file,
            # archive path and owner exactly, replacing only the main database.
            with tarfile.open(path) as source, tarfile.open(output, "x") as target:
                for member in members:
                    name = member.name.removeprefix("./")
                    if name in (filename + "-wal", filename + "-shm"):
                        continue
                    if name == filename:
                        body = (root / filename).read_bytes()
                        member.size = len(body)
                        target.addfile(member, io.BytesIO(body))
                    else:
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
    return counts


# These are the scheduler/dispatch stores of the pinned Hatchet version, not its
# immutable event/history tables. Unknown layouts fail closed at the SQL call.
POSTGRES_PENDING = (
    ('WorkflowRun', '"status"::text NOT IN (\'SUCCEEDED\',\'COMPLETED\',\'FAILED\',\'CANCELLED\')'),
    ('StepRun', '"status"::text NOT IN (\'SUCCEEDED\',\'COMPLETED\',\'FAILED\',\'CANCELLED\')'),
    ('QueueItem', '"isQueued" IS DISTINCT FROM FALSE'), ('RetryQueueItem', '"isQueued" IS DISTINCT FROM FALSE'), ('InternalQueueItem', '"isQueued" IS DISTINCT FROM FALSE'),
    ('MessageQueueItem', '"status"::text NOT IN (\'COMPLETED\',\'SUCCEEDED\')'),
    ('WorkflowTriggerCronRef', '"enabled" AND "deletedAt" IS NULL'),
    ('WorkflowTriggerScheduledRef', '"deletedAt" IS NULL'),
    ('v1_queue_item', 'TRUE'), ('v1_retry_queue_item', 'TRUE'),
    ('v1_task_runtime', 'TRUE'), ('v1_task_runtime_slot', 'TRUE'),
    ('v1_batched_queue_item', 'TRUE'), ('v1_rate_limited_queue_items', 'TRUE'),
    ('v1_step_slot_request', 'TRUE'), ('v1_workflow_concurrency_slot', 'TRUE'),
    ('v1_concurrency_slot', '"is_filled" IS DISTINCT FROM FALSE'), ('v1_durable_sleep', 'TRUE'),
    ('v1_durable_event_log_entry', '"is_satisfied" IS DISTINCT FROM TRUE'),
    ('v1_match', '"is_satisfied" IS DISTINCT FROM TRUE'), ('v1_match_condition', '"is_satisfied" IS DISTINCT FROM TRUE'),
)


def postgres_quiescent(container):
    counts = {}
    for table, predicate in POSTGRES_PENDING:
        counts[table] = int(pg.psql(container, f'SELECT count(*) FROM public."{table}" WHERE {predicate};'))
    pending = {table: count for table, count in counts.items() if count}
    # Only bounded table names and aggregate counts escape private SQL evidence.
    require(not pending, "Hatchet naming migration requires no pending work or enabled schedules: "
            + json.dumps(pending, sort_keys=True))
    return counts


def journal_evidence(path):
    path = state._path(path)
    require(path.is_file() and not path.stat().st_mode & 0o077, "gateway journal archive must be a private regular file")
    state._archive_check(path)
    intents, results, files = set(), set(), 0
    with tarfile.open(path) as archive:
        for member in archive:
            if not member.isfile():
                continue
            files += 1
            name = Path(member.name.removeprefix('./'))
            if name.name == 'intent.json':
                intents.add(str(name.parent))
            elif name.name == 'result.json':
                results.add(str(name.parent))
    return {"path": str(path), "sha256": state._hash(path), "files": files,
            "intents": len(intents), "results": len(results), "intents_without_results": len(intents - results),
            "policy": "archival evidence only; never an active journal"}


def disabled_execution_configuration(supplied):
    organization = json.loads(supplied['organization.json'])
    routing = json.loads(supplied['routing-registry.json'])
    review = json.loads(supplied['independent-review-policy.json'])
    require(organization['github']['enabled'] is False
            and all(route['execution_enabled'] is False for route in routing['routes'])
            and review['enabled'] is False and review['automatic_repairs_allowed'] is False,
            "naming preparation requires GitHub, route execution, and independent review disabled")
