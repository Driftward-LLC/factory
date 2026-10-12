"""Narrow, opt-in browser approvals; never a proxy for the root operator API.

Tailscale Serve must be the only untrusted-client ingress to this listener.
Header authentication is meaningful only inside that deployment boundary.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import operator, store

PREVIEW_TTL = 120
MAX_BODY_BYTES = 16384


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _origin() -> str | None:
    value = os.environ.get("FACTORY_CONSOLE_PUBLIC_ORIGIN", "")
    try:
        parsed = urlsplit(value)
        if (re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", value)
                and parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                and not parsed.username and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment and value == f"https://{parsed.netloc}"):
            return value
    except ValueError:
        pass
    return None


def _enabled(request: Request) -> bool:
    # The development bypass grants reads only, even with a spoofed login.
    if os.environ.get("FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    logins = {item.strip() for item in os.environ.get("FACTORY_CONSOLE_OPERATOR_LOGINS", "").split(",") if item.strip()}
    identities = request.headers.getlist("tailscale-user-login")
    return bool(_origin() and len(identities) == 1 and identities[0] == request.state.viewer
                and len(request.state.viewer) <= 200 and request.state.viewer in logins)


def _authorize(request: Request) -> str:
    if not _enabled(request):
        raise HTTPException(403, "Issue controls are disabled or this viewer is not an authorized operator")
    for header, expected in (("origin", _origin()), ("x-factory-action", "issue-control"), ("content-type", "application/json")):
        if request.headers.getlist(header) != [expected]:
            raise HTTPException(403, "Issue controls require an exact same-origin JSON request")
    fetch_site = request.headers.getlist("sec-fetch-site")
    if fetch_site and fetch_site != ["same-origin"]:
        raise HTTPException(403, "Cross-site issue control is forbidden")
    return request.state.viewer


def _body(value: Any, *, confirm: bool) -> dict[str, Any]:
    fields = {"action", "intent", "reason", "revision"} | ({"snapshot_digest", "preview_token", "request_id"} if confirm else set())
    if not isinstance(value, dict) or set(value) - fields:
        raise store.BridgeValidationError("Unsupported issue control fields")
    action, intent, reason = value.get("action"), value.get("intent"), value.get("reason")
    if not isinstance(action, str) or action not in {"takeover", "return", "revision"} or (intent != "fresh_attempt" if action == "return" else intent is not None):
        raise store.BridgeValidationError("Unsupported issue control action or handback intent")
    if action == "revision":
        value = {**value, "revision": store.validate_factory_revision(value.get("revision"))}
    elif "revision" in value:
        raise store.BridgeValidationError("Revision fields require the revision action")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 320 or any(ord(char) < 32 for char in reason):
        raise store.BridgeValidationError("A reason of 1–320 characters is required")
    if confirm:
        if not isinstance(value.get("request_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value["request_id"]):
            raise store.BridgeValidationError("A bounded request_id is required")
        if not isinstance(value.get("snapshot_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_digest"]):
            raise store.BridgeValidationError("The preview snapshot digest is required")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def projection(control: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    actions: list[dict[str, str]] = []
    if enabled and control["mode"] == "factory":
        actions = [{"action": "takeover", "label": "Take manual control"}]
        if control.get("revision_available"):
            actions.append({"action": "revision", "label": "Revise existing draft PR"})
    elif enabled and control["mode"] == "manual":
        actions = [{"action": "return", "intent": "fresh_attempt", "label": "Return for a fresh attempt"}]
    return {
        "schema": "factory-console-issue-control-v1", "work_item_key": control["work_item_key"],
        "status": control["mode"], "version": control["revision"],
        "safe_to_edit": control["mode"] == "manual" and not control["blockers"],
        "updated_at": control["updated_at"], "actor": control["actor"], "reason": control["reason"],
        "pending_attempts": control["blockers"], "history": control["history"],
        "automatic_intake_blocked": control["automatic_intake_blocked"],
        "actions_enabled": enabled, "available_actions": actions,
    }


class IssueControl:
    def __init__(self, db_path: str, ingress_db_path: str | None) -> None:
        self.db_path = db_path
        self.ingress_db_path = ingress_db_path
        self._key = secrets.token_bytes(32)

    def read(self, issue: str) -> dict[str, Any]:
        if not operator.ISSUE_RE.fullmatch(issue):
            raise store.BridgeValidationError("issue must be an ENG identifier")
        conn = store.connect_readonly(self.db_path)
        try:
            with conn:
                conn.execute("BEGIN")
                control = store.get_factory_issue_control(conn, issue)
                try:
                    store.factory_revision_source(conn, issue)
                    control["revision_available"] = os.environ.get("HATCHET_INTAKE_DRY_RUN", "true").strip().lower() == "false"
                except store.BridgeError:
                    control["revision_available"] = False
                return control
        finally:
            conn.close()

    def _context(self, issue: str, actor: str, value: dict[str, Any]) -> dict[str, Any]:
        control = self.read(issue)
        action = value["action"]
        if (action in {"takeover", "revision"} and control["mode"] != "factory") or (action == "return" and control["mode"] != "manual"):
            raise store.BridgeConflict("Issue control changed; refresh before approving")
        context = {"issue": issue, "actor": actor, "action": action, "intent": value.get("intent"), "reason": value["reason"],
                   "control": control, "mode": "real" if os.environ.get("HATCHET_INTAKE_DRY_RUN", "true").strip().lower() == "false" else "dry-run"}
        if action == "revision":
            if not self.ingress_db_path or context["mode"] != "real":
                raise store.BridgeConflict("revision requires real execution mode and trusted ingress")
            preview = operator.preview_revision(bridge_db=self.db_path, ingress_db=self.ingress_db_path, issue=issue,
                registry_path=os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", operator.DOCKER_ROUTING_REGISTRY),
                policy_path=os.environ.get("HATCHET_VERIFIER_POLICY_PATH", operator.DOCKER_VERIFIER_POLICY), revision=value["revision"])
            context["retry"] = preview
            context["revision_input"] = value["revision"]
            context["payload"] = operator.revision_payload(preview, operator._policy(preview["repository"], os.environ.get("HATCHET_VERIFIER_POLICY_PATH", operator.DOCKER_VERIFIER_POLICY)))
        elif action == "return":
            if not self.ingress_db_path:
                raise operator.OperatorError("trusted_ingress_unavailable")
            conn = store.connect_readonly(self.db_path)
            try:
                prior = next((item for item in store.list_factory_attempts(conn, issue) if item["mode"] == context["mode"]), None)
            finally:
                conn.close()
            preview = operator.preview_retry(
                bridge_db=self.db_path, ingress_db=self.ingress_db_path, issue=issue, mode=context["mode"],
                registry_path=os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", operator.DOCKER_ROUTING_REGISTRY),
                policy_path=os.environ.get("HATCHET_VERIFIER_POLICY_PATH", operator.DOCKER_VERIFIER_POLICY),
                supersede_pr_url=prior.get("prior_pr_url") if prior else None,
            )
            # Unlike legacy root retries, never skip a newer Factory-authored
            # observation and approve against an older Ready snapshot.
            if operator._latest_linear(self.ingress_db_path, issue) != preview["input"]:
                raise store.BridgeConflict("Latest Linear observation differs; return requires the current issue to be Ready")
            policy = operator._policy(preview["repository"], os.environ.get("HATCHET_VERIFIER_POLICY_PATH", operator.DOCKER_VERIFIER_POLICY))
            context["retry"] = preview
            context["payload"] = operator.retry_payload(preview, policy)
        return context

    def preview(self, issue: str, actor: str, value: dict[str, Any]) -> dict[str, Any]:
        value = _body(value, confirm=False)
        context = self._context(issue, actor, value)
        control = context["control"]
        expires = int(time.time()) + PREVIEW_TTL
        digest = _digest(context)
        claims = {"issue": issue, "actor": actor, "action": value["action"], "intent": value.get("intent"), "reason": value["reason"],
                  "digest": digest, "revision": control["revision"], "store_digest": control["snapshot_digest"], "expires": expires}
        encoded = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).decode()
        token = encoded + "." + hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
        retry = context.get("retry", {})
        artifact = retry.get("prior_pr")
        summary = ("Hold this issue, cancel queued work, and request running work to settle. Wait for Safe to edit before changing code."
                   if value["action"] == "takeover" else
                   "Reserve one new immutable revision attempt on this existing draft PR and branch at the displayed commit. Supplied review and design references are operator attestations, not proof an independent review ran or passed. Required verification returns evidence for handoff."
                   if value["action"] == "revision" else
                   f"Reserve one new {context['mode']} attempt from the latest Ready observation, with configured verification and repair policy. Old events stay fenced. This does not discover, adopt, continue, or verify manually created artifacts; reconcile those before confirming.")
        return {"eligible": True, "work_item_key": issue, "action": value["action"], "intent": value.get("intent"),
                "snapshot_digest": digest, "preview_token": token, "expires_at": expires,
                "control": projection(control, enabled=True), "effect_summary": summary,
                "linear_observed_at": retry.get("snapshot_updated_at"),
                "revision": {**retry["revision"], "source_attempt_id": retry["prior_attempt_id"], "source_job_id": retry["source_job_id"]} if value["action"] == "revision" else None,
                "artifact": {"pr_url": artifact["url"], "head_sha": artifact["head_sha"], "branch": artifact["head_branch"], "repository": retry["repository"]} if artifact else None}

    def confirm(self, issue: str, actor: str, value: dict[str, Any]) -> dict[str, Any]:
        value = _body(value, confirm=True)
        token = value.get("preview_token")
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError
            encoded, signature = token.split(".")
            if not re.fullmatch(r"[0-9a-f]{64}", signature):
                raise ValueError
            claims = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True), object_pairs_hook=_unique_object)
            if not isinstance(claims, dict) or set(claims) != {"issue", "actor", "action", "intent", "reason", "digest", "revision", "store_digest", "expires"}:
                raise ValueError
            if any(claims[key] != expected for key, expected in {"issue": issue, "actor": actor, "action": value["action"], "intent": value.get("intent"), "reason": value["reason"], "digest": value["snapshot_digest"]}.items()):
                raise ValueError
            if isinstance(claims["expires"], bool) or not isinstance(claims["expires"], int):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise store.BridgeValidationError("Preview token does not match this approval") from exc
        command = {"work_item_key": issue, "request_id": value["request_id"], "actor": actor, "reason": value["reason"],
                   "expected_revision": claims["revision"], "expected_snapshot_digest": claims["store_digest"],
                   "command_context_digest": _digest({"snapshot_digest": value["snapshot_digest"], "preview_token": token})}
        if value["action"] == "revision":
            request_id = f"console-revision:{value['request_id']}"
            command_digest = _digest({"issue": issue, "request_id": request_id, "actor": actor, "reason": value["reason"], "revision": value["revision"],
                "snapshot_digest": value["snapshot_digest"], "preview_token": token})
            conn = store.connect_readonly(self.db_path)
            try:
                previous = store.get_factory_revision_replay(conn, work_item_key=issue, request_id=request_id, actor=actor, reason=value["reason"], command_digest=command_digest)
                if previous is not None:
                    return self._result({**previous, "control": self.read(issue)}, value["request_id"])
            finally:
                conn.close()
            if not hmac.compare_digest(signature, hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()):
                raise store.BridgeConflict("Preview is no longer valid; preview again")
            if claims["expires"] <= time.time():
                raise HTTPException(410, "Preview expired; preview again")
            context = self._context(issue, actor, value)
            if _digest(context) != claims["digest"]:
                raise store.BridgeConflict("Issue, policy, or artifact changed; preview again")
            conn = store.connect_runtime(self.db_path)
            try:
                retry = context["retry"]
                attempt, job, created = store.reserve_factory_revision(conn, work_item_key=issue, request_id=request_id, actor=actor, reason=value["reason"],
                    payload=context["payload"], source_attempt_id=retry["prior_attempt_id"], expected_control_digest=claims["store_digest"], command_digest=command_digest)
                return self._result({"attempt": attempt, "control": self.read(issue)}, value["request_id"])
            finally:
                conn.close()
        conn = store.connect_readonly(self.db_path)
        try:
            previous = store.get_factory_issue_control_command(conn, **command, action="take_control" if value["action"] == "takeover" else "return_control")
        finally:
            conn.close()
        # Exact committed retries remain recoverable after timeout or restart.
        # No new command is authorized until signature, expiry and context pass.
        if previous is not None:
            return self._result(previous, value["request_id"])
        if not hmac.compare_digest(signature, hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()):
            raise store.BridgeConflict("Preview is no longer valid; preview again")
        if claims["expires"] <= time.time():
            raise HTTPException(410, "Preview expired; preview again")
        context = self._context(issue, actor, value)
        if _digest(context) != claims["digest"]:
            raise store.BridgeConflict("Issue, policy, or artifact changed; preview again")
        conn = store.connect_runtime(self.db_path)
        try:
            if value["action"] == "takeover":
                result = {"control": store.take_factory_issue_control(conn, **command)}
            else:
                retry = context["retry"]
                result = store.return_factory_issue_control(conn, **command, payload=context["payload"], mode=context["mode"],
                                                            prior_attempt_id=retry["prior_attempt_id"],
                                                            supersede_pr_url=retry["prior_pr"]["url"] if retry.get("prior_pr") else None)
        finally:
            conn.close()
        return self._result(result, value["request_id"])

    @staticmethod
    def _result(result: dict[str, Any], request_id: str) -> dict[str, Any]:
        return {"control": projection(result["control"], enabled=True), "request_id": request_id,
                "attempt_id": result.get("attempt", {}).get("attempt_id")}


def install_issue_control_routes(app: FastAPI, db_path: str, ingress_db_path: str | None) -> None:
    service = IssueControl(db_path, ingress_db_path)

    @app.get("/api/issues/{issue}/control")
    def issue_control(issue: str, request: Request) -> dict[str, Any]:
        return projection(service.read(issue), enabled=_enabled(request))

    async def command_body(request: Request) -> dict[str, Any]:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "Issue control request is too large")
            raw.extend(chunk)
        try:
            return json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "Issue control requires valid JSON") from exc

    @app.post("/api/issues/{issue}/revision/inspect")
    async def inspect_revision(issue: str, request: Request) -> dict[str, Any]:
        _authorize(request)
        if await command_body(request) != {}:
            raise HTTPException(400, "PR observation accepts no additional fields")
        return await run_in_threadpool(operator.inspect_revision, bridge_db=db_path, ingress_db=ingress_db_path,
            registry_path=os.environ.get("HATCHET_INTAKE_ROUTING_REGISTRY_PATH", operator.DOCKER_ROUTING_REGISTRY),
            policy_path=os.environ.get("HATCHET_VERIFIER_POLICY_PATH", operator.DOCKER_VERIFIER_POLICY), issue=issue)

    @app.post("/api/issues/{issue}/control/preview")
    async def preview(issue: str, request: Request) -> dict[str, Any]:
        actor = _authorize(request)
        return await run_in_threadpool(service.preview, issue, actor, await command_body(request))

    @app.post("/api/issues/{issue}/control/confirm")
    async def confirm(issue: str, request: Request) -> dict[str, Any]:
        actor = _authorize(request)
        return await run_in_threadpool(service.confirm, issue, actor, await command_body(request))
