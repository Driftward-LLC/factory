from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ROUTING_REGISTRY_PATH_ENV = "HATCHET_INTAKE_ROUTING_REGISTRY_PATH"


class LinearProjectExecutionPolicyError(Exception):
    """Raised when a reviewed Linear project execution policy is unusable."""


@dataclass(frozen=True)
class LinearProjectExecutionPolicy:
    project_id: str
    repo: str
    execution_enabled: bool
    registry_version: int


def resolve_linear_project_execution_policy(
    project_id: str | None,
    *,
    registry_path: str | None = None,
) -> LinearProjectExecutionPolicy:
    if not project_id:
        raise LinearProjectExecutionPolicyError("project_id_missing")
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise LinearProjectExecutionPolicyError("project_id_malformed")

    registry = load_linear_project_execution_registry(registry_path)
    routes = [route for route in registry["routes"] if route["project_id"] == project_id]
    if not routes:
        raise LinearProjectExecutionPolicyError("project_id_unmapped")
    if len(routes) != 1:
        raise LinearProjectExecutionPolicyError("project_id_ambiguous")

    route = routes[0]
    return LinearProjectExecutionPolicy(
        project_id=project_id,
        repo=route["repo"],
        execution_enabled=route["execution_enabled"],
        registry_version=registry["version"],
    )


def load_linear_project_execution_registry(registry_path: str | None = None) -> dict[str, Any]:
    path = Path(registry_path or _routing_registry_path())
    try:
        raw = path.read_text()
    except OSError as exc:
        raise LinearProjectExecutionPolicyError("routing_registry_read_failed") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LinearProjectExecutionPolicyError("routing_registry_parse_failed") from exc
    return _validate_registry(parsed)


def _validate_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LinearProjectExecutionPolicyError("routing_registry_malformed")
    if value.get("version") != 2:
        raise LinearProjectExecutionPolicyError("routing_registry_version_invalid")
    routes = value.get("routes")
    if not isinstance(routes, list) or not routes:
        raise LinearProjectExecutionPolicyError("routing_registry_routes_invalid")

    normalized_routes: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    for route in routes:
        if not isinstance(route, dict):
            raise LinearProjectExecutionPolicyError("routing_registry_route_malformed")
        project_id = route.get("project_id")
        repo = route.get("repo")
        execution_enabled = route.get("execution_enabled")
        if not isinstance(project_id, str) or not isinstance(repo, str):
            raise LinearProjectExecutionPolicyError("routing_registry_route_malformed")
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise LinearProjectExecutionPolicyError("routing_registry_project_id_malformed")
        if not REPOSITORY_PATTERN.fullmatch(repo):
            raise LinearProjectExecutionPolicyError("routing_registry_repo_malformed")
        if not isinstance(execution_enabled, bool):
            raise LinearProjectExecutionPolicyError("routing_registry_execution_policy_malformed")
        if project_id in project_ids:
            raise LinearProjectExecutionPolicyError("routing_registry_project_id_duplicate")
        project_ids.add(project_id)
        normalized_routes.append({
            "project_id": project_id,
            "repo": repo,
            "execution_enabled": execution_enabled,
        })

    return {"version": 2, "routes": normalized_routes}


def _routing_registry_path() -> str:
    configured = os.environ.get(ROUTING_REGISTRY_PATH_ENV)
    if configured:
        return configured
    return str(Path(__file__).resolve().parents[2] / "workers" / "routing-registry.json")
