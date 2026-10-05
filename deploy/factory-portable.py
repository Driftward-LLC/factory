#!/usr/bin/env python3
"""Operate an isolated, image-only Factory installation without host systemd."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import bootstrap
import portable_state
import telemetry

HERE = Path(__file__).resolve().parent
SERVICES = (
    "postgres", "hatchet-lite", "bridge", "event-ingress", "intake-worker",
    "execution-dispatch-worker", "bridge-executor-worker", "pr-event-worker",
    "reconciliation-worker", "factory-operator", "outbox-dispatcher",
)


@contextmanager
def installation_lock(directory: Path):
    """Serialize our lifecycle commands without adding a daemon or lock service."""
    directory = bootstrap.checked_path(directory)
    if not directory.parent.is_dir():
        raise ValueError("installation parent directory must already exist")
    path = directory.parent / ("." + directory.name + ".factory-lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("unsafe installation operation lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another operation is using this installation") from exc
        yield
    finally:
        os.close(fd)


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
    if values.get("FACTORY_RELEASE_SCHEMA") != "1" or values.get("FACTORY_RELEASE_PLATFORM") != "linux/amd64":
        raise ValueError("unsupported release bundle format or platform")
    if not re.fullmatch(r"ghcr\.io/driftward-llc/factory@sha256:[a-f0-9]{64}", values.get("FACTORY_IMAGE", "")):
        raise ValueError("release bundle must pin its runtime image by digest")
    return values["FACTORY_IMAGE"]


class Installation:
    def __init__(self, directory: Path):
        self.directory = bootstrap.checked_path(directory)
        self.metadata, self.values = bootstrap.validate_directory(self.directory)
        if self.metadata.get("profile", "portable-dry-run") != "portable-dry-run":
            raise ValueError("production candidates require separately reviewed operations; the portable wrapper is dry-run only")
        self.project = self.values["FACTORY_PROJECT_NAME"]
        if not re.fullmatch(bootstrap.PROJECT_PATTERN, self.project):
            raise ValueError("portable project must be factory or factory-NAME and be at most 57 characters")
        if self.values.get("FACTORY_INSTALL_DIR") != str(self.directory):
            raise ValueError("installation was moved; restore it with an explicit new directory")
        # Do not let an ambient Compose profile or another deployment's env
        # silently select a different project/image/secret directory.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "FACTORY_", "HATCHET_"))}
        self.env.update(self.values)
        self.env["FACTORY_INSTALLATION_ID"] = self.metadata["installation_id"]
        self.args = ["docker", "compose", "--project-name", self.project,
                     "--project-directory", str(self.directory), "--env-file", str(self.directory / ".env"),
                     "-f", str(HERE / "docker-compose.yml"), "-f", str(HERE / "compose.portable.yml")]
        telemetry.attach(self, SERVICES)

    def compose(self, *args: str, private: bool = False) -> str:
        return run([*self.args, *args], env=self.env, private=private)

    def assert_owned_resources(self) -> None:
        label = "io.driftward.factory.installation"
        volume_names = run(["docker", "volume", "ls", "--format", "{{.Name}}"], env=self.env, private=True).splitlines()
        expected_volumes = {f"{self.project}-{suffix}" for suffix in portable_state.PROJECT_VOLUMES}
        volumes = [name for name in volume_names if name in expected_volumes]
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
            print("Generating native Hatchet key material (private output withheld).")
            self.compose("run", "--rm", "--no-deps", "--user", f"{os.geteuid()}:{os.getegid()}", "keygen", private=True)
        if not all(path.is_file() and not path.is_symlink() for path in expected):
            raise ValueError("Hatchet keys are incomplete; refusing to regenerate or replace them")
        for path in expected:
            path.chmod(0o600)
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "postgres", "hatchet-lite")
        client_path = self.directory / "env" / "hatchet-client.env"
        worker_env = bootstrap.read_env(client_path) if client_path.exists() else {}
        if not worker_env.get("HATCHET_CLIENT_TOKEN"):
            print("Creating this installation's Hatchet worker credential (private output withheld).")
            token = self.compose("exec", "-T", "hatchet-lite", "/hatchet-admin", "token", "create", "--config", "/config",
                                 "--name", "factory-workers", "--tenant-id", self.values["HATCHET_DEFAULT_TENANT_ID"], private=True)
            bootstrap.finalize_directory(self.directory, bootstrap.extract_client_token(token), apply=True)
        print("Initializing the stopped bridge and ingress ledgers.")
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "python3", "bridge",
                     "/app/hatchet/bridge/maintenance_lock_exec.py", "exec", "python3", "-m", "hatchet_hermes_bridge.cli", "init", private=True)
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "python3", "event-ingress", "-m", "hatchet_event_ingress.cli", "init", private=True)
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", *self.services)
        self.status()

    def status(self) -> None:
        raw = self.compose("ps", "--all", "--format", "json", private=True)
        rows = json.loads(raw) if raw.strip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        found = {row["Service"]: row for row in rows if not row.get("Service") == "keygen"}
        failed = []
        for service in self.services:
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
    telemetry.add_command(sub)
    init = sub.add_parser("init", help="preview a fresh installation; --apply writes new private configuration")
    init.add_argument("--image", help="explicit release digest or local development image")
    init.add_argument("--project", required=True, help="unique factory or factory-NAME Compose namespace")
    init.add_argument("--apply", action="store_true")
    init.add_argument("--port-offset", type=int, default=0, help="offset published loopback ports for a second installation")
    sub.add_parser("pull", help="pull pinned runtime images; never build source")
    sub.add_parser("up", help="start this installation, explicitly migrating its stopped ledgers")
    sub.add_parser("status", help="inspect this installation only")
    sub.add_parser("stop", help="stop services; retain all volumes and credentials")
    logs = sub.add_parser("logs", help="view logs for one service")
    logs.add_argument("service", choices=(*SERVICES, telemetry.SERVICE))
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
        # Read-only status/logs and preview-only init/restore do not create a
        # lock file. All state-changing paths take the same host-local lock.
        mutating = args.command in {"up", "stop", "pull"} or getattr(args, "apply", False)
        if mutating:
            with installation_lock(args.directory):
                return dispatch(args)
        return dispatch(args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Factory: {exc}", file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace) -> int:
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
        if telemetry.SERVICE in install.services:
            # Compose cannot pull a local image ID. Fetch its reviewed digest,
            # then verify it still resolves to the installation's saved ID.
            run(["docker", "pull", telemetry.IMAGE], env=install.env, private=True)
            if portable_state._docker(["image", "inspect", "--format", "{{.Id}}", telemetry.IMAGE], install.env) != install.metadata["telemetry"]["image"]:
                raise ValueError("Collector image differs from its saved pin")
        install.compose("pull", *(name for name in install.services if name != telemetry.SERVICE), "keygen")
    elif args.command == "stop":
        install.assert_owned_resources()
        install.compose("stop")
    elif args.command == "logs":
        install.compose("logs", "--tail", "100", args.service)
    elif args.command == "backup":
        portable_state.backup(install, args.snapshot, apply=args.apply)
    elif args.command == "telemetry":
        telemetry.configure(install, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
