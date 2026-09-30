#!/usr/bin/env python3
"""Operate a reviewed same-host production candidate; never build, pull, or migrate."""
from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

import bootstrap
import portable_state as state

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("factory_production_portable", HERE / "factory-portable.py")
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)
SERVICES = (*portable.SERVICES, "linear-webhook-proxy", "factory-lifecycle-writer", "factory-linear-token-refresh")
CORE = ("postgres", "hatchet-lite", "bridge")
SOCKET = "/run/driftward-hermes-factory"
TENANT_LABEL = "io.driftward.factory.tenant"
MOUNTS = {
    "postgres": [("hatchet-postgres-data", "/var/lib/postgresql/data", False)],
    "hatchet-lite": [("hatchet-config-data", "/config", False)],
    "bridge": [("bridge-data", "/var/lib/driftward-hatchet-bridge", False), ("maintenance", "/run/driftward-hatchet-bridge", False)],
    "event-ingress": [("ingress-data", "/var/lib/driftward-hatchet-event-ingress", False)],
    "factory-lifecycle-writer": [("lifecycle-data", "/var/lib/driftward-factory-lifecycle-writer", False), ("linear-published-token", "/var/run/factory-linear-token", True)],
    "factory-linear-token-refresh": [("linear-token-data", "/var/lib/driftward-factory-linear-token", False), ("linear-published-token", "/var/run/factory-linear-token", False)],
    "factory-operator": [("operator-socket", "/run/driftward-factory-operator", False), ("bridge-data", "/var/lib/driftward-hatchet-bridge", False), ("ingress-data", "/var/lib/driftward-hatchet-event-ingress", True), ("lifecycle-data", "/var/lib/driftward-factory-lifecycle-writer", False), ("maintenance", "/run/driftward-hatchet-bridge", False)],
    "outbox-dispatcher": [("bridge-data", "/var/lib/driftward-hatchet-bridge", False), ("ingress-data", "/var/lib/driftward-hatchet-event-ingress", False), ("maintenance", "/run/driftward-hatchet-bridge", False)],
}


class ProductionRefusal(ValueError):
    """Safe operator-facing text, never captured configuration or subprocess output."""


class Production(portable.Installation):
    def __init__(self, directory: Path):
        # Reuse scoped Compose/ownership operations, not the dry-run constructor
        # or its bootstrap/migration/start path.
        self.directory = bootstrap.checked_path(directory)
        self.metadata, self.values = bootstrap.validate_directory(self.directory)
        self.project = self.values["FACTORY_PROJECT_NAME"]
        if self.metadata.get("profile") != "same-host-production" or not re.fullmatch(bootstrap.PROJECT_PATTERN, self.project):
            raise ProductionRefusal("a prepared same-host production installation is required")
        self.provenance = state.read_provenance(self.directory / state.PROVENANCE)
        self.tenant = str(uuid.UUID(self.values["HATCHET_DEFAULT_TENANT_ID"]))
        token = bootstrap.extract_client_token(bootstrap.read_env(self.directory / "env/hatchet-client.env")["HATCHET_CLIENT_TOKEN"])
        payload = token.split(".")[1]
        subject = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]
        if self.tenant != self.values["HATCHET_DEFAULT_TENANT_ID"] or self.tenant != self.provenance.get("tenant_id") or subject != self.tenant:
            raise ProductionRefusal("declared tenant differs from its saved identity or Hatchet credential")
        if (self.provenance.get("destination_project") != self.project
                or self.provenance.get("profile_sha256") != state._hash(HERE / "compose.production.yml")
                or self.provenance.get("deployment_sha256") != {name: state._hash(HERE / name) for name in state.deployment_files(state.PRODUCTION)}
                or self.provenance.get("factory_image") != self.values["FACTORY_IMAGE"]):
            raise ProductionRefusal("cutover destination, image, or deployment bundle provenance differs")
        self.images = {name: self.values[key] for name, key in (
            ("postgres", "FACTORY_POSTGRES_IMAGE"), ("hatchet-lite", "FACTORY_HATCHET_IMAGE"), ("factory", "FACTORY_IMAGE"))}
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in self.images.values()):
            raise ProductionRefusal("production requires exact saved local image IDs")
        expected = {name: self.images.get(name, self.images["factory"]) for name in SERVICES}
        if self.provenance.get("source_images") != expected:
            raise ProductionRefusal("saved source images do not match production image pins")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "FACTORY_", "HATCHET_"))}
        self.env.update(self.values)
        self.env["FACTORY_INSTALLATION_ID"] = self.metadata["installation_id"]
        self.args = ["docker", "compose", "--project-name", self.project,
                     "--project-directory", str(self.directory), "--env-file", str(self.directory / ".env")]
        for name in ("docker-compose.yml", "compose.portable.yml", "compose.production.yml"):
            self.args.extend(("-f", str(HERE / name)))

    def assert_storage(self) -> None:
        self.assert_owned_resources()
        names = [f"{self.project}-{suffix}" for suffix in state.VOLUMES]
        rows = json.loads(state._docker(["volume", "inspect", *names], self.env))
        if {row["Name"] for row in rows} != set(names):
            raise ProductionRefusal("all seven production state volumes must exist")
        for row in rows:
            suffix = row["Name"][len(self.project) + 1:]
            labels = row.get("Labels") or {}
            if (labels.get(state.IDENTITY_LABEL) != self.metadata["installation_id"]
                    or labels.get("com.docker.compose.project") != self.project
                    or labels.get("com.docker.compose.volume") != "factory-" + suffix):
                raise ProductionRefusal("production state volume ownership differs")

    def assert_candidate_writers(self) -> None:
        ids = state._docker(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project], self.env).splitlines()
        services = set()
        if ids:
            for row in json.loads(state._docker(["container", "inspect", *ids], self.env)):
                labels = row.get("Config", {}).get("Labels") or {}
                service = labels.get("com.docker.compose.service")
                if (service not in SERVICES or service in services
                        or labels.get(state.IDENTITY_LABEL) != self.metadata["installation_id"]
                        or labels.get(TENANT_LABEL) != self.tenant
                        or labels.get("com.docker.compose.oneoff", "False").lower() != "false"
                        or row["Image"] != self.provenance["source_images"].get(service)):
                    raise ProductionRefusal("candidate container inventory differs from reviewed service roles")
                services.add(service)
        for suffix in state.VOLUMES:
            writers = state._docker(["ps", "-q", "--no-trunc", "--filter", "volume=" + self.project + "-" + suffix], self.env).splitlines()
            if set(writers) - set(ids):
                raise ProductionRefusal("a foreign container mounts candidate state")

    def assert_source_fenced(self) -> None:
        source = self.provenance.get("source_project", "")
        ids = self.provenance.get("source_container_ids", [])
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", source) or source == self.project
                or len(ids) != len(SERVICES) or len(set(ids)) != len(ids)
                or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids)):
            raise ProductionRefusal("source fencing provenance is incomplete")
        observed = state._docker(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + source], self.env).splitlines()
        if set(observed) != set(ids):
            raise ProductionRefusal("source container inventory changed; review its fencing again")
        rows = json.loads(state._docker(["container", "inspect", *ids], self.env))
        services = set()
        for row in rows:
            labels = row.get("Config", {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if (row["Id"] not in ids or labels.get("com.docker.compose.project") != source
                    or service not in SERVICES or service in services
                    or row["Image"] != self.provenance["source_images"].get(service)
                    or row["State"]["Status"] not in ("created", "exited")
                    or row.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"):
                raise ProductionRefusal("source must be unchanged, stopped, and have restart disabled")
            services.add(service)
        if services != set(SERVICES):
            raise ProductionRefusal("source service inventory is incomplete")
        state._no_writers([f"{source}-{suffix}" for suffix in state.VOLUMES], self.env)
        predecessors = self.provenance.get("predecessors", [])
        if not isinstance(predecessors, list):
            raise ProductionRefusal("invalid predecessor fencing provenance")
        seen = {source, self.project}
        for predecessor in predecessors:
            if not isinstance(predecessor, dict) or set(predecessor) != {"project", "installation_id"}:
                raise ProductionRefusal("invalid predecessor fencing identity")
            project, identity = predecessor["project"], predecessor["installation_id"]
            if not isinstance(project, str) or not re.fullmatch(bootstrap.PROJECT_PATTERN, project) or project in seen or not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
                raise ProductionRefusal("invalid or duplicate predecessor fencing identity")
            seen.add(project)
            previous_ids = state._docker(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project], self.env).splitlines()
            previous_services = set()
            for row in json.loads(state._docker(["container", "inspect", *previous_ids], self.env)) if previous_ids else []:
                labels = row.get("Config", {}).get("Labels") or {}
                service = labels.get("com.docker.compose.service")
                if (labels.get("com.docker.compose.project") != project or labels.get(state.IDENTITY_LABEL) != identity
                        or service not in SERVICES or service in previous_services or labels.get("com.docker.compose.oneoff", "False").lower() != "false"
                        or row["State"]["Status"] not in ("created", "exited")
                        or row.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"):
                    raise ProductionRefusal("a predecessor is foreign, running, or restart-enabled")
                previous_services.add(service)
            state._no_writers([f"{project}-{suffix}" for suffix in state.VOLUMES], self.env)
        self.assert_tenant_fenced()

    def assert_tenant_fenced(self) -> None:
        ids = state._docker(["ps", "-aq", "--no-trunc", "--filter", "label=" + TENANT_LABEL + "=" + self.tenant], self.env).splitlines()
        owners, services = {}, set()
        for row in json.loads(state._docker(["container", "inspect", *ids], self.env)) if ids else []:
            labels = row.get("Config", {}).get("Labels") or {}
            project, identity = labels.get("com.docker.compose.project", ""), labels.get(state.IDENTITY_LABEL, "")
            service = labels.get("com.docker.compose.service")
            if (not re.fullmatch(bootstrap.PROJECT_PATTERN, project) or str(uuid.UUID(identity)) != identity
                    or labels.get(TENANT_LABEL) != self.tenant or owners.setdefault(project, identity) != identity
                    or service not in SERVICES or (project, service) in services
                    or labels.get("com.docker.compose.oneoff", "False").lower() != "false"
                    or (project == self.project and identity != self.metadata["installation_id"])):
                raise ProductionRefusal("tenant container ownership or service identity differs")
            services.add((project, service))
            if project != self.project and (row["State"]["Status"] not in ("created", "exited")
                    or row.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"):
                raise ProductionRefusal("another installation of this tenant is running or restart-enabled")

    def assert_config(self) -> None:
        config = json.loads(self.compose("config", "--format", "json", private=True))
        services = config["services"]
        if set(services) != set(SERVICES):
            raise ProductionRefusal("production Compose service set differs")
        label = self.metadata["installation_id"]
        for name, volume in config["volumes"].items():
            if (name not in {"factory-" + suffix for suffix in state.PROJECT_VOLUMES}
                    or volume.get("name") != self.project + name.removeprefix("factory")
                    or volume.get("labels", {}).get(state.IDENTITY_LABEL) != label or volume.get("external")):
                raise ProductionRefusal("Compose volume boundary differs")
        network = config.get("networks", {}).get("factory", {})
        if set(config.get("networks", {})) != {"factory"} or network.get("name") != self.project or network.get("labels", {}).get(state.IDENTITY_LABEL) != label or network.get("external"):
            raise ProductionRefusal("Compose network boundary differs")
        ports = {"hatchet-lite": ("FACTORY_HATCHET_PORT", 8888), "linear-webhook-proxy": ("FACTORY_WEBHOOK_PORT", 19879), "factory-operator": ("FACTORY_CONSOLE_PORT", 19881)}
        for name, service in services.items():
            if service.get("image") != self.images.get(name, self.images["factory"]):
                raise ProductionRefusal("resolved service image differs from its saved source")
            if (service.get("labels", {}).get(state.IDENTITY_LABEL) != label or service.get("labels", {}).get(TENANT_LABEL) != self.tenant or set(service.get("networks", {})) != {"factory"}
                    or any(service.get(key) for key in ("privileged", "network_mode", "pid", "ipc", "devices", "cap_add", "volumes_from", "secrets", "configs"))):
                raise ProductionRefusal("Compose service isolation differs")
            expected = {("volume", "factory-" + suffix, target, readonly) for suffix, target, readonly in MOUNTS.get(name, [])}
            if name == "hatchet-lite":
                expected.add(("bind", str(self.directory / "keys/hatchet"), "/keys", True))
            if name == "bridge-executor-worker":
                expected.add(("bind", SOCKET, SOCKET, True))
                if any(mount.get("bind", {}).get("create_host_path", False) for mount in service.get("volumes", [])):
                    raise ProductionRefusal("gateway socket directory must already exist")
                if service.get("environment", {}).get("DRIFTWARD_HERMES_GRPC_TARGET") != "unix://" + SOCKET + "/hermes.sock":
                    raise ProductionRefusal("executor gateway boundary differs")
            mounts = service.get("volumes", [])
            if len(mounts) != len(expected) or {(mount["type"], mount["source"], mount["target"], mount.get("read_only", False)) for mount in mounts} != expected:
                raise ProductionRefusal("Compose mount boundary differs")
            published = service.get("ports", [])
            wanted = [("127.0.0.1", self.values[ports[name][0]], ports[name][1])] if name in ports else []
            if [(port.get("host_ip"), str(port.get("published")), port["target"]) for port in published] != wanted:
                raise ProductionRefusal("Compose listener boundary differs")
            if name.endswith("-worker") or name in ("factory-operator", "outbox-dispatcher", "event-ingress"):
                if any(key.startswith(("OPENAI_", "ANTHROPIC_", "GH_", "GITHUB_", "LINEAR_", "FACTORY_LINEAR_", "BROWSERBASE_", "MCP_")) for key in service.get("environment", {})):
                    raise ProductionRefusal("worker or operator credentials cross the execution boundary")

    def preflight(self) -> None:
        self.assert_storage()
        self.assert_candidate_writers()
        self.assert_config()
        for image in set(self.images.values()):
            if state._docker(["image", "inspect", "--format", "{{.Id}}", image], self.env) != image:
                raise ProductionRefusal("a saved source image is unavailable locally")
        self.assert_source_fenced()
        for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key"):  # gitleaks:allow -- filenames only
            if not bootstrap._read_private(self.directory / "keys/hatchet" / name):
                raise ProductionRefusal("saved Hatchet key material is incomplete")

    def start(self, *, core: bool, apply: bool, approve_effects: bool = False) -> None:
        # Sibling restores share authority even when their directories/projects
        # differ. This same-host lock covers the scan through container creation.
        with portable.installation_lock(Path("/tmp") / ("factory-tenant-" + self.tenant)) if apply else nullcontext():
            self.preflight()
            selected = CORE if core else SERVICES
            print(f"{'Start' if apply else 'Preview'} {self.project}: " + ", ".join(selected))
            if not core and not approve_effects:
                raise ProductionRefusal("activation requires --approve-effects; token refresh alone crosses the rollback boundary")
            if not apply:
                return
            if not core and not stat.S_ISSOCK(Path(SOCKET, "hermes.sock").stat().st_mode):
                raise ProductionRefusal("the existing Hermes gateway socket is unavailable")
            if core:
                rows = self.service_rows()
                if any(row.get("State") == "running" and row.get("Service") not in CORE for row in rows):
                    raise ProductionRefusal("core-only startup requires all effect-bearing candidate services stopped")
            self.assert_source_fenced()  # Repeat immediately before the only start.
            self.compose("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180", *(["--no-deps"] if core else []), *selected, private=True)
            print("Core started; execution and delivery remain stopped." if core else "Production started; do not resume the source without reconciling new effects and token state.")

    def service_rows(self) -> list[dict]:
        raw = self.compose("ps", "--all", "--format", "json", private=True)
        return json.loads(raw) if raw.strip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]

    def status(self) -> None:
        self.assert_owned_resources()
        found = {row.get("Service"): row for row in self.service_rows()}
        for name in SERVICES:
            row = found.get(name, {})
            print(f"{name}: {row.get('State', 'missing')} ({row.get('Health') or 'no health result'})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preview", help="read-only startup boundary checks")
    sub.add_parser("status", help="read-only status of all production service roles")
    for name in ("core-start", "activate", "stop"):
        command = sub.add_parser(name)
        command.add_argument("--apply", action="store_true")
        if name == "activate":
            command.add_argument("--approve-effects", action="store_true")
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("snapshot", type=Path)
        command.add_argument("--apply", action="store_true")
        if name == "restore":
            command.add_argument("--project", required=True)
            command.add_argument("--port-offset", type=int, default=0)
    args = parser.parse_args()
    try:
        if getattr(args, "apply", False):
            with portable.installation_lock(args.directory):
                dispatch(args)
        else:
            dispatch(args)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, ProductionRefusal) else type(exc).__name__
        print(f"Production operation refused: {reason}; private output withheld.", file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace) -> None:
    if args.command == "restore":
        state.restore(args.snapshot, args.directory, args.project, apply=args.apply, port_offset=args.port_offset, production=True)
        return
    install = Production(args.directory)
    if args.command == "preview":
        install.preflight()
        print("Production boundaries checked; no services started or external effects authorized.")
    elif args.command == "status":
        install.status()
    elif args.command in ("core-start", "activate"):
        install.start(core=args.command == "core-start", apply=args.apply, approve_effects=getattr(args, "approve_effects", False))
    elif args.command == "stop":
        install.assert_owned_resources()
        install.assert_config()
        if args.apply:
            install.compose("stop", private=True)
        print("Production stopped; state retained." if args.apply else "Preview only: stop this production project and retain its state.")
    elif args.command == "backup":
        install.assert_storage()
        install.assert_config()
        state.backup(install, args.snapshot, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
