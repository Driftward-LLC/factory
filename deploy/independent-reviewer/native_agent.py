"""One native Hermes invocation. Only the host relay can reach the source sandbox."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time

MODEL = "gpt-6-astra"
TOOLSET = "factory-review-source"
PREFIX = "factory_source_"
API_FILES = ("run_agent.py", "agent/agent_init.py", "agent/agent_init_fallback.py", "agent/secret_scope.py",
             "agent/turn_api_request.py", "agent/tool_executor.py", "hermes_constants.py", "hermes_cli/auth.py",
             "hermes_cli/auth_codex.py", "hermes_cli/env_loader.py", "hermes_cli/plugins_discovery.py",
             "tools/registry.py", "toolsets.py", "model_tools.py", "tools/tool_search.py",
             "agent/chat_completion_helpers.py", "agent/tool_dispatch_helpers.py",
             "agent/codex_responses_adapter.py", "agent/vision_message_prep.py")
API_DIGEST = "88100e8471034d13ef8146701abf1401c3b38144b8ab1e2842a7ac6dab279e6a"
PROPERTIES = {
    "inspect_source": {}, "read_file": {"path": {"type": "string"}}, "diff": {},
    "run_check": {"argv": {"type": "array", "items": {"type": "string"}}},
    "start_preview": {"argv": {"type": "array", "items": {"type": "string"}}},
    "browser_check": {"url": {"type": "string"}},
    "validate_findings": {"findings": {"type": "array", "items": {"type": "object"}}},
}
CONFIG = {"model": {"default": MODEL, "provider": "openai-codex"},
          "fallback_providers": [], "plugins": {"enabled": []}, "mcp_servers": {},
          "tools": {"tool_search": {"enabled": "off"}, "connectors": {"enabled": False}},
          "context": {"engine": "compressor"},
          "compression": {"enabled": False}, "memory": {"enabled": False},
          "agent": {"background_review": {"enabled": False}}}
REQUEST_LIMIT = 1024 * 1024


def request_frame(session: str, prompt: str) -> bytes:
    frame = (json.dumps({"session": session, "prompt": prompt}, ensure_ascii=False) + "\n").encode("utf-8")
    if len(frame) > REQUEST_LIMIT:
        raise ValueError("native review request exceeds byte budget")
    return frame


def read_request(stream) -> dict:
    frame = stream.readline(REQUEST_LIMIT + 1)
    if not frame.endswith(b"\n") or len(frame) > REQUEST_LIMIT:
        raise ValueError("native review request exceeds byte budget")
    request = json.loads(frame)
    if (not isinstance(request, dict) or set(request) != {"session", "prompt"}
            or not isinstance(request["session"], str) or not isinstance(request["prompt"], str)):
        raise ValueError("invalid native review request")
    return request


def profile_path(session: str) -> Path:
    if not re.fullmatch(r"factory-reviewer:[a-f0-9-]{36}", session):
        raise ValueError("invalid native reviewer identity")
    return Path(os.environ["HERMES_HOME"]) / "profiles" / session.replace(":", "-")


def start_ticks(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def stop(session: str) -> None:
    """A missing/ambiguous marker is not evidence that a docker-exec child stopped."""
    profile = profile_path(session)
    marker = json.loads((profile / "invocation.json").read_text())
    pid = marker["pid"]
    try:
        ticks = start_ticks(pid)
    except FileNotFoundError:
        ticks = None
    if ticks is not None:
        if (ticks != marker["start_ticks"] or marker["session"] != session
                or session.encode() not in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                or os.getpgid(pid) != pid):
            raise RuntimeError("ambiguous native reviewer process; retain profile for inspection")
        os.killpg(pid, signal.SIGKILL)
        for _ in range(100):
            try:
                if start_ticks(pid) != ticks:
                    break
                # A zombie cannot make a provider call and is reaped by docker exec's parent.
                if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("native reviewer termination unconfirmed")
    shutil.rmtree(profile)


def guard(agent) -> None:
    names = {PREFIX + name for name in PROPERTIES}
    if (agent.model != MODEL or agent.provider != "openai-codex" or agent.api_mode != "codex_responses"
            or agent.enabled_toolsets != [TOOLSET] or agent.valid_tool_names != names
            or {t["function"]["name"] for t in agent.tools} != names
            or len(agent.tools) != len(names) or agent._fallback_chain
            or agent.compression_enabled):
        raise RuntimeError("native reviewer capability drift")


def review(request: dict, channel_in, channel_out) -> dict:
    # Use Hermes' routed-profile secret scope: its loader skips both default and project dotenv.
    from hermes_constants import set_hermes_home_override
    profile = profile_path(request["session"])
    set_hermes_home_override(profile)
    from agent.secret_scope import set_multiplex_context, set_secret_scope
    set_multiplex_context(True)
    set_secret_scope({}, profile_home=str(profile))
    from hermes_cli.plugins_discovery import suppress_plugin_discovery
    with suppress_plugin_discovery():
        from run_agent import AIAgent
        from tools.registry import registry

        lock = threading.Lock()
        calls = set()

        def handler(name):
            def invoke(args):
                if not isinstance(args, dict) or set(args) != set(PROPERTIES[name]):
                    raise ValueError("unsupported source arguments")
                with lock:
                    channel_out.write(json.dumps({"tool": name, "arguments": args}) + "\n")
                    channel_out.flush()
                    raw = channel_in.readline(4 * 1024 * 1024 + 1)
                    if not raw.endswith("\n") or len(raw) > 4 * 1024 * 1024:
                        raise ValueError("source response exceeds boundary")
                    response = json.loads(raw)
                    if response.get("isError"):
                        return json.dumps({"error": "Source check unavailable; return inconclusive"})
                    content = []
                    for block in response["content"]:
                        if block["type"] == "text":
                            content.append(block)
                        elif block["type"] == "image" and block["mimeType"] == "image/png":
                            content.append({"type": "image_url", "image_url": {
                                "url": "data:image/png;base64," + block["data"]}})
                        else:
                            raise ValueError("unsupported source content")
                    calls.add(name)
                    return {"_multimodal": True, "content": content}
            return invoke

        for name, properties in PROPERTIES.items():
            registry.register(name=PREFIX + name, toolset=TOOLSET,
                schema={"name": PREFIX + name, "description": name.replace("_", " "),
                    "parameters": {"type": "object", "additionalProperties": False,
                                   "properties": properties, "required": list(properties)}},
                handler=handler(name))

        class Reviewer(AIAgent):
            def _build_api_kwargs(self, *args, **kwargs):
                guard(self)
                value = super()._build_api_kwargs(*args, **kwargs)
                tools = value.get("tools", [])
                names = {t.get("name", t.get("function", {}).get("name")) for t in tools}
                if value.get("model") != MODEL or names != self.valid_tool_names or len(tools) != len(names):
                    raise RuntimeError("native reviewer request capability drift")
                return value

            def _execute_tool_calls(self, message, *args, **kwargs):
                guard(self)
                if any(call.function.name not in self.valid_tool_names for call in message.tool_calls):
                    raise RuntimeError("native reviewer denied tool")
                return super()._execute_tool_calls(message, *args, **kwargs)

        agent = Reviewer(model=MODEL, provider="openai-codex", api_mode="codex_responses",
            enabled_toolsets=[TOOLSET], max_iterations=40, run_budget_seconds=480,
            fallback_model=[], session_id=request["session"], session_db=None,
            quiet_mode=True, save_trajectories=False, verbose_logging=False,
            skip_context_files=True, load_soul_identity=False, skip_memory=True,
            skip_background_review=True, checkpoints_enabled=False, cwd=str(profile))
        try:
            guard(agent)
            result = agent.run_conversation(request["prompt"])
            guard(agent)
            if result.get("error") or result.get("interrupted") or not {"read_file", "diff"}.intersection(calls):
                raise ValueError("review did not complete source inspection")
            raw = result.get("final_response", "")
            if not isinstance(raw, str) or len(raw) > 20000:
                raise ValueError("invalid bounded model result")
            return json.loads(raw)
        finally:
            agent.close()


def main() -> None:
    # Preserve HOME only for Hermes' own root resolution; never import platform/provider environment.
    home = os.environ.get("HOME", "/root")
    os.environ.clear()
    os.environ.update({"HOME": home, "HERMES_HOME": "/opt/data",
                       "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
    sys.path.insert(0, "/opt/hermes")
    session = sys.argv[-1]
    if sys.argv[1] == "--stop":
        stop(session)
        return
    fingerprint = hashlib.sha256()
    for name in API_FILES:
        fingerprint.update(name.encode() + b"\0")
        fingerprint.update((Path("/opt/hermes") / name).read_bytes())
    if fingerprint.hexdigest() != API_DIGEST:
        raise RuntimeError("installed Hermes API differs from reviewed native adapter")
    profile = profile_path(session)
    # A single writer owns a fresh profile; existing profiles can never resume a review.
    profile.parent.mkdir(mode=0o700, exist_ok=True)
    profile.mkdir(mode=0o700, parents=False)
    if os.getpgrp() != os.getpid():
        os.setsid()
    (profile / "invocation.json").write_text(json.dumps({"pid": os.getpid(),
        "start_ticks": start_ticks(os.getpid()), "session": session}))
    (profile / "config.yaml").write_text(json.dumps(CONFIG))
    os.chmod(profile / "invocation.json", 0o600)
    os.chdir(profile)
    # Independent of the host docker-exec client's lifetime. Never signal the shared runtime.
    watchdog = threading.Timer(540, lambda: os.killpg(os.getpid(), signal.SIGKILL))
    watchdog.daemon = True
    watchdog.start()
    request = read_request(sys.stdin.buffer)
    if request["session"] != session:
        raise ValueError("native reviewer identity mismatch")
    channel_in, channel_out = sys.stdin, sys.stdout
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        assessment = review(request, channel_in, channel_out)
    channel_out.write(json.dumps({"assessment": assessment}) + "\n")
    channel_out.flush()
    watchdog.cancel()


if __name__ == "__main__":
    main()
