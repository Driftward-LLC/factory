from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .contracts import EVENT_IDS, EVENTS, PROVIDER_ACTION_MAPPINGS, REASON_CODES
from .routing_policy import LinearProjectExecutionPolicyError, resolve_linear_project_execution_policy


ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
INELIGIBLE_STATUSES = {"done", "canceled", "cancelled", "duplicate", "in review", "needs operator", "blocked"}


class IngressValidationError(Exception):
    """Raised when an ingress payload cannot be normalized safely."""


def normalize_github_pull_request(
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IngressValidationError("payload must be a JSON object")

    action = _required_string(payload, "action").lower()
    repository = _object(payload, "repository")
    pull_request = _object(payload, "pull_request")
    repo = _required_string(repository, "full_name")
    pr_number = _required_int(pull_request, "number")
    merged = bool(pull_request.get("merged"))

    event_id = _github_pull_request_event_id(action, merged)
    if not event_id:
        raise IngressValidationError("only opened, synchronize, or closed GitHub pull request events are accepted")
    event = EVENTS[event_id]

    closed_at = _optional_timestamp(pull_request, "closed_at")
    updated_at = _optional_timestamp(pull_request, "updated_at")
    created_at = _optional_timestamp(pull_request, "created_at")
    event_time = delivery_id or updated_at or closed_at or created_at
    if not event_time:
        raise IngressValidationError("delivery_id, updated_at, closed_at, or created_at is required")

    title = _optional_string(pull_request, "title") or ""
    body = _optional_string(pull_request, "body") or ""
    head = _object(pull_request, "head", required=False)
    base = _object(pull_request, "base", required=False)
    head_ref = _optional_string(head, "ref") or ""
    head_sha = _optional_string(head, "sha")
    base_ref = _optional_string(base, "ref")
    linear_issues = sorted(set(ISSUE_KEY_RE.findall("\n".join([title, body, head_ref]))))
    pr_url = _optional_string(pull_request, "html_url") or ""
    is_draft = bool(pull_request.get("draft"))
    state = _optional_string(pull_request, "state") or ("closed" if action == "closed" else "open")

    event_name = _contract_string(event, "wire_name")
    event_suffix = _contract_string(event, "event_key_suffix")
    reason_code = _contract_string(event, "default_reason_code_id")
    decision = _contract_string(event, "default_decision_id")

    if event_id == EVENT_IDS["GITHUB_PR_CREATED"]:
        summary = f"Observed opened PR {repo}#{pr_number}; no Hermes dispatch."
    elif event_id == EVENT_IDS["GITHUB_PR_SYNCHRONIZED"]:
        summary = f"Observed synchronized PR {repo}#{pr_number}; fresh verification may be needed."
    elif event_id == EVENT_IDS["GITHUB_PR_CLOSED_MERGED"]:
        summary = f"Observed merged PR {repo}#{pr_number}; no Hermes dispatch."
    else:
        summary = f"Observed closed-unmerged PR {repo}#{pr_number}; no Hermes dispatch."

    event_key = f"github:pull_request:{repo}:{pr_number}:{event_suffix}:{event_time}"

    return {
        "eventName": event_name,
        "eventKey": event_key,
        "source": "github",
        "providerEvent": "pull_request",
        "action": action,
        "repo": repo,
        "prNumber": pr_number,
        "prUrl": pr_url,
        "title": title,
        "state": state,
        "merged": merged,
        "isDraft": is_draft,
        "createdAt": created_at,
        "closedAt": closed_at,
        "updatedAt": updated_at,
        "headRefName": head_ref or None,
        "headSha": head_sha,
        "baseRefName": base_ref,
        "linearIssues": linear_issues,
        "deliveryId": delivery_id,
        "dispatchEligible": False,
        "decision": decision,
        "reasonCode": reason_code,
        "summary": summary,
    }


def normalize_linear_issue(
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IngressValidationError("payload must be a JSON object")

    action = _required_string(payload, "action").lower()
    entity_type = _required_string(payload, "type")
    if entity_type != "Issue":
        raise IngressValidationError("only Linear Issue events are accepted")
    if action not in {"create", "update", "remove"}:
        raise IngressValidationError("only Linear issue create, update, or remove events are accepted")

    data = _object(payload, "data")
    identifier = _optional_string(data, "identifier") or _optional_string(data, "id")
    if not identifier:
        raise IngressValidationError("Linear issue identifier or id is required")

    issue_id = _optional_string(data, "id")
    title = _optional_string(data, "title") or ""
    description = _optional_string(data, "description") or _optional_string(data, "body") or ""
    issue_url = _optional_string(payload, "url") or _optional_string(data, "url") or ""
    created_at = _optional_timestamp(payload, "createdAt") or _optional_timestamp(data, "createdAt")
    updated_at = _optional_timestamp(data, "updatedAt")
    webhook_id = _optional_string(payload, "webhookId")
    event_time = delivery_id or webhook_id or created_at or updated_at
    if not event_time:
        raise IngressValidationError("Linear delivery, webhookId, createdAt, or updatedAt is required")

    team_name = _name_from(data.get("team"))
    team_key = _optional_string(_object(data, "team", required=False), "key")
    project_id, project_name = _linear_project(data)
    status = _name_from(data.get("state")) or _optional_string(data, "status") or ""
    status_type = _optional_string(data, "statusType") or _optional_string(_object(data, "state", required=False), "type")
    labels = _label_names(data.get("labels"))
    delegate = _name_from(data.get("delegate")) or _optional_string(data, "delegate")
    blocked_by = _blocked_by(data)
    updated_from = payload.get("updatedFrom")
    updated_fields = sorted(updated_from.keys()) if isinstance(updated_from, dict) else []
    actor_name = _name_from(payload.get("actor")) or _name_from(data.get("actor"))
    comment_only = action == "update" and bool(updated_fields) and set(updated_fields).issubset({"comments", "comment", "commentsUpdatedAt", "activity", "updatedAt"})

    gates = _linear_issue_gates(
        action=action,
        team_name=team_name,
        team_key=team_key,
        status=status,
        blocked_by=blocked_by,
        delegate=delegate,
        actor_name=actor_name,
        comment_only=comment_only,
    )
    gates.append(_linear_project_execution_gate(project_id))
    failed = [gate for gate in gates if not gate["passed"]]
    dispatch_eligible = not failed
    event_id = (
        EVENT_IDS["LINEAR_ISSUE_READY_FOR_IMPLEMENTATION"]
        if dispatch_eligible
        else EVENT_IDS["LINEAR_ISSUE_OBSERVED"]
    )
    event = EVENTS[event_id]
    event_name = _contract_string(event, "wire_name")
    reason_code = (
        _contract_string(event, "default_reason_code_id")
        if dispatch_eligible
        else str(failed[0]["reasonCode"])
    )
    decision = _contract_string(event, "default_decision_id")
    event_key = f"linear:issue:{identifier}:{action}:{event_time}"

    return {
        "eventName": event_name,
        "eventKey": event_key,
        "source": "linear",
        "providerEvent": "Issue",
        "action": action,
        "subject": identifier,
        "issueId": issue_id,
        "identifier": identifier,
        "issueUrl": issue_url,
        "title": title,
        "description": description,
        "team": team_name,
        "teamKey": team_key,
        "projectId": project_id,
        "projectName": project_name,
        "status": status,
        "statusType": status_type,
        "labels": labels,
        "blockedBy": blocked_by,
        "delegate": delegate,
        "actorName": actor_name,
        "deliveryId": delivery_id,
        "webhookId": webhook_id,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "updatedFields": updated_fields,
        "gateResults": gates,
        "dispatchEligible": dispatch_eligible,
        "decision": decision,
        "reasonCode": reason_code,
        "summary": _linear_summary(identifier, title, status, dispatch_eligible, reason_code),
    }


def _linear_project(data: dict[str, Any]) -> tuple[str | None, str | None]:
    project_id = _optional_string(data, "projectId")
    project = data.get("project")
    project_name: str | None = None

    if isinstance(project, dict):
        project_id = project_id or _optional_string(project, "id")
        project_name = _name_from(project)
    elif isinstance(project, str):
        project_id = project_id or project

    return project_id, project_name


def _linear_issue_gates(
    *,
    action: str,
    team_name: str | None,
    team_key: str | None,
    status: str,
    blocked_by: list[dict[str, Any]],
    delegate: str | None,
    actor_name: str | None,
    comment_only: bool,
) -> list[dict[str, Any]]:
    normalized_status = _normalize_name(status)
    normalized_delegate = _normalize_name(delegate or "")
    engineering_team = _normalize_name(team_name or "") == "engineering" or _normalize_name(team_key or "") == "eng"

    return [
        _gate(action in {"create", "update"}, "linear_issue_create_or_update", REASON_CODES["LINEAR_ISSUE_REMOVED"]),
        _gate(_normalize_name(actor_name or "") != "shipwright", "linear_actor_not_factory_lifecycle_writer", REASON_CODES["LINEAR_ISSUE_FACTORY_LIFECYCLE_ACTOR"]),
        _gate(not comment_only, "linear_issue_not_comment_only", REASON_CODES["LINEAR_ISSUE_COMMENT_ONLY_UPDATE"]),
        _gate(engineering_team, "linear_team_engineering", REASON_CODES["LINEAR_ISSUE_WRONG_TEAM"]),
        _gate(normalized_status == "ready", "linear_status_ready", REASON_CODES["LINEAR_ISSUE_STATUS_NOT_READY"]),
        _gate(not blocked_by, "linear_no_active_blockers", REASON_CODES["LINEAR_ISSUE_BLOCKED"]),
        _gate(
            normalized_status not in INELIGIBLE_STATUSES,
            "linear_status_not_terminal",
            REASON_CODES["LINEAR_ISSUE_TERMINAL_OR_OPERATOR_STATE"],
        ),
        _gate(
            normalized_delegate != "driftward factory",
            "linear_delegate_not_retired_factory",
            REASON_CODES["LINEAR_ISSUE_RETIRED_FACTORY_DELEGATE"],
        ),
    ]


def _linear_project_execution_gate(project_id: str | None) -> dict[str, Any]:
    try:
        policy = resolve_linear_project_execution_policy(project_id)
    except LinearProjectExecutionPolicyError:
        return _gate(
            False,
            "linear_project_execution_policy_valid",
            REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_POLICY_INVALID"],
        )
    return _gate(
        policy.execution_enabled,
        "linear_project_execution_enabled",
        REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_DISABLED"],
    )


def _github_pull_request_event_id(action: str, merged: bool) -> str | None:
    provider_key = f"github.pull_request.{action}"
    if action == "closed":
        provider_key = f"{provider_key}.{'merged' if merged else 'unmerged'}"
    return PROVIDER_ACTION_MAPPINGS.get(provider_key)


def _contract_string(contract: dict[str, Any], key: str) -> str:
    value = contract.get(key)
    if not isinstance(value, str) or not value:
        raise IngressValidationError(f"contract field {key} must be a non-empty string")
    return value


def _gate(passed: bool, name: str, reason_code: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "reasonCode": None if passed else reason_code,
    }


def _linear_summary(
    identifier: str,
    title: str,
    status: str,
    dispatch_eligible: bool,
    reason_code: str,
) -> str:
    name = f"{identifier}: {title}" if title else identifier
    if dispatch_eligible:
        return f"Linear issue {name} is ready for implementation."
    return f"Observed Linear issue {name} in {status or 'unknown'}; no Hermes dispatch ({reason_code})."


def _normalize_name(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", "-").split())


def _name_from(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key in ("name", "displayName", "identifier", "key", "id"):
        found = _optional_string(value, key)
        if found:
            return found
    return None


def _label_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            value = nodes
        else:
            value = value.get("edges")

    labels: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
            elif isinstance(item, dict):
                node = item.get("node") if isinstance(item.get("node"), dict) else item
                name = _name_from(node)
                if name:
                    labels.append(name)
    return sorted(set(labels), key=str.lower)


def _blocked_by(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    relations = _object(data, "relations", required=False)
    for key in ("blockedBy", "blockedByIssues"):
        value = data.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    value = relations.get("blockedBy")
    if isinstance(value, list):
        candidates.extend(value)

    active: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, str):
            active.append({"id": item})
            continue
        if not isinstance(item, dict):
            continue
        state = _name_from(item.get("state")) or _optional_string(item, "status") or ""
        if _normalize_name(state) in {"done", "canceled", "cancelled"}:
            continue
        active.append({
            "id": _optional_string(item, "identifier") or _optional_string(item, "id"),
            "title": _optional_string(item, "title"),
            "status": state or None,
        })
    return active


def _object(value: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    found = value.get(key)
    if isinstance(found, dict):
        return found
    if required:
        raise IngressValidationError(f"{key} must be an object")
    return {}


def _required_string(value: dict[str, Any], key: str) -> str:
    found = value.get(key)
    if not isinstance(found, str) or not found.strip():
        raise IngressValidationError(f"{key} is required")
    return found.strip()


def _optional_timestamp(value: dict[str, Any], key: str) -> str | None:
    found = _optional_string(value, key)
    if found is None:
        return None
    try:
        parsed = datetime.fromisoformat(found.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngressValidationError(f"{key} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise IngressValidationError(f"{key} must include a timezone")
    return found


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    found = value.get(key)
    if found is None:
        return None
    if not isinstance(found, str):
        return None
    found = found.strip()
    return found or None


def _required_int(value: dict[str, Any], key: str) -> int:
    found = value.get(key)
    if isinstance(found, bool) or not isinstance(found, int):
        raise IngressValidationError(f"{key} is required")
    return found
