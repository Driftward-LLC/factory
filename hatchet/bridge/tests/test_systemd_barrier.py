import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SystemdBarrierHarnessTests(unittest.TestCase):
    def test_executable_harness_reproduces_precedence_and_condition_gate(self):
        result = subprocess.run(["python3", str(ROOT / "bridge" / "tests" / "systemd-barrier-harness.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "runtime_mask_etc_precedence": "reproduced", "dropin_activation_barrier": "verified", "recovery_failure_retains_barrier": "verified"})
