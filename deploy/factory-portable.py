#!/usr/bin/env python3
"""Operate an isolated, image-only Factory installation without host systemd."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import bootstrap
import portable_state

HERE = Path(__file__).resolve().parent
SERVICES = (
    "postgres", "hatchet-lite", "bridge", "event-ingress", "intake-worker",
    "execution-dispatch-worker", "bridge-executor-worker", "pr-event-worker",
    "reconciliation-worker", "factory-operator", "outbox-dispatcher",
)


def read_env(path: Path) -> dict[str, str]:
    """Read generated dotenv data, never execute a shell configuration file."""
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid configuration key in {path.name}")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("invalid configuration value")
        values[key] = value
    return values


def run(args: list[str], *, env: dict[str, str] | None = None, private: bool = False) -> str:
    result = subprocess.run(args, env=env, text=True, capture_output=private, check=False)
    if result.returncode:
        # Private admin output may contain keys/tokens even on failure.
        raise RuntimeError(f"{Path(args[0]).name} command failed (exit {result.returncode}); secret output withheld" if private else f"command failed (exit {result.returncode})")
    return result.stdout or ""


def release_image() -> str:
    metadata = HERE.parent / "factory.env"
    if not metadata.is_file():
        raise ValueError("use a release bundle, or explicitly supply --image for a local development build")
    values = read_env(metadata)
    if values.get("FACTORY_RELEASE_VALIDATION_ONLY") != "false":
        raise ValueError("validation-only artifacts cannot be installed")
    return values["FACTORY_IMAGE"]


class Installation:
    def __init__(self, directory: Path):
        self.directory = bootstrap.checked_path(directory)
        self.metadata, self.values = bootstrap.validate_directory(self.directory)
        self.project = self.values["FACTORY_PROJECT_NAME"]
        if not re.fullmatch(r"factory-[a-z0-9][a-z0-9-]{0,48}", self.project):
            raise ValueError("portable project must start with factory- and be at most 57 characters")
        if self.values.get("FACTORY_INSTALL_DIR") != str(self.directory):
            raise ValueError("installation was moved; restore it with an explicit new directory")
        # Do not let an ambient Compose profile or another deployment's env
        # silently select a different daemon/project/image/secret directory.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "FACTORY_", "HATCHET_"))}
        self.env.update(self.values)
        self.env["FACTORY_INSTALLATION_ID"] = self.metadata["installation_id"]
        self.args = ["docker", "compose", "--project-name", self.project,
                     "--project-directory", str(self.directory), "--env-file", str(self.directory / ".env"),
                     "-f", str(HERE / "docker-compose.yml"), "-f", str(HERE / "compose.portable.yml")]

    def compose(self, *args: str, private: bool = False) -> str:
        return run([*self.args, *args], env=self.env, private=private)

    def assert_owned_resources(self) -> None:
        label = "io.driftward.factory.installation"
        volume_names = run(["docker", "volume", "ls", "--format", "{{.Name}}"], env=self.env, private=True).splitlines()
        volumes = [name for name in volume_names if name.startswith(self.project + "-")]
        containers = run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project], env=self.env, private=True).splitlines()
        networks = run(["docker", "network", "ls", "-q", "--filter", "name=^" + self.project + "$"], env=self.env, private=True).splitlines()
        for kind, ids in (("volume", volumes), ("container", containers), ("network", networks)):
            if not ids:
                continue
            rows = json.loads(run(["docker", kind, "inspect", *ids], env=self.env, private=True))
            for row in rows:
                labels = row.get("Config", {}).get("Labels", {}) if kind == "container" else row.get("Labels", {})
                if not labels or labels.get(label) != self.metadata["installation_id"]:
                    raise ValueError("Compose project collides with resources owned by another installation; choose a new project")

    def up(self) -> None:
        self.assert_owned_resources()
        workers = bootstrap.read_env(self.directory / "env" / "hatchet-workers.env")
        executor = bootstrap.read_env(self.directory / "env" / "hatchet-worker-bridge-executor.env")
        if workers.get("HATCHET_INTAKE_DRY_RUN") != "true" or executor.get("DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED") != "0":
            raise ValueError("this portable profile supports dry-run only; real execution requires a separately reviewed executor rollout")
        # Explicit migrations run with all Factory writers stopped. This never
        # addresses the existing driftward-factory project or its volumes.
        self.compose("stop")
        keys = self.directory / "keys" / "hatchet"
        expected = [keys / name for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key")]
        if not any(path.exists() for path in expected):
            if any(keys.iterdir()):
                raise ValueError("key directory is not empty; refusing to overwrite unknown key material")
            self.compose("run", "--rm", "--no-deps", "keygen", private=True)
        if not all(path.is_file() and not path.is_symlink() for path in expected):
            raise ValueError("Hatchet keys are incomplete; refusing to regenerate or replace them")
        for path in expected:
            path.chmod(0o600)
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "postgres", "hatchet-lite")
        client_path = self.directory / "env" / "hatchet-client.env"
        worker_env = bootstrap.read_env(client_path) if client_path.exists() else {}
        if not worker_env.get("HATCHET_CLIENT_TOKEN"):
            token = self.compose("exec", "-T", "hatchet-lite", "/hatchet-admin", "token", "create", "--config", "/config",
                                 "--name", "factory-workers", "--tenant-id", self.values["HATCHET_DEFAULT_TENANT_ID"], private=True)
            bootstrap.finalize_directory(self.directory, bootstrap.extract_client_token(token), apply=True)
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "python3", "bridge",
                     "/app/hatchet/bridge/maintenance_lock_exec.py", "exec", "python3", "-m", "hatchet_hermes_bridge.cli", "init", private=True)
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "python3", "event-ingress", "-m", "hatchet_event_ingress.cli", "init", private=True)
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", *SERVICES)
        self.status()

    def status(self) -> None:
        raw = self.compose("ps", "--all", "--format", "json", private=True)
        rows = json.loads(raw) if raw.strip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        found = {row["Service"]: row for row in rows if not row.get("Service") == "keygen"}
        failed = []
        for service in SERVICES:
            row = found.get(service, {})
            state, health = row.get("State", "missing"), row.get("Health", "")
            print(f"{service}: {state}" + (f" ({health})" if health else ""))
            if state != "running" or health not in ("", "healthy"):
                failed.append(service)
        print("Linear delivery: disabled in the portable dry-run profile")
        print("Real execution: disabled by bootstrap; no Hermes credentials installed")
        if failed:
            raise RuntimeError("unhealthy services: " + ", ".join(failed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path, help="private installation directory (not the source checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="preview a fresh installation; --apply writes new private configuration")
    init.add_argument("--image", help="explicit release digest or local development image")
    init.add_argument("--project", required=True, help="unique factory-NAME Compose namespace")
    init.add_argument("--apply", action="store_true")
    init.add_argument("--port-offset", type=int, default=0, help="offset published loopback ports for a second installation")
    sub.add_parser("pull", help="pull pinned runtime images; never build source")
    sub.add_parser("up", help="start this installation, explicitly migrating its stopped ledgers")
    sub.add_parser("status", help="inspect this installation only")
    sub.add_parser("stop", help="stop services; retain all volumes and credentials")
    logs = sub.add_parser("logs", help="view logs for one service")
    logs.add_argument("service", choices=SERVICES)
    backup = sub.add_parser("backup", help="preview a private cold snapshot; --apply stops this project")
    backup.add_argument("snapshot", type=Path)
    backup.add_argument("--apply", action="store_true")
    restore = sub.add_parser("restore", help="restore a snapshot into a new installation; services remain stopped")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--project", required=True)
    restore.add_argument("--port-offset", type=int, default=0)
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            bootstrap.init_directory(args.directory.absolute(), args.image or release_image(), args.project, apply=args.apply, port_offset=args.port_offset)
            print("Private dry-run installation configured." if args.apply else "Preview only; use --apply to create the installation.")
            return 0
        if args.command == "restore":
            portable_state.restore(args.snapshot, args.directory, args.project, apply=args.apply, port_offset=args.port_offset)
            return 0
        install = Installation(args.directory)
        if args.command == "up":
            install.up()
        elif args.command == "status":
            install.status()
        elif args.command == "pull":
            install.compose("pull", *SERVICES, "keygen")
        elif args.command == "stop":
            install.assert_owned_resources()
            install.compose("stop")
        elif args.command == "logs":
            install.compose("logs", "--tail", "100", args.service)
        elif args.command == "backup":
            portable_state.backup(install, args.snapshot, apply=args.apply)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Factory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
