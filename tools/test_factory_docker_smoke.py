#!/usr/bin/env python3
"""Behavior tests for the Factory Docker smoke script."""

from __future__ import annotations

import os
import shlex
import socket
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SERVICES = (
    "postgres",
    "hatchet-lite",
    "bridge",
    "event-ingress",
    "linear-webhook-proxy",
    "intake-worker",
    "execution-dispatch-worker",
    "bridge-executor-worker",
    "pr-event-worker",
    "reconciliation-worker",
    "factory-lifecycle-writer",
    "factory-linear-token-refresh",
    "factory-operator",
    "outbox-dispatcher",
)


class FactoryDockerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "docker-compose.yml").write_text("name: driftward-factory\n", encoding="utf-8")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.hatchet_env = self.root / "hatchet.env"
        self.hatchet_env.write_text("HATCHET_POSTGRES_DB=hatchet\nHATCHET_POSTGRES_USER=hatchet\n", encoding="utf-8")
        self.bridge_env = self.root / "hatchet-bridge.env"
        self.bridge_env.write_text("HATCHET_HERMES_BRIDGE_TOKEN=bridge-token\n", encoding="utf-8")
        self.operator_env = self.root / "factory-operator.env"
        self.operator_env.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=operator-token\n", encoding="utf-8")
        self.socket_path = self.root / "operator.sock"
        self.operator_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.operator_socket.bind(str(self.socket_path))
        self.addCleanup(self.operator_socket.close)
        self._write_fake_commands()

    def _write_fake_commands(self) -> None:
        services = " ".join(shlex.quote(service) for service in REQUIRED_SERVICES)
        docker = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "inspect" ]]; then
  shift
  cid=""
  format=""
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --format) format="$2"; shift 2 ;;
      *) cid="$1"; shift ;;
    esac
  done
  if [[ "$format" == *'.State.Status'* ]]; then
    if [[ "$cid" == "cid-postgres" && "${{FAKE_POSTGRES_OOM:-false}}" == "true" ]]; then
      printf 'running|true|0|healthy\\n'
    else
      printf 'running|false|0|healthy\\n'
    fi
    exit 0
  fi
  if [[ "$format" == *'org.opencontainers.image.revision'* ]]; then
    printf '0123456789012345678901234567890123456789\\n'
    exit 0
  fi
  case "$cid" in
    cid-postgres) printf 'volume\\tdriftward-factory-hatchet-postgres-data\\t/var/lib/postgresql/data\\n' ;;
    cid-hatchet-lite) printf 'volume\\tdriftward-factory-hatchet-config-data\\t/config\\n' ;;
    cid-bridge) printf '%s\\t%s\\t/var/lib/driftward-hatchet-bridge\\n' "${{FAKE_BRIDGE_MOUNT_TYPE:-volume}}" "${{FAKE_BRIDGE_VOLUME_NAME:-driftward-factory-bridge-data}}" ;;
    cid-event-ingress) printf 'volume\\tdriftward-factory-ingress-data\\t/var/lib/driftward-hatchet-event-ingress\\n' ;;
    cid-factory-lifecycle-writer) printf 'volume\\tdriftward-factory-lifecycle-data\\t/var/lib/driftward-factory-lifecycle-writer\\nvolume\\tdriftward-factory-linear-published-token\\t/var/run/factory-linear-token\\n' ;;
    cid-factory-linear-token-refresh) printf 'volume\\tdriftward-factory-linear-token-data\\t/var/lib/driftward-factory-linear-token\\nvolume\\tdriftward-factory-linear-published-token\\t/var/run/factory-linear-token\\n' ;;
    cid-factory-operator)
      printf 'volume\\tdriftward-factory-bridge-data\\t/var/lib/driftward-hatchet-bridge\\n'
      printf 'volume\\tdriftward-factory-ingress-data\\t/var/lib/driftward-hatchet-event-ingress\\n'
      ;;
    cid-outbox-dispatcher)
      printf 'volume\\tdriftward-factory-bridge-data\\t/var/lib/driftward-hatchet-bridge\\n'
      printf 'volume\\tdriftward-factory-ingress-data\\t/var/lib/driftward-hatchet-event-ingress\\n'
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
[[ "${{1:-}}" == "compose" ]] || exit 2
shift
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --env-file|--project-directory) shift 2 ;;
    *) break ;;
  esac
done
case "${{1:-}}" in
  ps)
    if [[ "${{2:-}}" == "-q" ]]; then
      printf 'cid-%s\\n' "${{3:-}}"
    else
      printf '%s\\n' {services}
    fi
    ;;
  exec)
    shift
    [[ "${{1:-}}" == "-T" ]] && shift
    service="${{1:-}}"; shift || true
    if [[ "$service" =~ ^(bridge|intake-worker|bridge-executor-worker|factory-operator)$ && "${{1:-}}" == "printenv" ]]; then
      case "${{2:-}}" in
        HATCHET_FACTORY_RUNTIME) printf 'docker\\n'; exit 0 ;;
        HATCHET_VERIFIER_POLICY_PATH) printf '/app/hatchet/workers/verifier-policy.json\\n'; exit 0 ;;
      esac
      exit 0
    fi
    exit 0
    ;;
  *) exit 2 ;;
esac
"""
        curl = """#!/usr/bin/env bash
set -euo pipefail
args=" $* "
if [[ "$args" == *" /v1/retry/preview"* || "$args" == *" http://localhost/v1/retry/preview"* ]]; then
  error="${FAKE_OPERATOR_ERROR:-trusted_ingress_observation_missing}"
  status="${FAKE_OPERATOR_STATUS:-400}"
  if [[ "$error" == "__success__" ]]; then
    printf '{"eligible":true}'
  else
    printf '{"error":"%s"}' "$error"
  fi
  printf '\\n%s' "$status"
  exit 0
fi
if [[ "$args" == *"19877/health"* ]]; then
  printf '{"ok":true,"release_commit":"0123456789012345678901234567890123456789"}'
fi
exit 0
"""
        self._write_executable(self.fake_bin / "docker", docker)
        self._write_executable(self.fake_bin / "curl", curl)
        self._write_executable(self.fake_bin / "factory", "#!/usr/bin/env sh\nexit 0\n")

    def _write_executable(self, path: Path, body: str) -> None:
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self, **overrides: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "DRIFTWARD_HATCHET_ENV_FILE": str(self.hatchet_env),
            "FACTORY_HATCHET_BRIDGE_ENV": str(self.bridge_env),
            "FACTORY_OPERATOR_ENV": str(self.operator_env),
            "FACTORY_OPERATOR_SOCKET": str(self.socket_path),
            **overrides,
        }

    def test_operator_preview_smoke_accepts_bounded_missing_observation(self) -> None:
        result = subprocess.run(
            [str(ROOT / "deploy/factory-docker-smoke"), str(self.runtime)],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Factory Docker runtime is healthy", result.stdout)

    def test_operator_preview_smoke_rejects_unavailable_trusted_ingress(self) -> None:
        result = subprocess.run(
            [str(ROOT / "deploy/factory-docker-smoke"), str(self.runtime)],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(FAKE_OPERATOR_ERROR="trusted_ingress_unavailable"),
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cannot read trusted ingress", result.stderr)

    def test_rejects_bridge_bind_mount(self) -> None:
        result = subprocess.run(
            [str(ROOT / "deploy/factory-docker-smoke"), str(self.runtime)],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(FAKE_BRIDGE_MOUNT_TYPE="bind", FAKE_BRIDGE_VOLUME_NAME=""),
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unexpected mount for /var/lib/driftward-hatchet-bridge", result.stderr)

    def test_rejects_oom_killed_container_even_when_docker_health_is_green(self) -> None:
        result = subprocess.run(
            [str(ROOT / "deploy/factory-docker-smoke"), str(self.runtime)],
            text=True,
            capture_output=True,
            check=False,
            env=self._env(FAKE_POSTGRES_OOM="true"),
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("postgres state=running health=healthy oom=true", result.stderr)


if __name__ == "__main__":
    unittest.main()
