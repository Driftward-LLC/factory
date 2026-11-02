#!/usr/bin/env python3
"""Preview or prepare a stopped unified-image production installation from a cold backup."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
import sys
import uuid

import bootstrap
import migration_postgres as pg
import naming_migration as naming
import portable_state as state
import telemetry

HERE = Path(__file__).resolve().parent
HEX = r"[0-9a-f]{64}"
IMAGE = "sha256:" + HEX
require = pg.require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def production_module():
    spec = importlib.util.spec_from_file_location("migration_production", HERE / "factory-production.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_receipt(install):
    """Keep original retired-source pins distinct from the new runtime pins."""
    value = install.provenance.get("profile_migration")
    require(isinstance(value, dict) and value.get("format_version") == 1
            and value.get("layout") == state.UNIFIED, "unified layout requires a migration receipt")
    for key in ("preview_sha256", "snapshot_sha256", "hermes_archive_sha256", "previous_profile_sha256"):
        require(re.fullmatch(HEX, str(value.get(key, ""))), "invalid migration receipt digest")
    transition = value.get("naming_migration")
    if transition is not None:
        require(isinstance(transition, dict) and transition.get("source_generation") == "unmarked"
                and transition.get("target_generation") == bootstrap.NAMING_GENERATION
                and transition.get("gateway_policy") == "unbound"
                and re.fullmatch(HEX, str(transition.get("mapping_sha256", "")))
                and isinstance(transition.get("mapping"), dict), "invalid naming migration receipt")
        naming.validate_mapping(transition["mapping"], {
            "identity_label": transition["mapping"].get("source_identity_label"),
            "tenant_label": transition["mapping"].get("source_tenant_label")})
    previous = value.get("previous_images", {})
    require(set(previous) in ({"factory", "postgres", "hatchet-lite"}, {"factory", "postgres", "hatchet-lite", telemetry.SERVICE})
            and all(re.fullmatch(IMAGE, str(v)) for v in previous.values()), "invalid previous image pins")
    require(value.get("image") in install.release_images and previous["factory"] in install.release_images
            and {"project": value.get("previous_project"), "installation_id": value.get("previous_installation_id")}
                in install.provenance.get("predecessors", []), "migration predecessor is not fenced")
    upgrades = install.provenance.get("image_upgrades", [])
    require(any(row["image"] == value["image"] and row["previous_image"] == previous["factory"]
                and row["backup_manifest_sha256"] == value["snapshot_sha256"] for row in upgrades),
            "migration is not linked to image and backup history")
    require(isinstance(value.get("previous_deployment_sha256"), dict)
            and value["previous_deployment_sha256"]
            and all(re.fullmatch(HEX, str(v)) for v in value["previous_deployment_sha256"].values()),
            "migration must retain previous deployment hashes")


def bundle_hashes(directory):
    paths = sorted(p for p in directory.iterdir() if p.suffix in (".py", ".yml", ".yaml", ".sql"))
    require(paths and all(p.is_file() and not p.is_symlink() for p in paths), "unsafe deployment bundle")
    return {p.name: state._hash(p) for p in paths}


def source_check(args):
    # Load the source with its own helpers in another process. Mixing old and new
    # bootstrap/state modules would accidentally reinterpret the recorded layout.
    pg.run([sys.executable, str(args.source_bundle / "factory-production.py"),
            "--directory", str(args.source), "preview"])


def hermes_source(name):
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name), "invalid dedicated Hermes container name")
    rows = json.loads(state._docker(["container", "inspect", name]))
    require(len(rows) == 1, "dedicated Hermes container is ambiguous")
    row = rows[0]
    labels = row.get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    require(re.fullmatch(bootstrap.PROJECT_PATTERN, project) and labels.get("com.docker.compose.service") == "hermes",
            "source must be a dedicated Factory Hermes Compose service")
    mounts = [m for m in row.get("Mounts", []) if m.get("Destination") == "/opt/data"]
    require(len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["Name"].startswith(project + "-"),
            "Hermes data must be a dedicated Factory named volume")
    return {"container": row["Id"], "name": name, "project": project, "image": row["Image"], "volume": mounts[0]["Name"]}


def source_stopped(project, identity, services, identity_label=state.IDENTITY_LABEL):
    ids = state._docker(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project]).splitlines()
    rows = json.loads(state._docker(["container", "inspect", *ids])) if ids else []
    found = set()
    for row in rows:
        labels = row.get("Config", {}).get("Labels") or {}
        role = labels.get("com.docker.compose.service")
        require(labels.get(identity_label) == identity and role in services and role not in found
                and labels.get("com.docker.compose.oneoff", "false").lower() == "false"
                and row["State"]["Status"] in ("created", "exited")
                and row["HostConfig"]["RestartPolicy"]["Name"] == "no",
                "source must be stopped, owned and restart-disabled")
        found.add(role)
    require(found == set(services), "source service inventory is incomplete")


def hermes_stopped(source):
    row = json.loads(state._docker(["container", "inspect", source["container"]]))[0]
    require(hermes_source(source["name"]) == source and row["State"]["Status"] in ("created", "exited")
            and row["HostConfig"]["RestartPolicy"]["Name"] == "no", "dedicated Hermes must be stopped and restart-disabled")
    state._no_writers([source["volume"]])


def fresh_destination(args):
    require(not args.destination.exists() and args.destination.parent.is_dir(), "destination must be a new directory")
    require(not state._docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + args.project]), "destination has containers")
    require(args.project not in state._docker(["network", "ls", "--format", "{{.Name}}"]).splitlines(), "destination has a network")
    require(not any(name.startswith(args.project + "-") for name in state._available_volumes()), "destination has volumes")


def check_sqlite(snapshot):
    result = {}
    for volume, filename in (("bridge-data", "bridge.db"), ("ingress-data", "ingress.db"), ("lifecycle-data", "outbox.db")):
        with tempfile.TemporaryDirectory(prefix="factory-migration-sqlite-") as temporary:
            with tarfile.open(snapshot / "volumes" / (volume + ".tar")) as archive:
                for member in archive:
                    name = member.name.removeprefix("./")
                    if name in (filename, filename + "-wal", filename + "-shm"):
                        require(member.isfile(), "SQLite archive entry is not a file")
                        with (Path(temporary) / name).open("xb") as output:
                            output.write(archive.extractfile(member).read())
            path = Path(temporary) / filename
            require(path.is_file(), "SQLite database missing from cold backup")
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                require(conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)] and not conn.execute("PRAGMA foreign_key_check").fetchall(), "SQLite integrity check failed")
                if volume == "bridge-data":
                    terminal = ("completed", "failed", "verified_ready", "canceled", "canceled_after_effect", "superseded", "legacy_completed")
                    counts = [conn.execute("SELECT count(*) FROM factory_attempts WHERE status NOT IN (" + ",".join("?" for _ in terminal) + ")", terminal).fetchone()[0],
                              conn.execute("SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL").fetchone()[0],
                              conn.execute("SELECT count(*) FROM bridge_jobs WHERE state NOT IN ('completed','ready_for_human_review','failed')").fetchone()[0]]
                    require(not any(counts), "cold backup contains active or unresolved execution")
                result[volume] = "integrity and foreign keys passed"
    return result


def archive_contents(path, *, hermes=False):
    state._archive_check(path, hermes=hermes)
    with tarfile.open(path) as archive:
        return sorted((member.name, member.type.decode(), member.mode, member.uid, member.gid, member.size, member.linkname,
                       hashlib.sha256(archive.extractfile(member).read()).hexdigest() if member.isfile() else "")
                      for member in archive)


def preview(args):
    for name in ("source_bundle", "source", "snapshot", "destination"):
        setattr(args, name, state._path(getattr(args, name)))
    require(args.source_bundle != HERE, "use the preserved source bundle and the new target bundle")
    require(args.destination not in (args.source, args.snapshot) and args.source not in args.destination.parents
            and args.snapshot not in args.destination.parents, "destination must be separate from source and backup")
    require(re.fullmatch(IMAGE, args.image), "target requires an exact local Factory image ID")
    source_check(args)
    raw = json.loads(bootstrap._read_private(args.source / bootstrap.MARKER))
    naming_plan = None
    if raw.get("naming_generation") is None:
        require(getattr(args, "naming_map", None) is not None, "markerless source requires a reviewed --naming-map")
        info = naming.source_info(args.source_bundle, args.source)
        metadata, values = info["metadata"], info["values"]
        config = naming.mapping(args.naming_map, info)
        require(info["volumes"] == list(state.VOLUMES), "source volume inventory is unsupported")
        require(getattr(args, "gateway_journal_archive", None) is not None,
                "markerless source requires a private cold --gateway-journal-archive for immutable evidence")
        journal = naming.journal_evidence(args.gateway_journal_archive)
        naming_plan = {"journal_archive": journal, "mapping": config, "mapping_sha256": state._hash(args.naming_map),
                       "source_services": info["services"], "source_files": info["files"],
                       "gateway_policy": "unbound", "source_generation": "unmarked"}
    else:
        require(getattr(args, "naming_map", None) is None, "naming mapping is only supported for markerless sources")
        metadata, values = bootstrap.validate_directory(args.source)
    require(metadata.get("profile") == state.PRODUCTION and metadata.get("layout") is None,
            "only a legacy production installation can use this transition")
    require(args.project != metadata["project"], "migration requires a new project")
    bootstrap.init_directory(args.destination, args.image, args.project)  # Validation only.
    fresh_destination(args)
    require(args.snapshot.is_dir() and not args.snapshot.stat().st_mode & 0o077, "backup must be private")
    manifest = json.loads(bootstrap._read_private(args.snapshot / "manifest.json"))
    require(manifest.get("format_version") == 1 and manifest.get("profile") == state.PRODUCTION
            and manifest.get("project") == metadata["project"] and manifest.get("installation_id") == metadata["installation_id"]
            and manifest.get("factory_image") == values["FACTORY_IMAGE"]
            and set(manifest.get("volumes", [])) == set(state.VOLUMES)
            and len(manifest["volumes"]) == len(state.VOLUMES), "backup identity or volume inventory differs")
    files = set(naming_plan["source_files"]) if naming_plan else state.config_files(state.PRODUCTION, metadata)
    if metadata.get("telemetry"):
        files |= {telemetry.ENV_FILE}
    expected = {"config/" + n for n in files} | {"volumes/" + n + ".tar" for n in state.VOLUMES}
    require(set(manifest.get("sha256", {})) == expected and state._private_files(args.snapshot) == expected | {"manifest.json"}, "backup file inventory differs")
    for name, expected_hash in manifest["sha256"].items():
        require(state._hash(args.snapshot / name) == expected_hash, "backup checksum differs")
    for name in files:
        require(bootstrap._read_private(args.snapshot / "config" / name) == bootstrap._read_private(args.source / name), "configuration changed since backup")
    for suffix in state.VOLUMES:
        state._archive_check(args.snapshot / "volumes" / (suffix + ".tar"))
    sqlite_checks = ({volume: naming.sqlite_archive(args.snapshot / "volumes" / (volume + ".tar"), volume)
                      for volume in naming.SQLITE} if naming_plan else check_sqlite(args.snapshot))
    previous = json.loads(bootstrap._read_private(args.source / state.PROVENANCE))
    require(not previous.get("profile_migration"), "source already has a profile migration")
    source_files = [*state.DEPLOYMENT_FILES, "compose.production.yml"]
    if metadata.get("telemetry"):
        source_files.extend(telemetry.FILES)
    require(manifest.get("deployment_sha256") == {n: state._hash(args.source_bundle / n) for n in source_files}, "backup requires its original deployment bundle")
    images = {"factory": values["FACTORY_IMAGE"], "postgres": values["FACTORY_POSTGRES_IMAGE"], "hatchet-lite": values["FACTORY_HATCHET_IMAGE"]}
    if metadata.get("telemetry"):
        images[telemetry.SERVICE] = metadata["telemetry"]["image"]
    require(manifest.get("images") == {v: v for v in images.values()}, "backup image inventory differs")
    require(args.image != images["factory"], "migration requires the new unified image")
    image = json.loads(state._docker(["image", "inspect", args.image]))[0]
    labels = image.get("Config", {}).get("Labels") or {}
    require(image.get("Id") == args.image and image.get("Architecture") == "amd64" and image.get("Os") == "linux"
            and labels.get("org.opencontainers.image.source") == "https://github.com/Driftward-LLC/factory"
            and re.fullmatch(r"[a-f0-9]{40}", labels.get("org.opencontainers.image.revision", "")), "target image provenance differs")
    # This migration changes packaging, not Hatchet's schema/runtime version.
    binaries = ["/hatchet-lite", "/hatchet-admin", "/hatchet-migrate"]
    for binary in binaries:
        hashes = [state._docker(["run", "--rm", "--network", "none", "--read-only", "--entrypoint", "sha256sum", img, binary])
                  for img in (images["hatchet-lite"], args.image)]
        require(hashes[0] == hashes[1], "Hatchet binaries changed; a separate schema migration is required")
    configured = getattr(args, "configuration", None)
    if metadata.get("organization_config") != 1:
        require(configured is not None, "pre-configuration backups require --configuration with reviewed organization and policy files")
    supplied = bootstrap.configuration(configured) if configured is not None else bootstrap.configuration(args.source / "config")
    if naming_plan:
        naming.disabled_execution_configuration(supplied)
    hermes = hermes_source(args.hermes_container)
    require(hermes["volume"] not in {metadata["project"] + "-" + suffix for suffix in state.PROJECT_VOLUMES}, "Hermes data overlaps control-plane state")
    plan = {"format_version": 1, "layout": state.UNIFIED, "source": str(args.source), "source_bundle": str(args.source_bundle),
            "configuration_sha256": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in supplied.items()},
            "source_bundle_sha256": bundle_hashes(args.source_bundle), "target_bundle_sha256": bundle_hashes(HERE),
            "previous_project": metadata["project"], "previous_installation_id": metadata["installation_id"],
            "previous_images": images, "destination": str(args.destination), "project": args.project,
            "snapshot": str(args.snapshot), "snapshot_sha256": state._hash(args.snapshot / "manifest.json"),
            "image": args.image, "commit": labels["org.opencontainers.image.revision"], "hermes": hermes,
            "sqlite_checks": sqlite_checks, "ports_preserved": {k: values[k] for k in bootstrap.PORTS}, "execution": "stopped", "starts_services": False}
    if naming_plan:
        naming_plan["postgres_checks"] = pg.naming_preview(args.snapshot, images["postgres"], args.image, args.project)
        plan["naming_migration"] = naming_plan
    return plan, metadata, values, previous, files


def create_volume(project, identity, suffix):
    name = project + "-" + suffix
    require(name not in state._available_volumes(), "destination volume appeared concurrently")
    state._docker(["volume", "create", "--label", "com.docker.compose.project=" + project,
                   "--label", "com.docker.compose.volume=factory-" + suffix,
                   "--label", state.IDENTITY_LABEL + "=" + identity, name])
    row = json.loads(state._docker(["volume", "inspect", name]))[0]
    require(row.get("Labels", {}).get(state.IDENTITY_LABEL) == identity, "destination volume has foreign ownership")
    state._no_writers([name])
    return name


def apply(args, plan, metadata, values, previous, files):
    require(args.approve_digest == digest(plan), "migration preview digest is stale or missing")
    production = production_module()
    services = (*production.SERVICES, *((telemetry.SERVICE,) if metadata.get("telemetry") else ()))
    if plan.get("naming_migration"):
        require(plan["naming_migration"]["source_services"] == list(production.SERVICES), "source services changed across naming generations")
    naming_plan = plan.get("naming_migration")
    source_label = naming_plan["mapping"]["source_identity_label"] if naming_plan else state.IDENTITY_LABEL
    source_stopped(metadata["project"], metadata["installation_id"], services, source_label)
    hermes_stopped(plan["hermes"])
    state._no_writers([metadata["project"] + "-" + suffix for suffix in state.PROJECT_VOLUMES])
    identity = str(uuid.uuid4())
    args.destination.mkdir(mode=0o700)
    work = args.destination / "migration-evidence"
    work.mkdir(mode=0o700)
    bootstrap._create_private(work / "plan.json", json.dumps(plan, sort_keys=True, indent=2) + "\n")
    if naming_plan:
        journal = naming_plan["journal_archive"]
        require(naming.journal_evidence(Path(journal["path"])) == journal, "gateway journal archive changed after preview")
        # Keep unknown and partial receipts as evidence. Never mount this archive
        # into the new executor or mark it as the new naming generation.
        body = Path(journal["path"]).read_bytes()
        require(hashlib.sha256(body).hexdigest() == journal["sha256"], "gateway journal archive changed while reading")
        fd = os.open(work / "gateway-journal.tar", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    # A snapshot taken before a restart is stale even if its config is unchanged.
    # Compare the actual stopped volume bytes before creating destination state.
    for suffix in state.VOLUMES:
        name = suffix + ".tar"
        state._tar(args.image, metadata["project"] + "-" + suffix, work, name, restore=False)
        require(state._hash(work / name) == state._hash(args.snapshot / "volumes" / name), "source data changed since cold backup")
        (work / name).unlink()
    state._tar(args.image, plan["hermes"]["volume"], work, "hermes-data.tar", restore=False)
    state._archive_check(work / "hermes-data.tar", hermes=True)
    if naming_plan:
        require(state._hash(args.naming_map) == naming_plan["mapping_sha256"], "naming mapping changed after preview")
        transformed = work / "naming-volumes"
        transformed.mkdir(mode=0o700)
        for volume in naming.SQLITE:
            naming.sqlite_archive(args.snapshot / "volumes" / (volume + ".tar"), volume,
                                  output=transformed / (volume + ".tar"), config=naming_plan["mapping"])
    for suffix in state.DURABLE_VOLUMES:
        name = create_volume(args.project, identity, suffix)
        if suffix != "hatchet-postgres-data":
            source = (transformed if naming_plan and suffix in naming.SQLITE else
                      work if suffix == "hermes-data" else args.snapshot / "volumes")
            state._tar(args.image, name, source, suffix + ".tar", restore=True)
            check = "verify-" + suffix + ".tar"
            state._tar(args.image, name, work, check, restore=False)
            require(archive_contents(work / check, hermes=suffix == "hermes-data") == archive_contents(source / (suffix + ".tar"), hermes=suffix == "hermes-data"), "copied volume content or ownership differs")
            (work / check).unlink()
    result = pg.migrate(args.snapshot, args.project + "-hatchet-postgres-data", plan["previous_images"]["postgres"],
                        args.image, args.project, identity, work, **({"naming_transition": True} if naming_plan else {}))
    if naming_plan:
        require(result["naming_quiescence"] == naming_plan["postgres_checks"], "Hatchet quiescence changed after preview")
    # Recheck source fencing and configuration after the potentially long copy.
    source_check(args)
    source_stopped(metadata["project"], metadata["installation_id"], services, source_label)
    hermes_stopped(plan["hermes"])
    for name in files:
        require(bootstrap._read_private(args.snapshot / "config" / name) == bootstrap._read_private(args.source / name), "source configuration changed during migration")
    for folder in ("env", "config", "keys", "keys/hatchet", "run", "run/operator"):
        (args.destination / folder).mkdir(mode=0o700)
    supplied = bootstrap.configuration(getattr(args, "configuration", None) or args.source / "config")
    require({name: hashlib.sha256(content.encode()).hexdigest() for name, content in supplied.items()} == plan["configuration_sha256"],
            "supplied organization configuration changed during migration")
    for name, content in supplied.items():
        bootstrap._create_private(args.destination / "config" / name, content)
    for name in files - {".env", bootstrap.MARKER, state.PROVENANCE, *("config/" + name for name in bootstrap.ORGANIZATION_DEFAULTS)}:
        content = bootstrap._read_private(args.snapshot / "config" / name)
        if naming_plan and name.startswith("env/"):
            content = bootstrap.env_text(naming.environment(bootstrap.read_env(args.snapshot / "config" / name), naming_plan["mapping"]))
        bootstrap._create_private(args.destination / name, content)
    updated = {**(naming.environment(values, naming_plan["mapping"]) if naming_plan else values), "FACTORY_PROJECT_NAME": args.project, "COMPOSE_PROJECT_NAME": args.project,
               "FACTORY_INSTALL_DIR": str(args.destination), "FACTORY_ENV_DIR": str(args.destination / "env"),
               "FACTORY_CONFIG_DIR": str(args.destination / "config"),
               "FACTORY_OPERATOR_RUN_DIR": str(args.destination / "run/operator"),
               "FACTORY_IMAGE": args.image, "FACTORY_POSTGRES_IMAGE": args.image, "FACTORY_HATCHET_IMAGE": args.image}
    marker = {**metadata, "project": args.project, "installation_id": identity, "image": args.image, "layout": state.UNIFIED, "organization_config": 1,
              "naming_generation": bootstrap.NAMING_GENERATION}
    if marker.get("telemetry"):
        marker["telemetry"] = {**marker["telemetry"], "image": args.image,
                               "deployment_sha256": {n: state._hash(HERE / n) for n in telemetry.FILES}}
    receipt = {"format_version": 1, "layout": state.UNIFIED, "preview_sha256": digest(plan),
               "configuration_sha256": plan["configuration_sha256"],
               "previous_project": metadata["project"], "previous_installation_id": metadata["installation_id"],
               "previous_images": plan["previous_images"], "previous_profile_sha256": previous["profile_sha256"],
               "previous_deployment_sha256": previous["deployment_sha256"], "snapshot_sha256": plan["snapshot_sha256"],
               "image": args.image, "hermes_source": plan["hermes"], "hermes_archive_sha256": state._hash(work / "hermes-data.tar"),
               "postgres_verification": result}
    if naming_plan:
        receipt["naming_migration"] = {**naming_plan, "sqlite_checks": plan["sqlite_checks"],
                                      "target_generation": bootstrap.NAMING_GENERATION}
    provenance = {**previous, "destination_project": args.project, "activation_approved": False,
                  "profile_sha256": state._hash(HERE / "compose.production.yml"),
                  "deployment_sha256": {n: state._hash(HERE / n) for n in state.deployment_files(state.PRODUCTION, {"layout": state.UNIFIED})},
                  "profile_migration": receipt,
                  "predecessors": [*previous.get("predecessors", []), {"project": metadata["project"], "installation_id": metadata["installation_id"]}],
                  "image_upgrades": [*previous.get("image_upgrades", []), {"previous_image": values["FACTORY_IMAGE"], "image": args.image,
                     "commit": plan["commit"], "backup": str(args.snapshot), "backup_manifest_sha256": plan["snapshot_sha256"],
                     "recorded_at": datetime.now(timezone.utc).isoformat()}]}
    bootstrap._create_private(args.destination / ".env", bootstrap.env_text(updated))
    bootstrap._create_private(args.destination / state.PROVENANCE, json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    # The normal production constructor cannot accept an incomplete destination.
    bootstrap._create_private(args.destination / bootstrap.MARKER, json.dumps(marker, sort_keys=True, indent=2) + "\n")
    install = production.Production(args.destination)
    install.preflight()
    bootstrap._create_private(work / "complete.json", json.dumps({"preview_sha256": digest(plan), "installation_id": identity,
                               "execution": "stopped", "services_started": 0}, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-bundle", "source", "snapshot", "destination"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--image", required=True, help="verified exact local unified Factory image ID")
    parser.add_argument("--hermes-container", required=True, help="operator-reviewed dedicated Factory Hermes container; never a shared or personal runtime")
    parser.add_argument("--configuration", type=Path, help="reviewed four-file configuration; required for pre-configuration backups")
    parser.add_argument("--gateway-journal-archive", type=Path, help="private cold host gateway journal tar; naming migration evidence only")
    parser.add_argument("--naming-map", type=Path, help="private reviewed exact legacy naming map; markerless source only")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-digest", help="exact reviewed preview digest; apply only")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with ExitStack() as stack:
            production = production_module()
            if args.apply:
                stack.enter_context(production.portable.installation_lock(args.source))
                if getattr(args, "naming_map", None):
                    info = naming.source_info(args.source_bundle, args.source)
                    metadata, values = info["metadata"], info["values"]
                else:
                    metadata, values = bootstrap.validate_directory(args.source)
                tenant = str(uuid.UUID(values["HATCHET_DEFAULT_TENANT_ID"]))
                stack.enter_context(production.portable.installation_lock(Path("/tmp") / ("factory-tenant-" + tenant)))
                stack.enter_context(production.portable.installation_lock(args.destination))
            plan, metadata, values, previous, files = preview(args)
            if args.apply:
                apply(args, plan, metadata, values, previous, files)
                print(json.dumps({"status": "prepared_stopped", "project": args.project, "preview_sha256": digest(plan)}))
            else:
                print(json.dumps({"preview_sha256": digest(plan), "plan": plan}, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, tarfile.TarError) as exc:
        reason = str(exc) if isinstance(exc, pg.MigrationRefusal) else type(exc).__name__
        print("Profile migration refused: " + reason + "; retained private evidence and original state. No services were activated.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
