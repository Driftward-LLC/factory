"""Deployment-owned organization identity. No credentials or implicit authority."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

ENV = "FACTORY_ORGANIZATION_CONFIG"
ISSUE = re.compile(r"[A-Z][A-Z0-9]{0,15}-[1-9][0-9]{0,8}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def load(path: str | None = None) -> dict[str, Any]:
    configured = path if path is not None else os.environ.get(ENV)
    if not configured:
        return {"schema": "factory-organization-v1", "linear": None, "github": {"enabled": False}}
    try:
        value = json.loads(Path(configured).read_text())
        if (not isinstance(value, dict) or set(value) != {"schema", "linear", "github"}
                or value["schema"] != "factory-organization-v1"
                or not isinstance(value["github"], dict) or set(value["github"]) != {"enabled"}
                or type(value["github"]["enabled"]) is not bool):
            raise ValueError
        linear = value["linear"]
        if linear is not None:
            if (not isinstance(linear, dict)
                    or set(linear) != {"workspace_id", "team_id", "team_key", "workspace_slug", "actor_id", "delegate_name"}
                    or any(not isinstance(linear[key], str) or not UUID.fullmatch(linear[key]) for key in ("workspace_id", "team_id", "actor_id"))
                    or not isinstance(linear["team_key"], str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", linear["team_key"])
                    or not isinstance(linear["workspace_slug"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", linear["workspace_slug"])
                    or not isinstance(linear["delegate_name"], str) or not 1 <= len(linear["delegate_name"]) <= 100
                    or linear["delegate_name"].strip() != linear["delegate_name"] or any(ord(c) < 32 for c in linear["delegate_name"])):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Factory organization configuration is unavailable or malformed") from exc


POLICY_ENV = {
    "routing": "HATCHET_INTAKE_ROUTING_REGISTRY_PATH",
    "verifier": "HATCHET_VERIFIER_POLICY_PATH",
    "review": "HATCHET_INDEPENDENT_REVIEW_POLICY_PATH",
}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def validate_policy(value: Any, kind: str) -> None:
    """Validate deployment policy catalogs before any background consumer starts."""
    def text(item: Any) -> bool:
        return isinstance(item, str) and bool(item.strip()) and len(item) <= 300

    def texts(items: Any) -> bool:
        return isinstance(items, list) and 1 <= len(items) <= 32 and all(text(item) for item in items)

    try:
        if not isinstance(value, dict):
            raise ValueError
        if kind == "routing":
            if value.get("version") != 2 or not isinstance(value.get("routes"), list):
                raise ValueError
            projects = set()
            for route in value["routes"]:
                if (not isinstance(route, dict) or not isinstance(route.get("project_id"), str) or not UUID.fullmatch(route["project_id"])
                        or not isinstance(route.get("repo"), str) or not REPOSITORY.fullmatch(route["repo"])
                        or type(route.get("execution_enabled")) is not bool or route["project_id"] in projects):
                    raise ValueError
                projects.add(route["project_id"])
        elif kind == "verifier":
            if type(value.get("version")) is not int or value["version"] < 1 or not isinstance(value.get("policies"), list):
                raise ValueError
            repositories = set()
            for policy in value["policies"]:
                fields = {"id", "repo", "expected_base", "allowed_changed_paths", "forbidden_changed_paths", "required_local_commands", "secret_check_command"}
                if (not isinstance(policy, dict) or set(policy) != fields
                        or any(not text(policy[field]) for field in ("id", "repo", "expected_base", "secret_check_command"))
                        or not REPOSITORY.fullmatch(policy["repo"]) or policy["repo"] in repositories
                        or any(not texts(policy[field]) for field in ("allowed_changed_paths", "forbidden_changed_paths", "required_local_commands"))
                        or len(set(policy["required_local_commands"])) != len(policy["required_local_commands"])):
                    raise ValueError
                repositories.add(policy["repo"])
        elif kind == "review":
            if (set(value) != {"schema", "enabled", "automatic_repairs_allowed", "max_passes", "repositories"}
                    or value["schema"] != "factory-independent-review-policy-v1"
                    or type(value["enabled"]) is not bool or type(value["automatic_repairs_allowed"]) is not bool
                    or type(value["max_passes"]) is not int or value["max_passes"] != 2
                    or not isinstance(value["repositories"], list)
                    or any(not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) for repo in value["repositories"])
                    or len(set(value["repositories"])) != len(value["repositories"])):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Factory {kind} policy is malformed") from exc


def validate_startup(path: str | None = None) -> dict[str, Any]:
    value = load(path)
    for kind, name in POLICY_ENV.items():
        configured = os.environ.get(name)
        if configured:
            try:
                validate_policy(json.loads(Path(configured).read_text()), kind)
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(f"Factory {kind} policy is unavailable or malformed") from exc
    return value


def linear_identity() -> dict[str, str]:
    value = load()["linear"]
    if value is None:
        raise ValueError("Factory Linear integration is disabled")
    return value


def matches_issue(issue: Any) -> bool:
    linear = load()["linear"]
    return bool(linear and isinstance(issue, str) and ISSUE.fullmatch(issue) and issue.startswith(linear["team_key"] + "-"))


def matches_observation(value: dict[str, Any]) -> bool:
    linear = load()["linear"]
    return bool(linear and matches_issue(value.get("identifier")) and value.get("teamId") == linear["team_id"]
                and value.get("workspaceId") == linear["workspace_id"] and value.get("teamKey") == linear["team_key"])


def issue_url(issue: str) -> str | None:
    linear = load()["linear"]
    return f"https://linear.app/{linear['workspace_slug']}/issue/{issue}" if linear and matches_issue(issue) else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path")
    args = parser.parse_args()
    value = validate_startup(args.path)
    state = "configured" if value["linear"] or value["github"]["enabled"] else "disabled"
    print(f"Factory organization configuration valid (integrations {state})")


if __name__ == "__main__":
    main()
