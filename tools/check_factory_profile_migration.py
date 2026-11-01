#!/usr/bin/env python3
"""Exercise the migration CLI with synthetic state; no live installation or network."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "deploy")]
import test_factory_production as fixtures
import check_factory_cutover as cutover_check
import migration_postgres as pg
import profile_migration as migration
import portable_state as state
import bootstrap
import telemetry

LABEL = "io.driftward.factory.profile-migration-test"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "source-image", "postgres-image", "hatchet-image"):
        parser.add_argument("--" + name, required=True, help="existing exact local image ID")
    args = parser.parse_args()
    os.umask(0o077)
    for image in (args.image, args.source_image, args.postgres_image, args.hatchet_image):
        assert state._docker(["image", "inspect", "--format", "{{.Id}}", image]) == image
    tag = uuid.uuid4().hex[:10]
    project, target, retired = ("factory-migration-" + tag + suffix for suffix in ("-source", "-target", "-retired"))
    root = Path(tempfile.mkdtemp(prefix="factory-profile-check-"))
    source, destination, snapshot = (root / n for n in ("source", "target", "snapshot"))
    bundle = root / "old-bundle"
    shutil.copytree(ROOT / "deploy", bundle, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(ROOT / "hatchet/bridge/factory_organization.py", bundle / "factory_organization.py")
    source_id = str(uuid.uuid4())
    containers, volumes = [], []
    try:
        bootstrap.init_directory(source, args.source_image, project, apply=True)
        values = bootstrap.read_env(source / ".env")
        values.update(FACTORY_POSTGRES_IMAGE=args.postgres_image, FACTORY_HATCHET_IMAGE=args.hatchet_image)
        marker = json.loads((source / bootstrap.MARKER).read_text())
        marker.update(profile=state.PRODUCTION, installation_id=source_id,
                      telemetry={"image": args.source_image, "port": 4318, "enabled": True,
                                 "deployment_sha256": {n: state._hash(bundle / n) for n in telemetry.FILES}})
        bootstrap._create_private(source / telemetry.ENV_FILE, bootstrap.env_text({"POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces", "POSTHOG_PROJECT_TOKEN": "phc_synthetic_migration_fixture"}))
        (source / ".env").write_text(bootstrap.env_text(values))
        (source / bootstrap.MARKER).write_text(json.dumps(marker))
        tenant = values["HATCHET_DEFAULT_TENANT_ID"]
        token = "fixture." + base64.urlsafe_b64encode(json.dumps({"sub": tenant}).encode()).decode().rstrip("=") + ".fixture"
        bootstrap._create_private(source / "env/hatchet-client.env", bootstrap.env_text({"HATCHET_CLIENT_TOKEN": token}))
        for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key"):
            bootstrap._create_private(source / "keys/hatchet" / name, "synthetic fixture key")
        images = {name: {"postgres": args.postgres_image, "hatchet-lite": args.hatchet_image}.get(name, args.source_image)
                  for name in fixtures.production.SERVICES}
        retired_ids = []
        for owner, roles in ((retired, fixtures.production.SERVICES), (project, (*fixtures.production.SERVICES, telemetry.SERVICE))):
            for role in roles:
                command = ["create", "--name", owner + "-" + role, "--network", "none", "--restart", "no", "--entrypoint", "true",
                           "--label", LABEL + "=" + tag, "--label", "com.docker.compose.project=" + owner,
                           "--label", "com.docker.compose.service=" + role]
                if owner == project:
                    command.extend(["--label", state.IDENTITY_LABEL + "=" + source_id, "--label", fixtures.production.TENANT_LABEL + "=" + tenant])
                container = state._docker([*command, images.get(role, args.source_image)])
                containers.append(container)
                if owner == retired:
                    retired_ids.append(container)
        provenance = {"source_project": retired, "destination_project": project, "source_container_ids": retired_ids,
                      "source_images": images, "factory_image": args.source_image, "tenant_id": tenant, "activation_approved": False,
                      "profile_sha256": state._hash(bundle / "compose.production.yml"),
                      "deployment_sha256": {n: state._hash(bundle / n) for n in state.deployment_files(state.PRODUCTION)}}
        bootstrap._create_private(source / state.PROVENANCE, json.dumps(provenance))
        for suffix in state.VOLUMES:
            volume = migration.create_volume(project, source_id, suffix)
            volumes.append(volume)
        pg_env = root / "postgres.env"
        bootstrap._create_private(pg_env, "POSTGRES_USER=hatchet\nPOSTGRES_DB=hatchet\nPOSTGRES_PASSWORD=synthetic-fixture-password\nPGDATA=/var/lib/postgresql/data\nLANG=en_US.utf8\n")
        seed = state._docker(["run", "-d", "--name", project + "-seed", "--label", LABEL + "=" + tag,
                              "--network", "none", "--restart", "no", "--env-file", str(pg_env), "--mount",
                              "type=volume,src=" + project + "-hatchet-postgres-data,dst=/var/lib/postgresql/data", args.postgres_image,
                              "postgres", "-c", "listen_addresses=127.0.0.1"])
        containers.append(seed)
        pg.ready(seed)
        pg.psql(seed, """CREATE EXTENSION btree_gist;
          CREATE TABLE history (id serial PRIMARY KEY, removed text, created_at timestamp, value text NOT NULL);
          INSERT INTO history(value) SELECT 'synthetic-' || n FROM generate_series(1,20) n;
          ALTER TABLE history DROP COLUMN removed;
          SELECT setval('history_id_seq',25);
          CREATE INDEX history_value_idx ON history(value);
          CREATE TABLE events (created_at timestamp DEFAULT now(), id int) PARTITION BY RANGE (created_at);
          CREATE TABLE events_old PARTITION OF events FOR VALUES FROM ('2020-01-01') TO ('2021-01-01');
          ALTER TABLE events_old ALTER COLUMN created_at DROP DEFAULT;
          INSERT INTO events VALUES ('2020-02-01',1);""")
        state._docker(["stop", "--timeout", "30", seed])
        state._docker(["rm", seed]); containers.remove(seed)
        for suffix, filename in (("bridge-data", "bridge.db"), ("ingress-data", "ingress.db"), ("lifecycle-data", "outbox.db")):
            payload = root / suffix
            payload.mkdir(mode=0o700)
            if suffix == "bridge-data":
                cutover_check.seed_ledger(payload / filename)
                with sqlite3.connect(payload / filename) as conn:
                    conn.execute("UPDATE factory_execution_leases SET released_at='2026-01-01T00:00:00Z'")
            else:
                with sqlite3.connect(payload / filename) as conn:
                    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, value TEXT)")
                    conn.execute("INSERT INTO history VALUES (1,'synthetic history')")
            archive = root / (suffix + ".tar")
            with tarfile.open(archive, "w") as handle:
                handle.add(payload / filename, arcname="./" + filename)
            state._tar(args.image, project + "-" + suffix, root, archive.name, restore=True)
        hermes_project = "factory-hermes-fixture-" + tag
        hermes_name = hermes_project + "-data"
        state._docker(["volume", "create", "--label", LABEL + "=" + tag, hermes_name]); volumes.append(hermes_name)
        hermes = state._docker(["create", "--name", project + "-hermes", "--label", LABEL + "=" + tag,
                                "--network", "none", "--restart", "no", "--label", "com.docker.compose.project=" + hermes_project,
                                "--label", "com.docker.compose.service=hermes", "--mount", "type=volume,src=" + hermes_name + ",dst=/opt/data",
                                "--entrypoint", "true", args.image]); containers.append(hermes)
        with (root / "hermes.txt").open("w") as handle:
            handle.write("synthetic dedicated Hermes state")
        with tarfile.open(root / "hermes.tar", "w") as handle:
            handle.add(root / "hermes.txt", arcname="./retained.txt")
            link = tarfile.TarInfo("./current")
            link.type, link.linkname = tarfile.SYMTYPE, "/opt/data/retained.txt"
            handle.addfile(link)
        state._tar(args.image, hermes_name, root, "hermes.tar", restore=True)
        source_install = fixtures.production.Production(source)
        state.backup(source_install, snapshot, apply=True)
        configuration = root / "reviewed-configuration"
        shutil.copytree(source / "config", configuration)
        for path in configuration.iterdir():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        command = [sys.executable, str(ROOT / "deploy/profile_migration.py"), "--source-bundle", str(bundle), "--source", str(source),
                   "--snapshot", str(snapshot), "--destination", str(destination), "--project", target, "--image", args.image,
                   "--configuration", str(configuration), "--hermes-container", project + "-hermes"]
        preview = json.loads(pg.run(command))
        assert not destination.exists()
        assert preview["plan"]["configuration_sha256"] == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in configuration.iterdir()}
        rejected = subprocess.run([*command, "--apply", "--approve-digest", "0" * 64], capture_output=True)
        assert rejected.returncode != 0 and not destination.exists()
        pg.run([*command, "--apply", "--approve-digest", preview["preview_sha256"]])
        candidate = fixtures.production.Production(destination)
        candidate.preflight()
        assert not candidate.service_rows()
        assert bootstrap.configuration(candidate.directory / "config") == bootstrap.configuration(configuration)
        for path in configuration.iterdir():
            assert (candidate.directory / "config" / path.name).read_bytes() == path.read_bytes()
        assert candidate.provenance["profile_migration"]["configuration_sha256"] == preview["plan"]["configuration_sha256"]
        assert candidate.provenance["source_images"] == provenance["source_images"]
        assert len(candidate.durable_volumes) == 8
        assert set(candidate.images.values()) == {args.image}
        assert candidate.provenance["profile_migration"]["postgres_verification"]["rows"] == 21
        second = root / "unified-backup"
        state.backup(candidate, second, apply=True)
        restored = root / "restored"
        restore_project = target + "-restore"
        state.restore(second, restored, restore_project, apply=True, production=True, port_offset=10000)
        fixtures.production.Production(restored).preflight()
        print(json.dumps({"migration_cli": "passed", "stale_approval": "refused", "source_images_preserved": True,
                          "durable_volumes": 8, "unified_backup_restore": "passed", "services_started": 0}))
        return 0
    finally:
        # All test project names were generated uniquely; check ownership before deletion.
        for container in reversed(containers):
            row = json.loads(state._docker(["container", "inspect", container]))[0]
            assert row["Config"]["Labels"].get(LABEL) == tag
            if row["State"]["Running"]:
                state._docker(["stop", "--timeout", "30", container])
            state._docker(["rm", container])
        for name in state._available_volumes():
            if name in volumes or name.startswith(target + "-"):
                row = json.loads(state._docker(["volume", "inspect", name]))[0]
                labels = row.get("Labels") or {}
                assert labels.get(LABEL) == tag or labels.get("com.docker.compose.project") in (project, target, target + "-restore")
                state._no_writers([name])
                state._docker(["volume", "rm", name])
        for name in (project, target, target + "-restore"):
            ids = state._docker(["network", "ls", "-q", "--filter", "name=^" + name + "$"])
            if ids:
                row = json.loads(state._docker(["network", "inspect", ids]))[0]
                assert row.get("Labels", {}).get(state.IDENTITY_LABEL)
                state._docker(["network", "rm", ids])
        # Evidence is synthetic, but retain it on failure to diagnose without rerunning live work.
        print("Synthetic rehearsal evidence: " + str(root))


if __name__ == "__main__":
    raise SystemExit(main())
