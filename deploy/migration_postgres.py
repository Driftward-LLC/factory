"""Offline PostgreSQL 15 logical migration; never mount the live source database."""
from __future__ import annotations

import copy
import json
import secrets
import subprocess
import time
from pathlib import Path

import bootstrap
import portable_state as state

HERE = Path(__file__).resolve().parent


class MigrationRefusal(ValueError):
    """Fixed operator-facing refusal; never subprocess output or secret values."""


def require(condition, message):
    if not condition:
        raise MigrationRefusal(message)


def run(args, *, data=None, output=None):
    result = subprocess.run(args, input=data, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("isolated migration command failed; private output withheld")
    return result.stdout


def psql(container, sql, database="hatchet"):
    return run(["docker", "exec", "-i", container, "psql", "-X", "-q", "-A", "-t", "-v",
                "ON_ERROR_STOP=1", "-U", "hatchet", "-d", database], data=sql.encode()).decode().strip()


def ready(container):
    # Wait for the final server, not the temporary initdb bootstrap server.
    for _ in range(90):
        if subprocess.run(["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "hatchet", "-d", "postgres"],
                          capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("isolated PostgreSQL startup timed out")


def fingerprints(container):
    sql = (HERE / "postgres-fingerprint.sql").read_text()
    return {db: json.loads(psql(container, sql, db)) for db in ("hatchet", "postgres", "template1")}


def compare(before, after):
    before, after = copy.deepcopy(before), copy.deepcopy(after)
    for databases in (before, after):
        for data in databases.values():
            positions = {}
            for column in data["columns"]:
                key = (column["table_schema"], column["table_name"])
                positions[key] = positions.get(key, 0) + 1
                column["ordinal_position"] = positions[key]
    equivalent_defaults = 0
    require(before.keys() == after.keys(), "restored database inventory differs")
    for db in before:
        for old, new in zip(before[db]["columns"], after[db]["columns"]):
            # pg_dump makes an absent timestamp partition default explicit NULL.
            if (old["column_default"] is None and new["column_default"] == "NULL::timestamp without time zone"
                    and old["column_name"] == new["column_name"] == "created_at"
                    and old["data_type"] == new["data_type"] == "timestamp without time zone"):
                new["column_default"] = None
                equivalent_defaults += 1
    require(before == after, "restored PostgreSQL schema, rows, indexes, constraints or sequences differ")
    return {"tables": sum(len(v["tables"]) for v in before.values()),
            "rows": sum(t["row_count"] for v in before.values() for t in v["tables"]),
            "equivalent_null_defaults": equivalent_defaults}


def migrate(snapshot, target_volume, old_image, image, project, identity, work):
    old, new = project + "-migration-old-pg", project + "-migration-new-pg"
    scratch = project + "-migration-pg-data"
    label = state.IDENTITY_LABEL + "=" + identity
    require(scratch not in state._available_volumes(), "migration scratch volume already exists")
    for name in (old, new):
        require(not state._docker(["ps", "-aq", "--filter", "name=^/" + name + "$"]), "migration container already exists")
    state._docker(["volume", "create", "--label", label, scratch])
    owned = json.loads(state._docker(["volume", "inspect", scratch]))[0]
    require(owned.get("Labels", {}).get(state.IDENTITY_LABEL) == identity, "foreign migration scratch volume")
    containers = []
    try:
        state._tar(image, scratch, snapshot / "volumes", "hatchet-postgres-data.tar", restore=True)
        common = ["--label", label, "--network", "none", "--restart", "no", "--security-opt",
                  "no-new-privileges:true", "--cpus", "2", "--memory", "1g"]
        old_id = state._docker(["run", "-d", "--name", old, *common, "--mount",
                               f"type=volume,src={scratch},dst=/var/lib/postgresql/data", old_image,
                               "postgres", "-c", "listen_addresses=127.0.0.1"])
        containers.append(old_id)
        ready(old)
        inventory = json.loads(psql(old, """SELECT json_build_object(
          'version',current_setting('server_version_num')::int,
          'locale',(SELECT datcollate FROM pg_database WHERE datname='postgres'),
          'ctype',(SELECT datctype FROM pg_database WHERE datname='postgres'),
          'databases',(SELECT json_agg(datname ORDER BY datname) FROM pg_database),
          'roles',(SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg_%'),
          'tablespaces',(SELECT json_agg(spcname ORDER BY spcname) FROM pg_tablespace));"""))
        require(150000 <= inventory["version"] < 160000 and inventory["databases"] == ["hatchet", "postgres", "template0", "template1"]
                and inventory["roles"] == ["hatchet"] and inventory["tablespaces"] == ["pg_default", "pg_global"],
                "migration supports the standard single-role PostgreSQL 15 Factory cluster only")
        require(inventory["locale"] == inventory["ctype"] and inventory["locale"] in ("en_US.utf8", "en_US.UTF-8"), "unsupported source database locale")
        before = fingerprints(old)
        dump = work / "postgres.sql"
        with dump.open("xb") as output:
            run(["docker", "exec", old, "pg_dumpall", "-U", "hatchet"], output=output)
        body = dump.read_bytes()
        require(body.count(b"CREATE ROLE hatchet;\n") == 1, "unexpected PostgreSQL bootstrap role")
        body = body.replace(b"CREATE ROLE hatchet;\n", b"", 1)
        env = work / "postgres.env"
        bootstrap._create_private(env, "POSTGRES_USER=hatchet\nPOSTGRES_DB=postgres\nPOSTGRES_PASSWORD=" + secrets.token_hex(24)
                                  + "\nPGDATA=/var/lib/postgresql/data\nLANG=" + inventory["locale"] + "\n")
        new_id = state._docker(["run", "-d", "--name", new, *common, "--env-file", str(env), "--mount",
                               f"type=volume,src={target_volume},dst=/var/lib/postgresql/data", "--entrypoint", "/usr/bin/tini", image,
                               "--", "/usr/local/bin/docker-entrypoint.sh", "postgres", "-c", "listen_addresses=127.0.0.1"])
        containers.append(new_id)
        ready(new)
        run(["docker", "exec", "-i", new, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "hatchet", "-d", "postgres"], data=body)
        after = fingerprints(new)
        result = compare(before, after)
        for sql in (
            "SELECT json_agg(t ORDER BY rolname) FROM (SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolconnlimit,rolpassword,rolvaliduntil,rolbypassrls FROM pg_authid WHERE rolname NOT LIKE 'pg_%') t;",
            "SELECT json_agg(t ORDER BY rolname) FROM (SELECT rolname,rolconfig FROM pg_roles WHERE rolname NOT LIKE 'pg_%') t;",
            "SELECT json_agg(t ORDER BY datname) FROM (SELECT datname,pg_get_userbyid(datdba) owner,encoding,datcollate,datctype,datistemplate,datallowconn,datconnlimit,datacl FROM pg_database) t;",
        ):
            require(psql(old, sql) == psql(new, sql), "PostgreSQL role/auth or database settings differ")
        require(psql(new, "SELECT bool_and(datcollversion IS NOT DISTINCT FROM pg_database_collation_actual_version(oid)) FROM pg_database WHERE datname <> 'template0';") == "t", "target database collation version differs")
        require(150000 <= int(psql(new, "SHOW server_version_num;")) < 160000, "target must remain PostgreSQL 15")
        for db in ("hatchet", "postgres"):
            psql(new, "ANALYZE;", db)
        bootstrap._create_private(work / "postgres-verification.json", json.dumps(result, sort_keys=True) + "\n")
        return result
    finally:
        # Only IDs created by this call are stopped/removed; never a source service.
        for container in reversed(containers):
            state._docker(["stop", "--timeout", "30", container])
            state._docker(["rm", container])
        state._docker(["volume", "rm", scratch])
