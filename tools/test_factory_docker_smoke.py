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
[[ "${{1:-}}" == "compose" ]] || exit 2
shift
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --env-file|--project-directory) shift 2 ;;
    *) break ;;
  esac
done
case "${{1:-}}" in
  ps) printf '%s\\n' {services} ;;
  exec) exit 0 ;;
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
exit 0
"""
        self._write_executable(self.fake_bin / "docker", docker)
        self._write_executable(self.fake_bin / "curl", curl)

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


if __name__ == "__main__":
    unittest.main()
