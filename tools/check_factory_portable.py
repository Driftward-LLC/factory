#!/usr/bin/env python3
"""Exercise a NEW disposable Factory project; delete only that project's test data.

Requires a locally built runtime image and Docker Compose >= 2.24.4. Does not
read production configuration or authorize real execution. Hermes starts idle.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap  # noqa: E402
import telemetry  # noqa: E402

spec = importlib.util.spec_from_file_location("acceptance_portable", ROOT / "deploy/factory-portable.py")
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


def run(command: list[str], *, label: str, env: dict[str, str] | None = None,
        source: str | None = None, timeout: int = 600) -> str:
    result = subprocess.run(command, input=source, env=env, text=True,
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Docker/admin output can contain generated credentials. Never echo it.
        raise RuntimeError(f"{label} failed (exit {result.returncode}); captured output withheld")
    return result.stdout


def assert_unused(project: str) -> None:
    containers = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                      "--format", "{{.ID}}"], label="project collision check")
    volumes = run(["docker", "volume", "ls", "--format", "{{.Name}}"], label="volume collision check")
    networks = run(["docker", "network", "ls", "--format", "{{.Name}}"], label="network collision check")
    if containers.strip() or any(name.startswith(project + "-") for name in volumes.splitlines()) or project in networks.splitlines():
        raise RuntimeError("generated test project already exists; refusing to touch it")


def credential_snapshot(directory: Path) -> dict[str, str]:
    paths = [directory / "env" / "hatchet-client.env", *sorted((directory / "keys" / "hatchet").iterdir())]
    if len(paths) < 4:
        raise RuntimeError("native keys and worker credentials were not generated")
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def console_boundary(port: int) -> None:
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("console health failed")
    try:
        urlopen(f"http://127.0.0.1:{port}/", timeout=10).close()
    except HTTPError as error:
        if error.code == 401:
            return
    raise RuntimeError("console accepted an unauthenticated request")


SAFETY_CHECK = """
import os
assert os.environ.get('HATCHET_INTAKE_DRY_RUN') == 'true'
assert os.environ.get('DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED') == '0'
assert not os.environ.get('DRIFTWARD_HERMES_GRPC_TARGET')
print('dry-run-only')
"""

SIGNED_DELIVERY = """
import hashlib, hmac, json, os, sys, time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
assert os.environ.get('HATCHET_INTAKE_DRY_RUN') == 'true'
issue = sys.argv[1]
now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
payload = {
    'action': 'update', 'type': 'Issue', 'createdAt': now,
    'url': 'https://linear.app/driftward/issue/' + issue + '/portable-acceptance',
    'webhookTimestamp': int(time.time() * 1000),
    'data': {
        'id': 'portable-acceptance-' + issue, 'identifier': issue,
        'title': 'Disposable portable dry-run acceptance',
        'description': 'Synthetic test only. Never perform real execution.',
        'updatedAt': now, 'team': {'name': 'Engineering', 'key': 'ENG'},
        'project': {'id': 'd779c532-620d-458c-8abd-05e37f5ca4c1', 'name': 'Capykit'},
        'state': {'name': 'Ready', 'type': 'unstarted'},
        'labels': [], 'relations': {'blockedBy': []},
    },
    'updatedFrom': {'state': {'name': 'Backlog'}},
}
raw = json.dumps(payload).encode()
signature = hmac.new(os.environ['HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
def deliver(signed, delivery):
    headers = {'Content-Type': 'application/json', 'Linear-Delivery': delivery}
    if signed: headers['Linear-Signature'] = signature
    with urlopen(Request('http://127.0.0.1:19878/events/linear/issue', data=raw, headers=headers), timeout=60) as response:
        return response.status, json.load(response)
try:
    deliver(False, 'unsigned-acceptance')
    raise AssertionError('unsigned delivery accepted')
except HTTPError as error:
    assert error.code == 401
status, first = deliver(True, 'first-acceptance')
duplicate_status, duplicate = deliver(True, 'replayed-acceptance')
assert status == 201 and first['created'] is True
assert duplicate_status == 200 and duplicate['created'] is False
assert first['event']['event_key'] == duplicate['event']['event_key']
assert first['event']['hatchet_published'] and first['event']['hatchet_event_id']
print(json.dumps({'event_key': first['event']['event_key']}))
"""

BRIDGE_SNAPSHOT = """
import json, sqlite3, sys
connection = sqlite3.connect('file:/var/lib/driftward-hatchet-bridge/bridge.db?mode=ro', uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute('SELECT * FROM bridge_jobs').fetchall()
assert len(rows) <= 1, 'duplicate bridge jobs'
if not rows:
    print('{}')
else:
    row = rows[0]
    payload = json.loads(row['payload_json'])
    assert payload.get('dry_run') is True
    assert payload.get('repo') == 'Driftward-LLC/capykit'
    assert payload.get('work_item_id') == sys.argv[1] or payload.get('linear_issue') == sys.argv[1]
    assert row['hatchet_workflow_run_id'] and row['hatchet_step_run_id']
    if row['state'] == 'completed':
        assert row['pr_url'].startswith('dry-run://')
        assert row['commit_sha'] == 'dry-run'
        assert row['reason_code'] == 'dry_run_completed'
        assert row['hermes_session_id'] is None
    print(json.dumps({key: row[key] for key in ('job_id', 'state', 'pr_url', 'commit_sha', 'reason_code', 'hatchet_workflow_run_id')}))
"""

INGRESS_SNAPSHOT = """
import json, sqlite3, sys
connection = sqlite3.connect('file:/var/lib/driftward-hatchet-event-ingress/ingress.db?mode=ro', uri=True)
row = connection.execute('SELECT event_key,hatchet_event_id,hatchet_published FROM ingress_events WHERE subject=?', (sys.argv[1],)).fetchall()
assert len(row) == 1 and row[0][1] and row[0][2] == 1
receipts = connection.execute('SELECT signature_verified,disposition FROM ingress_receipts WHERE event_key=? ORDER BY disposition', (row[0][0],)).fetchall()
assert receipts == [(1, 'accepted'), (1, 'duplicate')]
print(json.dumps({'event': row[0], 'receipts': receipts}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="existing local runtime image, e.g. factory:test")
    parser.add_argument("--port-offset", type=int, default=20000)
    parser.add_argument("--telemetry", action="store_true", help="also test optional Collector packaging with synthetic credentials and exporters paused")
    args = parser.parse_args()
    project = "factory-acceptance-" + uuid.uuid4().hex[:16]
    issue = "ENG-" + str(int(uuid.uuid4().hex[:10], 16))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("FACTORY_", "HATCHET_", "COMPOSE_"))}
    compose: list[str] | None = None
    print(f"Creating disposable project {project}; its containers and volumes will be deleted on exit.", flush=True)
    try:
        assert_unused(project)
        for port in (8888, 19881, 9888, 20881, *((4318, 5318) if args.telemetry else ())):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port + args.port_offset))
        temporary = tempfile.mkdtemp(prefix="factory-portable-acceptance-")
        cleanup_failed = False
        restore_incomplete = False
        try:
            directory = Path(temporary) / "installation"
            wrapper = [sys.executable, str(ROOT / "deploy" / "factory-portable.py"), "--directory", str(directory)]
            restored_compose: list[str] | None = None
            restored_env: dict[str, str] = {}
            restored_wrapper: list[str] = []
            try:
                run([*wrapper, "init", "--image", args.image, "--project", project,
                     "--port-offset", str(args.port_offset), "--apply"], env=env, label="fresh configuration")
                metadata, config = bootstrap.validate_directory(directory)
                if config["FACTORY_PROJECT_NAME"] != project:
                    raise RuntimeError("unexpected test project identity")
                env.update(config)
                env["FACTORY_INSTALLATION_ID"] = metadata["installation_id"]
                compose = ["docker", "compose", "--profile", "hermes", "--project-name", project,
                           "--project-directory", str(directory), "--env-file", str(directory / ".env"),
                           "-f", str(ROOT / "deploy" / "docker-compose.yml"),
                           "-f", str(ROOT / "deploy" / "compose.portable.yml")]
                print("Starting empty installation and native bootstrap.", flush=True)
                run([*wrapper, "up"], env=env, label="fresh startup")
                if args.telemetry:
                    print("Configuring optional Collector with synthetic credentials; exporters stay paused.", flush=True)
                    secret = Path(temporary) / "collector.env"
                    bootstrap._create_private(secret, bootstrap.env_text({
                        "POSTHOG_TRACES_ENDPOINT": "https://us.i.posthog.com/i/v1/traces",
                        "POSTHOG_PROJECT_TOKEN": "phc_synthetic_acceptance_not_a_real_key"}))
                    before = Path(temporary) / "before-telemetry"
                    run([*wrapper, "backup", str(before), "--apply"], env=env, label="pre-telemetry backup")
                    run([*wrapper, "telemetry", "--snapshot", str(before), "--credentials", str(secret),
                         "--port", str(4318 + args.port_offset), "--paused", "--apply"], env=env, label="Collector configuration")
                    configured = portable.Installation(directory)
                    compose, env = configured.args, configured.env
                    run([*wrapper, "up"], env=env, label="Collector startup")
                    run([*wrapper, "status"], env=env, label="Collector status")
                def assert_shared_image(command, environment):
                    ids = run([*command, "ps", "-q"], env=environment, label="image identity inventory").split()
                    rows = json.loads(run(["docker", "inspect", *ids], label="service image inspection"))
                    expected = run(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], label="release image identity").strip()
                    if not rows or {row["Image"] for row in rows} != {expected}:
                        raise RuntimeError("service containers do not share one release image")
                    for row in rows:
                        role = row["Config"]["Labels"]["com.docker.compose.service"]
                        for mount in row["Mounts"]:
                            if mount["Destination"] == "/opt/data" and role != "factory-hermes":
                                raise RuntimeError("Hermes state leaked into another role")
                            if mount["Destination"] in ("/var/run/docker.sock", "/run/docker.sock", "/"):
                                raise RuntimeError("unexpected host access")
                    run([*command, "exec", "-T", "factory-hermes", "/opt/hermes/.venv/bin/python", "-c",
                         "import hermes_cli; print('native Hermes import passed')"], env=environment, label="Hermes runtime import")
                assert_shared_image(compose, env)
                run([*compose, "exec", "-T", "factory-hermes", "sh", "-c",
                     "printf preserved > /opt/data/factory-acceptance-marker"], env=env, label="Hermes volume marker")
                def execute(service: str, source: str) -> str:
                    return run([*compose, "exec", "-T", service, "python3", "-", issue],
                               env=env, source=source, label=f"{service} acceptance assertion")
                execute("bridge-executor-worker", SAFETY_CHECK)
                console_boundary(int(config["FACTORY_CONSOLE_PORT"]))
                credentials = credential_snapshot(directory)
                print("Checking signed ingress, duplicate suppression, and Hatchet dry-run completion.", flush=True)
                execute("event-ingress", SIGNED_DELIVERY)
                deadline = time.monotonic() + 180
                while True:
                    receipt = json.loads(execute("bridge", BRIDGE_SNAPSHOT))
                    if receipt.get("state") == "completed":
                        break
                    if receipt.get("state") == "failed" or time.monotonic() >= deadline:
                        raise RuntimeError("dry-run workflow did not complete within 180 seconds")
                    time.sleep(2)
                ingress = json.loads(execute("event-ingress", INGRESS_SNAPSHOT))
                print("Restarting installation and verifying credentials and durable receipts are unchanged.", flush=True)
                run([*wrapper, "stop"], env=env, label="test restart stop")
                run([*wrapper, "up"], env=env, label="test restart startup")
                execute("bridge-executor-worker", SAFETY_CHECK)
                console_boundary(int(config["FACTORY_CONSOLE_PORT"]))
                if credential_snapshot(directory) != credentials:
                    raise RuntimeError("restart replaced native keys or worker credentials")
                if json.loads(execute("bridge", BRIDGE_SNAPSHOT)) != receipt:
                    raise RuntimeError("restart changed the durable dry-run receipt")
                if json.loads(execute("event-ingress", INGRESS_SNAPSHOT)) != ingress:
                    raise RuntimeError("restart changed ingress delivery evidence")
                print("Cold backup and restore into a second empty project; source remains stopped.", flush=True)
                snapshot = Path(temporary) / "snapshot"
                restored_directory = Path(temporary) / "restored"
                restored_project = project + "-restored"
                assert_unused(restored_project)
                run([*wrapper, "backup", str(snapshot), "--apply"], env=env, label="cold backup")
                if run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"], label="source stopped check").strip():
                    raise RuntimeError("cold backup left source containers running")
                restored_wrapper = [sys.executable, str(ROOT / "deploy" / "factory-portable.py"), "--directory", str(restored_directory)]
                restore_incomplete = True
                run([*restored_wrapper, "restore", str(snapshot), "--project", restored_project,
                     "--port-offset", str(args.port_offset + 1000), "--apply"], env=env, label="cold restore")
                restored_metadata, restored_config = bootstrap.validate_directory(restored_directory)
                if restored_config["FACTORY_PROJECT_NAME"] != restored_project:
                    raise RuntimeError("unexpected restored test project identity")
                restored_env = {**env, **restored_config, "FACTORY_INSTALLATION_ID": restored_metadata["installation_id"]}
                restored_compose = ["docker", "compose", "--profile", "hermes", "--project-name", restored_project,
                                    "--project-directory", str(restored_directory), "--env-file", str(restored_directory / ".env"),
                                    "-f", str(ROOT / "deploy" / "docker-compose.yml"),
                                    "-f", str(ROOT / "deploy" / "compose.portable.yml")]
                if args.telemetry:
                    configured = portable.Installation(restored_directory)
                    restored_compose, restored_env = configured.args, configured.env
                    if telemetry.credentials(restored_directory / telemetry.ENV_FILE) != telemetry.credentials(directory / telemetry.ENV_FILE):
                        raise RuntimeError("restore changed isolated Collector credentials")
                restore_incomplete = False
                if credential_snapshot(restored_directory) != credentials:
                    raise RuntimeError("restore replaced native keys or worker credentials")
                if run(["docker", "ps", "--filter", f"label=com.docker.compose.project={restored_project}", "--format", "{{.ID}}"], label="restore stopped check").strip():
                    raise RuntimeError("restore started services without explicit up")
                run([*restored_wrapper, "up"], env=restored_env, label="restored startup")
                run([*restored_wrapper, "status"], env=restored_env, label="restored status")
                def restored_execute(service: str, source: str) -> str:
                    return run([*restored_compose, "exec", "-T", service, "python3", "-", issue],
                               env=restored_env, source=source, label=f"restored {service} assertion")
                assert_shared_image(restored_compose, restored_env)
                if run([*restored_compose, "exec", "-T", "factory-hermes", "cat", "/opt/data/factory-acceptance-marker"],
                       env=restored_env, label="Hermes state restored") != "preserved":
                    raise RuntimeError("restore changed Hermes state")
                restored_execute("bridge-executor-worker", SAFETY_CHECK)
                console_boundary(int(restored_config["FACTORY_CONSOLE_PORT"]))
                if json.loads(restored_execute("bridge", BRIDGE_SNAPSHOT)) != receipt:
                    raise RuntimeError("restore changed the durable dry-run receipt")
                if json.loads(restored_execute("event-ingress", INGRESS_SNAPSHOT)) != ingress:
                    raise RuntimeError("restore changed ingress deduplication evidence")
                if credential_snapshot(restored_directory) != credentials:
                    raise RuntimeError("restored startup replaced native keys or worker credentials")
                print("PASS: bootstrap, signed intake, dry-run receipt, deduplication, restart, cold backup/restore.", flush=True)
            finally:
                for owned_compose, owned_wrapper, owned_env in ((restored_compose, restored_wrapper, restored_env), (compose, wrapper, env)):
                    if owned_compose is not None:
                        owned_project = owned_env["FACTORY_PROJECT_NAME"]
                        try:
                            # The wrapper verifies installation UUID labels before stopping.
                            run([*owned_wrapper, "stop"], env=owned_env, label="cleanup ownership check")
                            print(f"Removing only disposable project {owned_project} and its test volumes (not recoverable).", flush=True)
                            run([*owned_compose, "down", "--volumes", "--timeout", "10"], env=owned_env, label="disposable project cleanup")
                        except (OSError, RuntimeError, subprocess.TimeoutExpired):
                            cleanup_failed = True
                            print(f"Cleanup incomplete for {owned_project}; retaining its private test configuration.", file=sys.stderr)
                if cleanup_failed:
                    raise RuntimeError("test project cleanup requires attention")
        finally:
            if cleanup_failed or restore_incomplete:
                print(f"Private test artifacts retained for recovery at {temporary}; only project {project} and its -restored sibling were used.", file=sys.stderr)
            else:
                shutil.rmtree(temporary)
        return 0
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("FAIL: portable acceptance failed; raw command output withheld to protect generated secrets.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
