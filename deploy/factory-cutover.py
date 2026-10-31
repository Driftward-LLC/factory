#!/usr/bin/env python3
"""Prepare a stopped same-host Factory replacement; never stop/start or route traffic."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tarfile
import tempfile
import uuid

import bootstrap
import portable_state as state

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("factory_portable", HERE / "factory-portable.py")
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)
SERVICES = (*portable.CONTROL_PLANE_SERVICES, "linear-webhook-proxy", "factory-lifecycle-writer", "factory-linear-token-refresh")
KEYS = ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key")  # gitleaks:allow -- filenames, not key material
ENV_CONSUMERS = {
    "hatchet-workers.env": ("event-ingress", "intake-worker", "execution-dispatch-worker", "bridge-executor-worker", "pr-event-worker", "reconciliation-worker"),
    "hatchet-bridge.env": ("bridge", "factory-lifecycle-writer"),
    "hatchet-verifier.env": ("bridge", "bridge-executor-worker"),
    "hatchet-event-ingress.env": ("event-ingress", "linear-webhook-proxy"),
    "hatchet-worker-bridge-executor.env": ("bridge-executor-worker",),
    "hatchet-worker-reconciliation.env": ("reconciliation-worker",),
    "factory-operator.env": ("factory-operator", "outbox-dispatcher"),
    "factory-lifecycle-writer.env": ("factory-lifecycle-writer",),
    "factory-linear-token.env": ("factory-linear-token-refresh",),
}
VOLUME_CONSUMERS = {
    "hatchet-postgres-data": ("postgres", "/var/lib/postgresql/data"),
    "hatchet-config-data": ("hatchet-lite", "/config"),
    "bridge-data": ("bridge", "/var/lib/driftward-hatchet-bridge"),
    "ingress-data": ("event-ingress", "/var/lib/driftward-hatchet-event-ingress"),
    "lifecycle-data": ("factory-lifecycle-writer", "/var/lib/driftward-factory-lifecycle-writer"),
    "linear-token-data": ("factory-linear-token-refresh", "/var/lib/driftward-factory-linear-token"),
    "linear-published-token": ("factory-linear-token-refresh", "/var/run/factory-linear-token"),
}


class CutoverRefusal(ValueError):
    """An operator-facing message containing no private runtime values."""


def compose_overrides() -> dict:
    # Let Compose interpret its own environment precedence. No env files are
    # opened and no daemon/container is contacted by this configuration render.
    env = {"PATH": os.environ.get("PATH", os.defpath),
           "HATCHET_POSTGRES_PASSWORD": "render-only",
           "HATCHET_SERVER_AUTH_COOKIE_SECRETS": "render-only",
           "HATCHET_ADMIN_PASSWORD": "render-only"}
    raw = portable.run(["docker", "compose", "--project-directory", str(HERE),
                        "--env-file", "/dev/null", "-f", str(HERE / "docker-compose.yml"),
                        "config", "--no-env-resolution", "--format", "json"], env=env, private=True)
    return {name: row.get("environment", {}) for name, row in json.loads(raw)["services"].items()}


def legacy_env(path: Path) -> dict[str, str]:
    """A deliberately strict literal subset of dotenv, never a shell evaluator."""
    values = {}
    for line in bootstrap._read_private(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise CutoverRefusal(f"unsupported or duplicate configuration entry in {path.name}")
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote) or quote in value[1:-1]:
                raise CutoverRefusal(f"unsupported quoting in {path.name}")
            value = value[1:-1]
            if quote == '"' and "$" in value:
                raise CutoverRefusal(f"interpolation requires operator normalization in {path.name}")
        elif any(char in value for char in "#$'\""):
            raise CutoverRefusal(f"ambiguous literal in {path.name}")
        bootstrap.env_text({key: value})  # Also rejects escapes/control characters.
        values[key] = value
    return values


def environment(row: dict) -> dict[str, str]:
    entries = row["Config"]["Env"]
    result = dict(item.split("=", 1) for item in entries)
    if len(result) != len(entries):
        raise CutoverRefusal("source container has duplicate environment keys")
    return result


def inspect_source(project: str, env_dir: Path, keys: Path, *, allow_unlabelled: bool = False) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project):
        raise CutoverRefusal("invalid source project")
    ids = state._docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).splitlines()
    if not ids:
        raise CutoverRefusal("source project has no containers")
    rows = json.loads(state._docker(["container", "inspect", *ids]))
    # Docker's container enumeration and Mounts arrays can reorder between
    # inspections. Canonicalize only unordered inventories; command/env ordering
    # and every actual configuration value remain part of the drift check.
    rows.sort(key=lambda row: row["Id"])
    for row in rows:
        row["Mounts"].sort(key=lambda mount: mount["Destination"])
    services = {}
    for row in rows:
        labels = row["Config"].get("Labels") or {}
        name = labels.get("com.docker.compose.service")
        if labels.get("com.docker.compose.project") != project or name not in SERVICES or name in services:
            raise CutoverRefusal("source must have exactly the expected services, without one-off containers")
        services[name] = row
    if set(services) != set(SERVICES):
        raise CutoverRefusal("source production service set is incomplete")
    factory_ids = {row["Image"] for name, row in services.items() if name not in ("postgres", "hatchet-lite")}
    if len(factory_ids) != 1:
        raise CutoverRefusal("source Factory containers use different images")
    image = next(iter(factory_ids))
    images = {row["Image"] for row in rows}
    if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in images):
        raise CutoverRefusal("source images must have immutable local IDs")
    # The production overlay requires these saved IDs for infrastructure too.
    # The preparation is a relocation, never a simultaneous upgrade.
    for value in images:
        if state._docker(["image", "inspect", "--format", "{{.Id}}", value]) != value:
            raise CutoverRefusal("source image is unavailable")
    volumes = [f"{project}-{suffix}" for suffix in state.VOLUMES]
    volume_rows = json.loads(state._docker(["volume", "inspect", *volumes]))
    unlabelled = []
    for suffix, row in zip(state.VOLUMES, volume_rows, strict=True):
        labels = row.get("Labels") or {}
        if not labels:
            if not allow_unlabelled:
                raise CutoverRefusal("legacy source volumes lack labels; review their canonical mounts before using --allow-unlabelled-source-volumes")
            unlabelled.append(row["Name"])
        elif (labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.volume") != "factory-" + suffix):
            raise CutoverRefusal("source volume ownership mismatch")
        if row["Name"] != f"{project}-{suffix}":
            raise CutoverRefusal("source volume name mismatch")
        service, target = VOLUME_CONSUMERS[suffix]
        mounts = [m for m in services[service]["Mounts"] if m["Destination"] == target]
        if len(mounts) != 1 or mounts[0]["Type"] != "volume" or mounts[0]["Name"] != row["Name"]:
            raise CutoverRefusal("source service uses an unexpected state mount")
    key_mounts = [m for m in services["hatchet-lite"]["Mounts"] if m["Destination"] == "/keys"]
    if len(key_mounts) != 1 or key_mounts[0]["Type"] != "bind" or key_mounts[0]["Source"] != str(keys) or key_mounts[0]["RW"]:
        raise CutoverRefusal("source Hatchet key mount does not match the selected read-only directory")
    key_text = {name: bootstrap._read_private(keys / name) for name in KEYS}
    if not all(key_text.values()):
        raise CutoverRefusal("source Hatchet keys are incomplete; never regenerate during cutover")
    envs = {name: legacy_env(env_dir / name) for name in bootstrap.ENV_FILES}
    # Compare private source files with the actual container configuration. Any
    # stale file or Compose/shell interpolation must be resolved by the operator.
    overrides = compose_overrides()
    for filename, consumers in ENV_CONSUMERS.items():
        normalized = {}
        for consumer in consumers:
            actual = environment(services[consumer])
            effective = {key: str(overrides[consumer].get(key, value)) for key, value in envs[filename].items()}
            if any(actual.get(key) != value for key, value in effective.items()):
                raise CutoverRefusal(f"file/runtime configuration drift: {filename} -> {consumer}")
            if normalized and normalized != effective:
                raise CutoverRefusal(f"shared configuration has conflicting service overrides: {filename}")
            normalized = effective
        envs[filename] = normalized
    token = environment(services["intake-worker"]).get("HATCHET_CLIENT_TOKEN", "")
    bootstrap.extract_client_token(token)
    try:
        payload = token.split(".")[1]
        tenant = str(uuid.UUID(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise CutoverRefusal("source Hatchet token lacks a tenant UUID") from exc
    # Hatchet's installed SDK uses JWT sub as tenant ID; no token is minted here.
    clients = (*ENV_CONSUMERS["hatchet-workers.env"], *ENV_CONSUMERS["factory-operator.env"])
    if any(environment(services[name]).get("HATCHET_CLIENT_TOKEN") != token for name in clients):
        raise CutoverRefusal("source Hatchet clients disagree; reconcile credentials before copying")
    optional_client = env_dir / "hatchet-client.env"
    if optional_client.exists() and legacy_env(optional_client) != {"HATCHET_CLIENT_TOKEN": token}:
        raise CutoverRefusal("source client credential file differs from running clients")
    envs["hatchet-client.env"] = {"HATCHET_CLIENT_TOKEN": token}
    executor = environment(services["bridge-executor-worker"])
    if (envs["hatchet-workers.env"].get("HATCHET_INTAKE_DRY_RUN") != "false"
            or executor.get("DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED") != "1"
            or executor.get("DRIFTWARD_HERMES_GRPC_TARGET") != "unix:///run/driftward-hermes-factory/hermes.sock"):
        raise CutoverRefusal("source must use the reviewed same-host real-execution boundary")
    for name in clients:
        if any(key.startswith(("OPENAI_", "ANTHROPIC_", "GITHUB_", "GH_", "LINEAR_", "FACTORY_LINEAR_", "BROWSERBASE_", "MCP_")) for key in environment(services[name])):
            raise CutoverRefusal("source workers/operator contain credentials outside their boundary")
    postgres, hatchet = environment(services["postgres"]), environment(services["hatchet-lite"])
    if hatchet.get("DEFAULT_TENANT_ID", tenant) != tenant:
        raise CutoverRefusal("source tenant differs from its worker credential")
    values = {"FACTORY_IMAGE": image, "HATCHET_DEFAULT_TENANT_ID": tenant,
              "FACTORY_POSTGRES_IMAGE": services["postgres"]["Image"],
              "FACTORY_HATCHET_IMAGE": services["hatchet-lite"]["Image"]}
    for suffix in ("USER", "DB", "PASSWORD"):
        values["HATCHET_POSTGRES_" + suffix] = postgres["POSTGRES_" + suffix]
    for suffix in ("SERVER_AUTH_COOKIE_DOMAIN", "SERVER_AUTH_COOKIE_SECRETS", "SERVER_URL", "ADMIN_EMAIL", "ADMIN_PASSWORD", "ADMIN_NAME", "DEFAULT_TENANT_NAME", "DEFAULT_TENANT_SLUG"):
        values["HATCHET_" + suffix] = hatchet[suffix]
    return {"project": project, "rows": rows, "services": services, "volumes": volumes,
            "image": image, "images": images, "envs": envs, "keys": key_text, "values": values,
            "unlabelled_volumes": unlabelled}


def assert_cold(source: dict) -> None:
    rows = json.loads(state._docker(["container", "inspect", *[row["Id"] for row in source["rows"]]]))
    if any(row["State"]["Status"] not in ("created", "exited") for row in rows):
        raise CutoverRefusal("source must already be stopped; this command never stops services")
    if state._docker(["ps", "-q", "--filter", "label=com.docker.compose.project=" + source["project"]]):
        raise CutoverRefusal("a source container started concurrently")
    state._no_writers(source["volumes"])


def ledger_summary(archive: Path) -> dict:
    """Read a private copy INCLUDING WAL, never initialize or mutate the source."""
    with tempfile.TemporaryDirectory(prefix="factory-cutover-ledger-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive) as stream:
            for member in stream:
                name = member.name.removeprefix("./")
                if name in ("bridge.db", "bridge.db-wal", "bridge.db-shm"):
                    if not member.isfile() or (root / name).exists():
                        raise CutoverRefusal("invalid or duplicate bridge database archive entry")
                    with (root / name).open("xb") as output:
                        output.write(stream.extractfile(member).read())
        if not (root / "bridge.db").is_file():
            raise CutoverRefusal("bridge database missing from source volume")
        try:
            with sqlite3.connect((root / "bridge.db").as_uri() + "?mode=ro", uri=True) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CutoverRefusal("source bridge database failed integrity check")
                jobs = dict(conn.execute("SELECT state, count(*) FROM bridge_jobs GROUP BY state"))
                attempts = dict(conn.execute("SELECT status, count(*) FROM factory_attempts GROUP BY status"))
                pending_effects = conn.execute("SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL AND effect_observed_at IS NULL").fetchone()[0]
                pending_verification = conn.execute("SELECT count(*) FROM verification_leases WHERE closed_at IS NULL").fetchone()[0]
                if (set(jobs) - {"completed", "failed", "ready_for_human_review"}
                        # legacy_completed is an imported historical receipt,
                        # not an active attempt (see bridge/legacy_artifacts.py).
                        or set(attempts) - {"completed", "legacy_completed", "failed", "verified_ready", "canceled", "canceled_after_effect", "superseded"}
                        or pending_effects or pending_verification):
                    raise CutoverRefusal("source has active/unknown execution or verification; reconcile before migration")
                return {"jobs": jobs, "attempts": attempts}
        except sqlite3.Error as exc:
            raise CutoverRefusal("source bridge schema is unsupported or unreadable") from exc


def target_unused(directory: Path, project: str) -> None:
    if directory.exists():
        raise CutoverRefusal("destination directory already exists; never overwrite a pilot or partial migration")
    if state._docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]):
        raise CutoverRefusal("destination project already has containers")
    if project in state._docker(["network", "ls", "--format", "{{.Name}}"]).splitlines():
        raise CutoverRefusal("destination network already exists")
    if {f"{project}-{suffix}" for suffix in state.PROJECT_VOLUMES} & state._available_volumes():
        raise CutoverRefusal("destination project already has volumes")


def prepare(source_project: str, env_dir: Path, keys: Path, directory: Path,
            project: str, snapshot: Path, *, port_offset: int = 0, apply: bool = False,
            allow_unlabelled: bool = False) -> dict:
    env_dir, keys, directory, snapshot = map(state._path, (env_dir, keys, directory, snapshot))
    if project == source_project:
        raise CutoverRefusal("source and destination projects must differ")
    if snapshot.exists() or not snapshot.parent.is_dir():
        raise CutoverRefusal("snapshot requires a new directory with an existing parent")
    for left, right in ((directory, snapshot), (directory, env_dir), (directory, keys), (snapshot, env_dir), (snapshot, keys)):
        if left == right or left in right.parents or right in left.parents:
            raise CutoverRefusal("source, snapshot, and destination paths must not overlap")
    source = inspect_source(source_project, env_dir, keys, allow_unlabelled=allow_unlabelled)

    def revalidate_source() -> None:
        current = inspect_source(source_project, env_dir, keys, allow_unlabelled=allow_unlabelled)
        if current != source:
            raise CutoverRefusal("source configuration, keys, or container identity changed during preparation")
        assert_cold(current)

    target_unused(directory, project)
    generated = bootstrap.init_directory(directory, source["image"], project, port_offset=port_offset)
    running = sorted(row["Config"]["Labels"]["com.docker.compose.service"] for row in source["rows"] if row["State"]["Status"] not in ("created", "exited"))
    report = {"source_project": source_project, "destination_project": project,
              "factory_image": source["image"], "durable_volumes": len(source["volumes"]),
              "unlabelled_source_volumes": source["unlabelled_volumes"],
              "running_source_services": running, "destination_will_start": False,
              "remaining_gates": ["cold source and drained/reconciled execution", "reviewed production activation and real canary", "single intake/execution authority"]}
    print(json.dumps(report, indent=2))
    if not apply:
        return report
    revalidate_source()
    snapshot.mkdir(mode=0o700)
    for relative in ("config", "config/env", "config/keys", "config/keys/hatchet", "volumes"):
        (snapshot / relative).mkdir(mode=0o700)
    # All source operational identity is retained; generated values supply only
    # destination paths/ports. No bootstrap-generated credential is copied.
    generated.update(source["values"], FACTORY_PROJECT_NAME=source_project, COMPOSE_PROJECT_NAME=source_project)
    bootstrap._create_private(snapshot / "config/.env", bootstrap.env_text(generated))
    source_id = str(uuid.uuid4())
    bootstrap._create_private(snapshot / "config" / bootstrap.MARKER, json.dumps({"format_version": 1, "installation_id": source_id, "project": source_project, "image": source["image"], "profile": "same-host-production"}))
    for name, values in source["envs"].items():
        bootstrap._create_private(snapshot / "config/env" / name, bootstrap.env_text(values))
    for name, content in source["keys"].items():
        bootstrap._create_private(snapshot / "config/keys/hatchet" / name, content)
    for suffix in state.VOLUMES:
        assert_cold(source)
        state._tar(source["image"], f"{source_project}-{suffix}", snapshot / "volumes", suffix + ".tar", restore=False)
        state._archive_check(snapshot / "volumes" / (suffix + ".tar"))
    revalidate_source()
    counts = ledger_summary(snapshot / "volumes/bridge-data.tar")
    provenance = {**report, "ledger_counts": counts, "source_container_ids": sorted(row["Id"] for row in source["rows"]),
                  "tenant_id": source["values"]["HATCHET_DEFAULT_TENANT_ID"],
                  "profile_sha256": state._hash(HERE / "compose.production.yml"),
                  "deployment_sha256": {name: state._hash(HERE / name) for name in state.deployment_files(state.PRODUCTION)},
                  "source_images": {name: row["Image"] for name, row in source["services"].items()},
                  "activation_approved": False}
    bootstrap._create_private(snapshot / "config" / state.PROVENANCE, json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    hashes = {name: state._hash(snapshot / name) for name in sorted(state._private_files(snapshot))}
    manifest = {"format_version": 1, "project": source_project, "installation_id": source_id,
                "factory_image": source["image"], "images": {image: image for image in source["images"]},
                "volumes": list(state.VOLUMES), "sha256": hashes, "profile": state.PRODUCTION,
                "deployment_sha256": {name: state._hash(HERE / name) for name in state.deployment_files(state.PRODUCTION)}}
    bootstrap._create_private(snapshot / "manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    target_unused(directory, project)
    revalidate_source()
    state.restore(snapshot, directory, project, apply=True, port_offset=port_offset, production=True)
    revalidate_source()
    provenance = json.loads(bootstrap._read_private(directory / state.PROVENANCE))
    print("Prepared and STOPPED. No routing, credential rotation, schema upgrade, or execution activation occurred.")
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--source-env-dir", required=True, type=Path)
    parser.add_argument("--source-keys", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--port-offset", type=int, default=0)
    parser.add_argument("--allow-unlabelled-source-volumes", action="store_true",
                        help="explicitly accept label-free legacy volumes only at verified canonical source mounts")
    parser.add_argument("--apply", action="store_true", help="copy only an already-stopped source; leave destination stopped")
    args = parser.parse_args()
    try:
        kwargs = dict(source_project=args.source_project, env_dir=args.source_env_dir, keys=args.source_keys,
                      directory=args.directory, project=args.project, snapshot=args.snapshot, port_offset=args.port_offset,
                      apply=args.apply, allow_unlabelled=args.allow_unlabelled_source_volumes)
        if args.apply:
            with portable.installation_lock(args.directory):
                prepare(**kwargs)
        else:
            prepare(**kwargs)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        # No raw configuration, subprocess output, or token parsing error escapes.
        reason = str(exc) if isinstance(exc, CutoverRefusal) else type(exc).__name__
        print(f"Cutover preparation refused: {reason}. Source unchanged; private/partial artifacts, if any, retained.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
