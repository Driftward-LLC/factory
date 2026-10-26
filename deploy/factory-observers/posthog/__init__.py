"""Metadata-only PostHog AI observability for Hermes.

The observer never exports prompts, completions, tool arguments, or tool results.
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import queue
import re
import selectors
import shlex
import shutil
import signal
import subprocess
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
_TOOL_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("factory_github_tool", default={})
_GITHUB_COMMANDS = {
    "pr": {"checks", "checkout", "close", "comment", "create", "diff", "edit", "list", "merge", "ready", "reopen", "review", "status", "view"},
    "issue": {"close", "comment", "create", "delete", "edit", "list", "reopen", "status", "view"},
    "repo": {"clone", "create", "fork", "list", "sync", "view"},
    "run": {"cancel", "download", "list", "rerun", "view", "watch"},
    "workflow": {"disable", "enable", "list", "run", "view"},
    "release": {"create", "delete", "download", "edit", "list", "upload", "view"},
}
_GITHUB_SCHEMA = {
    "name": "factory_github",
    "description": "Run the existing local gh CLI without a shell. Use for GitHub CLI/API operations; returns exit_code, stdout and stderr (100,000 bytes each; larger output stops the process). Native command approval applies. Noninteractive; remote effects are not verified by telemetry. Set workdir explicitly; terminal session exports/cwd are not inherited.",
    "parameters": {"type": "object", "properties": {
        "args": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "string", "maxLength": 65536}, "description": "gh arguments, e.g. [pr, view, 123]; never include the gh executable. At most 262,144 characters total."},
        "stdin": {"type": "string", "maxLength": 1048576, "description": "Optional literal standard input; never exported to telemetry."},
        "workdir": {"type": "string", "maxLength": 4096},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 180, "default": 60},
    }, "required": ["args"], "additionalProperties": False},
}


def _tool_span_id(kwargs: dict[str, Any], call_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"driftward-hermes:tool:{_request_key(kwargs)}:{call_id}"))


def _github_operation(args: list[str]) -> str:
    # These label CLI invocations, never inferred remote effects or resource names.
    help_flags = {"--help", "-h", "--version"}
    if args and args[0] in help_flags | {"help", "version"}:
        return "help_or_version"
    if args and (args[0] in _GITHUB_COMMANDS or args[0] == "api"):
        if len(args) > 1 and args[1] in help_flags:
            return "help_or_version"
        if (len(args) > 2 and args[2] in help_flags
                and (args[1] in _GITHUB_COMMANDS.get(args[0], set())
                     or (args[0] == "api" and not args[1].startswith("-")))):
            return "help_or_version"
    # Later flags might be option payloads; do not attempt to parse all gh options.
    if any(item in help_flags for item in args[3:]):
        return "unknown"
    if args and args[0] == "api":
        return "api.graphql" if len(args) > 1 and args[1] == "graphql" else "api"
    if len(args) > 1 and args[1] in _GITHUB_COMMANDS.get(args[0], set()):
        return f"{args[0]}.{args[1]}"
    return "unknown"


def _github_available() -> bool:
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config_readonly
        from gateway.session_context import get_session_env
        return (str(get_hermes_home()) == "/opt/data/profiles/factory-executor"
                and (load_config_readonly() or {}).get("terminal", {}).get("backend", "local") == "local"
                and get_session_env("TERMINAL_ENV", "local") == "local"
                and shutil.which("gh") is not None)
    except Exception:
        # Optional tool discovery must not disable the existing generation/tool observer.
        return False


def _github_context(*, tool_name: str, args: dict[str, Any], next_call: Any, **kwargs: Any) -> Any:
    if tool_name != "factory_github":
        return next_call(args)
    token = _TOOL_CONTEXT.set({key: kwargs.get(key) for key in
                               ("session_id", "task_id", "turn_id", "api_request_id", "tool_call_id")})
    try:
        return next_call(args)
    finally:
        _TOOL_CONTEXT.reset(token)


def _github_event(context: dict[str, Any], operation: str, status: str, exit_code: int | None,
                  started: float) -> dict[str, Any]:
    call_id = str(context.get("tool_call_id") or "")
    properties = {
        "$ai_trace_id": _trace_id(context), "$ai_span_id": str(uuid.uuid4()),
        "$ai_parent_id": _tool_span_id(context, call_id), "$ai_span_name": f"github.{operation}",
        "$ai_session_id": str(context.get("session_id") or ""),
        "$ai_latency": round(max(0, time.monotonic() - started), 3),
        "hermes.session_id": str(context.get("session_id") or ""),
        "hermes.task_id": str(context.get("task_id") or ""),
        "hermes.turn_id": str(context.get("turn_id") or ""),
        "hermes.request_id": str(context.get("api_request_id") or ""),
        "hermes.tool_call_id": call_id,
        "github.cli_operation": operation, "github.cli_status": status,
        "github.observation": "cli_invocation",
        "github.process_started": status not in {"spawn_error", "interrupted_before_spawn"},
        "github.effect_status": "unverified", "github.coverage": "controlled_tool_only",
        "$process_person_profile": False, **_factory_properties(),
    }
    if exit_code is not None:
        properties["github.exit_code"] = exit_code
    if status != "success":
        properties.update({"$ai_is_error": True, "$ai_error": {
            "nonzero_exit": "GitHubCliExit", "timeout": "GitHubCliTimeout", "spawn_error": "GitHubCliSpawnError",
            "output_limit": "GitHubCliOutputLimit",
            "interrupted": "GitHubCliInterrupted", "interrupted_before_spawn": "GitHubCliInterrupted",
        }[status]})
    return {"event": "$ai_span", "distinct_id": "driftward-hermes",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "properties": properties}


def _run_github(argv: list[str], stdin: str, workdir: str | None, timeout: int, environment: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Bound both captured streams while preserving their bytes below the limit."""
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"error": "GitHub CLI interrupted before launch; no process started.", "exit_code": None}, "interrupted_before_spawn"
    output = {"stdout": bytearray(), "stderr": bytearray()}
    response: dict[str, Any] = {"stdout_truncated": False, "stderr_truncated": False}
    status = "success"
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False, cwd=workdir, env=environment, start_new_session=True)
    except OSError:
        return {"error": "GitHub CLI process could not start.", "exit_code": None}, "spawn_error"
    with process:
        try:
            with selectors.DefaultSelector() as selector:
                for name in ("stdout", "stderr"):
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                pending = memoryview(stdin.encode("utf-8"))
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    if is_interrupted():
                        status = "interrupted"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = "timeout"
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        if key.data == "stdin":
                            try:
                                written = os.write(key.fd, pending[:65536])
                                pending = pending[written:]
                            except BrokenPipeError:
                                pending = memoryview(b"")
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        data = os.read(key.fd, 32768)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        available = 100000 - len(output[key.data])
                        output[key.data].extend(data[:available])
                        if len(data) > available:
                            response[f"{key.data}_truncated"] = True
                            status = "output_limit"
                            break
                    if status != "success":
                        break
                while status == "success":
                    if is_interrupted():
                        status = "interrupted"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = "timeout"
                        break
                    try:
                        process.wait(timeout=min(remaining, 0.1))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        finally:
            if status != "success" or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        response["exit_code"] = process.returncode
    response.update({name: value.decode("utf-8", "replace") for name, value in output.items()})
    if status == "success" and response["exit_code"] != 0:
        status = "nonzero_exit"
        response["error"] = "GitHub CLI exited unsuccessfully; remote effect is unknown."
    if status in {"timeout", "output_limit"}:
        response["error"] = "GitHub CLI stopped at its time or output limit; remote effect is unknown."
    if status == "interrupted":
        response["error"] = "GitHub CLI interrupted; remote effect is unknown."
    return response, status


def _github_tool(args: dict[str, Any], **kwargs: Any) -> str:
    argv = args.get("args")
    timeout = args.get("timeout", 60)
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 256
            or not all(isinstance(item, str) and "\0" not in item and len(item) <= 65536 for item in argv)
            or sum(len(item) for item in argv) > 262144
            or type(timeout) is not int or not 1 <= timeout <= 180
            or not isinstance(args.get("stdin", ""), str) or len(args.get("stdin", "")) > 1048576
            or ("workdir" in args and (not isinstance(args["workdir"], str)
                                      or "\0" in args["workdir"] or len(args["workdir"]) > 4096))):
        return json.dumps({"error": "Invalid factory_github arguments; no process started."})
    try:
        if sum(len(item.encode("utf-8")) for item in argv) > 262144 or len(args.get("stdin", "").encode("utf-8")) > 1048576:
            raise ValueError
    except (UnicodeError, ValueError):
        return json.dumps({"error": "GitHub CLI arguments/input exceed UTF-8 byte limits; no process started."})
    if not _github_available():
        return json.dumps({"error": "factory_github requires the existing local terminal backend and gh executable."})
    context = _TOOL_CONTEXT.get()
    if not context.get("tool_call_id"):
        return json.dumps({"error": "factory_github requires native tool execution context; no process started."})
    try:
        # Match the local terminal's native approval and scoped secret-scrub policy.
        from tools.terminal_tool import _check_all_guards
        from tools.environments.local import build_subprocess_env
        decision = _check_all_guards(shlex.join(["gh", *argv]), "local")
        if not decision.get("approved"):
            return json.dumps({"error": "GitHub CLI blocked by native command approval; no process started."})
        environment = build_subprocess_env(strip_launch_profile=True)
    except Exception:
        return json.dumps({"error": "GitHub CLI native execution guards unavailable; no process started."})
    started = time.monotonic()
    response, status = _run_github(["gh", *argv], args.get("stdin", ""), args.get("workdir"), timeout, environment)
    try:
        _enqueue(_github_event(context, _github_operation(argv), status, response["exit_code"], started))
    except Exception:
        # Telemetry must neither retry an operation nor change its returned result.
        logger.debug("GitHub CLI telemetry unavailable")
    return json.dumps(response)


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
    from hermes_constants import get_hermes_home
    if str(get_hermes_home()) == "/opt/data/profiles/factory-executor":
        properties.update({"factory.role": "executor", "factory.profile": "factory-executor"})
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


def observe_owner(event: object) -> bool:
    """Optional native-owner metadata uses this installed observer's queue/trace."""
    if not isinstance(event, dict) or not isinstance(event.get('properties'), dict):
        return False
    p = event['properties']
    fields = {'factory.invocation_id', 'factory.lease_epoch', 'factory.experimental', 'factory.stop.stage',
              'hermes.session_id', 'hermes.task_id', 'factory.runtime_incarnation', '$process_person_profile'}
    if (set(p) != fields or p['factory.experimental'] is not True or p['$process_person_profile'] is not False
            or not isinstance(p['factory.stop.stage'], str) or p['factory.stop.stage'] not in {'registered', 'stopping', 'unknown'}
            or type(p['factory.lease_epoch']) is not int or not 0 < p['factory.lease_epoch'] <= 9007199254740991
            or not isinstance(p['factory.invocation_id'], str)
            or not re.fullmatch(r'[a-f0-9-]{36}:[a-f0-9-]{36}:' + str(p['factory.lease_epoch']), p['factory.invocation_id'])
            or any(not isinstance(p[key], str) or not re.fullmatch(r'factory-[a-f0-9]{32}', p[key])
                   for key in ('hermes.session_id', 'hermes.task_id'))
            or not isinstance(p['factory.runtime_incarnation'], str)
            or not re.fullmatch(r'[a-f0-9-]{36}', p['factory.runtime_incarnation'])):
        return False
    trace = _trace_id({'session_id': p['hermes.session_id']})
    span = str(uuid.uuid5(uuid.NAMESPACE_URL, 'factory-native-owner:' + p['factory.invocation_id'] + ':' + p['factory.stop.stage']))
    properties = {**p, **_factory_properties(), '$ai_trace_id': trace, '$ai_parent_id': trace,
                  '$ai_span_id': span, '$ai_session_id': p['hermes.session_id'],
                  '$ai_span_name': 'factory.native_owner.' + p['factory.stop.stage']}
    try:
        _enqueue({'event': '$ai_span', 'distinct_id': 'driftward-hermes',
                  'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'properties': properties})
        return True
    except Exception:
        return False


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
        "hermes.input_chars": int(kwargs.get("request_char_count") or 0),
        "hermes.output_chars": int(kwargs.get("assistant_content_chars") or 0),
        "$process_person_profile": False,
        **_factory_properties(),
    }
    if error:
        properties["$ai_is_error"] = True
        properties["$ai_error"] = error[:500]
    else:
        # Read only names from the native message; never serialize its content or arguments.
        message = kwargs.get("assistant_message")
        calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
        if isinstance(calls, (list, tuple)):
            names = []
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
                name = function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
                names.append(name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name) else "tool_call")
            properties["$ai_tools_called"] = ",".join(names)
            properties["$ai_tool_call_count"] = len(calls)
        else:
            count = kwargs.get("assistant_tool_call_count")
            if type(count) is int and 0 <= count <= 9007199254740991:
                properties["$ai_tool_call_count"] = count
    return {
        "event": "$ai_generation",
        "distinct_id": "driftward-hermes",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "properties": properties,
    }


def on_pre_api_request(**kwargs: Any) -> None:
    if _enabled():
        with _LOCK:
            _PENDING[_request_key(kwargs)] = {"started": time.monotonic()}


def on_post_api_request(**kwargs: Any) -> None:
    _enqueue(_event(kwargs))


def on_api_request_error(**kwargs: Any) -> None:
    error_type = str(kwargs.get("error_type") or "").strip()
    if not error_type:
        error = kwargs.get("error")
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
        "$ai_span_id": _tool_span_id(kwargs, call_id),
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
        "factory.external_operation_coverage": "controlled_github_cli" if tool_name == "factory_github" else "unknown",
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
    if _github_available():
        ctx.register_tool(name="factory_github", toolset="terminal", schema=_GITHUB_SCHEMA,
                          handler=_github_tool, check_fn=_github_available)
        ctx.register_middleware("tool_execution", _github_context)
        ctx.register_system_prompt_section("factory-github", (
            "For GitHub CLI/API operations, prefer factory_github with an argv array and explicit workdir. "
            "Existing permissions and approval requirements still apply. Telemetry observes only this "
            "tool's local gh invocation and exit, and never confirms remote effects. GitHub calls through "
            "terminal, execute_code, scripts, direct HTTP, git or alternate executables have unknown coverage."
        ), max_chars=1024)


atexit.register(_flush_at_exit)
