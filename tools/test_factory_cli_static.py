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
        self.assertIn('docker_service_status "Hatchet intake worker" intake-worker required', source)
        self.assertIn("docker_compose up -d --force-recreate --no-deps intake-worker >/dev/null", source)
        self.assertIn('docker_compose logs "${args[@]}" "${units[@]}"', source)

    def test_compose_honors_protected_worker_mode_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("- path: /etc/driftward/hatchet-workers.env", compose)
        self.assertIn("- path: /etc/driftward/hatchet-worker-bridge-executor.env", compose)
        self.assertNotIn("HATCHET_INTAKE_DRY_RUN: ${HATCHET_INTAKE_DRY_RUN", compose)
        self.assertNotIn("DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED: ${DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED", compose)

    def test_compose_accepts_pinned_factory_image_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        service = (ROOT / "deploy/systemd/driftward-factory.service").read_text(encoding="utf-8")

        self.assertIn("image: ${FACTORY_IMAGE:-driftward/factory:${FACTORY_IMAGE_TAG:-local}}", compose)
        self.assertIn("build:", compose)
        self.assertIn("Environment=COMPOSE_PROJECT_NAME=driftward-factory", service)
        self.assertIn("Environment=FACTORY_SOURCE_ROOT=__SOURCE_ROOT__", service)
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

    def test_release_image_tags_require_matching_clean_checkout(self) -> None:
        source = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")

        self.assertIn("require_clean_release_checkout()", source)
        self.assertIn('image_basename="${factory_image_override##*/}"', source)
        self.assertIn('release_tag="${image_basename##*:}"', source)
        self.assertIn('[[ "$release_tag" =~ ^v[0-9]{4}\\.[0-9]{1,2}\\.[0-9]{1,2}($|[.-]) ]]', source)
        self.assertIn('"refs/tags/$release_tag^{commit}"', source)
        self.assertIn('git -C "$repo_root" diff-index --quiet HEAD --', source)
        self.assertIn('git -C "$repo_root" ls-files --others --exclude-standard', source)

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
            finally:
                sock.close()

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
