#!/usr/bin/env python3
"""Tests for the Factory Hatchet desired-state doctor."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "tools/factory-hatchet-doctor.py"
DESIRED = ROOT / "hatchet/desired-state/factory-hatchet.json"
CATALOG = ROOT / "hatchet/workers/catalog.json"


class FactoryHatchetDoctorTests(unittest.TestCase):
    def test_clean_live_inventory_matches_desired_state(self) -> None:
        live = clean_live_inventory()
        result = run_doctor(live)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "clean")
        self.assertEqual(payload["findings"], [])

    def test_reports_retired_unknown_and_no_worker_drift(self) -> None:
        live = clean_live_inventory()
        live["workflows"].extend(
            [
                {
                    "name": "first-workflow",
                    "active": True,
                    "tasks": ["quickstart-step"],
                },
                {
                    "name": "inspect-pr-event-smoke",
                    "active": True,
                    "tasks": ["inspect-pr-event"],
                },
            ]
        )
        for action in live["taskActions"]:
            if action["name"] == "normalize-pr-event":
                action["activeWorkers"] = 0
                action["workers"] = []
        live["taskActions"].append(
            {
                "name": "inspect-pr-event",
                "active": True,
                "activeWorkers": 0,
                "workers": [],
            }
        )

        result = run_doctor(live)

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        kinds = {finding["kind"] for finding in payload["findings"]}
        self.assertIn("retired_workflow_still_active", kinds)
        self.assertIn("unknown_active_workflow", kinds)
        self.assertIn("task_action_without_repo_worker", kinds)
        self.assertIn("unknown_active_task_action", kinds)

    def test_collects_live_inventory_from_hatchet_profile(self) -> None:
        live = clean_live_inventory()
        workflows = [
            {
                "name": workflow["name"],
                "isPaused": False,
            }
            for workflow in live["workflows"]
        ]
        workers = []
        for worker in live["workers"]:
            workflow_by_task = {
                task: workflow["name"]
                for workflow in live["workflows"]
                for task in workflow["tasks"]
            }
            workers.append(
                {
                    "name": worker["name"],
                    "status": "ACTIVE",
                    "actions": [
                        f"{workflow_by_task[task]}:{task}"
                        for task in worker["tasks"]
                    ],
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            fake_hatchet = Path(tmp) / "hatchet"
            fake_hatchet.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"workflows = {json.dumps({'rows': workflows})!r}\n"
                f"workers = {json.dumps({'rows': workers})!r}\n"
                "if sys.argv[1:3] == ['workflows', 'list']:\n"
                "    print(workflows)\n"
                "elif sys.argv[1:3] == ['worker', 'list']:\n"
                "    print(workers)\n"
                "else:\n"
                "    sys.exit(2)\n",
                encoding="utf-8",
            )
            fake_hatchet.chmod(fake_hatchet.stat().st_mode | 0o700)

            result = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR),
                    "--desired",
                    str(DESIRED),
                    "--hatchet-bin",
                    str(fake_hatchet),
                    "--profile",
                    "driftward",
                    "--json",
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["live"], "hatchet profile driftward")
        self.assertEqual(payload["findings"], [])

    def test_desired_workflow_names_match_worker_catalog(self) -> None:
        desired = json.loads(DESIRED.read_text(encoding="utf-8"))
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

        desired_workflows = {item["name"] for item in desired["active_workflows"]}
        catalog_workflows = {item["name"] for item in catalog["workflows"] if item["status"] == "source_added"}

        self.assertEqual(desired_workflows, catalog_workflows)


def clean_live_inventory() -> dict[str, object]:
    desired = json.loads(DESIRED.read_text(encoding="utf-8"))
    task_actions = []
    for worker in desired["active_workers"]:
        for task in worker["tasks"]:
            task_actions.append(
                {
                    "name": task,
                    "active": True,
                    "activeWorkers": 1,
                    "workers": [worker["name"]],
                }
            )
    return {
        "workflows": [
            {
                "name": workflow["name"],
                "active": True,
                "worker": workflow["worker"],
                "tasks": workflow["tasks"],
                "eventTriggers": workflow["event_triggers"],
            }
            for workflow in desired["active_workflows"]
        ],
        "workers": [
            {
                "name": worker["name"],
                "active": True,
                "tasks": worker["tasks"],
                "workflows": worker["workflows"],
            }
            for worker in desired["active_workers"]
        ],
        "taskActions": task_actions,
    }


def run_doctor(live: dict[str, object]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        live_path = Path(tmp) / "live.json"
        live_path.write_text(json.dumps(live), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(DOCTOR),
                "--desired",
                str(DESIRED),
                "--live-json",
                str(live_path),
                "--json",
            ],
            text=True,
            capture_output=True,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
