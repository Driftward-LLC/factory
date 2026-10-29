#!/usr/bin/env python3
"""Static contract tests for the managed factory CLI."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FactoryCliStaticTests(unittest.TestCase):
    def test_interrupted_resolution_cli_requires_explicit_review_and_uses_operator(self) -> None:
        if not hasattr(socket, "AF_UNIX") or shutil.which("jq") is None:
            self.skipTest("Unix sockets and jq required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"; fake_bin.mkdir()
            env_file = root / "operator.env"
            env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture\n")
            self._write_executable(fake_bin / "id", '#!/bin/sh\necho 0\n')
            self._write_executable(fake_bin / "curl", '#!/usr/bin/env python3\nimport sys,json\na=sys.argv\nprint(json.dumps({"url":a[-1],"body":json.loads(a[a.index("--data")+1])}))\n')
            # Simulate root-owned fixture metadata on unprivileged CI runners.
            self._write_executable(fake_bin / "stat", '#!/bin/sh\nif [ "$2" = "%u:%a" ]; then echo 0:600; else echo 100; fi\n')
            attestation = root / "attestation.json"
            attestation.write_text('{"schema":"fixture"}')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.bind(str(root / "operator.sock"))
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "FACTORY_OPERATOR_ENV": str(env_file),
                   "FACTORY_OPERATOR_SOCKET": str(root / "operator.sock")}
            prefix = [str(ROOT / "factory/factory"), "attempts", "resolve-interrupted", "11111111-2222-4333-8444-555555555555"]
            def run(*args):
                return subprocess.run(prefix + list(args), env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(run("inspect").returncode, 0)
            inspected = run("inspect", "--yes")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertTrue(json.loads(inspected.stdout)["url"].endswith("/interrupted-resolution/inspect"))
            review = ["--attestation", str(attestation), "--reason", "reviewed containment"]
            preview = run("preview", *review)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["body"]["attestation"], {"schema": "fixture"})
            self.assertNotEqual(run("confirm", *review).returncode, 0)
            confirm = run("confirm", *review, "--request-id", "fixture-request", "--snapshot-digest", "a" * 64, "--yes")
            self.assertEqual(confirm.returncode, 0, confirm.stderr)
            self.assertEqual(json.loads(confirm.stdout)["body"]["snapshot_digest"], "a" * 64)
            self.assertNotEqual(run("preview", *review, "--yes").returncode, 0)
            link = root / "link.json"; link.symlink_to(attestation)
            self.assertNotEqual(run("preview", "--attestation", str(link), "--reason", "reviewed").returncode, 0)
            absence = root / "absence.json"
            absence.write_text('{"kind":"github_branch_pr_absence_v1"}')
            review += ["--absence-evidence", str(absence)]
            preview = run("preview", *review)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["body"]["absence_evidence"], {"kind": "github_branch_pr_absence_v1"})
            confirm = run("confirm", *review, "--request-id", "fixture-request", "--snapshot-digest", "a" * 64, "--yes")
            self.assertEqual(confirm.returncode, 0, confirm.stderr)
            self.assertEqual(json.loads(confirm.stdout)["body"]["absence_evidence"], {"kind": "github_branch_pr_absence_v1"})
            self.assertNotEqual(run("inspect", "--yes", "--absence-evidence", str(absence)).returncode, 0)
            self.assertNotEqual(run("preview", *review[:-1], str(link)).returncode, 0)
            absence.write_text('[]')
            self.assertNotEqual(run("preview", *review).returncode, 0)

            self._write_executable(fake_bin / "curl", '#!/bin/sh\necho \'{"created":false,"absence_evidence_target":{"repository":"owner/repo","head_branch":"codex/fixture"}}\'\n')
            self._write_executable(fake_bin / "gh", '''#!/bin/sh
if [ "$1" = "pr" ]; then echo '[]'; exit 0; fi
test "$2" = "repos/owner/repo/branches/codex%2Ffixture" || exit 2
echo 'HTTP 404' >&2
exit 1
''')
            inspected = run("inspect", "--yes")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            observed = json.loads(inspected.stdout)
            self.assertEqual(observed["kind"], "github_branch_pr_absence_v1")
            self.assertEqual(observed["repository"], "owner/repo")
            self.assertEqual(observed["head_branch"], "codex/fixture")
            self.assertIs(observed["branch_exists"], False)
            self.assertEqual(observed["pull_requests"], [])
            self._write_executable(fake_bin / "gh", '#!/bin/sh\necho "HTTP 403" >&2\nexit 1\n')
            self.assertNotEqual(run("inspect", "--yes").returncode, 0)

    def test_analytics_uses_root_operator_and_bounded_window(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn("factory analytics [--days N] [--json]", source)
        self.assertIn('operator_request GET "/v1/analytics?days=$days"', source)
        self.assertIn("format_analytics()", source)
        self.assertIn("analytics --days must be between 1 and 365", source)

    def test_observed_receipt_refresh_uses_the_root_operator(self) -> None:
        source = (ROOT / "factory/factory").read_text(encoding="utf-8")
        operator = (ROOT / "hatchet/bridge/hatchet_hermes_bridge/operator.py").read_text(encoding="utf-8")

        self.assertIn("factory attempts refresh-receipt ATTEMPT-ID --reason TEXT --yes", source)
        self.assertIn('operator_request POST "/v1/attempts/$id/receipt-recovery/refresh"', source)
        self.assertIn('path.endswith("/receipt-recovery/refresh")', operator)

    def test_record_time_converts_minutes_and_rejects_invalid_input_before_request(self) -> None:
        if not hasattr(socket, "AF_UNIX") or shutil.which("jq") is None:
            self.skipTest("Unix sockets and jq required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            env_file = root / "operator.env"
            env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=fixture\n")
            self._write_executable(fake_bin / "id", '#!/bin/sh\necho 0\n')
            self._write_executable(fake_bin / "stat", '#!/bin/sh\necho 0:600\n')
            self._write_executable(fake_bin / "curl", '''#!/usr/bin/env python3
import json, sys
a = sys.argv
with open(a[a.index("--output")+1], "w") as output:
    json.dump({"method": "POST" if "--data" in a else "GET", "url": a[-1], "body": json.loads(a[a.index("--data")+1])}, output)
''')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.bind(str(root / "operator.sock"))
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                   "FACTORY_OPERATOR_ENV": str(env_file), "FACTORY_OPERATOR_SOCKET": str(root / "operator.sock")}
            prefix = [str(ROOT / "factory/factory"), "analytics", "record-time",
                      "11111111-ABCD-4333-8444-555555555555"]

            def run(*args):
                return subprocess.run(prefix + list(args), env=env, capture_output=True, text=True, timeout=5)

            result = run("--minutes", "12.345", "--request-id", "review-1", "--category", "review")
            self.assertEqual(result.returncode, 0, result.stderr)
            request = json.loads(result.stdout)
            self.assertEqual(request["method"], "POST")
            self.assertTrue(request["url"].endswith("/v1/attempts/11111111-abcd-4333-8444-555555555555/human-time"))
            self.assertEqual(request["body"], {"duration_ms": 740700, "category": "review", "request_id": "review-1"})
            for minutes, duration in [("0", 0), ("0.001", 60), ("1440", 86400000)]:
                result = run("--minutes", minutes, "--request-id", "entry-1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["body"], {
                    "duration_ms": duration, "category": "intervention", "request_id": "entry-1"})
            invalid = [
                [], ["--minutes", "1"], ["--minutes"],
                ["--minutes", "1", "--request-id", "entry-1", "--minutes", "2"],
                ["--minutes", "1", "--request-id", "entry-1", "--category", "unknown"],
                ["--minutes", "1", "--request-id", "contains a space"],
                ["--minutes", "1", "--request-id", "entry-1", "--unknown", "yes"],
            ] + [["--minutes", value, "--request-id", "entry-1"] for value in
                 ("-1", "1440.001", "0.0001", "1e3", "nan", "inf", "true", "")]
            for args in invalid:
                with self.subTest(args=args):
                    result = run(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")

    def test_analytics_formatter_preserves_funnel_order_and_discloses_active_cohort(self) -> None:
        if not hasattr(socket, "AF_UNIX") or shutil.which("jq") is None:
            self.skipTest("Unix sockets and jq are required for the analytics formatter")

        factory = ROOT / "factory/factory"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            env_file = tmp_path / "factory-operator.env"
            socket_path = tmp_path / "operator.sock"
            fixture = tmp_path / "analytics.json"
            env_file.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=test-token\n", encoding="utf-8")
            analytics_payload = {
                "schema": "factory-analytics-v1",
                "generated_at": "2026-09-13T00:00:00Z",
                "window": {"days": 30},
                "coverage": {
                    "cohort_work_items": 2,
                    "cohort_attempts": 2,
                    "execution_claimed_attempts": 2,
                    "effect_authorized_attempts": 1,
                    "backend_attribution": {"observed": 0, "eligible": 1, "rate": 0},
                    "configured_auth_label": {"observed": 0, "eligible": 0, "rate": None},
                    "attempt_failure_attribution": {"observed": 0, "eligible": 1, "rate": 0},
                },
                "intake": {
                    "available": True,
                    "linear_events": 2,
                    "eligible_events": 2,
                    "authenticated_eligible_events": 2,
                    "eligible_work_items": 1,
                    "rejected_events": 1,
                    "published_events": 2,
                    "unpublished_events": 0,
                    "repeated_eligible_events": 1,
                    "authenticated_repeated_eligible_events": 1,
                    "repeated_eligible_event_rate": 0.5,
                    "eligible_event_authenticity": {
                        "observed": 2, "eligible": 2, "rate": 1,
                    },
                    "correlation": {
                        "available": True,
                        "eligible_to_real_attempt": {
                            "observed": 2, "eligible": 2, "rate": 1,
                        },
                        "active_attempt_reuse_events": 1,
                        "active_attempt_reuse_rate": 0.5,
                    },
                    "receipts": {"available": False},
                    "publication": {"current_backlog": 0},
                },
                "funnel": {"stages": {
                    "attempted": {"count": 2, "rate_from_attempted": 1},
                    "executor_started": {"count": 2, "rate_from_attempted": 1},
                    "effect_committed": {"count": 1, "rate_from_attempted": 0.5},
                    "draft_pr_available": {"count": 1, "rate_from_attempted": 0.5},
                    "verified_ready": {"count": 0, "rate_from_attempted": 0},
                }},
                "outcomes": {
                    "by_status": {"failed": 1, "running": 1},
                    "active_attempts": 1,
                    "right_censored": True,
                    "github_observation_coverage": None,
                    "open_prs": None,
                    "merged_prs": None,
                    "closed_unmerged_prs": None,
                    "unobserved_prs": None,
                },
                "github": {
                    "authenticated_outcomes_available": False,
                    "closed_unmerged_rates": None,
                },
                "latency_ms": {},
                "failures": {
                    "currently_failed_attempts": 1,
                    "attempts_with_failure_evidence": 1,
                    "recovered_failure_attempts": 0,
                    "by_classification": [],
                    "backend_invocations": {"failed": 0, "by_classification": []},
                    "dispatch_boundaries": {"failed": 0, "by_classification": []},
                    "effect_commit_boundaries": {"failed": 0, "by_classification": []},
                },
                "recovery": {
                    "by_attempt_kind": {},
                    "repair_budgets_exhausted": 0,
                    "failed_attempts_eventually_verified": 0,
                    "failed_attempts_verified_within_horizon": 0,
                    "recovery_maturity_days": 7,
                    "age_qualified_failed_attempts": 0,
                    "settled_failed_attempts": 0,
                    "failed_attempt_recovery_rate": None,
                    "failed_attempt_recovery_outcome_coverage": {
                        "observed": 0, "eligible": 1, "rate": 0,
                    },
                    "right_censored_failed_attempts": 1,
                    "active_recovery_lineages": 1,
                    "immature_failed_attempts": 0,
                    "immature_unrecovered_failed_attempts": 0,
                    "unaged_unrecovered_failed_attempts": 0,
                    "late_observed_attempt_recoveries": 0,
                },
                "safety": {
                    "bridge_submit_deduplicated_events": 0,
                    "unknown_effect_outcomes": 0,
                    "observed_receipt_commit_recovery_incidents": 0,
                    "unresolved_observed_receipts": 0,
                    "observed_receipt_recovery_exhausted": 0,
                    "rejected_observed_receipts": 0,
                    "duplicate_effects_detected": None,
                },
                "human": {"work_items_ready_for_review": 0, "exception_intervention_attempts": 0},
                "effort": {
                    "work_items": 2, "verified_work_items": 1, "attempts": 3, "active_attempts": 1,
                    "execution": {"observed_ms": 60000, "ms_per_verified_work_item": None,
                                  "coverage": {"observed": 1, "eligible": 2, "rate": 0.5, "complete": False}},
                    "human": {"recorded_ms": 0, "recorded_ms_per_verified_work_item": 0,
                              "entries": 1, "recorded_interventions": 0,
                              "attempt_recording_coverage": {"observed": 1, "eligible": 3, "rate": 1 / 3, "complete": False}},
                },
                "backends": [],
                "models": {
                    "coverage": {"observed": 1, "eligible": 2, "rate": 0.5, "complete": False},
                    "unknown_attempts": 1, "attempts_before_invocation": 0,
                    "groups": [{"selected_provider": "openai-codex", "selected_model": "gpt-6.1-sol", "attempts": 1}],
                },
                "lifecycle": {
                    "available": True,
                    "cohort_records": 1,
                    "current_dead_letters": 0,
                    "by_state": {"delivered": 1},
                    "dead_letter_delivery_errors": {},
                },
                "unavailable_metrics": [],
            }
            fixture.write_text(
                json.dumps(analytics_payload, sort_keys=True), encoding="utf-8"
            )
            self._write_executable(
                fake_bin / "id",
                "#!/usr/bin/env sh\nif [ \"$1\" = \"-u\" ]; then echo 0; else /usr/bin/id \"$@\"; fi\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/usr/bin/env sh\ncat \"$FACTORY_TEST_ANALYTICS\"\n",
            )

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(socket_path))
                env = os.environ.copy()
                env.update({
                    "FACTORY_OPERATOR_ENV": str(env_file),
                    "FACTORY_OPERATOR_SOCKET": str(socket_path),
                    "FACTORY_TEST_ANALYTICS": str(fixture),
                    "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                })
                result = subprocess.run(
                    [str(factory), "analytics"],
                    check=True,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                self.assertIn("Executor-selected models (configuration, not provider usage)", result.stdout)
                self.assertIn("openai-codex/gpt-6.1-sol", result.stdout)
                self.assertIn("Backend runtime / verified item  unavailable", result.stdout)
                self.assertIn("Recorded human time / verified   0ms", result.stdout)
                self.assertIn("unrecorded work is unknown", result.stdout)
                analytics_payload.pop("models")  # Older operators remain compatible.
                analytics_payload.pop("effort")
                analytics_payload["intake"]["correlation"] = {"available": False}
                fixture.write_text(
                    json.dumps(analytics_payload, sort_keys=True), encoding="utf-8"
                )
                unavailable_result = subprocess.run(
                    [str(factory), "analytics"],
                    check=True,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
            finally:
                sock.close()

            output = result.stdout
            stages = ["attempted", "executor started", "effect committed", "draft pr available", "verified ready"]
            positions = [output.index(f"  {stage}  ") for stage in stages]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("Attempt status (attempt grain)", output)
            self.assertIn("running  1", output)
            self.assertIn("outcome rates are right-censored", output)
            self.assertIn("Raw repeated Ready observations 1", output)
            self.assertIn("Authenticated repeated Ready rate 1/2  50%", output)
            self.assertIn("Active-attempt reuse rate      1/2  50%", output)
            self.assertIn("GitHub outcomes unavailable.", output)
            self.assertNotIn("merged 0", output)
            self.assertIn(
                "Authenticated Ready correlation unavailable.",
                unavailable_result.stdout,
            )
            self.assertNotIn("Active-attempt reuse rate", unavailable_result.stdout)
            self.assertNotIn("Time per verified work item", unavailable_result.stdout)
            self.assertNotIn(
                "Authenticated repeated Ready rate", unavailable_result.stdout
            )

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
        self.assertIn('http_status "Factory console" http://127.0.0.1:19881/health "200" required', source)
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
                "unit=\n"
                "for arg in \"$@\"; do unit=\"$arg\"; done\n"
                "case \"$1\" in\n"
                "  is-active)\n"
                "    case \"$unit\" in driftward-factory.service|driftward-hermes-factory-gateway.service|driftward-hermes.service) echo active ;; *) echo inactive ;; esac\n"
                "    ;;\n"
                "  is-enabled)\n"
                "    case \"$unit\" in driftward-factory.service|driftward-hermes-factory-gateway.service|driftward-hermes.service) echo enabled ;; *) echo disabled ;; esac\n"
                "    ;;\n"
                "  list-timers) ;;\n"
                "esac\n",
            )
            self._write_executable(
                fake_bin / "docker",
                "#!/usr/bin/env sh\n"
                "case \" $* \" in\n"
                "  *' inspect '*'org.opencontainers.image.version'*) printf 'test|0123456789012345678901234567890123456789\\n' ;;\n"
                "  *' inspect '*) printf 'running|false|0|healthy|driftward/factory:test\\n' ;;\n"
                "  *' ps -q '*) printf 'cid-test\\n' ;;\n"
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
                "if echo \" $* \" | grep -q '19877/health' && echo \" $* \" | grep -q -- '--write-out'; then printf '200'; exit 0; fi\n"
                "case \" $* \" in\n"
                "  *'reconciliation/findings'*) printf '{\"findings\":{\"current\":0,\"historical\":0,\"severity\":{\"critical\":0,\"warning\":0},\"scope\":{\"active_work\":0,\"terminal_work\":0,\"system\":0},\"actionable\":0,\"actionable_severity\":{\"critical\":0,\"warning\":0}}}' ;;\n"
                "  *'19877/health'*) printf '{\"release_commit\":\"0123456789012345678901234567890123456789\"}' ;;\n"
                "  *'19880/status'*) printf '{\"outbox\":{\"dead_letters\":0},\"last_success\":1}\\n200' ;;\n"
                "  *' --write-out '*) printf '200' ;;\n"
                "  *) printf '{}' ;;\n"
                "esac\n",
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
            self._write_executable(
                fake_bin / "hatchet",
                "#!/usr/bin/env sh\n"
                "case \" $* \" in\n"
                "  *' workflows list '*) printf '{\"rows\":[]}' ;;\n"
                "  *' worker list '*) printf '{\"rows\":[]}' ;;\n"
                "  *' cron list '*) printf '{\"rows\":[{\"workflowName\":\"reconciliation-bridge-state-check\",\"cron\":\"*/15 * * * *\",\"enabled\":true}]}' ;;\n"
                "esac\n",
            )

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
                check=False,
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Linear webhook readiness", result.stdout)
            self.assertIn("Linear webhook public readiness", result.stdout)
            self.assertIn("url=https://factory.test/ready", result.stdout)
            logged_curl_args = curl_args.read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:19879/ready", logged_curl_args)
            self.assertIn("https://factory.test/ready", logged_curl_args)

    def test_compose_honors_protected_worker_mode_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("- path: ${FACTORY_ENV_DIR:-/etc/driftward}/hatchet-workers.env", compose)
        self.assertIn("- path: ${FACTORY_ENV_DIR:-/etc/driftward}/hatchet-worker-bridge-executor.env", compose)
        self.assertIn("/run/driftward-hermes-factory:/run/driftward-hermes-factory", compose)
        self.assertIn("HATCHET_FACTORY_OPERATOR_RUNTIME: docker", compose)
        self.assertIn("HATCHET_FACTORY_OPERATOR_INGRESS_DB: /var/lib/driftward-hatchet-event-ingress/ingress.db", compose)
        self.assertIn("HATCHET_FACTORY_OPERATOR_LIFECYCLE_DB: /var/lib/driftward-factory-lifecycle-writer/outbox.db", compose)
        self.assertIn("factory-bridge-data:/var/lib/driftward-hatchet-bridge", compose)
        self.assertIn("factory-ingress-data:/var/lib/driftward-hatchet-event-ingress:ro", compose)
        self.assertIn("factory-lifecycle-data:/var/lib/driftward-factory-lifecycle-writer", compose)
        self.assertIn("name: ${FACTORY_PROJECT_NAME:-driftward-factory}-bridge-data", compose)
        self.assertIn("name: ${FACTORY_PROJECT_NAME:-driftward-factory}-ingress-data", compose)
        self.assertNotIn("./data/bridge:/var/lib/driftward-hatchet-bridge", compose)
        self.assertNotIn("./data/ingress:/var/lib/driftward-hatchet-event-ingress", compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertNotIn("/run/docker.sock", compose)
        self.assertNotIn("HATCHET_INTAKE_DRY_RUN: ${HATCHET_INTAKE_DRY_RUN", compose)
        self.assertNotIn("DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED: ${DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED", compose)

    def test_factory_console_is_read_only_loopback_and_tailscale_authenticated(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        console = (ROOT / "hatchet/bridge/hatchet_hermes_bridge/console.py").read_text(encoding="utf-8")
        cli = (ROOT / "factory/factory").read_text(encoding="utf-8")

        self.assertIn('"--console-host", "0.0.0.0", "--console-port", "19881"', compose)
        self.assertIn('"127.0.0.1:19881:19881"', compose)
        self.assertIn("curl -fsS http://127.0.0.1:19881/health", compose)
        self.assertIn('headers.get("Tailscale-User-Login")', console)
        self.assertIn("FastAPI(", console)
        self.assertIn('@app.get("/api/overview")', console)
        self.assertIn('@app.get("/api/lifecycle/{issue}")', console)
        self.assertNotIn("@app.post(", console)
        self.assertIn("show_console()", cli)

    def test_compose_allows_docker_ingress_only_for_reconciliation_worker(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        intake = compose.split("\n  intake-worker:", 1)[1].split("\n  execution-dispatch-worker:", 1)[0]
        reconciliation = compose.split("\n  reconciliation-worker:", 1)[1].split("\n  factory-lifecycle-writer:", 1)[0]

        self.assertNotIn("HATCHET_RECONCILIATION_ALLOW_DOCKER_INGRESS", intake)
        self.assertIn('HATCHET_RECONCILIATION_ALLOW_DOCKER_INGRESS: "1"', reconciliation)

    def test_compose_accepts_pinned_factory_image_env(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        service = (ROOT / "deploy/systemd/driftward-factory.service").read_text(encoding="utf-8")

        self.assertIn("image: ${FACTORY_IMAGE:-driftward/factory:${FACTORY_IMAGE_TAG:-local}}", compose)
        self.assertIn("build:", compose)
        self.assertIn("Environment=COMPOSE_PROJECT_NAME=driftward-factory", service)
        self.assertIn("Environment=FACTORY_SOURCE_ROOT=__SOURCE_ROOT__", service)
        self.assertIn("driftward-hermes-factory-gateway.service", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("RemainAfterExit=yes", service)
        self.assertIn("ExecReload=/usr/bin/docker compose", service)
        self.assertIn("ExecStop=/usr/bin/docker compose", service)
        self.assertIn(" stop", service)
        self.assertNotIn(" down", service)
        self.assertNotIn("Environment=FACTORY_IMAGE_TAG=", service)
        self.assertNotIn("Environment=FACTORY_IMAGE=", service)

    def test_compose_has_truthful_health_tuning_and_durable_ingress_replay(self) -> None:
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("command: postgres -c 'max_connections=100'", compose)
        self.assertIn("mem_limit: 2g", compose)
        self.assertIn("DATABASE_MAX_CONNS: ${HATCHET_DATABASE_MAX_CONNS:-20}", compose)
        self.assertIn('http://127.0.0.1:8733/ready', compose)
        self.assertIn("dispatch-pending --limit 50", compose)
        self.assertIn("factory-ingress-data:/var/lib/driftward-hatchet-event-ingress", compose)
        self.assertNotIn("dispatch-factory-outbox --limit 50 || true", compose)

    def test_release_image_embeds_oci_and_bridge_provenance(self) -> None:
        dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertIn("FACTORY_RELEASE_COMMIT", dockerfile)
        self.assertIn("/app/hatchet/bridge/RELEASE_METADATA.json", dockerfile)
        self.assertIn("/app/hatchet/bridge/requirements.txt", dockerfile)
        self.assertIn("/opt/driftward-factory/bin", dockerfile)

    def test_install_docker_persists_factory_image_selection(self) -> None:
        source = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")

        self.assertIn("--image-tag TAG", source)
        self.assertIn("--image IMAGE", source)
        self.assertIn('factory_image_dropin_dir="${service_unit}.d"', source)
        self.assertIn('factory_image_override="${FACTORY_IMAGE:-}"', source)
        self.assertIn("Environment=FACTORY_IMAGE_TAG=%s", source)
        self.assertIn("Environment=FACTORY_IMAGE=%s", source)
        self.assertIn("env -u FACTORY_IMAGE -u FACTORY_IMAGE_TAG", source)
        self.assertIn('--build-arg "FACTORY_RELEASE_COMMIT=$factory_release_commit"', source)
        self.assertIn('-f "$repo_root/deploy/Dockerfile" -t "$factory_image" "$repo_root"', source)
        self.assertIn('systemctl reload "$service_name"', source)
        self.assertIn("rollback_factory_runtime()", source)
        self.assertIn('systemctl disable --now "$legacy_unit"', source)
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
        self.assertIn('bridge_env_file="${FACTORY_HATCHET_BRIDGE_ENV:-/etc/driftward/hatchet-bridge.env}"', source)
        self.assertIn('reconciliation_env_file="${DRIFTWARD_HATCHET_WORKER_RECONCILIATION_ENV_FILE:-/etc/driftward/hatchet-worker-reconciliation.env}"', source)
        self.assertIn('hatchet_workers_env_file="${FACTORY_HATCHET_WORKERS_ENV:-/etc/driftward/hatchet-workers.env}"', source)
        self.assertIn("hatchet_postgres_volume=driftward-factory-hatchet-postgres-data", source)
        self.assertIn("compose_service_file_exists()", source)
        self.assertIn("require_compose_volume_file postgres /var/lib/postgresql/data/PG_VERSION", source)
        self.assertIn("require_legacy_file_migrated bridge /var/lib/driftward-hatchet-bridge/bridge.db", source)
        self.assertNotIn('"$runtime_root/data/bridge" \\', source)
        self.assertNotIn('Hatchet Postgres data is missing under $runtime_root/data/hatchet/postgres', source)
        self.assertIn('operator_dry_run="$(awk -F= ', source)
        self.assertIn('upsert_env "$factory_operator_env_file" HATCHET_INTAKE_DRY_RUN "$operator_dry_run"', source)
        self.assertIn('ensure_env_secret "$bridge_env_file" HATCHET_HERMES_INSPECTION_TOKEN', source)
        self.assertIn('ensure_env_secret "$bridge_env_file" HATCHET_HERMES_RECOVERY_TOKEN', source)
        self.assertIn('upsert_env "$bridge_executor_env_file" HATCHET_HERMES_INSPECTION_TOKEN "$(env_file_value "$bridge_env_file" HATCHET_HERMES_INSPECTION_TOKEN)"', source)
        self.assertIn('upsert_env "$reconciliation_env_file" HATCHET_HERMES_RECOVERY_TOKEN "$(env_file_value "$bridge_env_file" HATCHET_HERMES_RECOVERY_TOKEN)"', source)

    def test_hermes_gateway_systemd_unit_is_local_socket_boundary(self) -> None:
        unit = (ROOT / "deploy/systemd/driftward-hermes-factory-gateway.service").read_text(encoding="utf-8")

        self.assertIn("DRIFTWARD_HERMES_GRPC_TARGET=unix:///run/driftward-hermes-factory/hermes.sock", unit)
        self.assertIn("ExecStart=__NODE_BIN__ __FACTORY_SHARE_DIR__/hermes-gateway/hermes-gateway-server.js", unit)
        self.assertIn("ReadWritePaths=/run/driftward-hermes-factory /var/lib/driftward-hermes-factory /var/lib/driftward-factory-reviewer /run/docker.sock /var/run/docker.sock", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertNotIn("OPENAI_API_KEY", unit)
        self.assertNotIn("ANTHROPIC_API_KEY", unit)

    def test_installers_neutralize_secret_shaped_execution_labels(self) -> None:
        for relative in ("deploy/install-docker.sh", "hatchet/workers/install.sh"):
            with self.subTest(installer=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("analytics_label_secret_shaped()", source)
                self.assertIn("normalize_analytics_label()", source)
                self.assertIn("sk-", source)
                self.assertIn("github_pat_", source)
                self.assertIn("upsert_env \"$file\" \"$key\" unknown", source)
                self.assertIn("remove_env \"$file\" \"$key\"", source)
                for key in (
                    "DRIFTWARD_HERMES_AUTH_MODE",
                    "DRIFTWARD_HERMES_VERSION",
                ):
                    self.assertIn(f'normalize_analytics_label "$bridge_executor_env', source)
                    self.assertIn(key, source)
                for key in ("DRIFTWARD_HERMES_MODEL", "DRIFTWARD_HERMES_PROVIDER"):
                    self.assertRegex(source, rf'remove_env "\$bridge_executor_env(?:_file)?" {key}')

                # Exercise the real shell helpers with a harmless install
                # shim. A valid first definition followed by a secret-shaped
                # duplicate must collapse to one canonical value; otherwise
                # systemd/Compose would use the unsafe last definition.
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "executor.env"
                    target.write_text(
                        "DRIFTWARD_HERMES_MODEL=gpt-5.5\n"
                        "DRIFTWARD_HERMES_MODEL=sk-proj-FAKE_DUPLICATE\n"
                        "DRIFTWARD_HERMES_VERSION=1.2.3\n"
                        "DRIFTWARD_HERMES_VERSION=eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE\n"
                        "KEEP=this\n",
                        encoding="utf-8",
                    )
                    subprocess.run(
                        [
                            "bash",
                            "-c",
                            r'''
set -euo pipefail
install() {
  local args=("$@")
  local count="${#args[@]}"
  cp -- "${args[$((count - 2))]}" "${args[$((count - 1))]}"
}
eval "$(awk '/^upsert_env\(\)/ { capture=1 } /^ensure_env_secret\(\)/ { capture=0 } capture { print }' "$INSTALLER")"
remove_env "$TARGET" DRIFTWARD_HERMES_MODEL
normalize_analytics_label "$TARGET" DRIFTWARD_HERMES_VERSION version ""
''',
                        ],
                        check=True,
                        env={
                            **os.environ,
                            "INSTALLER": str(ROOT / relative),
                            "TARGET": str(target),
                        },
                        text=True,
                        capture_output=True,
                    )
                    lines = target.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(
                        [line for line in lines if line.startswith("DRIFTWARD_HERMES_MODEL=")],
                        [],
                    )
                    self.assertIn("KEEP=this", lines)
                    self.assertFalse(any("sk-proj-" in line for line in lines))
                    self.assertEqual(
                        [line for line in lines if line.startswith("DRIFTWARD_HERMES_VERSION=")],
                        ["DRIFTWARD_HERMES_VERSION=1.2.3"],
                    )
                    self.assertFalse(any("eyJ" in line for line in lines))
        docker = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")
        self.assertIn('remove_env "$bridge_executor_env_file" FACTORY_RELEASE_VERSION', docker)

    def test_docker_installer_rejects_secret_shaped_image_labels_before_use(self) -> None:
        source = (ROOT / "deploy/install-docker.sh").read_text(encoding="utf-8")
        validation_prefix = source.split('factory_release_commit="', 1)[0]
        for secret in (
            "sk-proj-FAKE_IMAGE_LABEL",
            "eyJhbGciOiJSUzI1NiJ9.eyJsubIjoiFAKE.OAUTH_TOKEN_VALUE",
            ("Ab7_" * 50) + ".xY9",
        ):
            for arguments in (
                ("--image-tag", secret),
                ("--image", f"registry.example/driftward/factory:{secret}"),
            ):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        ["bash", "-s", "--", *arguments],
                        input=validation_prefix,
                        env={**os.environ, "FACTORY_IMAGE_TAG": "local", "FACTORY_IMAGE": ""},
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, 64)
                    output = result.stdout + result.stderr
                    self.assertIn("credential-like material", output)
                    self.assertNotIn(secret, output)

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
        self.assertIn('FACTORY_RELEASE_COMMIT="$(git rev-parse HEAD)"', workflow)
        self.assertIn('FACTORY_RELEASE_VERSION="$RELEASE_TAG"', workflow)
        self.assertIn("-f deploy/Dockerfile -t driftward/factory:ci .", workflow)
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
