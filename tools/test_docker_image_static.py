#!/usr/bin/env python3
"""Static checks for Factory Docker image packaging."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FactoryDockerImageStaticTests(unittest.TestCase):
    def test_dependency_stages_include_every_workspace_before_installing(self) -> None:
        source = (ROOT / 'deploy/Dockerfile').read_text()
        for name, root in [('console-dependencies', 'factory/console'), ('worker-dependencies', 'hatchet/workers')]:
            with self.subTest(stage=name):
                stage = source.split(f'FROM node AS {name}\n', 1)[1].split('\nFROM ', 1)[0]
                install = stage.index(f'RUN npm ci --prefix {root}')
                self.assertIn(f'COPY {root}/package.json {root}/package-lock.json {root}/', stage[:install])
                workspaces = json.loads((ROOT / root / 'package.json').read_text())['workspaces']
                for workspace in workspaces:
                    self.assertIn(f'COPY {root}/{workspace}/package.json {root}/{workspace}/', stage[:install])
                self.assertNotIn('COPY . .', stage)
                # Copy the complete installed tree, including any dependencies
                # npm nests inside workspaces instead of hoisting to the root.
                self.assertIn(f'COPY --from={name} /app/{root}/ {root}/', source)
        assembled = source.split(' AS assembled\n', 1)[1].split('FROM scratch', 1)[0]
        self.assertNotIn('--from=build', assembled)
        self.assertIn('COPY hatchet/bridge/requirements.txt /tmp/factory-requirements.txt', assembled)

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
