"""Narrow Linear GraphQL client; this is the only Shipwright write boundary."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


ENDPOINT = "https://api.linear.app/graphql"
EXPECTED_WORKSPACE = "Driftward"
EXPECTED_ACTOR = "Shipwright"
EXPECTED_TEAM = "Engineering"
EXPECTED_TEAM_KEY = "ENG"
EXPECTED_ACTOR_ID = "4945f724-8539-46c6-b024-e03b90257ac2"
EXPECTED_WORKSPACE_ID = "aa6f0902-c91a-4aa3-a8cb-13f03df89bbf"
EXPECTED_TEAM_ID = "2e9999a3-4e98-4c3f-ba16-1de1406dfa89"
DENIED_TEAM_ID = "ea7dc7b9-7d13-4147-9db8-387a8fa8b715"


class LinearError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    workspace: str


class LinearClient:
    def __init__(self, token: str, *, timeout: int = 15):
        if not token or "\n" in token or "\r" in token:
            raise LinearError("factory_access_token_invalid")
        self.token = token
        self.timeout = timeout

    def verify_scope(self) -> Actor:
        data = self._request("""
          query FactoryIdentity {
            viewer { id name organization { id name } }
            teams(first: 50) { nodes { id name key } }
          }
        """, {})
        viewer = data.get("viewer")
        teams = ((data.get("teams") or {}).get("nodes"))
        if not isinstance(viewer, dict) or not isinstance(teams, list):
            raise LinearError("linear_identity_invalid")
        actor_id, name = viewer.get("id"), viewer.get("name")
        organization = viewer.get("organization")
        workspace = organization.get("name") if isinstance(organization, dict) else None
        workspace_id = organization.get("id") if isinstance(organization, dict) else None
        if actor_id != EXPECTED_ACTOR_ID or workspace_id != EXPECTED_WORKSPACE_ID:
            raise LinearError("linear_actor_mismatch")
        visible = {entry.get("id") for entry in teams if isinstance(entry, dict)}
        if visible != {EXPECTED_TEAM_ID} or DENIED_TEAM_ID in visible:
            raise LinearError("linear_team_scope_mismatch")
        return Actor(actor_id=actor_id, name=name, workspace=workspace)

    def verify_issue_team(self, issue_id: str) -> None:
        data = self._request("""
          query FactoryIssueTeam($id: String!) { issue(id: $id) { id team { id } } }
        """, {"id": issue_id})
        issue = data.get("issue")
        team = issue.get("team") if isinstance(issue, dict) else None
        if not isinstance(issue, dict) or issue.get("id") != issue_id or not isinstance(team, dict) or team.get("id") != EXPECTED_TEAM_ID:
            raise LinearError("linear_issue_team_mismatch")

    def marker_exists(self, issue_id: str, marker: str, *, heartbeat: Callable[[], bool] | None = None) -> bool:
        cursor: str | None = None
        pages = 0
        while True:
            if heartbeat and not heartbeat():
                raise LinearError("lifecycle_lease_lost")
            data = self._request("""
              query FactoryCommentMarkers($id: String!, $after: String) {
                issue(id: $id) { comments(first: 100, after: $after) {
                  nodes { body }
                  pageInfo { hasNextPage endCursor }
                } }
              }
            """, {"id": issue_id, "after": cursor})
            issue = data.get("issue")
            comments = issue.get("comments") if isinstance(issue, dict) else None
            nodes = comments.get("nodes") if isinstance(comments, dict) else None
            page = comments.get("pageInfo") if isinstance(comments, dict) else None
            if not isinstance(nodes, list) or not isinstance(page, dict):
                raise LinearError("linear_comment_lookup_malformed")
            for comment in nodes:
                if isinstance(comment, dict) and marker in str(comment.get("body") or ""):
                    return True
            pages += 1
            if pages > 100:
                raise LinearError("linear_comment_lookup_incomplete")
            if page.get("hasNextPage") is not True:
                return False
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise LinearError("linear_comment_lookup_malformed")

    def create_comment(self, issue_id: str, body: str) -> None:
        if len(body) > 3500:
            raise LinearError("comment_body_too_large")
        data = self._request("""
          mutation FactoryLifecycleComment($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) { success }
          }
        """, {"issueId": issue_id, "body": body})
        if ((data.get("commentCreate") or {}).get("success")) is not True:
            raise LinearError("linear_comment_create_failed")

    def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(ENDPOINT, data=json.dumps({"query": query, "variables": variables}).encode(), headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value: Any = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LinearError("linear_auth_failure" if exc.code in {401, 403} else "linear_api_failure") from exc
        except (OSError, socket.timeout, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LinearError("linear_api_timeout") from exc
        if not isinstance(value, dict) or value.get("errors") or not isinstance(value.get("data"), dict):
            raise LinearError("linear_api_malformed")
        return value["data"]
