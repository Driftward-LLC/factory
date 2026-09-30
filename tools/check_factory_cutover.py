#!/usr/bin/env python3
"""Rehearse cold copying synthetic state; never start Factory or integrations.

Deletes only this run's disposable resources after verifying ownership. This
proves raw state preservation, not startup of a historical Hatchet database.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
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
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import portable_state as state

spec = importlib.util.spec_from_file_location("factory_cutover_check", ROOT / "deploy/factory-cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
sys.path.insert(0, str(ROOT / "hatchet/bridge"))
from hatchet_hermes_bridge import store

TEST_LABEL = "io.driftward.factory.cutover-test"


def run(*args: str, env: dict[str, str] | None = None, stage: str | None = None) -> str:
    label = stage or " ".join(args[:3])
    try:
        result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out; captured output withheld") from None
    if result.returncode:
        detail = "captured output withheld"
        if len(args) > 1 and args[1] == str(ROOT / "deploy/factory-cutover.py"):
            # Only this CLI's deliberately sanitized refusal line is public.
            # Never expose arbitrary Python tracebacks or Docker diagnostics.
            safe = [line for line in result.stderr.splitlines()
                    if line.startswith("Cutover preparation refused: ")
                    and line.endswith("Source unchanged; private/partial artifacts, if any, retained.")]
            if len(safe) == 1:
                detail = safe[0]
        raise RuntimeError(f"{label} failed (exit {result.returncode}): {detail}")
    return result.stdout.strip()


def seed_ledger(path: Path) -> dict[str, int]:
    """Use the real schema but only synthetic, quiescent rows in a private DB."""
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        store.init_db(conn)
        now = "2026-09-30T00:00:00Z"
        for number, status in enumerate(["ready_for_human_review"] * 6 + ["completed", "failed"], 1):
            job, attempt, branch = f"test-job-{number}", f"test-attempt-{number}", f"factory/test-{number}"
            pr_url = f"https://github.com/example/cutover-fixture/pull/{number}"
            conn.execute(
                "INSERT INTO bridge_jobs(job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job, f"test-dedupe-{number}", status, "create_draft_pr", "{}", "a" * 64, branch, pr_url, now, now),
            )
            conn.execute(
                "INSERT INTO factory_attempts(attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt, f"ENG-{number}", "create_draft_pr", "test", 1, f"request-{number}", f"test-dedupe-{number}",
                 branch, "verified_ready" if status == "ready_for_human_review" else status, job, "synthetic-rehearsal", now, now),
            )
            conn.execute(
                "INSERT INTO factory_execution_leases(attempt_id,lease_id,owner,acquired_at,expires_at,effect_observed_at,fence_token) VALUES (?,?,?,?,?,?,?)",
                (attempt, f"lease-{number}", "synthetic-rehearsal", now, now, now, f"fence-{number}"),
            )
            conn.execute(
                "INSERT INTO verification_leases(job_id,lease_key,action,owner_attempt_id,acquired_at,expires_at,closed_at) VALUES (?,?,?,?,?,?,?)",
                (job, f"verification-{number}", "create_draft_pr", attempt, now, now, now),
            )
            conn.execute(
                "INSERT INTO bridge_events(event_id,job_id,event_type,state,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (f"event-{number}", job, "synthetic-preserved-history", status, "{}", now),
            )
        conn.commit()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    path.chmod(0o600)
    return {"ready_for_human_review": 6, "completed": 1, "failed": 1}


def archive_files(path: Path) -> dict[str, str]:
    with tarfile.open(path) as archive:
        return {member.name.removeprefix("./"): hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                for member in archive if member.isfile()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="existing local Factory image; never pulled or built")
    args = parser.parse_args()
    image = run("docker", "image", "inspect", "--format", "{{.Id}}", args.image)
    token_id = uuid.uuid4().hex[:12]
    source_project, project = f"factory-cutover-test-{token_id}-source", f"factory-cutover-test-{token_id}-target"
    root = Path(tempfile.mkdtemp(prefix="factory-cutover-check-"))
    env_dir, keys, snapshot, directory = (root / name for name in ("env", "keys", "snapshot", "candidate"))
    containers, volumes = [], []
    try:
        cutover.target_unused(root / "unused-source", source_project)
        cutover.target_unused(directory, project)
        env_dir.mkdir(mode=0o700)
        keys.mkdir(mode=0o700)
        for name in cutover.KEYS:
            bootstrap._create_private(keys / name, "synthetic-key-not-valid-for-execution\n" + name)
        tenant = str(uuid.uuid4())
        token = "e30." + base64.urlsafe_b64encode(json.dumps({"sub": tenant}).encode()).decode().rstrip("=") + ".synthetic"
        envs = {name: {} for name in bootstrap.ENV_FILES}
        envs.update({
            "hatchet-workers.env": {"HATCHET_INTAKE_DRY_RUN": "false", "HATCHET_HERMES_BRIDGE_TOKEN": "synthetic-bridge"},
            "hatchet-bridge.env": {"HATCHET_HERMES_BRIDGE_TOKEN": "synthetic-bridge"},
            "hatchet-event-ingress.env": {"HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET": "synthetic-webhook"},
            "hatchet-worker-bridge-executor.env": {
                "DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED": "1",
                "DRIFTWARD_HERMES_GRPC_TARGET": "unix:///run/driftward-hermes-factory/hermes.sock",
            },
            "factory-operator.env": {"HATCHET_INTAKE_DRY_RUN": "false"},
            "factory-linear-token.env": {"FACTORY_LINEAR_CLIENT_SECRET": "synthetic-linear"},
        })
        for name, values in envs.items():
            bootstrap._create_private(env_dir / name, bootstrap.env_text(values))
        expected_counts = seed_ledger(root / "bridge.db")
        expected_files = {}
        for suffix in state.VOLUMES:
            volume = f"{source_project}-{suffix}"
            run("docker", "volume", "create", "--label", f"{TEST_LABEL}={token_id}",
                "--label", f"com.docker.compose.project={source_project}",
                "--label", f"com.docker.compose.volume=factory-{suffix}", volume)
            labels = json.loads(run("docker", "volume", "inspect", volume))[0].get("Labels") or {}
            if (labels.get(TEST_LABEL) != token_id or labels.get("com.docker.compose.project") != source_project
                    or labels.get("com.docker.compose.volume") != "factory-" + suffix):
                raise RuntimeError("test volume ownership changed; refusing to write it")
            volumes.append(volume)
            with tempfile.TemporaryDirectory(dir=root) as payload:
                payload_dir = Path(payload)
                bootstrap._create_private(payload_dir / "retained", f"synthetic-{suffix}\n")
                if suffix == "bridge-data":
                    shutil.copyfile(root / "bridge.db", payload_dir / "bridge.db")
                archive_path = root / f"seed-{suffix}.tar"
                with tarfile.open(archive_path, "w") as archive:
                    for path in sorted(payload_dir.iterdir()):
                        archive.add(path, arcname="./" + path.name)
            expected_files[suffix] = archive_files(archive_path)
            state._tar(image, volume, root, archive_path.name, restore=True)

        overrides = cutover.compose_overrides()
        for service in cutover.SERVICES:
            values = {}
            for filename, consumers in cutover.ENV_CONSUMERS.items():
                if service in consumers:
                    values.update({key: str(overrides[service].get(key, value)) for key, value in envs[filename].items()})
            if service in (*cutover.ENV_CONSUMERS["hatchet-workers.env"], *cutover.ENV_CONSUMERS["factory-operator.env"]):
                values["HATCHET_CLIENT_TOKEN"] = token
            if service == "postgres":
                values.update(POSTGRES_USER="hatchet", POSTGRES_DB="hatchet", POSTGRES_PASSWORD="synthetic-db")
            if service == "hatchet-lite":
                values.update(DEFAULT_TENANT_ID=tenant, SERVER_AUTH_COOKIE_DOMAIN="localhost",
                              SERVER_AUTH_COOKIE_SECRETS="synthetic-cookie-one synthetic-cookie-two", SERVER_URL="http://localhost:8888",
                              ADMIN_EMAIL="fixture@example.invalid", ADMIN_PASSWORD="synthetic-admin", ADMIN_NAME="Synthetic Admin",
                              DEFAULT_TENANT_NAME="Synthetic Factory", DEFAULT_TENANT_SLUG="synthetic-factory")
            name = f"{source_project}-{service}"
            command = ["docker", "create", "--name", name, "--network", "none", "--read-only",
                       "--security-opt", "no-new-privileges:true", "--entrypoint", "true",
                       "--label", f"{TEST_LABEL}={token_id}", "--label", f"com.docker.compose.project={source_project}",
                       "--label", f"com.docker.compose.service={service}"]
            for suffix, (consumer, target) in cutover.VOLUME_CONSUMERS.items():
                if consumer == service:
                    command.extend(["--mount", f"type=volume,src={source_project}-{suffix},dst={target}"])
            if service == "hatchet-lite":
                command.extend(["--mount", f"type=bind,src={keys},dst=/keys,readonly"])
            for key, value in values.items():
                command.extend(["--env", f"{key}={value}"])
            run(*command, image)
            containers.append(name)

        command = [sys.executable, str(ROOT / "deploy/factory-cutover.py"), "--source-project", source_project,
                   "--source-env-dir", str(env_dir), "--source-keys", str(keys), "--directory", str(directory),
                   "--project", project, "--snapshot", str(snapshot), "--port-offset", "22000"]
        print("Checking read-only cutover preview.", flush=True)
        run(*command, stage="cutover preview")
        assert not directory.exists() and not snapshot.exists(), "preview created state"
        print("Checking cold-copy apply into stopped candidate.", flush=True)
        run(*command, "--apply", stage="cutover apply")
        metadata, values = bootstrap.validate_directory(directory)
        assert metadata["profile"] == "same-host-production"
        assert all(values[key] == image for key in ("FACTORY_IMAGE", "FACTORY_POSTGRES_IMAGE", "FACTORY_HATCHET_IMAGE"))
        assert values["HATCHET_DEFAULT_TENANT_ID"] == tenant
        assert bootstrap.read_env(directory / "env/hatchet-client.env") == {"HATCHET_CLIENT_TOKEN": token}
        for name, contents in envs.items():
            assert bootstrap.read_env(directory / "env" / name) == contents, f"configuration changed: {name}"
        for name in cutover.KEYS:
            assert (directory / "keys/hatchet" / name).read_bytes() == (keys / name).read_bytes()
        report = json.loads((directory / ".factory-cutover.json").read_text())
        assert report["activation_approved"] is False
        assert report["ledger_counts"]["jobs"] == expected_counts
        run(sys.executable, str(ROOT / "deploy/factory-production.py"), "--directory", str(directory), "preview",
            stage="production preview")
        compose_command = ["docker", "compose", "--project-name", project, "--project-directory", str(directory),
                           "--env-file", str(directory / ".env")]
        for name in ("docker-compose.yml", "compose.portable.yml", "compose.production.yml"):
            compose_command.extend(["-f", str(ROOT / "deploy" / name)])
        config = json.loads(run(*compose_command, "config", "--format", "json",
                                env={"PATH": os.environ.get("PATH", os.defpath), **values,
                                     "FACTORY_INSTALLATION_ID": metadata["installation_id"]},
                                stage="production Compose render"))
        assert set(config["services"]) == set(cutover.SERVICES)
        assert all(row["image"] == image for row in config["services"].values())
        assert config["services"]["bridge-executor-worker"]["environment"]["DRIFTWARD_HERMES_GRPC_TARGET"] == "unix:///run/driftward-hermes-factory/hermes.sock"
        assert all(not config["services"][name].get("profiles") for name in
                   ("factory-lifecycle-writer", "factory-linear-token-refresh", "linear-webhook-proxy"))
        for suffix in state.VOLUMES:
            for prefix in (source_project, project):
                filename = f"verify-{prefix}-{suffix}.tar"
                state._tar(image, f"{prefix}-{suffix}", root, filename, restore=False)
                assert archive_files(root / filename) == expected_files[suffix], f"volume content changed: {suffix}"
        assert not run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}")
        assert not run("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={source_project}")
        assert metadata["project"] == project
        print("PASS: seven-volume cold copy preserved keys, tenant, credentials, six review jobs, fences, and terminal history; candidate remained stopped.")
        print("PASS: production marker and three-file Compose configuration preserve image pins and explicit integration boundaries.")
        print("Scope: synthetic raw-state migration only; no historical Hatchet startup or real execution was tested.")
    finally:
        # No broad prune, Compose down, mutable glob, or unverified rm target.
        # Preserve files/resources on ownership uncertainty for explicit review.
        for name in containers:
            row = json.loads(run("docker", "container", "inspect", name))[0]
            labels = row["Config"].get("Labels") or {}
            if (labels.get(TEST_LABEL) != token_id or labels.get("com.docker.compose.project") != source_project
                    or row["State"]["Status"] not in ("created", "exited")):
                raise RuntimeError("test container ownership/state changed; refusing cleanup")
            run("docker", "container", "rm", name)
        target_volumes = {f"{project}-{suffix}" for suffix in state.VOLUMES} & state._available_volumes()
        target_identity = None
        if target_volumes:
            marker, _ = bootstrap.validate_directory(directory)
            target_identity = marker["installation_id"]
        for name in [*volumes, *sorted(target_volumes)]:
            row = json.loads(run("docker", "volume", "inspect", name))[0]
            labels = row.get("Labels") or {}
            expected_project = source_project if name in volumes else project
            suffix = name.removeprefix(expected_project + "-")
            identity_matches = labels.get(TEST_LABEL) == token_id if name in volumes else labels.get(state.IDENTITY_LABEL) == target_identity
            if (suffix not in state.VOLUMES or labels.get("com.docker.compose.project") != expected_project
                    or labels.get("com.docker.compose.volume") != "factory-" + suffix or not identity_matches):
                raise RuntimeError("test volume ownership changed; refusing cleanup")
            run("docker", "volume", "rm", name)
        shutil.rmtree(root)
        print("Removed this run's disposable containers, volumes, and synthetic snapshot/configuration; nothing recoverable was retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
