"""Explicit reviewer-only Hermes observer. Export metadata, never execution content."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

HOOKS = ("pre_api_request", "post_api_request", "api_request_error", "post_tool_call")


def review_metadata(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"role", "review_id", "head_sha", "pass_number", "repository"}
            or value.get("role") != "reviewer"
            or not isinstance(value.get("review_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["review_id"])
            or not isinstance(value.get("head_sha"), str) or not re.fullmatch(r"[a-f0-9]{40}", value["head_sha"])
            or type(value.get("pass_number")) is not int or value["pass_number"] not in {1, 2}
            or not isinstance(value.get("repository"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"])
            or len(value["repository"]) > 300 or any(p in {".", ".."} for p in value["repository"].split("/"))):
        return {}
    return dict(value)


def trace_id(metadata: dict) -> str:
    scope = json.dumps([metadata[k] for k in ("repository", "review_id", "head_sha", "pass_number")], separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "driftward-factory:reviewer:trace:" + scope))


def endpoint(host: str) -> str:
    url = urlsplit(host)
    if (len(host) > 300 or any(c.isspace() for c in host) or "?" in host or "#" in host
            or url.scheme != "https" or not url.hostname
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", url.hostname)
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.path not in {"", "/"} or url.port not in {None, 443}):
        raise ValueError("invalid PostHog endpoint")
    return "https://" + url.hostname + "/batch/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def number(value: object, maximum: float) -> float:
    try:
        return min(maximum, max(0, float(value))) if type(value) in {int, float} and math.isfinite(value) else 0
    except (OverflowError, ValueError):
        return 0


class ReviewObserver:
    def __init__(self, metadata: dict, session: str, tools: set[str], config: dict):
        self.metadata = review_metadata(metadata)
        self.profile = "factory-independent-reviewer" if os.environ.get("HERMES_HOME") == "/opt/data/profiles/factory-independent-reviewer" else None
        self.session, self.tools = session, tools
        self.key = config.get("POSTHOG_API_KEY", "").strip()
        self.url = endpoint(config.get("POSTHOG_HOST") or "https://us.i.posthog.com") if self.key else ""
        self.enabled = bool(self.key and len(self.key) <= 1024 and self.metadata)
        self.trace = trace_id(self.metadata) if self.metadata else ""
        self.events = queue.Queue(maxsize=128)
        self.pending = {}
        self.lock = threading.Lock()
        self.handles = []
        self.worker = None

    def attach(self) -> None:
        if not self.enabled:
            return
        from hermes_cli.plugins import PluginContext, get_plugin_manager
        from hermes_cli.plugins_manifest import PluginManifest
        context = PluginContext(PluginManifest(name="factory-review-observer"), get_plugin_manager())
        for hook in HOOKS:
            def callback(_hook=hook, **kwargs):
                try:
                    self.capture(_hook, kwargs)
                except Exception:
                    pass
            self.handles.append(context.register_hook(hook, callback))

    def span(self, request: str, tool: str = "") -> str:
        return str(uuid.uuid5(uuid.UUID(self.trace), ("tool:" + tool + ":" if tool else "generation:") + request))

    def capture(self, hook: str, value: dict) -> None:
        request = value.get("api_request_id")
        if (not self.enabled or value.get("session_id") != self.session
                or not isinstance(request, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", request)):
            return
        now = time.time()
        if hook == "pre_api_request":
            with self.lock:
                if len(self.pending) < 64:
                    self.pending[request] = (now, time.monotonic())
            return
        properties = {"$ai_trace_id": self.trace, "$ai_session_id": self.session,
            "$process_person_profile": False, **{"factory." + k: v for k, v in self.metadata.items()},
            "hermes.request_id": request}
        if self.profile:
            properties["factory.profile"] = self.profile
        if hook == "post_tool_call":
            name, call = value.get("tool_name"), value.get("tool_call_id")
            if name not in self.tools or not isinstance(call, str) or not 1 <= len(call) <= 240:
                return
            duration = number(value.get("duration_ms"), 900000) / 1000
            properties.update({"$ai_span_id": self.span(request, call), "$ai_parent_id": self.span(request),
                "$ai_span_name": name, "$ai_latency": duration,
                "hermes.tool_status": value.get("status") if value.get("status") in {"ok", "error", "blocked"} else "unknown"})
            if value.get("status") in {"error", "blocked"}:
                properties.update({"$ai_is_error": True, "$ai_error": "ToolError"})
            started = now - duration
            event = "$ai_span"
        elif hook in {"post_api_request", "api_request_error"}:
            with self.lock:
                started, monotonic = self.pending.pop(request, (now, time.monotonic()))
            duration = number(value.get("api_duration", time.monotonic() - monotonic), 900)
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            properties.update({"$ai_span_id": self.span(request), "$ai_parent_id": self.trace,
                "$ai_model": "gpt-6-astra", "$ai_provider": "openai-codex", "$ai_latency": duration,
                "$ai_input_tokens": int(number(usage.get("input_tokens", usage.get("prompt_tokens")), 9007199254740991)),
                "$ai_output_tokens": int(number(usage.get("output_tokens", usage.get("completion_tokens")), 9007199254740991))})
            if hook == "api_request_error":
                properties.update({"$ai_is_error": True, "$ai_error": "RequestError"})
            event = "$ai_generation"
        else:
            return
        item = {"event": event, "distinct_id": "driftward-factory-reviewer",
            "timestamp": datetime.fromtimestamp(started, timezone.utc).isoformat(), "properties": properties}
        with self.lock:
            if self.worker is None:
                self.worker = threading.Thread(target=self.export, daemon=True, name="factory-review-posthog")
                self.worker.start()
        try:
            self.events.put_nowait(item)
        except queue.Full:
            pass

    def export(self) -> None:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        while True:
            item = self.events.get()
            try:
                payload = json.dumps({"api_key": self.key, "batch": [item]}).encode()
                request = Request(self.url, data=payload, headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=1) as response:
                    if not 200 <= response.status < 300:
                        raise ValueError("PostHog delivery failed")
            except Exception:
                pass
            finally:
                self.events.task_done()

    def close(self) -> None:
        for handle in self.handles:
            try:
                handle.dispose()
            except Exception:
                pass
        deadline = time.monotonic() + 1.5
        while self.events.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)


def attach_observer(metadata: dict, session: str, tools: set[str], config: dict) -> ReviewObserver | None:
    observer = None
    try:
        observer = ReviewObserver(metadata, session, tools, config)
        observer.attach()
        return observer
    except Exception:
        if observer is not None:
            try:
                observer.close()
            except Exception:
                pass
        return None
