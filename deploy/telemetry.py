"""Opt-in Collector packaging. Credentials belong only to the Collector role."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import bootstrap
import portable_state as state

HERE = Path(__file__).resolve().parent
SERVICE = "otel-collector"
IMAGE = "otel/opentelemetry-collector-contrib@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1"
FILES = ("compose.telemetry.yml", "otel-collector.yaml")
ENV_FILE = "env/factory-otel-collector.env"


def validate(metadata: dict) -> dict | None:
    value = metadata.get("telemetry")
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"image", "port", "enabled", "deployment_sha256"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("image", "")))
            or type(value.get("port")) is not int or not 1024 <= value["port"] <= 65535
            or type(value.get("enabled")) is not bool
            or value.get("deployment_sha256") != {name: state._hash(HERE / name) for name in FILES}):
        raise ValueError("invalid telemetry settings or changed Collector deployment bundle")
    return value


def credentials(path: Path) -> dict:
    values = bootstrap.read_env(path)
    if (set(values) != {"POSTHOG_TRACES_ENDPOINT", "POSTHOG_PROJECT_TOKEN"}
            or values["POSTHOG_TRACES_ENDPOINT"] not in {
                "https://us.i.posthog.com/i/v1/traces", "https://eu.i.posthog.com/i/v1/traces"}
            or not re.fullmatch(r"phc_[A-Za-z0-9_-]+", values["POSTHOG_PROJECT_TOKEN"])):
        raise ValueError("Collector requires only a PostHog ingestion endpoint and project token")
    return values


def attach(installation, services: tuple[str, ...]) -> None:
    installation.services = services
    feature = validate(installation.metadata)
    if not feature:
        return
    credentials(installation.directory / ENV_FILE)
    installation.services = (*services, SERVICE)
    installation.args.extend(("-f", str(HERE / FILES[0])))
    installation.env.update(
        FACTORY_OTEL_COLLECTOR_IMAGE=feature["image"],
        FACTORY_OTEL_COLLECTOR_ENV_FILE=str(installation.directory / ENV_FILE),
        FACTORY_OTEL_COLLECTOR_CONFIG=str(HERE / FILES[1]),
        FACTORY_OTEL_PORT=str(feature["port"]),
        FACTORY_OTEL_INTERNAL_ENDPOINT="http://otel-collector:4318/v1/traces" if feature["enabled"] else "",
    )
    if hasattr(installation, "images"):
        installation.images[SERVICE] = feature["image"]


def add_command(sub) -> None:
    command = sub.add_parser("telemetry", help="configure Collector after a current cold backup; leave stopped")
    command.add_argument("--credentials", type=Path, help="private generated dotenv; required on first installation")
    command.add_argument("--snapshot", type=Path, required=True)
    command.add_argument("--port", type=int, default=4318)
    command.add_argument("--paused", action="store_true", help="disable application exporters; retain Collector and its backup configuration")
    command.add_argument("--apply", action="store_true")


def configure(installation, args) -> None:
    """Keep topology changes stopped and reversible without changing cutover history."""
    if hasattr(installation, "preflight"):
        installation.preflight()
    else:
        installation.assert_owned_resources()
    snapshot = state._path(args.snapshot)
    manifest = json.loads(bootstrap._read_private(snapshot / "manifest.json"))
    metadata = installation.metadata
    profile = metadata.get("profile", "portable-dry-run")
    config = state.config_files(profile, metadata)
    volumes = manifest.get("volumes", [])
    expected = {"config/" + name for name in config} | {"volumes/" + name + ".tar" for name in volumes}
    if (manifest.get("format_version") != 1 or manifest.get("profile") != profile
            or manifest.get("installation_id") != metadata["installation_id"]
            or manifest.get("project") != installation.project
            or manifest.get("factory_image") != installation.values["FACTORY_IMAGE"]
            or len(volumes) != len(set(volumes)) or not set(volumes).issubset(state.VOLUMES)
            or not set(state.VOLUMES if profile == state.PRODUCTION else state.VOLUMES[:5]).issubset(volumes)
            or manifest.get("deployment_sha256") != {name: state._hash(HERE / name) for name in state.deployment_files(profile, metadata)}
            or set(manifest.get("sha256", {})) != expected
            or state._private_files(snapshot) != expected | {"manifest.json"}):
        raise ValueError("telemetry requires this installation's complete current cold backup")
    for name, digest in manifest["sha256"].items():
        if state._hash(snapshot / name) != digest:
            raise ValueError("telemetry backup checksum differs")
    for name in config:
        if bootstrap._read_private(snapshot / "config" / name) != bootstrap._read_private(installation.directory / name):
            raise ValueError("configuration changed since the telemetry backup")
    for name in volumes:
        state._archive_check(snapshot / "volumes" / (name + ".tar"))
    previous = validate(metadata)
    if previous and args.credentials:
        raise ValueError("Collector is already configured; credential rotation requires separate review")
    source = installation.directory / ENV_FILE if previous else args.credentials
    if source is None:
        raise ValueError("first Collector installation requires --credentials")
    secret = credentials(source)
    image = state._docker(["image", "inspect", "--format", "{{.Id}}", IMAGE], installation.env)
    feature = {"image": image, "port": args.port, "enabled": not args.paused,
               "deployment_sha256": {name: state._hash(HERE / name) for name in FILES}}
    updated = {**metadata, "telemetry": feature}
    validate(updated)
    if str(args.port) in {installation.values[key] for key in bootstrap.PORTS}:
        raise ValueError("Collector port collides with a Factory listener")
    print("Configure optional Collector; credentials isolated; application tracing " + ("paused" if args.paused else "enabled"))
    if not args.apply:
        return
    # Check all containers, not just durable-volume writers (Collector has none).
    ids = state._docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + installation.project], installation.env).splitlines()
    rows = json.loads(state._docker(["container", "inspect", *ids], installation.env)) if ids else []
    if any(row["State"]["Status"] not in ("created", "exited") for row in rows):
        raise ValueError("telemetry changes require every installation service stopped")
    state._no_writers([installation.project + "-" + suffix for suffix in state.PROJECT_VOLUMES], installation.env)
    if not previous:
        bootstrap._create_private(installation.directory / ENV_FILE, bootstrap.env_text(secret))
    target = installation.directory / bootstrap.MARKER
    pending = target.with_name(target.name + ".telemetry-pending")
    bootstrap._create_private(pending, json.dumps(updated, sort_keys=True, indent=2) + "\n")
    os.replace(pending, target)
    descriptor = os.open(installation.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print("Collector configured; services remain stopped. Resume through the matching operations wrapper.")
