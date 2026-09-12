"""Cursor-based lifecycle discovery and leased outbox delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .linear import LinearClient, LinearError
from .outbox import Outbox, canonical_timestamp, now

LOG = logging.getLogger(__name__)
ROUTING_FAILURES = {"linear_issue_project_execution_policy_invalid"}
FAILURE_MESSAGES = {
    "real_create_draft_pr_disabled": "Draft PR execution is disabled by the approved execution gate.",
    "hermes_command_failed": "The bounded implementation executor failed before a draft PR was produced.",
    "hermes_result_invalid": "The implementation executor returned an invalid result.",
    "github_identity_mismatch": "The resulting GitHub identity did not match the approved execution identity.",
    "pr_validation_failed": "The resulting draft PR did not satisfy required validation.",
    "verification_pr_base_mismatch": "Draft PR verification did not match the requested base branch.",
    "verification_pr_author_mismatch": "Draft PR verification did not match the approved Factory GitHub App identity.",
    "verification_pr_linear_link_missing": "Draft PR verification did not find the required Linear reference.",
    "verification_changed_file_scope_invalid": "Draft PR verification found a changed file outside the declared scope.",
    "verification_changed_file_forbidden": "Draft PR verification found a changed forbidden path.",
    "verification_github_checks_missing": "Draft PR verification could not find required GitHub checks.",
    "verification_github_checks_pending": "Draft PR verification found incomplete GitHub checks.",
    "verification_github_checks_timeout": "Draft PR verification timed out while waiting for required GitHub checks.",
    "verification_github_check_failed": "Draft PR verification found a failed GitHub check.",
    "verification_github_required_check_missing": "Draft PR verification could not find a required successful GitHub check.",
    "verification_local_command_evidence_missing": "Draft PR verification is missing reported local command evidence.",
    "verification_local_command_failed": "Draft PR verification found a failed reported local command.",
    "verification_local_command_policy_mismatch": "Draft PR verification found reported local command evidence that did not match the verifier policy.",
    "verification_secret_check_evidence_missing": "Draft PR verification is missing reported secret-check evidence.",
    "verification_secret_check_failed": "Draft PR verification found a failed reported secret check.",
    "verification_secret_check_policy_mismatch": "Draft PR verification found reported secret-check evidence that did not match the verifier policy.",
    "verification_evidence_malformed": "Draft PR verification evidence was malformed.",
    "verification_evidence_unavailable": "Draft PR verification evidence was unavailable.",
}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
IDENTIFIER = re.compile(r"^ENG-[1-9][0-9]{0,8}$")
REPO = re.compile(r"^Driftward-LLC/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DRAFT_PR_ACTIONS = {"create_draft_pr", "repair_draft_pr"}


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bridge_url: str
    bridge_token: str
    ingress_url: str
    token: str
    activation_watermark: str
    allowed_repos: frozenset[str]

    @classmethod
    def from_env(cls) -> "Config":
        env = lifecycle_env()
        token = env.get("LINEAR_FACTORY_ACCESS_TOKEN", "").strip()
        bridge_token = env.get("HATCHET_HERMES_BRIDGE_TOKEN", "").strip()
        watermark = _canonical_timestamp(env.get("FACTORY_LIFECYCLE_ACTIVATION_WATERMARK", "").strip())
        repos = frozenset(item.strip() for item in env.get("FACTORY_LIFECYCLE_ALLOWED_REPOS", "Driftward-LLC/capykit").split(",") if item.strip())
        if not token or not bridge_token or not watermark or not repos or any(not REPO.fullmatch(repo) for repo in repos):
            raise SourceError("writer_configuration_invalid")
        return cls(env.get("FACTORY_LIFECYCLE_BRIDGE_URL", "http://127.0.0.1:19877").rstrip("/"), bridge_token, env.get("FACTORY_LIFECYCLE_INGRESS_URL", "http://127.0.0.1:19878").rstrip("/"), token, watermark, repos)


def lifecycle_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = env.get("FACTORY_LIFECYCLE_ENV_FILE", "").strip()
    if not env_file:
        return env
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        env[key] = value
    return env


def _canonical_timestamp(value: Any) -> str | None:
    return canonical_timestamp(value)


def _timestamp(value: Any) -> bool:
    return _canonical_timestamp(value) is not None


class LifecycleWriter:
    def __init__(self, config: Config, outbox: Outbox):
        self.config, self.outbox = config, outbox
        self.linear = LinearClient(config.token)
        self.source_errors: dict[str, str] = {}
        self.outbox.suppress_legacy_pre_activation_unknown_modes(config.activation_watermark)

    def verify(self) -> None:
        self.linear.verify_scope()

    def scan(self) -> dict[str, int]:
        self.source_errors = {}
        counts = {"queued": 0, "suppressed": 0, "dead_letter": 0}
        upper = {"createdAt": now(), "eventId": "ffffffff-ffff-4fff-8fff-ffffffffffff"}
        for name, path, field, candidate in (
            ("bridge", "/lifecycle/events", "events", self._bridge_candidate),
            ("ingress", "/lifecycle/observations", "observations", self._routing_candidate),
        ):
            try:
                for record in self._stream(name, path, field, upper):
                    item = candidate(record)
                    if item:
                        self.outbox.record(item)
                        counts[item["delivery_state"]] = counts.get(item["delivery_state"], 0) + 1
            except SourceError as exc:
                self.source_errors[name] = str(exc)
                LOG.warning("factory lifecycle %s source unavailable: %s", name, str(exc))
        return counts

    def _stream(self, source: str, path: str, field: str, upper: dict[str, str]):
        cursor_key = f"cursor:{source}:v1"
        cursor = self._cursor(cursor_key)
        for _ in range(1000):
            query = {"limit": "250", "before_created_at": upper["createdAt"], "before_event_id": upper["eventId"]}
            if cursor:
                query.update({"after_created_at": cursor["createdAt"], "after_event_id": cursor["eventId"]})
            base = self.config.bridge_url if source == "bridge" else self.config.ingress_url
            response = self._json(base + path + "?" + urllib.parse.urlencode(query), bridge=source == "bridge")
            records = response.get(field)
            if not isinstance(records, list):
                raise SourceError(f"{source}_source_malformed")
            if not records:
                return
            for record in records:
                if not isinstance(record, dict):
                    raise SourceError(f"{source}_source_malformed")
                yield record
            next_cursor = self._record_cursor(records[-1])
            # Advance only after every record from this page was durably recorded.
            self.outbox.set_meta(cursor_key, json.dumps(next_cursor, sort_keys=True))
            cursor = next_cursor
            if len(records) < 250:
                return

    def _cursor(self, key: str) -> dict[str, str] | None:
        value = self.outbox.get_meta(key)
        if not value:
            return None
        try:
            cursor = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SourceError("source_cursor_malformed") from exc
        normalized = self._record_cursor(cursor)
        normalized_value = json.dumps(normalized, sort_keys=True)
        if value != normalized_value:
            self.outbox.set_meta(key, normalized_value)
        return normalized

    def _record_cursor(self, record: dict[str, Any]) -> dict[str, str]:
        created, event_id = _canonical_timestamp(record.get("createdAt")), record.get("eventId")
        if not created or not isinstance(event_id, str) or not UUID.fullmatch(event_id):
            raise SourceError("source_cursor_malformed")
        return {"createdAt": created, "eventId": event_id}

    def deliver(self) -> dict[str, int]:
        owner = str(uuid.uuid4())
        delivered = recovered = retried = 0
        while item := self.outbox.claim_one(owner):
            try:
                if not self.outbox.renew(item["outbox_key"], owner):
                    continue
                self.linear.verify_issue_team(item["issue_id"])
                if not self.outbox.renew(item["outbox_key"], owner):
                    continue
                heartbeat = lambda: self.outbox.renew(item["outbox_key"], owner)
                if self.linear.marker_exists(item["issue_id"], item["marker"], heartbeat=heartbeat):
                    recovered += int(self.outbox.delivered(item["outbox_key"], owner, recovered=True))
                else:
                    if not self.outbox.renew(item["outbox_key"], owner):
                        continue
                    self.linear.create_comment(item["issue_id"], item["body"])
                    delivered += int(self.outbox.delivered(item["outbox_key"], owner))
            except LinearError as exc:
                retried += int(self.outbox.retry(item["outbox_key"], owner, exc.reason))
        return {"delivered": delivered, "recovered": recovered, "retried": retried}

    def canary(self, *, issue_id: str, identifier: str, issue_url: str) -> str:
        base = dict(issue_id=issue_id, identifier=identifier, issue_url=issue_url, job_id=None, idempotency_key=None, source_event_key="factory-lifecycle-writer-canary-v2", source_delivery_id=None, repo=None, pr_url=None, execution_mode="canary", source_kind="operator_canary", source_recorded_at=now())
        if not self._valid_issue(base):
            raise SourceError("canary_identity_invalid")
        item = self._item(phase="canary", reason_code=None, body=f"Factory lifecycle writer canary completed for `{identifier}`. Shipwright attribution and outbox deduplication were exercised; no factory execution or provider effect was requested.", **base)
        self.outbox.record(item)
        return item["outbox_key"]

    def _bridge_candidate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("sourceKind") == "factory_attempt":
            return self._attempt_candidate(event)
        requested_action = event.get("requestedAction")
        if requested_action is not None and requested_action not in DRAFT_PR_ACTIONS:
            return None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if isinstance(payload.get("factory_retry"), dict):
            # Generationed attempt events below are the canonical lifecycle
            # source for retries; do not replay the older bridge phrasing.
            return None
        source_time = _canonical_timestamp(event.get("createdAt"))
        base = dict(issue_id=payload.get("linear_issue_id"), identifier=payload.get("linear_issue"), issue_url=payload.get("linear_issue_url"), job_id=event.get("jobId"), idempotency_key=event.get("idempotencyKey"), source_event_key=payload.get("source_event_key"), source_delivery_id=payload.get("source_delivery_id"), repo=payload.get("repo"), pr_url=event.get("prUrl"), execution_mode="unknown", source_kind="bridge_event", source_recorded_at=source_time)
        phase = {
            "running": "started",
            "effect_committed": "artifact_created",
            "verification_pending": "verification_pending",
            "completed": "artifact_completed",
            "failed": "failed",
            "ready_for_human_review": "succeeded",
        }.get(event.get("state"))
        if not phase:
            return None
        if not source_time:
            return self._dead_letter(base, "source_timestamp_missing")
        if source_time <= self.config.activation_watermark:
            return self._suppressed(base, phase, "pre_activation_suppressed")
        dry_run = payload.get("dry_run")
        if dry_run is True:
            base["execution_mode"] = "dry_run"
            return self._suppressed(base, phase, "dry_run_suppressed")
        if dry_run is not False:
            return self._dead_letter(base, "execution_mode_unknown")
        base["execution_mode"] = "real"
        if not self._valid_issue(base):
            return self._dead_letter(base, "linear_issue_identity_invalid")
        if not self._valid_repo(base.get("repo")):
            return self._dead_letter(base, "repo_not_allowlisted")
        if phase in {"artifact_created", "artifact_completed"}:
            return self._suppressed(base, phase, "verification_not_ready")
        if phase == "started":
            return None
        if phase == "failed":
            code = event.get("reasonCode") if isinstance(event.get("reasonCode"), str) else ""
            safe_code = code if code in FAILURE_MESSAGES else "execution_failed_unclassified"
            message = FAILURE_MESSAGES.get(safe_code, "Factory execution stopped before a valid draft PR was produced. An operator can inspect the bridge job for the sanitized reason code.")
            return self._item(phase=phase, reason_code=safe_code, body=f"Factory work failed for `{base['identifier']}` in `{base['repo']}`. {message}\n\nReason: `{safe_code}`.", **base)
        if self._real_pr(base.get("pr_url"), base.get("repo")):
            if phase == "verification_pending":
                return self._item(phase=phase, reason_code=None, body=f"Factory created a draft PR for `{base['identifier']}` in `{base['repo']}`: {base['pr_url']}\n\nDeterministic verification is pending; no implementation success has been recorded.", **base)
            return self._item(phase=phase, reason_code=None, body=f"Factory deterministic draft-PR verification passed for `{base['identifier']}` in `{base['repo']}`. Ready for human review: {base['pr_url']}", **base)
        return self._dead_letter(base, "draft_pr_identity_invalid")

    def _attempt_candidate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Attempt lineage announces retry/start progress; bridge verification
        remains the sole source for success comments."""
        event_type = event.get("eventType")
        if event_type == "attempt_verified_ready":
            return None
        phase = {"retry_requested": "retry_requested", "repair_requested": "repair_requested", "attempt_started": "started", "attempt_failed": "failed", "superseded": "superseded"}.get(event_type)
        if not phase:
            return None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        source_time = _canonical_timestamp(event.get("createdAt"))
        dry_run = payload.get("dry_run")
        base = dict(issue_id=payload.get("linear_issue_id"), identifier=payload.get("linear_issue"), issue_url=payload.get("linear_issue_url"), job_id=event.get("jobId"), idempotency_key=event.get("idempotencyKey"), source_event_key=None, source_delivery_id=None, repo=payload.get("repo"), pr_url=event.get("prUrl"), execution_mode="dry_run" if dry_run is True else "real" if dry_run is False else "unknown", source_kind="factory_attempt", source_recorded_at=source_time)
        if not source_time or source_time <= self.config.activation_watermark:
            return None
        if dry_run is True:
            return self._suppressed(base, phase, "dry_run_suppressed")
        if base["execution_mode"] != "real" or not self._valid_issue(base) or not self._valid_repo(base["repo"]):
            return self._dead_letter(base, "attempt_identity_invalid")
        generation = event.get("generation")
        attempt = event.get("attemptId")
        if not isinstance(generation, int) or not isinstance(attempt, str):
            return self._dead_letter(base, "attempt_identity_invalid")
        attempt_label = "Factory attempt" if generation == 1 else "Factory retry attempt"
        text = {
            "retry_requested": f"Factory retry requested for `{base['identifier']}` (attempt {generation}) in `{base['repo']}`.",
            "repair_requested": f"Factory repair requested for `{base['identifier']}` (attempt {generation}) in `{base['repo']}` against the existing draft PR: {base['pr_url']}.",
            "started": f"{attempt_label} {generation} started for `{base['identifier']}` in `{base['repo']}`.",
            "failed": f"{attempt_label} {generation} failed for `{base['identifier']}` in `{base['repo']}`. An operator can inspect the sanitized bridge record.",
            "superseded": f"{attempt_label} {generation} for `{base['identifier']}` was superseded after the exact closed-unmerged PR acknowledgement.",
        }[phase]
        item = self._item(phase=phase, reason_code=None, body=text, **base)
        item["outbox_key"] = "lifecycle:v2:" + hashlib.sha256(f"attempt:{attempt}:{event_type}".encode()).hexdigest()
        item["marker"] = f"<!-- driftward-factory:lifecycle:{item['outbox_key']} -->"
        item["body"] = (text[:3200] + "\n\n" + item["marker"])[:3500]
        return item

    def _routing_candidate(self, observation: dict[str, Any]) -> dict[str, Any] | None:
        if observation.get("reasonCode") not in ROUTING_FAILURES:
            return None
        source_time = _canonical_timestamp(observation.get("createdAt"))
        base = dict(issue_id=observation.get("issueId"), identifier=observation.get("identifier"), issue_url=observation.get("issueUrl"), job_id=None, idempotency_key=None, source_event_key=observation.get("eventKey"), source_delivery_id=observation.get("deliveryId"), repo=None, pr_url=None, execution_mode="real", source_kind="ingress_routing", source_recorded_at=source_time)
        if not source_time:
            return self._dead_letter(base, "source_timestamp_missing")
        if base["source_recorded_at"] <= self.config.activation_watermark:
            return self._suppressed(base, "routing_failed", "pre_activation_suppressed")
        if not self._valid_issue(base):
            return self._dead_letter(base, "linear_issue_identity_invalid")
        return self._item(phase="routing_failed", reason_code="linear_issue_project_execution_policy_invalid", body=f"Factory could not route `{base['identifier']}` for implementation because the reviewed project routing policy is unavailable or invalid. No bridge job or external execution was started. An operator should repair the routing registry before retrying.", **base)

    def _suppressed(self, base: dict[str, Any], phase: str, reason: str) -> dict[str, Any]:
        item = self._item(phase=phase, reason_code=reason, body="suppressed", **base)
        item["delivery_state"], item["suppressed_at"] = "suppressed", now()
        return item

    def _dead_letter(self, base: dict[str, Any], reason: str) -> dict[str, Any]:
        item = self._item(phase="unresolved", reason_code=reason, body="dead-lettered", **base)
        item["delivery_state"] = "dead_letter"
        return item

    def _item(self, *, phase: str, issue_id: Any, identifier: Any, issue_url: Any, job_id: Any, idempotency_key: Any, source_event_key: Any, source_delivery_id: Any, repo: Any, pr_url: Any, reason_code: Any, execution_mode: str, source_kind: str, source_recorded_at: Any, body: str) -> dict[str, Any]:
        subject = f"{issue_id}|{job_id or source_event_key or 'issue'}|{phase}"
        key = "lifecycle:v2:" + hashlib.sha256(subject.encode()).hexdigest()
        marker = f"<!-- driftward-factory:lifecycle:{key} -->"
        return {"outbox_key": key, "phase": phase, "delivery_state": "pending", "issue_id": issue_id if isinstance(issue_id, str) else None, "identifier": identifier if isinstance(identifier, str) else None, "issue_url": issue_url if isinstance(issue_url, str) else None, "job_id": job_id if isinstance(job_id, str) else None, "idempotency_key": idempotency_key if isinstance(idempotency_key, str) else None, "source_event_key": source_event_key if isinstance(source_event_key, str) else None, "source_delivery_id": source_delivery_id if isinstance(source_delivery_id, str) else None, "repo": repo if isinstance(repo, str) else None, "pr_url": pr_url if isinstance(pr_url, str) else None, "reason_code": reason_code if isinstance(reason_code, str) else None, "execution_mode": execution_mode, "source_kind": source_kind, "source_recorded_at": source_recorded_at if isinstance(source_recorded_at, str) else "", "marker": marker, "body": (body[:3200] + "\n\n" + marker)[:3500], "next_attempt_at": None, "delivered_at": None, "suppressed_at": None, "last_error_code": None}

    def _valid_issue(self, value: dict[str, Any]) -> bool:
        if not isinstance(value.get("issue_id"), str) or not UUID.fullmatch(value["issue_id"]):
            return False
        identifier, raw_url = value.get("identifier"), value.get("issue_url")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or not isinstance(raw_url, str) or len(raw_url) > 500:
            return False
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
            return False
        try:
            parsed = urlparse(raw_url)
            port = parsed.port
        except ValueError:
            return False
        parts = parsed.path.split("/")
        return parsed.scheme == "https" and parsed.netloc == "linear.app" and parsed.hostname == "linear.app" and port is None and parsed.username is None and parsed.password is None and not parsed.params and not parsed.query and not parsed.fragment and len(parts) == 5 and parts[0] == "" and parts[1] == "driftward" and parts[2] == "issue" and parts[3] == identifier and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,199}", parts[4]))

    def _valid_repo(self, value: Any) -> bool:
        return isinstance(value, str) and bool(REPO.fullmatch(value)) and value in self.config.allowed_repos

    def _real_pr(self, value: Any, repo: Any) -> bool:
        if not self._valid_repo(repo) or not isinstance(value, str) or len(value) > 500:
            return False
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return False
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError:
            return False
        return parsed.scheme == "https" and parsed.netloc == "github.com" and parsed.hostname == "github.com" and port is None and parsed.username is None and parsed.password is None and not parsed.params and not parsed.query and not parsed.fragment and bool(re.fullmatch(rf"/{re.escape(repo)}/pull/[1-9][0-9]*", parsed.path))

    def _json(self, url: str, *, bridge: bool) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.config.bridge_token}"} if bridge else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
                value = json.load(response)
        except (OSError, socket.timeout, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SourceError("bridge_source_unavailable" if bridge else "ingress_source_unavailable") from exc
        if not isinstance(value, dict):
            raise SourceError("source_response_malformed")
        return value
