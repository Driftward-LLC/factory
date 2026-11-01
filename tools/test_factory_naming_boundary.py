"""Historical source installers must refuse before invoking host commands."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = (
    "deploy/install-docker.sh", "hatchet/install.sh", "hatchet/bridge/install.sh",
    "hatchet/workers/install.sh", "hatchet/event-ingress/install.sh",
    "hatchet/event-ingress/install-public-webhook.sh",
    "hatchet/factory-lifecycle-writer/install.sh",
)


class NamingBoundaryTests(unittest.TestCase):
    def test_source_installers_refuse_without_invoking_host_commands(self):
        with tempfile.TemporaryDirectory() as empty:
            for installer in INSTALLERS:
                with self.subTest(installer=installer):
                    result = subprocess.run(
                        ["/bin/bash", str(ROOT / installer), "--start"],
                        env={**os.environ, "PATH": empty}, cwd=empty,
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 64, result.stderr)
                    self.assertIn("Source installation is disabled", result.stderr)
                    self.assertIn("NAMING-MIGRATION.md", result.stderr)
                    self.assertEqual(list(Path(empty).iterdir()), [])
