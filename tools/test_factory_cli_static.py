#!/usr/bin/env python3
"""Static contract tests for the managed factory CLI."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FactoryCliStaticTests(unittest.TestCase):
    def test_status_reports_legacy_bridge_reconciler_as_retired(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn("retired_timer_status()", source)
        self.assertIn('"Retired bridge reconciler" driftward-hatchet-bridge-reconciler.timer driftward-hatchet-bridge-reconciler.service', source)
        self.assertIn('timer_active=${active:-unknown} timer_enabled=${enabled:-unknown}', source)
        self.assertIn('service_active=${service_active:-unknown}', source)
        self.assertNotIn('"Hatchet bridge reconciler timer" driftward-hatchet-bridge-reconciler.timer advisory', source)

    def test_cli_prefers_docker_runtime_when_active(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn("factory_docker_runtime_active()", source)
        self.assertIn('unit_status "Factory Docker wrapper" "$FACTORY_SERVICE" required', source)
        self.assertIn('unit_status "Hermes Factory gRPC gateway" "$HERMES_FACTORY_GATEWAY_SERVICE" required', source)
        self.assertIn('socket_status "Hermes Factory gRPC socket" "$HERMES_FACTORY_GATEWAY_SOCKET" advisory', source)
        self.assertIn('docker_service_status "Hatchet intake worker" intake-worker required', source)
        self.assertIn("docker_compose up -d --force-recreate --no-deps intake-worker >/dev/null", source)
        self.assertIn('docker_compose logs "${args[@]}" "${units[@]}"', source)

    def test_status_checks_linear_webhook_readiness(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn("linear_webhook_public_readiness_status()", source)
        self.assertIn('http_status "Linear webhook readiness" http://127.0.0.1:19879/ready "200" required', source)
        self.assertIn('linear_webhook_public_readiness_status "Linear webhook public readiness" required', source)
        self.assertNotIn('http_status "Linear webhook proxy health" http://127.0.0.1:19879/health "200" required', source)

    def test_status_exercises_linear_webhook_public_readiness(self) -> None:
        factory = ROOT / "factory/factory"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            runtime_root = tmp_path / "runtime"
            bridge_env = tmp_path / "hatchet-bridge.env"
            worker_env = tmp_path / "hatchet-workers.env"
            curl_args = tmp_path / "curl-args.txt"
            fake_bin.mkdir()
            runtime_root.mkdir()
            (runtime_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            bridge_env.write_text("HATCHET_HERMES_BRIDGE_TOKEN=test-token\n", encoding="utf-8")
            worker_env.write_text("HATCHET_INTAKE_DRY_RUN=true\n", encoding="utf-8")
            self._write_executable(
                fake_bin / "systemctl",
                "#!/usr/bin/env sh\n"
                "case \"$1\" in\n"
                "  is-active) echo active ;;\n"
                "  is-enabled) echo enabled ;;\n"
                "  list-timers) ;;\n"
                "esac\n",
            )
            self._write_executable(
                fake_bin / "docker",
                "#!/usr/bin/env sh\n"
                "case \" $* \" in\n"
                "  *' ps '*|*' ps')\n"
                "    printf '%s\\n' \\\n"
                "      postgres \\\n"
                "      hatchet-lite \\\n"
                "      bridge \\\n"
                "      event-ingress \\\n"
                "      linear-webhook-proxy \\\n"
                "      intake-worker \\\n"
                "      execution-dispatch-worker \\\n"
                "      bridge-executor-worker \\\n"
                "      pr-event-worker \\\n"
                "      reconciliation-worker \\\n"
                "      factory-lifecycle-writer \\\n"
                "      factory-linear-token-refresh \\\n"
                "      factory-operator \\\n"
                "      outbox-dispatcher\n"
                "    ;;\n"
                "esac\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/usr/bin/env sh\n"
                "printf '%s\\n' \"$@\" >> \"$FACTORY_TEST_CURL_ARGS\"\n"
                "case \" $* \" in *' --write-out '*) printf '200' ;; *) printf '{}' ;; esac\n",
            )
            self._write_executable(
                fake_bin / "tailscale",
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = status ] && [ \"$2\" = --json ]; then\n"
                "  printf '{\"Self\":{\"DNSName\":\"factory.test.\"}}\\n'\n"
                "else\n"
                "  exit 1\n"
                "fi\n",
            )
            self._write_executable(fake_bin / "driftward-hermes", "#!/usr/bin/env sh\nexit 0\n")

            env = os.environ.copy()
            env.update(
                {
                    "DRIFTWARD_FACTORY_RUNTIME_ROOT": str(runtime_root),
                    "FACTORY_HATCHET_BRIDGE_ENV": str(bridge_env),
                    "FACTORY_HATCHET_WORKERS_ENV": str(worker_env),
                    "FACTORY_TEST_CURL_ARGS": str(curl_args),
                    "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                }
            )
            result = subprocess.run(
                [str(factory), "status"],
                check=True,
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )

            self.assertIn("Linear webhook readiness", result.stdout)
            self.assertIn("Linear webhook public readiness", result.stdout)
            self.assertIn("url=https://factory.test/ready", result.stdout)
            logged_curl_args = curl_args.read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:19879/ready", logged_curl_args)
            self.assertIn("https://factory.test/ready", logged_curl_args)

    def test_compose_honors_protected_worker_mode_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("- path: /etc/driftward/hatchet-workers.env", compose)
        self.assertIn("- path: /etc/driftward/hatchet-worker-bridge-executor.env", compose)
        self.assertIn("/run/driftward-hermes-factory:/run/driftward-hermes-factory", compose)
        self.assertIn("HATCHET_FACTORY_OPERATOR_RUNTIME: docker", compose)
        self.assertIn("HATCHET_FACTORY_OPERATOR_INGRESS_DB: /var/lib/driftward-hatchet-event-ingress/ingress.db", compose)
        self.assertIn("./data/ingress:/var/lib/driftward-hatchet-event-ingress:ro", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertNotIn("/run/docker.sock", compose)
        self.assertNotIn("HATCHET_INTAKE_DRY_RUN: ${HATCHET_INTAKE_DRY_RUN", compose)
        self.assertNotIn("DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED: ${DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED", compose)

    def test_compose_accepts_pinned_factory_image_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        service = (ROOT / "deploy/systemd/driftward-factory.service").read_text(encoding="utf-8")

        self.assertIn("image: ${FACTORY_IMAGE:-driftward/factory:${FACTORY_IMAGE_TAG:-local}}", compose)
        self.assertIn("build:", compose)
        self.assertIn("Environment=COMPOSE_PROJECT_NAME=driftward-factory", service)
        self.assertIn("Environment=FACTORY_SOURCE_ROOT=__SOURCE_ROOT__", service)
        self.assertIn("driftward-hermes-factory-gateway.service", service)
        self.assertNotIn("Environment=FACTORY_IMAGE_TAG=", service)
        self.assertNotIn("Environment=FACTORY_IMAGE=", service)

    def test_install_docker_persists_factory_image_selection(self) -> None:
        source = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")

        self.assertIn("--image-tag TAG", source)
        self.assertIn("--image IMAGE", source)
        self.assertIn('factory_image_dropin_dir="${service_unit}.d"', source)
        self.assertIn('factory_image_override="${FACTORY_IMAGE:-}"', source)
        self.assertIn("Environment=FACTORY_IMAGE_TAG=%s", source)
        self.assertIn("Environment=FACTORY_IMAGE=%s", source)
        self.assertIn("env -u FACTORY_IMAGE -u FACTORY_IMAGE_TAG", source)
        self.assertIn('docker build -f "$repo_root/deploy/Dockerfile" -t "$factory_image" "$repo_root"', source)
        self.assertIn('systemctl restart "$service_name"', source)
        self.assertIn('DRIFTWARD_FACTORY_SMOKE_TIMEOUT:-90', source)
        self.assertIn('DRIFTWARD_FACTORY_SMOKE_INTERVAL:-5', source)
        self.assertIn("wait_for_factory_smoke()", source)
        self.assertIn("Factory Docker smoke is not ready; retrying", source)
        self.assertIn("hermes_gateway_bundle_target=", source)
        self.assertIn("hermes-gateway-server.ts", source)
        self.assertIn('node_bin="${DRIFTWARD_FACTORY_NODE_BIN:-/usr/bin/node}"', source)
        self.assertIn("Hermes Factory gateway requires Node.js 18 or newer", source)
        self.assertIn("DRIFTWARD_HERMES_GRPC_TARGET", source)
        self.assertIn("driftward-hermes-factory-gateway.service", source)
        self.assertIn('factory_operator_env_file="${DRIFTWARD_FACTORY_OPERATOR_ENV_FILE:-/etc/driftward/factory-operator.env}"', source)
        self.assertIn('hatchet_workers_env_file="${FACTORY_HATCHET_WORKERS_ENV:-/etc/driftward/hatchet-workers.env}"', source)
        self.assertIn('operator_dry_run="$(awk -F= ', source)
        self.assertIn('upsert_env "$factory_operator_env_file" HATCHET_INTAKE_DRY_RUN "$operator_dry_run"', source)

    def test_hermes_gateway_systemd_unit_is_local_socket_boundary(self) -> None:
        unit = (ROOT / "deploy/systemd/driftward-hermes-factory-gateway.service").read_text(encoding="utf-8")

        self.assertIn("DRIFTWARD_HERMES_GRPC_TARGET=unix:///run/driftward-hermes-factory/hermes.sock", unit)
        self.assertIn("ExecStart=__NODE_BIN__ __FACTORY_SHARE_DIR__/hermes-gateway/hermes-gateway-server.js", unit)
        self.assertIn("ReadWritePaths=/run/driftward-hermes-factory /run/docker.sock /var/run/docker.sock", unit)
        self.assertNotIn("OPENAI_API_KEY", unit)
        self.assertNotIn("ANTHROPIC_API_KEY", unit)

    def test_release_image_tags_require_matching_clean_checkout(self) -> None:
        source = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")

        self.assertIn("require_clean_release_checkout()", source)
        self.assertIn('image_basename="${factory_image_override##*/}"', source)
        self.assertIn('release_tag="${image_basename##*:}"', source)
        self.assertIn('[[ "$release_tag" =~ ^v[0-9]{4}\\.[0-9]{1,2}\\.[0-9]{1,2}($|[.-]) ]]', source)
        self.assertIn('"refs/tags/$release_tag^{commit}"', source)
        self.assertIn('git -C "$repo_root" diff-index --quiet HEAD --', source)
        self.assertIn('git -C "$repo_root" ls-files --others --exclude-standard', source)

    def test_factory_release_workflow_is_manual_validated_and_host_deploy_only(self) -> None:
        workflow = (ROOT / ".github/workflows/factory-release.yml").read_text(encoding="utf-8")

        self.assertIn("name: Factory Release", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("release_tag:", workflow)
        self.assertIn("permissions:\n  contents: write", workflow)
        self.assertIn("ref: main", workflow)
        self.assertIn("release_tag must match vYYYY.M.D or vYYYY.M.D.N", workflow)
        self.assertIn("release tag already exists at a different commit", workflow)
        self.assertIn("using existing annotated tag", workflow)
        self.assertIn("python3 tools/hatchet-contracts.py check", workflow)
        self.assertIn("python3 -m unittest discover -s tools -p 'test_*.py'", workflow)
        self.assertIn("npm run build --prefix hatchet/workers", workflow)
        self.assertIn("docker build -f deploy/Dockerfile -t driftward/factory:ci .", workflow)
        self.assertIn("Ensure checkout remains clean", workflow)
        self.assertIn('git status --short', workflow)
        self.assertIn("/tmp/gitleaks git --redact --verbose .", workflow)
        self.assertIn('git tag -a "$RELEASE_TAG"', workflow)
        self.assertIn('git push origin "refs/tags/$RELEASE_TAG"', workflow)
        self.assertIn('gh "${args[@]}"', workflow)
        self.assertIn("This workflow does not deploy the VPS runtime", workflow)
        self.assertNotIn("ssh ", workflow)
        self.assertNotIn("systemctl restart", workflow)

    def test_attempts_list_accepts_json_option(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("Unix sockets are required for the factory operator wrapper")

        factory = ROOT / "factory/factory"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            curl_args = tmp_path / "curl-args.txt"
            env_file = tmp_path / "factory-operator.env"
            socket_path = tmp_path / "operator.sock"

            env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=test-token\n", encoding="utf-8")
            self._write_executable(
                fake_bin / "id",
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = \"-u\" ]; then echo 0; else /usr/bin/id \"$@\"; fi\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/usr/bin/env sh\n"
                "printf '%s\\n' \"$@\" > \"$FACTORY_TEST_CURL_ARGS\"\n"
                "printf '{\"attempts\":[]}\\n'\n",
            )

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(socket_path))
                env = os.environ.copy()
                env.update(
                    {
                        "FACTORY_OPERATOR_ENV": str(env_file),
                        "FACTORY_OPERATOR_SOCKET": str(socket_path),
                        "FACTORY_TEST_CURL_ARGS": str(curl_args),
                        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                    }
                )

                for args in (("ENG-12", "--json"), ("--json", "ENG-12")):
                    with self.subTest(args=args):
                        result = subprocess.run(
                            [str(factory), "attempts", "list", *args],
                            check=True,
                            env=env,
                            text=True,
                            capture_output=True,
                            timeout=5,
                        )
                        self.assertEqual(result.stdout, '{"attempts":[]}\n')
                        self.assertIn("http://localhost/v1/attempts/work-item/ENG-12", curl_args.read_text(encoding="utf-8"))
                command_expectations = (
                    (("attempts", "active", "--json"), "http://localhost/v1/attempts/active"),
                    (("attempts", "events", "attempt-1", "--json", "--limit", "25"), "http://localhost/v1/attempts/attempt-1/events?limit=25"),
                    (("lifecycle", "ENG-12", "--json", "--limit", "25"), "http://localhost/v1/lifecycle/work-item/ENG-12?limit=25"),
                    (("lifecycle", "--limit", "25", "--json", "ENG-12"), "http://localhost/v1/lifecycle/work-item/ENG-12?limit=25"),
                )
                for command, expected_url in command_expectations:
                    with self.subTest(command=command):
                        result = subprocess.run(
                            [str(factory), *command],
                            check=True,
                            env=env,
                            text=True,
                            capture_output=True,
                            timeout=5,
                        )
                        self.assertEqual(result.stdout, '{"attempts":[]}\n')
                        self.assertIn(expected_url, curl_args.read_text(encoding="utf-8"))
            finally:
                sock.close()

    def test_force_absence_retry_collects_fixed_github_evidence(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn("factory retry ENG-X force-absence preview --attempt-id ATTEMPT-ID", source)
        self.assertIn("github_absence_evidence()", source)
        self.assertIn('mode="$(mode_status)"', source)
        self.assertIn('--arg mode "$mode"', source)
        self.assertIn('mode:$mode', source)
        self.assertIn('gh pr list --repo "$repo" --head "$branch" --state all', source)
        self.assertIn('gh api "repos/$repo/branches/$encoded_branch"', source)
        self.assertIn("github_branch_pr_absence_v1", source)
        self.assertIn("/v1/retry/force-absence/preview", source)
        self.assertIn("/v1/retry/force-absence/reserve", source)
        self.assertIn("branch_exists", source)

    def test_doctor_hatchet_forwards_manifest_and_args(self) -> None:
        factory = ROOT / "factory/factory"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doctor = tmp_path / "doctor.py"
            desired = tmp_path / "desired.json"
            live = tmp_path / "live.json"
            args_file = tmp_path / "doctor-args.txt"
            doctor.write_text(
                "import os, sys\n"
                "open(os.environ['FACTORY_TEST_DOCTOR_ARGS'], 'w', encoding='utf-8').write('\\n'.join(sys.argv[1:]))\n"
                "print('doctor ok')\n",
                encoding="utf-8",
            )
            desired.write_text("{}", encoding="utf-8")
            live.write_text("{}", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "FACTORY_HATCHET_DOCTOR_SCRIPT": str(doctor),
                    "FACTORY_HATCHET_DESIRED_STATE": str(desired),
                    "FACTORY_TEST_DOCTOR_ARGS": str(args_file),
                }
            )
            result = subprocess.run(
                [str(factory), "doctor", "hatchet", "--live-json", str(live), "--json"],
                check=True,
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )

            self.assertEqual(result.stdout, "doctor ok\n")
            args = args_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(args[:2], ["--desired", str(desired)])
            self.assertIn("--live-json", args)
            self.assertIn(str(live), args)
            self.assertIn("--json", args)

    @staticmethod
    def _write_executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
