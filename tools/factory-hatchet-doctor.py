#!/usr/bin/env python3
"""Read-only Factory Hatchet desired-state drift checker."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESIRED = ROOT / "hatchet" / "desired-state" / "factory-hatchet.json"


@dataclass(frozen=True)
class Workflow:
    name: str
    active: bool = True
    worker: str | None = None
    tasks: tuple[str, ...] = ()
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class Worker:
    name: str
    active: bool = True
    tasks: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskAction:
    name: str
    active: bool = True
    workers: tuple[str, ...] = ()
    active_workers: int | None = None


@dataclass(frozen=True)
class EventTrigger:
    workflow: str
    event: str
    active: bool = True


@dataclass
class LiveInventory:
    workflows: list[Workflow] = field(default_factory=list)
    workers: list[Worker] = field(default_factory=list)
    task_actions: list[TaskAction] = field(default_factory=list)
    event_triggers: list[EventTrigger] = field(default_factory=list)
    has_workflows: bool = False
    has_workers: bool = False
    has_task_actions: bool = False
    has_event_triggers: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desired", default=str(DEFAULT_DESIRED), help="desired-state manifest path")
    parser.add_argument("--live-json", help="sanitized live Hatchet inventory JSON path")
    parser.add_argument("-p", "--profile", help="Hatchet profile for read-only live inventory collection")
    parser.add_argument("--hatchet-bin", default="hatchet", help="Hatchet CLI binary used with --profile")
    parser.add_argument("--workflow-limit", type=int, default=200, help="maximum workflows to collect with --profile")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--fail-on-warning", action="store_true", help="return nonzero for warnings as well as critical findings")
    args = parser.parse_args(argv)
    if args.live_json and args.profile:
        parser.error("--live-json and --profile are mutually exclusive")

    desired_path = Path(args.desired)
    desired = load_json(desired_path)
    findings: list[dict[str, Any]] = []
    validate_desired(desired, findings)

    live_path = Path(args.live_json) if args.live_json else None
    if live_path:
        live = normalize_live(load_json(live_path))
        findings.extend(compare_live(desired, live))
        live_ref = str(live_path)
    elif args.profile:
        live = collect_live(args.hatchet_bin, args.profile, args.workflow_limit)
        findings.extend(compare_live(desired, live))
        live_ref = f"hatchet profile {args.profile}"
    else:
        live_ref = None

    critical_count = sum(1 for finding in findings if finding["severity"] == "critical")
    warning_count = sum(1 for finding in findings if finding["severity"] == "warning")
    result = {
        "status": "drift" if findings else "clean",
        "desired": str(desired_path),
        "live": live_ref,
        "desiredWorkflowCount": len(active_workflows(desired)),
        "desiredWorkerCount": len(active_workers(desired)),
        "findingCount": len(findings),
        "criticalCount": critical_count,
        "warningCount": warning_count,
        "findings": findings,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_text(result)

    if critical_count > 0 or (args.fail_on_warning and warning_count > 0):
        return 1
    return 0


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"factory doctor: missing JSON file: {path}") from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"factory doctor: invalid JSON in {path}: {error}") from None


def collect_live(hatchet_bin: str, profile: str, workflow_limit: int) -> LiveInventory:
    limit = max(1, min(workflow_limit, 1000))
    workflows = run_hatchet_json([
        hatchet_bin,
        "workflows",
        "list",
        "-o",
        "json",
        "-p",
        profile,
        "--limit",
        str(limit),
    ])
    workers = run_hatchet_json([
        hatchet_bin,
        "worker",
        "list",
        "-o",
        "json",
        "-p",
        profile,
    ])
    return normalize_live({
        "workflows": rows(workflows),
        "workers": rows(workers),
    })


def run_hatchet_json(command: list[str]) -> Any:
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=20)
    except FileNotFoundError:
        raise SystemExit(f"factory doctor: Hatchet CLI not found: {command[0]}") from None
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"factory doctor: Hatchet CLI command failed ({' '.join(command)}){detail}") from None
    except subprocess.TimeoutExpired:
        raise SystemExit(f"factory doctor: Hatchet CLI command timed out: {' '.join(command)}") from None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"factory doctor: Hatchet CLI returned invalid JSON ({' '.join(command)}): {error}") from None


def rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("rows", "data", "items", "results"):
            rows_value = value.get(key)
            if isinstance(rows_value, list):
                return rows_value
    return []


def validate_desired(desired: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    workflows = active_workflows(desired)
    workers = active_workers(desired)
    worker_names = set(workers)
    task_owners = task_owner_map(desired)

    for workflow in workflows.values():
        worker = workflow.get("worker")
        if not isinstance(worker, str) or worker not in worker_names:
            findings.append(finding(
                "critical",
                "desired_workflow_worker_missing",
                f"desired workflow {workflow.get('name')} references a worker absent from active_workers",
                {"workflow": workflow.get("name"), "worker": worker},
            ))
        for task in strings(workflow.get("tasks")):
            if task not in task_owners:
                findings.append(finding(
                    "critical",
                    "desired_workflow_task_unowned",
                    f"desired workflow {workflow.get('name')} declares task {task} without an active worker owner",
                    {"workflow": workflow.get("name"), "task": task},
                ))


def compare_live(desired: dict[str, Any], live: LiveInventory) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    desired_workflows = active_workflows(desired)
    retired_workflows = retired_workflow_names(desired)
    desired_workers = active_workers(desired)
    desired_worker_names = set(desired_workers)
    task_owners = task_owner_map(desired)

    live_active_workflows = {workflow.name: workflow for workflow in live.workflows if workflow.active}
    for workflow in live_active_workflows.values():
        desired_workflow = desired_workflows.get(workflow.name)
        if not desired_workflow:
            kind = "retired_workflow_still_active" if workflow.name in retired_workflows else "unknown_active_workflow"
            severity = "critical"
            reason = (
                f"retired workflow {workflow.name} is still active in Hatchet"
                if kind == "retired_workflow_still_active"
                else f"workflow {workflow.name} is active in Hatchet but absent from desired state"
            )
            findings.append(finding(severity, kind, reason, {"workflow": workflow.name}))
            continue

        expected_worker = desired_workflow.get("worker")
        if workflow.worker and workflow.worker != expected_worker:
            findings.append(finding(
                "warning",
                "workflow_worker_mismatch",
                f"workflow {workflow.name} is served by {workflow.worker}, expected {expected_worker}",
                {"workflow": workflow.name, "expectedWorker": expected_worker, "observedWorker": workflow.worker},
            ))

        desired_tasks = set(strings(desired_workflow.get("tasks")))
        for task in workflow.tasks:
            if task not in desired_tasks:
                findings.append(finding(
                    "warning",
                    "unexpected_workflow_task",
                    f"workflow {workflow.name} exposes task {task} outside desired state",
                    {"workflow": workflow.name, "task": task},
                ))

        desired_events = desired_workflow_events(desired_workflow)
        for event in workflow.events:
            if event not in desired_events:
                findings.append(finding(
                    "warning",
                    "unexpected_event_trigger",
                    f"workflow {workflow.name} consumes unexpected Hatchet event {event}",
                    {"workflow": workflow.name, "event": event},
                ))

    if live.has_workflows:
        for name in desired_workflows:
            if name not in live_active_workflows:
                findings.append(finding(
                    "warning",
                    "desired_workflow_not_live",
                    f"desired workflow {name} was not found active in the live inventory",
                    {"workflow": name},
                ))

    active_worker_names = {worker.name for worker in live.workers if worker.active}
    for action in (action for action in live.task_actions if action.active):
        expected_workers = set(task_owners.get(action.name, ()))
        if not expected_workers:
            findings.append(finding(
                "critical",
                "unknown_active_task_action",
                f"task action {action.name} is active in Hatchet but absent from desired state",
                {"task": action.name, "observedWorkers": list(action.workers), "activeWorkers": action.active_workers},
            ))
            continue

        if action.active_workers == 0:
            findings.append(finding(
                "critical",
                "task_action_without_repo_worker",
                f"task action {action.name} has no active worker",
                {"task": action.name, "expectedWorkers": sorted(expected_workers), "activeWorkers": action.active_workers},
            ))
            continue

        if action.workers:
            observed = set(action.workers)
            repo_managed = observed & expected_workers & desired_worker_names
            if not repo_managed:
                findings.append(finding(
                    "critical",
                    "task_action_without_repo_worker",
                    f"task action {action.name} is not served by an active repo-managed worker",
                    {"task": action.name, "expectedWorkers": sorted(expected_workers), "observedWorkers": sorted(observed)},
                ))
        elif live.has_workers and expected_workers.isdisjoint(active_worker_names):
            findings.append(finding(
                "critical",
                "task_action_without_repo_worker",
                f"task action {action.name} has no active expected worker in the live worker inventory",
                {"task": action.name, "expectedWorkers": sorted(expected_workers), "activeWorkers": sorted(active_worker_names)},
            ))

    for trigger in (trigger for trigger in live.event_triggers if trigger.active):
        desired_workflow = desired_workflows.get(trigger.workflow)
        if not desired_workflow:
            kind = "retired_event_trigger_still_active" if trigger.workflow in retired_workflows else "unknown_event_trigger"
            findings.append(finding(
                "critical",
                kind,
                f"event trigger {trigger.event} is active for workflow {trigger.workflow} outside desired state",
                {"workflow": trigger.workflow, "event": trigger.event},
            ))
            continue
        if trigger.event not in desired_workflow_events(desired_workflow):
            findings.append(finding(
                "warning",
                "unexpected_event_trigger",
                f"workflow {trigger.workflow} has unexpected active event trigger {trigger.event}",
                {"workflow": trigger.workflow, "event": trigger.event},
            ))

    if live.has_event_triggers:
        live_trigger_pairs = {(trigger.workflow, trigger.event) for trigger in live.event_triggers if trigger.active}
        for workflow in desired_workflows.values():
            for event in desired_workflow_events(workflow):
                if (workflow["name"], event) not in live_trigger_pairs:
                    findings.append(finding(
                        "warning",
                        "desired_event_trigger_not_live",
                        f"desired event trigger {event} for workflow {workflow['name']} was not found active in live inventory",
                        {"workflow": workflow["name"], "event": event},
                    ))

    return findings


def active_workflows(desired: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["name"]: item
        for item in desired.get("active_workflows", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("status") != "retired"
    }


def active_workers(desired: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["name"]: item
        for item in desired.get("active_workers", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("status") != "retired"
    }


def retired_workflow_names(desired: dict[str, Any]) -> set[str]:
    return {
        item["name"]
        for item in desired.get("retired_workflows", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def task_owner_map(desired: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    owners: dict[str, list[str]] = {}
    for worker in active_workers(desired).values():
        worker_name = worker["name"]
        for task in strings(worker.get("tasks")):
            owners.setdefault(task, []).append(worker_name)
    return {task: tuple(sorted(set(worker_names))) for task, worker_names in owners.items()}


def desired_workflow_events(workflow: dict[str, Any]) -> set[str]:
    events: set[str] = set()
    for item in workflow.get("event_triggers", []):
        if isinstance(item, str):
            events.add(item)
        elif isinstance(item, dict) and isinstance(item.get("event"), str):
            events.add(item["event"])
    return events


def normalize_live(data: Any) -> LiveInventory:
    inventory = LiveInventory()
    if isinstance(data, list):
        inventory.workflows = [workflow for item in data if (workflow := parse_workflow(item))]
        inventory.has_workflows = True
        return inventory
    if not isinstance(data, dict):
        raise SystemExit("factory doctor: live JSON must be an object or array")

    workflow_items = first_list(data, "workflows", "workflowDefinitions", "workflow_definitions")
    if workflow_items is None and looks_like_collection(data.get("data")):
        workflow_items = data["data"]
    if workflow_items is not None:
        inventory.has_workflows = True
        inventory.workflows = [workflow for item in workflow_items if (workflow := parse_workflow(item))]

    worker_items = first_list(data, "workers", "workerStatuses", "worker_statuses")
    if worker_items is not None:
        inventory.has_workers = True
        inventory.workers = [worker for item in worker_items if (worker := parse_worker(item))]

    task_items = first_list(data, "taskActions", "task_actions", "tasks", "taskTypes", "task_types")
    if task_items is not None:
        inventory.has_task_actions = True
        inventory.task_actions = [task for item in task_items if (task := parse_task_action(item))]

    trigger_items = first_list(data, "eventTriggers", "event_triggers", "triggers")
    if trigger_items is not None:
        inventory.has_event_triggers = True
        inventory.event_triggers = [trigger for item in trigger_items if (trigger := parse_event_trigger(item))]

    if not inventory.has_task_actions and inventory.workers:
        inventory.has_task_actions = True
        inventory.task_actions = task_actions_from_workers(inventory.workers)

    workflow_triggers = [
        EventTrigger(workflow=workflow.name, event=event, active=workflow.active)
        for workflow in inventory.workflows
        for event in workflow.events
    ]
    if workflow_triggers and not inventory.has_event_triggers:
        inventory.has_event_triggers = True
    inventory.event_triggers.extend(workflow_triggers)

    return inventory


def parse_workflow(item: Any) -> Workflow | None:
    if not isinstance(item, dict):
        return None
    name = first_string(item, "name", "displayName", "display_name", "workflowName", "workflow_name")
    if not name and isinstance(item.get("workflow"), dict):
        name = first_string(item["workflow"], "name", "displayName", "display_name")
    if not name:
        return None
    return Workflow(
        name=name,
        active=is_active(item),
        worker=first_string(item, "worker", "workerName", "worker_name", "service"),
        tasks=tuple(strings(item.get("tasks") or item.get("taskActions") or item.get("actions"))),
        events=tuple(strings(item.get("onEvents") or item.get("events") or item.get("eventTriggers"))),
    )


def parse_worker(item: Any) -> Worker | None:
    if not isinstance(item, dict):
        return None
    name = first_string(item, "name", "worker", "workerName", "worker_name", "displayName", "display_name")
    if not name:
        return None
    return Worker(
        name=name,
        active=is_active(item),
        tasks=tuple(action_names(strings(item.get("tasks") or item.get("taskActions") or item.get("actions")))),
        workflows=tuple(strings(item.get("workflows") or item.get("workflowNames") or item.get("workflow_names"))),
    )


def parse_task_action(item: Any) -> TaskAction | None:
    if isinstance(item, str):
        return TaskAction(name=action_name(item))
    if not isinstance(item, dict):
        return None
    name = first_string(item, "name", "action", "task", "taskName", "task_name", "displayName", "display_name")
    if not name:
        return None
    active_workers = first_int(item, "activeWorkers", "active_workers", "workerCount", "worker_count")
    return TaskAction(
        name=action_name(name),
        active=is_active(item),
        workers=tuple(strings(item.get("workers") or item.get("workerNames") or item.get("worker_names"))),
        active_workers=active_workers,
    )


def parse_event_trigger(item: Any) -> EventTrigger | None:
    if not isinstance(item, dict):
        return None
    workflow = first_string(item, "workflow", "workflowName", "workflow_name")
    event = first_string(item, "event", "eventName", "event_name", "name")
    if workflow and event:
        return EventTrigger(workflow=workflow, event=event, active=is_active(item))
    return None


def first_list(data: dict[str, Any], *keys: str) -> list[Any] | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            text = first_string(item, "name", "event", "eventName", "event_name", "action", "task", "taskName", "task_name")
            if text:
                result.append(text)
    return result


def is_active(item: dict[str, Any]) -> bool:
    for key in ("active", "enabled", "isActive", "is_active"):
        value = item.get(key)
        if isinstance(value, bool):
            return value
    for key in ("paused", "archived", "deleted", "disabled"):
        value = item.get(key)
        if isinstance(value, bool) and value:
            return False
    value = item.get("isPaused")
    if isinstance(value, bool) and value:
        return False
    status = first_string(item, "status", "state")
    if status and status.lower() in {"paused", "inactive", "disabled", "deleted", "archived", "retired"}:
        return False
    return True


def task_actions_from_workers(workers: list[Worker]) -> list[TaskAction]:
    owners: dict[str, set[str]] = {}
    active_counts: dict[str, int] = {}
    for worker in workers:
        for task in worker.tasks:
            owners.setdefault(task, set()).add(worker.name)
            active_counts[task] = active_counts.get(task, 0) + (1 if worker.active else 0)
    return [
        TaskAction(name=task, workers=tuple(sorted(worker_names)), active_workers=active_counts.get(task, 0))
        for task, worker_names in sorted(owners.items())
    ]


def action_names(values: list[str]) -> list[str]:
    return [action_name(value) for value in values]


def action_name(value: str) -> str:
    return value.rsplit(":", 1)[-1].strip()


def looks_like_collection(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def finding(severity: str, kind: str, reason: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": severity,
        "kind": kind,
        "reason": reason,
        "details": details,
    }


def print_text(result: dict[str, Any]) -> None:
    print("Factory Hatchet doctor")
    print("======================")
    print(f"desired: {result['desired']}")
    print(f"live: {result['live'] or 'not supplied'}")
    print(f"desired workflows: {result['desiredWorkflowCount']}")
    print(f"desired workers: {result['desiredWorkerCount']}")
    print(f"findings: {result['findingCount']} ({result['criticalCount']} critical, {result['warningCount']} warning)")
    if result["live"] is None:
        print("live Hatchet inventory was not supplied; desired-state manifest validation only")
    for item in result["findings"]:
        print(f"{item['severity'].upper()} {item['kind']}: {item['reason']}")


if __name__ == "__main__":
    sys.exit(main())
