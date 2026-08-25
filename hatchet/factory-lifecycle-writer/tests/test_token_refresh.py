from __future__ import annotations

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class TokenRefreshTest(unittest.TestCase):
    def test_credentials_can_come_from_container_env(self) -> None:
        sys.path.insert(0, str(ROOT))
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "FACTORY_LINEAR_CLIENT_ID": "client-id",
                    "FACTORY_LINEAR_CLIENT_SECRET": "client-secret",
                },
                clear=True,
            ):
                script = runpy.run_path(str(ROOT / "factory-lifecycle-token-refresh"), run_name="token_refresh_test")
                self.assertEqual(script["credential"]("factory-client-id"), "client-id")
                self.assertEqual(script["credential"]("factory-client-secret"), "client-secret")
        finally:
            sys.path.remove(str(ROOT))

    def test_publish_is_atomic_private_and_preserves_non_token_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "writer.env"
            env.write_text("HATCHET_HERMES_BRIDGE_TOKEN=bridge\nLINEAR_FACTORY_ACCESS_TOKEN=old\n", encoding="utf-8")
            sys.path.insert(0, str(ROOT))
            try:
                with mock.patch.dict(os.environ, {"FACTORY_LIFECYCLE_ENV_FILE": str(env)}, clear=False):
                    script = runpy.run_path(str(ROOT / "factory-lifecycle-token-refresh"), run_name="token_refresh_test")
                    self.assertTrue(script["publish_token"]("new-token"))
                    self.assertFalse(script["publish_token"]("new-token"))
            finally:
                sys.path.remove(str(ROOT))
            self.assertEqual(env.read_text(encoding="utf-8"), "HATCHET_HERMES_BRIDGE_TOKEN=bridge\nLINEAR_FACTORY_ACCESS_TOKEN=new-token\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
