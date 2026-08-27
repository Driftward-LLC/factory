#!/usr/bin/env python3
"""Static checks for Factory Docker image packaging."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FactoryDockerImageStaticTests(unittest.TestCase):
    def test_outbox_trigger_is_bundled_in_image_build(self) -> None:
        source = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

        self.assertIn("npm exec --prefix hatchet/workers -- esbuild", source)
        self.assertIn("/app/hatchet/workers/bridge-executor-worker/src/triggers/push-accepted-event.ts", source)
        self.assertIn("--bundle", source)
        self.assertIn("--outfile=/app/worker-triggers/push-accepted-event.js", source)
        self.assertNotIn("cp /app/hatchet/workers/bridge-executor-worker/dist/triggers/push-accepted-event.js", source)
        self.assertLess(source.index("--outfile=/app/worker-triggers/push-accepted-event.js"), source.index("npm prune --omit=dev --prefix hatchet/workers"))


if __name__ == "__main__":
    unittest.main()
