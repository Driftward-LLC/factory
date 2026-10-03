"""Metadata-only PostHog AI observability for Hermes.

The observer never exports prompts, completions, tool arguments, or tool results.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_EVENTS: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
_WORKER_STARTED = False
_PROCESS_TRACE = str(uuid.uuid4())


def _enabled() -> bool:
    return bool(os.environ.get("POSTHOG_API_KEY", "").strip())


def _request_key(kwargs: dict[str, Any]) -> str:
    # Tool hooks include api_request_id, but not api_call_count.
    return ":".join(
        str(kwargs.get(name) or "")
        for name in ("session_id", "task_id", "turn_id", "api_request_id")
    ) + ("" if kwargs.get("api_request_id") else f":{kwargs.get('api_call_count') or ''}")


def _factory_properties() -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in ("job_id", "attempt_id"):
        value = os.environ.get(f"FACTORY_OBSERVABILITY_{field.upper()}", "")
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
            properties[f"factory.{field}"] = value.lower()
    issue = os.environ.get("FACTORY_OBSERVABILITY_LINEAR_ISSUE", "")
    if re.fullmatch(r"[A-Z][A-Z0-9]{0,31}-[1-9][0-9]{0,14}", issue):
        properties["factory.linear_issue"] = issue
    generation = os.environ.get("FACTORY_OBSERVABILITY_GENERATION", "")
    if "factory.attempt_id" in properties and re.fullmatch(r"[1-9][0-9]{0,15}", generation) and int(generation) <= 9007199254740991:
        properties["factory.generation"] = int(generation)
    return properties


def _trace_id(kwargs: dict[str, Any]) -> str:
    factory = _factory_properties()
    if factory.get("factory.attempt_id"):
        scope = f"attempt:{factory['factory.attempt_id']}"
    elif factory.get("factory.job_id"):
        scope = f"job:{factory['factory.job_id']}"
    else:
        # A standalone conversation retains one trace across turns.
        scope = f"session:{kwargs.get('session_id') or _PROCESS_TRACE}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"driftward-hermes:trace:{scope}"))


def _span_id(kwargs: dict[str, Any]) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"driftward-hermes:span:{_request_key(kwargs)}"))


def _deliver(event: dict[str, Any]) -> None:
    host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
    key = os.environ.get("POSTHOG_API_KEY", "").strip()
    try:
        response = requests.post(
            f"{host}/batch/",
            json={"api_key": key, "batch": [event]},
            timeout=(1, 2),
        )
        response.raise_for_status()
        if os.environ.get("HERMES_POSTHOG_DELIVERY_PROOF", "").lower() in {"1", "true", "yes", "on"}:
            properties = event["properties"]
            print(
                "PostHog AI delivery accepted: "
                f"status={response.status_code} event={event['event']} model={properties.get('$ai_model')} "
                f"input_tokens={properties.get('$ai_input_tokens')} "
                f"output_tokens={properties.get('$ai_output_tokens')} "
                f"session_id={properties.get('$ai_session_id')}",
                file=sys.stderr,
                flush=True,
            )
    except Exception:
        logger.debug("PostHog AI event delivery failed", exc_info=True)


def _worker() -> None:
    while True:
        event = _EVENTS.get()
        try:
            _deliver(event)
        finally:
            _EVENTS.task_done()


def _flush_at_exit() -> None:
    deadline = time.monotonic() + 3
    while _EVENTS.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


def _enqueue(event: dict[str, Any]) -> None:
    global _WORKER_STARTED
    if not _enabled():
        return
    with _LOCK:
        if not _WORKER_STARTED:
            threading.Thread(target=_worker, daemon=True, name="posthog-ai-observer").start()
            _WORKER_STARTED = True
    try:
        _EVENTS.put_nowait(event)
    except queue.Full:
        logger.warning("PostHog AI event queue is full; dropping event")


def _event(kwargs: dict[str, Any], *, error: str = "") -> dict[str, Any]:
    usage = kwargs.get("usage") if isinstance(kwargs.get("usage"), dict) else {}
    with _LOCK:
        started = _PENDING.pop(_request_key(kwargs), {})
    duration = kwargs.get("api_duration") or (time.monotonic() - started.get("started", time.monotonic()))
    properties: dict[str, Any] = {
        "$ai_model": str(kwargs.get("response_model") or kwargs.get("model") or "unknown"),
        "$ai_provider": str(kwargs.get("provider") or "unknown"),
        "$ai_latency": round(float(duration), 3),
        "$ai_input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "$ai_output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "$ai_trace_id": _trace_id(kwargs),
        "$ai_span_id": _span_id(kwargs),
        "$ai_session_id": str(kwargs.get("session_id") or ""),
        "hermes.session_id": str(kwargs.get("session_id") or ""),
        "hermes.task_id": str(kwargs.get("task_id") or ""),
        "hermes.turn_id": str(kwargs.get("turn_id") or ""),
        "hermes.request_id": str(kwargs.get("api_request_id") or ""),
        "hermes.api_mode": str(kwargs.get("api_mode") or ""),
        "hermes.finish_reason": str(kwargs.get("finish_reason") or ""),
        "hermes.input_chars": int(kwargs.get("request_char_count", started.get("request_char_count")) or 0),
        "hermes.output_chars": int(kwargs.get("assistant_content_chars") or 0),
        "$process_person_profile": False,
        **_factory_properties(),
    }
    if error:
        properties["$ai_is_error"] = True
        properties["$ai_error"] = error[:500]
    return {
        "event": "$ai_generation",
        "distinct_id": "driftward-hermes",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "properties": properties,
    }


def on_pre_api_request(**kwargs: Any) -> None:
    if _enabled():
        with _LOCK:
            _PENDING[_request_key(kwargs)] = {
                "started": time.monotonic(),
                "request_char_count": int(kwargs.get("request_char_count") or 0),
            }


def on_post_api_request(**kwargs: Any) -> None:
    _enqueue(_event(kwargs))


def on_api_request_error(**kwargs: Any) -> None:
    error_type = str(kwargs.get("error_type") or "").strip()
    if not error_type:
        error = kwargs.get("error")
        if isinstance(error, dict):
            error_type = str(error.get("type") or "RequestError").strip()
        else:
            error_type = type(error).__name__ if error is not None else "RequestError"
    if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", error_type):
        error_type = "RequestError"
    _enqueue(_event(kwargs, error=error_type))


def on_post_tool_call(**kwargs: Any) -> None:
    # Native hooks supply duration/status; never inspect args, result, or error_message.
    trace_id = _trace_id(kwargs)
    call_id = str(kwargs.get("tool_call_id") or uuid.uuid4())
    tool_name = str(kwargs.get("tool_name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool_name):
        tool_name = "tool_call"
    status = kwargs.get("status")
    status = status if status in {"ok", "error", "blocked"} else "unknown"
    properties: dict[str, Any] = {
        "$ai_trace_id": trace_id,
        "$ai_span_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"driftward-hermes:tool:{_request_key(kwargs)}:{call_id}")),
        "$ai_parent_id": _span_id(kwargs) if kwargs.get("api_request_id") else trace_id,
        "$ai_span_name": tool_name,
        "$ai_session_id": str(kwargs.get("session_id") or ""),
        "$ai_latency": round(max(0, float(kwargs.get("duration_ms") or 0)) / 1000, 3),
        "hermes.session_id": str(kwargs.get("session_id") or ""),
        "hermes.task_id": str(kwargs.get("task_id") or ""),
        "hermes.turn_id": str(kwargs.get("turn_id") or ""),
        "hermes.request_id": str(kwargs.get("api_request_id") or ""),
        "hermes.tool_call_id": call_id,
        "hermes.tool_status": status,
        "$process_person_profile": False,
        **_factory_properties(),
    }
    if status in {"error", "blocked"}:
        error_type = str(kwargs.get("error_type") or "")
        properties["$ai_is_error"] = True
        properties["$ai_error"] = error_type if re.fullmatch(r"[A-Za-z0-9_]{1,128}", error_type) else "ToolError"
    _enqueue({
        "event": "$ai_span",
        "distinct_id": "driftward-hermes",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "properties": properties,
    })


def register(ctx: Any) -> None:
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("post_tool_call", on_post_tool_call)


atexit.register(_flush_at_exit)
