"""Old installed smoke entry points must refuse before touching any service."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class RetiredSmokeTests(unittest.TestCase):
    def test_legacy_smoke_refuses_without_external_commands(self):
        script = Path(__file__).resolve().parents[2] / 'driftward-hatchet-smoke'
        with tempfile.TemporaryDirectory() as directory:
            # An empty PATH proves the refusal needs no service, network or filesystem command.
            result = subprocess.run(['/bin/bash', str(script)], env={**os.environ, 'PATH': directory}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, '')
        self.assertIn('factory-docker-smoke', result.stderr)
