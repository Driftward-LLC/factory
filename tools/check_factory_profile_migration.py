#!/usr/bin/env python3
"""Exercise the migration CLI with synthetic state; no live installation or network."""
import argparse
import base64
import hashlib
import io
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
import check_factory_naming_migration as naming_fixture
import migration_postgres as pg
import profile_migration as migration
import portable_state as state
import bootstrap
import telemetry

LABEL = "io.factory.profile-migration-test"


def prior_naming_bundle(bundle):
    """Only the disposable source fixture accepts a markerless naming generation."""
    for path in bundle.iterdir():
        if path.suffix in (".py", ".yml", ".yaml"):
            text = path.read_text().replace("io.factory.installation", "io.example.installation").replace("io.factory.tenant", "io.example.tenant")
            if path.name == "bootstrap.py":
                guard = "        validate_naming_generation(metadata)\n"
                assert text.count(guard) == 1
                text = text.replace(guard, "", 1)
            path.write_text(text)


def source_volume(project, identity, suffix, identity_label):
    name = project + "-" + suffix
    assert name not in state._available_volumes()
    state._docker(["volume", "create", "--label", "com.docker.compose.project=" + project,
                   "--label", "com.docker.compose.volume=factory-" + suffix,
                   "--label", identity_label + "=" + identity, name])
    row = json.loads(state._docker(["volume", "inspect", name]))[0]
    assert row.get("Labels", {}).get(identity_label) == identity
    state._no_writers([name])
    return name


def seed_naming_postgres(container):
    """Model the pinned Hatchet dispatch schemas with terminal rows or empty queues."""
    inserted = 0
    for table, predicate in migration.naming.POSTGRES_PENDING:
        if '"status"' in predicate:
            declaration, values = '"status" text', "('SUCCEEDED')" if table != "MessageQueueItem" else "('COMPLETED')"
        elif '"isQueued"' in predicate:
            declaration, values = '"isQueued" boolean', "(false)"
        elif '"enabled"' in predicate:
            declaration, values = '"enabled" boolean,"deletedAt" timestamp', "(false,NULL)"
        elif '"deletedAt"' in predicate:
            declaration, values = '"deletedAt" timestamp', "('2026-01-01')"
        elif '"is_filled"' in predicate:
            declaration, values = '"is_filled" boolean', "(false)"
        elif '"is_satisfied"' in predicate:
            declaration, values = '"is_satisfied" boolean', "(true)"
        else:
            assert predicate == "TRUE"
            declaration, values = '"id" text', None
        sql = f'CREATE TABLE public."{table}" ({declaration});'
        if values:
            sql += f' INSERT INTO public."{table}" VALUES {values};'
            inserted += 1
        pg.psql(container, sql)
    # Per-step resource requirements persist as configuration; unlike runtime
    # slots they are not queued work and must survive the logical restore.
    pg.psql(container, """CREATE TABLE public.v1_step_slot_request (
        tenant_id uuid, step_id uuid, slot_type text, units integer, created_at timestamp, updated_at timestamp,
        PRIMARY KEY(tenant_id, step_id, slot_type));
        INSERT INTO public.v1_step_slot_request VALUES (
        '11111111-2222-4333-8444-555555555555','22222222-3333-4444-8555-666666666666',
        'synthetic',1,'2026-01-01','2026-01-01');""")
    inserted += 1
    assert set(migration.naming.postgres_quiescent(container).values()) == {0}
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "source-image", "postgres-image", "hatchet-image"):
        parser.add_argument("--" + name, required=True, help="existing exact local image ID")
    parser.add_argument("--naming", action="store_true", help="exercise the markerless naming transition")
    parser.add_argument("--source-bundle", type=Path, help="preserved public deployment bundle to copy without edits; --naming only")
    args = parser.parse_args()
    if args.source_bundle and not args.naming:
        parser.error("--source-bundle requires --naming")
    if args.source_bundle and not args.source_bundle.is_dir():
        parser.error("--source-bundle must name a preserved deployment directory")
    os.umask(0o077)
    for image in (args.image, args.source_image, args.postgres_image, args.hatchet_image):
        assert state._docker(["image", "inspect", "--format", "{{.Id}}", image]) == image
    tag = uuid.uuid4().hex[:10]
    project, target, retired = ("factory-migration-" + tag + suffix for suffix in ("-source", "-target", "-retired"))
    root = Path(tempfile.mkdtemp(prefix="factory-profile-check-"))
    source, destination, snapshot = (root / n for n in ("source", "target", "snapshot"))
    bundle = root / "old-bundle"
    original_bundle_hashes = naming_fixture.hashes(args.source_bundle) if args.source_bundle else None
    shutil.copytree(args.source_bundle or ROOT / "deploy", bundle, ignore=shutil.ignore_patterns("__pycache__"))
    if not args.source_bundle:
        shutil.copyfile(ROOT / "hatchet/bridge/factory_organization.py", bundle / "factory_organization.py")
        if args.naming:
            prior_naming_bundle(bundle)
    identity_label = "io.example.installation" if args.naming else state.IDENTITY_LABEL
    tenant_label = "io.example.tenant" if args.naming else fixtures.production.TENANT_LABEL
    source_id = str(uuid.uuid4())
    containers, volumes = [], []
    try:
        if args.source_bundle:
            pg.run([sys.executable, "-c", "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import bootstrap; bootstrap.init_directory(Path(sys.argv[2]),sys.argv[3],sys.argv[4],apply=True)",
                    str(bundle), str(source), args.source_image, project])
        else:
            bootstrap.init_directory(source, args.source_image, project, apply=True)
        values = bootstrap.read_env(source / ".env")
        values.update(FACTORY_POSTGRES_IMAGE=args.postgres_image, FACTORY_HATCHET_IMAGE=args.hatchet_image)
        marker = json.loads((source / bootstrap.MARKER).read_text())
        marker.update(profile=state.PRODUCTION, installation_id=source_id,
                      telemetry={"image": args.source_image, "port": 4318, "enabled": True,
                                 "deployment_sha256": {n: state._hash(bundle / n) for n in telemetry.FILES}})
        if args.naming:
            gate = source / "env/hatchet-worker-bridge-executor.env"
            execution = bootstrap.read_env(gate)
            if args.source_bundle:
                assert "naming_generation" not in marker, "preserved source must predate the naming generation"
                keys = [key for key in execution if key.endswith("HERMES_CREATE_DRAFT_PR_ENABLED")]
                assert len(keys) == 1, "preserved executor gate must be unambiguous"
                source_gate = keys[0]
                source_prefix = source_gate.removesuffix("HERMES_CREATE_DRAFT_PR_ENABLED")
                discovered = {key for file in (source / "env").glob("*.env") for key in bootstrap.read_env(file)
                              if key.startswith(source_prefix)} | {key for key in values if key.startswith(source_prefix)}
                assert discovered == {source_gate}, "additional prior environment keys require an explicit reviewed fixture map"
            else:
                marker.pop("naming_generation")
                marker.pop("organization_config")
                execution.pop("FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED")
                source_prefix, source_gate = "SOURCE_", "SOURCE_HERMES_CREATE_DRAFT_PR_ENABLED"
            execution[source_gate] = "1"
            gate.write_text(bootstrap.env_text(execution))
        bootstrap._create_private(source / telemetry.ENV_FILE, bootstrap.env_text({"POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces", "POSTHOG_PROJECT_TOKEN": "phc_synthetic_migration_fixture"}))
        (source / ".env").write_text(bootstrap.env_text(values))
        (source / bootstrap.MARKER).write_text(json.dumps(marker))
        if args.source_bundle:
            info = migration.naming.source_info(bundle, source)
            identity_label, tenant_label = info["identity_label"], info["tenant_label"]
            assert info["services"] == list(fixtures.production.SERVICES)
            assert info["volumes"] == list(state.VOLUMES)
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
                    command.extend(["--label", identity_label + "=" + source_id, "--label", tenant_label + "=" + tenant])
                container = state._docker([*command, images.get(role, args.source_image)])
                containers.append(container)
                if owner == retired:
                    retired_ids.append(container)
        provenance = {"source_project": retired, "destination_project": project, "source_container_ids": retired_ids,
                      "source_images": images, "factory_image": args.source_image, "tenant_id": tenant, "activation_approved": False,
                      "profile_sha256": state._hash(bundle / "compose.production.yml"),
                      "deployment_sha256": {n: state._hash(bundle / n) for n in state.deployment_files(state.PRODUCTION)}}
        if args.naming:
            ancestor, ancestor_id = project + "-ancestor", str(uuid.uuid4())
            provenance["predecessors"] = [{"project": ancestor, "installation_id": ancestor_id}]
            containers.append(state._docker(["create", "--name", ancestor + "-bridge", "--label", LABEL + "=" + tag,
                "--network", "none", "--restart", "no", "--entrypoint", "true",
                "--label", "com.docker.compose.project=" + ancestor, "--label", "com.docker.compose.service=bridge",
                "--label", identity_label + "=" + ancestor_id, "--label", tenant_label + "=" + tenant, args.source_image]))
        bootstrap._create_private(source / state.PROVENANCE, json.dumps(provenance))
        for suffix in state.VOLUMES:
            volume = source_volume(project, source_id, suffix, identity_label) if args.naming else migration.create_volume(project, source_id, suffix)
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
        naming_pg_rows = seed_naming_postgres(seed) if args.naming else 0
        state._docker(["stop", "--timeout", "30", seed])
        state._docker(["rm", seed]); containers.remove(seed)
        for suffix, filename in (("bridge-data", "bridge.db"), ("ingress-data", "ingress.db"), ("lifecycle-data", "outbox.db")):
            payload = root / suffix
            payload.mkdir(mode=0o700)
            if args.naming:
                naming_fixture.seed(payload / filename, suffix)
            elif suffix == "bridge-data":
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
        if args.naming:
            # Keep source helpers in a subprocess so their ownership labels never
            # leak into target bootstrap/state modules in this process.
            pg.run([sys.executable, str(bundle / "factory-production.py"), "--directory", str(source), "backup", str(snapshot), "--apply"])
        else:
            source_install = fixtures.production.Production(source)
            state.backup(source_install, snapshot, apply=True)
        original_source, original_snapshot = naming_fixture.hashes(source), naming_fixture.hashes(snapshot)
        configuration = root / "reviewed-configuration"
        if (source / "config").is_dir():
            shutil.copytree(source / "config", configuration)
        else:
            configuration.mkdir(mode=0o700)
            for name, value in bootstrap.ORGANIZATION_DEFAULTS.items():
                bootstrap._create_private(configuration / name, json.dumps(value, indent=2) + "\n")
        for path in configuration.iterdir():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        command = [sys.executable, str(ROOT / "deploy/profile_migration.py"), "--source-bundle", str(bundle), "--source", str(source),
                   "--snapshot", str(snapshot), "--destination", str(destination), "--project", target, "--image", args.image,
                   "--configuration", str(configuration), "--hermes-container", project + "-hermes"]
        if args.naming:
            mapping = naming_fixture.configuration()
            mapping.update(source_identity_label=identity_label, source_tenant_label=tenant_label,
                           source_environment_prefix=source_prefix, environment_values={},
                           environment_keys={source_gate: "FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED"})
            mapping_path = root / "naming-map.json"
            bootstrap._create_private(mapping_path, json.dumps(mapping))
            journal = root / "gateway-journal.tar"
            with tarfile.open(journal, "x") as handle:
                body = json.dumps({"attempt_id": naming_fixture.ATTEMPT, "source_binding": "historical-only"}).encode()
                entry = tarfile.TarInfo("./synthetic-attempt/intent.json")
                entry.mode, entry.size = 0o600, len(body)
                handle.addfile(entry, io.BytesIO(body))
            command.extend(["--naming-map", str(mapping_path), "--gateway-journal-archive", str(journal)])
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
        assert candidate.provenance["profile_migration"]["postgres_verification"]["rows"] == 21 + naming_pg_rows
        if args.naming:
            transition = candidate.provenance["profile_migration"]["naming_migration"]
            assert transition["gateway_policy"] == "unbound"
            assert {"project": ancestor, "installation_id": ancestor_id} in transition["source_predecessors"]
            assert transition["journal_archive"]["intents_without_results"] == 1
            assert (destination / "migration-evidence/gateway-journal.tar").read_bytes() == journal.read_bytes()
            target_gate = bootstrap.read_env(destination / "env/hatchet-worker-bridge-executor.env")
            assert target_gate["FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED"] == "0"
            assert source_gate not in target_gate
            verification = candidate.provenance["profile_migration"]["postgres_verification"]
            assert set(verification["naming_quiescence"]) == {table for table, _ in migration.naming.POSTGRES_PENDING}
            assert set(verification["naming_quiescence"].values()) == {0}
            for volume, filename in migration.naming.SQLITE.items():
                before, old_schema = naming_fixture.archive_rows(snapshot / "volumes" / (volume + ".tar"), filename)
                after, new_schema = naming_fixture.archive_rows(destination / "migration-evidence/naming-volumes" / (volume + ".tar"), filename)
                assert {name: after[name] for name in before} == before
                assert all(row in new_schema for row in old_schema)
                assert after["factory_naming_generation"] == [(bootstrap.NAMING_GENERATION,)]
                if volume == "bridge-data":
                    expected_trace = str(uuid.uuid5(uuid.NAMESPACE_URL, mapping["agent_trace_seed"].replace("{attempt_id}", naming_fixture.ATTEMPT)))
                    assert after["factory_imported_agent_traces"] == [(naming_fixture.ATTEMPT, expected_trace)]
            refused = subprocess.run([sys.executable, str(ROOT / "deploy/factory-production.py"), "--directory", str(destination), "activate"], capture_output=True, text=True)
            assert refused.returncode != 0 and "gateway unbound" in refused.stderr
            assert not candidate.service_rows()
        second = root / "unified-backup"
        state.backup(candidate, second, apply=True)
        restored = root / "restored"
        restore_project = target + "-restore"
        state.restore(second, restored, restore_project, apply=True, production=True, port_offset=10000)
        restored_install = fixtures.production.Production(restored)
        restored_install.preflight()
        if args.naming:
            assert (restored_install.provenance["profile_migration"]["naming_migration"]["source_predecessors"]
                    == candidate.provenance["profile_migration"]["naming_migration"]["source_predecessors"])
        assert naming_fixture.hashes(source) == original_source
        assert naming_fixture.hashes(snapshot) == original_snapshot
        if args.source_bundle:
            assert naming_fixture.hashes(args.source_bundle) == original_bundle_hashes
        print(json.dumps({"naming_migration": args.naming, "preserved_source_bundle": bool(args.source_bundle),
                          "gateway_journal": "UNBOUND" if args.naming else "not_exercised",
                          "source_unchanged": True, "snapshot_unchanged": True,
                          "migration_cli": "passed", "stale_approval": "refused", "source_images_preserved": True,
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
                assert row.get("Labels", {}).get(identity_label if name == project else state.IDENTITY_LABEL)
                state._docker(["network", "rm", ids])
        # Evidence is synthetic, but retain it on failure to diagnose without rerunning live work.
        print("Synthetic rehearsal evidence: " + str(root))


if __name__ == "__main__":
    raise SystemExit(main())
