import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "bridge" / "tests" / "installer-failpoint-harness.sh"
STAGES = (
    "predeploy", "staging", "writer-quiesce", "backup", "disposable-migration",
    "live-migration", "config-publication", "policy-publication", "routing-publication",
    "unit-publication", "release-publication", "symlink-switch", "daemon-reload",
    "restart", "health",
)


class InstallerFailpointHarnessTests(unittest.TestCase):
    def run_harness(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(overrides)
        return subprocess.run(["bash", str(HARNESS)], text=True, capture_output=True, env=env)

    def test_normal_path_traverses_the_complete_mocked_state_machine(self):
        result = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stderr)
        for stage in STAGES:
            self.assertIn(f"enter:{stage}", result.stdout)
            self.assertIn(f"pass:{stage}", result.stdout)
        self.assertIn("complete", result.stdout)
        self.assertIn("cleanup:0", result.stdout)

    def test_each_supported_failpoint_stops_at_its_named_stage(self):
        for index, stage in enumerate(STAGES):
            with self.subTest(stage=stage):
                result = self.run_harness(HATCHET_BRIDGE_INSTALL_FAIL_AT=stage)
                self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
                self.assertIn(f"enter:{stage}", result.stdout)
                self.assertNotIn(f"pass:{stage}", result.stdout)
                self.assertIn(f"injected installer failure: {stage}", result.stderr)
                self.assertIn("cleanup:97", result.stdout)
                for later in STAGES[index + 1 :]:
                    self.assertNotIn(f"enter:{later}", result.stdout)

    def test_nonmatching_stage_and_conditional_function_contexts_succeed(self):
        result = self.run_harness(
            HATCHET_BRIDGE_HARNESS_MODE="conditional-contexts",
            HATCHET_BRIDGE_INSTALL_FAIL_AT="health",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("conditional:staging:pass", result.stdout)
        self.assertIn("conditional-function:backup:pass", result.stdout)
        self.assertIn("complete", result.stdout)

    def test_matching_failpoint_is_preserved_when_function_is_used_as_conditional(self):
        result = self.run_harness(
            HATCHET_BRIDGE_HARNESS_MODE="conditional-contexts",
            HATCHET_BRIDGE_INSTALL_FAIL_AT="backup",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("conditional-function:backup:fail:97", result.stdout)
        self.assertIn("complete", result.stdout)

    def test_unknown_failpoint_rejects_before_the_state_machine(self):
        result = self.run_harness(HATCHET_BRIDGE_INSTALL_FAIL_AT="not-a-stage")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown bridge installer failpoint: not-a-stage", result.stderr)
        self.assertNotIn("enter:", result.stdout)

    def test_production_installer_rejects_unknown_failpoint_before_setup(self):
        result = subprocess.run(
            ["bash", str(ROOT / "bridge" / "install.sh")],
            text=True,
            capture_output=True,
            env={**os.environ, "HATCHET_BRIDGE_INSTALL_FAIL_AT": "not-a-stage"},
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown bridge installer failpoint: not-a-stage", result.stderr)

    def test_signal_runs_exit_cleanup_without_changing_failpoint_semantics(self):
        result = self.run_harness(HATCHET_BRIDGE_HARNESS_SIGNAL_AT="predeploy")
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertIn("pass:predeploy", result.stdout)
        self.assertIn("signal:TERM", result.stdout)
        self.assertIn("cleanup:143", result.stdout)


if __name__ == "__main__":
    unittest.main()
