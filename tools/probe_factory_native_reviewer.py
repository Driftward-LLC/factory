"""Credential-free installed SDK probe; never calls a provider or creates a live profile.

Run: python3 tools/probe_factory_native_reviewer.py hermes-driftward-hermes-1
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def inside(script: str) -> None:
    native = ModuleType("native_probe")
    exec(compile(script, "native_agent.py", "exec"), native.__dict__)
    with tempfile.TemporaryDirectory(prefix="factory-sdk-probe-") as temporary:
        os.environ.clear()
        os.environ.update({"HOME": temporary, "HERMES_HOME": temporary,
                           "PATH": "/usr/local/bin:/usr/bin:/bin"})
        session = "factory-reviewer:" + "a" * 36
        profile = native.profile_path(session)
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(json.dumps(native.CONFIG))
        sys.path.insert(0, "/opt/hermes")
        os.chdir(profile)
        # Bind the native profile scope before any import that can load dotenv or config.
        from hermes_constants import set_hermes_home_override
        from agent.secret_scope import set_multiplex_context, set_secret_scope
        set_hermes_home_override(profile)
        set_multiplex_context(True)
        set_secret_scope({}, profile_home=str(profile))
        from hermes_cli.plugins_discovery import suppress_plugin_discovery
        with suppress_plugin_discovery():
            from run_agent import AIAgent
            from agent import agent_init
            from tools.registry import registry
            from agent.tool_dispatch_helpers import make_tool_result_message
            original_client = agent_init._build_client
            checks = []

            def fake_client(agent, *_):
                original_client(agent, "synthetic-not-a-real-key", "https://chatgpt.com/backend-api/codex", [])

            def conversation(agent, prompt):
                native.guard(agent)
                checks.append("native_constructor_and_exact_tools")
                text = registry.dispatch(native.PREFIX + "read_file", {"path": "app.py"})
                image = registry.dispatch(native.PREFIX + "browser_check", {"url": "http://localhost:3000"})
                assert text["_multimodal"] and text["content"][0]["text"] == "synthetic committed source"
                assert image["_multimodal"] and image["content"][1]["type"] == "image_url"
                active_content = agent._tool_result_content_for_active_model(native.PREFIX + "browser_check", image)
                message = make_tool_result_message(native.PREFIX + "browser_check", active_content, "synthetic-tool")
                # Exercise Hermes' actual Responses request renderer, without sending the request.
                wire = agent._build_api_kwargs([{"role": "user", "content": "Synthetic review"}, message])
                assert wire["model"] == native.MODEL
                assert "data:image/png;base64," in json.dumps(wire)
                assert "input_image" in json.dumps(wire)
                checks.append("native_text_and_image_in_responses_request")
                with patch.object(agent, "model", "gpt-6.1-sol"):
                    try:
                        agent._build_api_kwargs([])
                    except RuntimeError:
                        checks.append("model_drift_denied_before_request")
                    else:
                        raise AssertionError("model drift was accepted")
                bad = SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name="terminal"))])
                try:
                    agent._execute_tool_calls(bad, [], "synthetic")
                except RuntimeError:
                    checks.append("forbidden_local_tool_denied")
                else:
                    raise AssertionError("local tool was accepted")
                return {"final_response": '{"verdict":"passed","findings":[]}'}

            content = [{"type": "text", "text": "synthetic committed source"}]
            image = [{"type": "text", "text": "synthetic screenshot"},
                     {"type": "image", "mimeType": "image/png", "data":
                      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFz8AAAAASUVORK5CYII="}]
            replies = io.StringIO(json.dumps({"content": content}) + "\n" + json.dumps({"content": image}) + "\n")
            # Any accidental network attempt fails; there is no real credential in this process.
            with patch.object(socket.socket, "connect", side_effect=AssertionError("probe must not use network")), \
                 patch.object(agent_init, "_build_client", fake_client), \
                 patch.object(AIAgent, "run_conversation", conversation):
                result = native.review({"session": session, "prompt": "Synthetic review"}, replies, io.StringIO())
                assert result == {"verdict": "passed", "findings": []}
            assert not (profile / "auth.json").exists()
            checks.append("no_profile_oauth_copy_or_network")
            print(json.dumps({"checks": checks, "mode": "installed-sdk-synthetic-no-provider"}))


if __name__ == "__main__":
    if sys.argv[1] == "--inside":
        inside(sys.argv[2])
    else:
        root = Path(__file__).resolve().parents[1]
        code = Path(__file__).read_text()
        native = (root / "deploy/independent-reviewer/native_agent.py").read_text()
        result = subprocess.run(["/usr/bin/docker", "exec", "--workdir", "/opt/hermes", sys.argv[1],
            "/opt/hermes/.venv/bin/python", "-c", code, "--inside", native], timeout=60)
        sys.exit(result.returncode)
