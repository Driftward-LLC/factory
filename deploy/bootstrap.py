#!/usr/bin/env python3
"""Create a new portable Factory installation without touching host credentials.

Hatchet owns key generation, migrations, tenant seeding and API token minting.
This helper only provisions private configuration and installs the captured token.
Every mutating entry point is preview-only unless explicitly applied.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import uuid


MARKER = ".factory-install.json"
PROJECT_PATTERN = r"factory(?:-[a-z0-9][a-z0-9-]{0,48})?"
ENV_FILES = (
    "hatchet-bridge.env", "hatchet-workers.env", "hatchet-event-ingress.env",
    "hatchet-worker-bridge-executor.env", "hatchet-worker-reconciliation.env",
    "hatchet-verifier.env", "factory-operator.env", "factory-lifecycle-writer.env",
    "factory-linear-token.env",
)
EXECUTOR_ENV_FILES = ("executor.env", "executor-identity.env")
EXECUTOR_IDENTITY_KEYS = {
    "DRIFTWARD_HERMES_ALLOWED_REPOS", "DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN",
    "DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME", "DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL",
}
BUNDLED_PROFILE = "portable-bundled-executor"
PORTS = {
    "FACTORY_HATCHET_PORT": 8888,
    "FACTORY_HATCHET_GRPC_PORT": 7077,
    "FACTORY_HATCHET_HEALTH_PORT": 8733,
    "FACTORY_BRIDGE_PORT": 19877,
    "FACTORY_INGRESS_PORT": 19878,
    "FACTORY_WEBHOOK_PORT": 19879,
    "FACTORY_LIFECYCLE_PORT": 19880,
    "FACTORY_CONSOLE_PORT": 19881,
}


def checked_path(path: str | Path) -> Path:
    """Reject symlinks in every existing component, without following them."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError("installation paths must not contain symlinks")
    return path


def _read_private(path: Path) -> str:
    checked_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("configuration must be a private regular file")
        return handle.read()


def _create_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def env_text(values: dict[str, str]) -> str:
    """Compose single quotes prevent dollar interpolation in configured paths."""
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid environment key")
        if any(character in value for character in ("\\", "\n", "\r", "\0")):
            raise ValueError("configuration values cannot contain backslashes or control characters")
        lines.append(key + "='" + value.replace("'", "\\'") + "'\n")
    return "".join(lines)


def read_env(path: str | Path) -> dict[str, str]:
    """Read the deliberately narrow generated dotenv format; never evaluate it."""
    result = {}
    for line in _read_private(Path(path)).splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='((?:[^'\\]|\\')*)'", line)
        if match is None or match[1] in result:
            raise ValueError("invalid or duplicate generated environment entry")
        result[match[1]] = match[2].replace("\\'", "'")
    return result


def validate_directory(directory: str | Path) -> tuple[dict, dict[str, str]]:
    directory = checked_path(directory)
    if not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("installation must be a private directory")
    try:
        metadata = json.loads(_read_private(directory / MARKER))
        if metadata.get("format_version") != 1:
            raise ValueError("unsupported installation format")
        uuid.UUID(metadata["installation_id"])
        config = read_env(directory / ".env")
        if (config["FACTORY_INSTALL_DIR"] != str(directory)
                or config["FACTORY_ENV_DIR"] != str(directory / "env")
                or config["FACTORY_PROJECT_NAME"] != metadata["project"]
                or config["COMPOSE_PROJECT_NAME"] != metadata["project"]
                or config["FACTORY_IMAGE"] != metadata["image"]):
            raise ValueError("installation identity does not match its configuration")
        for name in ENV_FILES:
            read_env(directory / "env" / name)
        if metadata.get("profile") == BUNDLED_PROFILE:
            if config.get("FACTORY_EXECUTOR_IMAGE") != metadata.get("executor_image") or not metadata.get("executor_image"):
                raise ValueError("executor image differs from its installation identity")
            for name in EXECUTOR_ENV_FILES:
                read_env(directory / "env" / name)
            identity = read_env(directory / "env" / "executor-identity.env")
            if set(identity) != EXECUTOR_IDENTITY_KEYS:
                raise ValueError("executor identity envelope may contain only the four public identity fields")
        elif config.get("FACTORY_EXECUTOR_IMAGE"):
            raise ValueError("executor image requires its matching installation profile")
        for relative in ("env", "keys", "keys/hatchet", "run", "run/operator"):
            child = checked_path(directory / relative)
            if not child.is_dir() or child.stat().st_mode & 0o077:
                raise ValueError("installation contains a non-private directory")
        return metadata, config
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("installation is foreign or incomplete; nothing was overwritten") from exc


def init_directory(directory: str | Path, image: str, project: str, *,
                   apply: bool = False, port_offset: int = 0, executor_image: str = "") -> dict[str, str]:
    directory = checked_path(directory)
    if not re.fullmatch(PROJECT_PATTERN, project):
        raise ValueError("project must be factory or a unique factory-NAME lowercase name of at most 57 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", image):
        raise ValueError("invalid container image reference")
    if ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"):
        raise ValueError("select an explicit image version or digest, not latest")
    if executor_image and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", executor_image)
                           or ":" not in executor_image.rsplit("/", 1)[-1] or executor_image.endswith(":latest")):
        raise ValueError("select an explicit executor image version or digest, not latest")
    if any(not 1024 <= port + port_offset <= 65535 for port in PORTS.values()):
        raise ValueError("port offset must keep every listener between 1024 and 65535")
    # Validate values before creating anything, including quote/control handling.
    env_text({"FACTORY_INSTALL_DIR": str(directory)})
    if directory.exists():
        metadata, config = validate_directory(directory)
        if metadata["project"] != project or metadata["image"] != image:
            raise ValueError("existing installation has a different project or image")
        if config.get("FACTORY_EXECUTOR_IMAGE", "") != executor_image:
            raise ValueError("existing installation has a different executor; never attach an executor implicitly")
        if any(config[name] != str(port + port_offset) for name, port in PORTS.items()):
            raise ValueError("existing installation uses different ports")
        return config
    if not directory.parent.is_dir():
        raise ValueError("installation parent directory must already exist")
    config = {
        "FACTORY_PROJECT_NAME": project,
        "COMPOSE_PROJECT_NAME": project,
        "FACTORY_IMAGE": image,
        "FACTORY_INSTALL_DIR": str(directory),
        "FACTORY_ENV_DIR": str(directory / "env"),
        "FACTORY_OPERATOR_RUN_DIR": str(directory / "run" / "operator"),
        "HATCHET_POSTGRES_USER": "hatchet",
        "HATCHET_POSTGRES_DB": "hatchet",
        "HATCHET_POSTGRES_PASSWORD": secrets.token_hex(32),
        "HATCHET_SERVER_AUTH_COOKIE_SECRETS": secrets.token_hex(16) + " " + secrets.token_hex(16),
        "HATCHET_SERVER_AUTH_COOKIE_DOMAIN": "localhost",
        "HATCHET_SERVER_URL": f"http://localhost:{8888 + port_offset}",
        "HATCHET_ADMIN_EMAIL": "admin@factory.local",
        "HATCHET_ADMIN_PASSWORD": "Aa1" + secrets.token_urlsafe(32),
        "HATCHET_ADMIN_NAME": "Factory Admin",
        "HATCHET_DEFAULT_TENANT_NAME": "Factory",
        "HATCHET_DEFAULT_TENANT_SLUG": project,
        "HATCHET_DEFAULT_TENANT_ID": str(uuid.uuid4()),
        **{name: str(port + port_offset) for name, port in PORTS.items()},
    }
    if executor_image:
        config["FACTORY_EXECUTOR_IMAGE"] = executor_image
    if not apply:
        return config
    bridge_token = secrets.token_hex(32)
    inspection_token = secrets.token_hex(32)
    recovery_token = secrets.token_hex(32)
    env_files = {
        "hatchet-bridge.env": {
            "HATCHET_HERMES_BRIDGE_TOKEN": bridge_token,
            "HATCHET_HERMES_INSPECTION_TOKEN": inspection_token,
            "HATCHET_HERMES_RECOVERY_TOKEN": recovery_token,
        },
        "hatchet-workers.env": {
            "HATCHET_HERMES_BRIDGE_TOKEN": bridge_token,
            "HATCHET_INTAKE_DRY_RUN": "true",
        },
        "hatchet-event-ingress.env": {
            "HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET": secrets.token_hex(32),
            "HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET": secrets.token_hex(32),
        },
        "hatchet-worker-bridge-executor.env": {
            "HATCHET_HERMES_INSPECTION_TOKEN": inspection_token,
            "DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED": "0",
        },
        "hatchet-worker-reconciliation.env": {"HATCHET_HERMES_RECOVERY_TOKEN": recovery_token},
        "hatchet-verifier.env": {"HATCHET_HERMES_VERIFIER_TOKEN": secrets.token_hex(32)},
        "factory-operator.env": {
            "HATCHET_FACTORY_OPERATOR_TOKEN": secrets.token_hex(32),
            "HATCHET_INTAKE_DRY_RUN": "true",
        },
        "factory-lifecycle-writer.env": {},
        "factory-linear-token.env": {},
    }
    if executor_image:
        env_files.update({"executor.env": {}, "executor-identity.env": {key: "" for key in sorted(EXECUTOR_IDENTITY_KEYS)}})
    directory.mkdir(mode=0o700)
    for relative in ("env", "keys", "keys/hatchet", "run", "run/operator"):
        (directory / relative).mkdir(mode=0o700)
    _create_private(directory / ".env", env_text(config))
    for name, values in env_files.items():
        _create_private(directory / "env" / name, env_text(values))
    # Last write is the completion marker. An interrupted creation is never
    # guessed to be safe to resume or silently populated with replacement keys.
    _create_private(directory / MARKER, json.dumps({
        "format_version": 1, "installation_id": str(uuid.uuid4()),
        "project": project, "image": image,
        **({"profile": BUNDLED_PROFILE, "executor_image": executor_image} if executor_image else {}),
    }, sort_keys=True) + "\n")
    return config


def extract_client_token(output: str) -> str:
    tokens = [line.strip() for line in output.splitlines()
              if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", line.strip())]
    if len(tokens) != 1:
        raise ValueError("Hatchet did not return exactly one worker token")
    return tokens[0]


def finalize_directory(directory: str | Path, token: str, *, apply: bool = False) -> None:
    directory = checked_path(directory)
    validate_directory(directory)
    token = extract_client_token(token)
    destination = directory / "env" / "hatchet-client.env"
    if destination.exists() or destination.is_symlink():
        if read_env(destination) != {"HATCHET_CLIENT_TOKEN": token}:
            raise ValueError("worker credentials already exist; rotation must be explicit")
        return
    if apply:
        _create_private(destination, env_text({"HATCHET_CLIENT_TOKEN": token}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="prepare private configuration for a fresh installation")
    init.add_argument("--directory", required=True)
    init.add_argument("--image", required=True)
    init.add_argument("--project", required=True)
    init.add_argument("--port-offset", type=int, default=0)
    init.add_argument("--apply", action="store_true")
    finalize = commands.add_parser("finalize", help="install native Hatchet token from standard input")
    finalize.add_argument("--directory", required=True)
    finalize.add_argument("--token-stdin", action="store_true", required=True)
    finalize.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            init_directory(args.directory, args.image, args.project, apply=args.apply, port_offset=args.port_offset)
        else:
            finalize_directory(args.directory, sys.stdin.read(), apply=args.apply)
    except (OSError, ValueError) as exc:
        print(f"Factory bootstrap refused: {exc}", file=sys.stderr)
        return 1
    print("Configuration prepared; no secrets printed." if args.apply else "Dry run: no files changed. Add --apply to proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
