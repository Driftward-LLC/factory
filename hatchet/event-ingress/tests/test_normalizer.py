from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hatchet_event_ingress.contracts import DECISIONS, HATCHET_EVENTS, REASON_CODES
from hatchet_event_ingress.normalizer import (
    IngressValidationError,
    normalize_github_pull_request,
    normalize_linear_issue,
)


class NormalizeGitHubPullRequestTest(unittest.TestCase):
    def test_opened_pr_is_observation_only(self) -> None:
        payload = _github_pr_payload(action="opened", state="open", merged=False)
        payload["pull_request"]["draft"] = True

        normalized = normalize_github_pull_request(payload, delivery_id="delivery-opened-1")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["GITHUB_PR_CREATED"])
        self.assertEqual(
            normalized["eventKey"],
            "github:pull_request:Driftward-LLC/capykit:11:created:delivery-opened-1",
        )
        self.assertEqual(normalized["linearIssues"], ["ENG-7"])
        self.assertTrue(normalized["isDraft"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["GITHUB_PR_CREATED"])

    def test_synchronized_pr_requires_fresh_verification(self) -> None:
        payload = _github_pr_payload(action="synchronize", state="open", merged=False)
        payload["pull_request"]["head"]["sha"] = "def456"

        normalized = normalize_github_pull_request(payload, delivery_id="delivery-sync-1")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["GITHUB_PR_SYNCHRONIZED"])
        self.assertEqual(
            normalized["eventKey"],
            "github:pull_request:Driftward-LLC/capykit:11:synchronized:delivery-sync-1",
        )
        self.assertEqual(normalized["headSha"], "def456")
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["GITHUB_PR_SYNCHRONIZED"])

    def test_closed_unmerged_pr_is_observation_only(self) -> None:
        payload = _github_pr_payload(action="closed", state="closed", merged=False)

        normalized = normalize_github_pull_request(payload, delivery_id="delivery-1")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["GITHUB_PR_CLOSED_UNMERGED"])
        self.assertEqual(
            normalized["eventKey"],
            "github:pull_request:Driftward-LLC/capykit:11:closed_unmerged:delivery-1",
        )
        self.assertEqual(normalized["linearIssues"], ["ENG-7"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["GITHUB_PR_CLOSED_UNMERGED"])

    def test_closed_merged_pr_is_observation_only(self) -> None:
        payload = _github_pr_payload(action="closed", state="closed", merged=True)

        normalized = normalize_github_pull_request(payload, delivery_id="delivery-merged-1")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["GITHUB_PR_CLOSED_MERGED"])
        self.assertEqual(
            normalized["eventKey"],
            "github:pull_request:Driftward-LLC/capykit:11:closed_merged:delivery-merged-1",
        )
        self.assertTrue(normalized["merged"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["GITHUB_PR_CLOSED_MERGED"])

    def test_unsupported_pr_action_is_rejected(self) -> None:
        payload = _github_pr_payload(action="reopened", state="open", merged=False)

        with self.assertRaises(IngressValidationError):
            normalize_github_pull_request(payload)

    def test_invalid_provider_timestamp_is_rejected(self) -> None:
        payload = _github_pr_payload(action="opened", state="open", merged=False)
        payload["pull_request"]["updated_at"] = "not-a-timestamp"
        with self.assertRaises(IngressValidationError):
            normalize_github_pull_request(payload, delivery_id="delivery")


class NormalizeLinearIssueTest(unittest.TestCase):
    def test_ready_linear_issue_is_dispatch_eligible(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["data"]["labels"] = [{"name": "Feature"}, {"name": "has-active-pr"}]

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-1")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_READY_FOR_IMPLEMENTATION"])
        self.assertEqual(normalized["eventKey"], "linear:issue:ENG-40:update:linear-delivery-1")
        self.assertEqual(normalized["subject"], "ENG-40")
        self.assertTrue(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["READY_FOR_IMPLEMENTATION"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["LINEAR_ISSUE_READY_FOR_IMPLEMENTATION"])
        self.assertEqual(normalized["labels"], ["Feature", "has-active-pr"])
        self.assertEqual(normalized["projectId"], "d779c532-620d-458c-8abd-05e37f5ca4c1")
        self.assertEqual(normalized["projectName"], "Capykit")
        self.assertEqual(normalized["updatedFields"], ["state"])
        self.assertTrue(all(gate["passed"] for gate in normalized["gateResults"]))

    def test_project_id_is_preserved_when_only_project_id_is_present(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["data"]["project"] = None
        payload["data"]["projectId"] = "c670f223-bcaf-4e48-9209-6cca65da4568"

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-project-id")

        self.assertEqual(normalized["projectId"], "c670f223-bcaf-4e48-9209-6cca65da4568")
        self.assertIsNone(normalized["projectName"])

    def test_disabled_project_is_observed_without_ready_dispatch(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["data"]["project"] = {
            "id": "c670f223-bcaf-4e48-9209-6cca65da4568",
            "name": "Driftward Software Factory",
        }

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-disabled-project")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_OBSERVED"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(
            normalized["reasonCode"],
            REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_DISABLED"],
        )
        failed = [gate for gate in normalized["gateResults"] if not gate["passed"]]
        self.assertEqual(failed, [{
            "name": "linear_project_execution_enabled",
            "passed": False,
            "reasonCode": REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_DISABLED"],
        }])

    def test_missing_or_malformed_execution_policy_fails_closed_but_preserves_observation(self) -> None:
        payload = _ready_linear_issue_payload()
        with patch.dict(os.environ, {"HATCHET_INTAKE_ROUTING_REGISTRY_PATH": "/missing/routing-registry.json"}):
            missing = normalize_linear_issue(payload, delivery_id="linear-delivery-missing-policy")
        self.assertEqual(missing["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_OBSERVED"])
        self.assertFalse(missing["dispatchEligible"])
        self.assertEqual(
            missing["reasonCode"],
            REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_POLICY_INVALID"],
        )
        self.assertEqual(missing["projectId"], "d779c532-620d-458c-8abd-05e37f5ca4c1")

        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "routing-registry.json"
            registry.write_text('{"version":2,"routes":[{"project_id":"d779c532-620d-458c-8abd-05e37f5ca4c1","repo":"Driftward-LLC/capykit"}]}')
            with patch.dict(os.environ, {"HATCHET_INTAKE_ROUTING_REGISTRY_PATH": str(registry)}):
                malformed = normalize_linear_issue(payload, delivery_id="linear-delivery-malformed-policy")
        self.assertEqual(malformed["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_OBSERVED"])
        self.assertFalse(malformed["dispatchEligible"])
        self.assertEqual(
            malformed["reasonCode"],
            REASON_CODES["LINEAR_ISSUE_PROJECT_EXECUTION_POLICY_INVALID"],
        )

    def test_eng_7_shape_is_observation_only(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["data"].update({
            "identifier": "ENG-7",
            "title": "Implement Capykit discovery CLI commands",
            "state": {"name": "Done", "type": "completed"},
            "labels": [{"name": "Feature"}, {"name": "Human Decision"}],
            "delegate": {"name": "Driftward Factory"},
            "relations": {
                "blockedBy": [{"id": "ENG-6", "title": "Implement layered registry loading"}]
            },
        })

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-2")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_OBSERVED"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["decision"], DECISIONS["OBSERVE_ONLY"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["LINEAR_ISSUE_STATUS_NOT_READY"])
        failed_reasons = {gate["reasonCode"] for gate in normalized["gateResults"] if not gate["passed"]}
        self.assertIn(REASON_CODES["LINEAR_ISSUE_RETIRED_FACTORY_DELEGATE"], failed_reasons)
        self.assertIn(REASON_CODES["LINEAR_ISSUE_BLOCKED"], failed_reasons)

    def test_shipwright_comment_update_is_observed_and_never_dispatched(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["actor"] = {"name": "Shipwright"}
        payload["updatedFrom"] = {"comments": None}

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-shipwright-comment")

        self.assertEqual(normalized["eventName"], HATCHET_EVENTS["LINEAR_ISSUE_OBSERVED"])
        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["LINEAR_ISSUE_FACTORY_LIFECYCLE_ACTOR"])
        self.assertEqual(normalized["actorName"], "Shipwright")

    def test_comment_only_update_is_observed_and_never_dispatched(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["updatedFrom"] = {"commentsUpdatedAt": None}

        normalized = normalize_linear_issue(payload, delivery_id="linear-delivery-comment-only")

        self.assertFalse(normalized["dispatchEligible"])
        self.assertEqual(normalized["reasonCode"], REASON_CODES["LINEAR_ISSUE_COMMENT_ONLY_UPDATE"])

    def test_non_issue_payload_is_rejected(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["type"] = "Comment"

        with self.assertRaises(IngressValidationError):
            normalize_linear_issue(payload, delivery_id="linear-delivery-3")

    def test_invalid_linear_provider_timestamp_is_rejected(self) -> None:
        payload = _ready_linear_issue_payload()
        payload["data"]["updatedAt"] = "not-a-timestamp"
        with self.assertRaises(IngressValidationError):
            normalize_linear_issue(payload, delivery_id="linear-delivery-invalid-time")


def _ready_linear_issue_payload() -> dict:
    return {
        "action": "update",
        "type": "Issue",
        "createdAt": "2026-08-11T17:00:00.000Z",
        "url": "https://linear.app/driftward/issue/ENG-40/example",
        "webhookTimestamp": 1797008400000,
        "data": {
            "id": "issue-uuid",
            "identifier": "ENG-40",
            "title": "Build a narrow Linear ingress test",
            "description": "Implementation-ready work item.",
            "updatedAt": "2026-08-11T17:00:00.000Z",
            "team": {"name": "Engineering", "key": "ENG"},
            "project": {
                "id": "d779c532-620d-458c-8abd-05e37f5ca4c1",
                "name": "Capykit",
            },
            "state": {"name": "Ready", "type": "unstarted"},
            "labels": [{"name": "Feature"}],
            "relations": {"blockedBy": []},
        },
        "updatedFrom": {
            "state": {"name": "Backlog"}
        },
    }


def _github_pr_payload(*, action: str, state: str, merged: bool) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "Driftward-LLC/capykit"},
        "pull_request": {
            "number": 11,
            "html_url": "https://github.com/Driftward-LLC/capykit/pull/11",
            "title": "ENG-7: implement Capykit discovery CLI commands",
            "body": "Refs ENG-7",
            "state": state,
            "merged": merged,
            "created_at": "2026-08-11T08:00:00Z",
            "closed_at": "2026-08-11T08:59:43Z" if state == "closed" else None,
            "updated_at": "2026-08-11T08:59:43Z",
            "head": {
                "ref": "codex/eng-7-implement-capykit-discovery-cli-commands",
                "sha": "abc123",
            },
            "base": {"ref": "main"},
        },
    }


if __name__ == "__main__":
    unittest.main()
