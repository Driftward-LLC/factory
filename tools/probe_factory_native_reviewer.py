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


def inside(script: str, observer_code: str) -> None:
    observer = ModuleType("review_observer")
    sys.modules["review_observer"] = observer
    exec(compile(observer_code, "review_observer.py", "exec"), observer.__dict__)
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
                 patch.object(socket.socket, "connect_ex", side_effect=AssertionError("probe must not use network")), \
                 patch.object(agent_init, "_build_client", fake_client), \
                 patch.object(AIAgent, "run_conversation", conversation):
                result = native.review({"session": session, "prompt": "Synthetic review"}, replies, io.StringIO())
                assert result == {"verdict": "passed", "findings": []}
            assert not (profile / "auth.json").exists()
            checks.append("no_profile_oauth_copy_or_network")
            # Real Hermes conversation loop and native hooks; only provider/HTTP boundaries are fake.
            requests, exported = [], []
            export_failure = [False]
            def fake_api(agent, kwargs, **ignored):
                requests.append(kwargs)
                assert kwargs["model"] == native.MODEL
                assert {t["name"] for t in kwargs["tools"]} == {native.PREFIX + n for n in native.PROPERTIES}
                if len(requests) <= 2:
                    name = "read_file" if len(requests) == 1 else "browser_check"
                    args = '{"path":"app.py"}' if len(requests) == 1 else '{"url":"http://localhost:3000"}'
                    output = [SimpleNamespace(type="function_call", id="fc_" + str(len(requests)),
                        call_id="call_" + str(len(requests)), name=native.PREFIX + name, arguments=args, status="completed")]
                else:
                    assert len(requests) == 3 and "input_image" in json.dumps(kwargs)
                    output = [SimpleNamespace(type="message", id="msg_test", role="assistant", status="completed",
                        content=[SimpleNamespace(type="output_text", text='{"verdict":"passed","findings":[]}')])]
                return SimpleNamespace(id="resp_test", object="response", model=native.MODEL,
                    status="completed", output=output, usage=SimpleNamespace(input_tokens=17, output_tokens=5,
                        total_tokens=22, input_tokens_details=SimpleNamespace(cached_tokens=0),
                        output_tokens_details=SimpleNamespace(reasoning_tokens=0)))

            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *_): pass
            class FakeHTTP:
                def open(self, request, timeout):
                    if export_failure[0]:
                        raise RuntimeError("private simulated exporter failure")
                    assert request.full_url == "https://synthetic.posthog.invalid/batch/" and timeout == 1
                    body = json.loads(request.data)
                    assert body["api_key"] == "synthetic-export-key"
                    exported.extend(body["batch"])
                    return Response()
            class Sink(io.StringIO):
                def write(self, text):
                    if text.startswith('{"assessment":'):
                        assert len(exported) == 5, "assessment emitted before exporter flush"
                    return super().write(text)
            metadata = {"role": "reviewer", "review_id": "synthetic-review", "head_sha": "b" * 40,
                        "pass_number": 1, "repository": "Driftward-LLC/factory"}
            outgoing = Sink()
            replies = io.StringIO(json.dumps({"content": content}) + "\n" + json.dumps({"content": image}) + "\n")
            with patch.object(socket.socket, "connect", side_effect=AssertionError("probe must not use network")), \
                 patch.object(socket.socket, "connect_ex", side_effect=AssertionError("probe must not use network")), \
                 patch.object(agent_init, "_build_client", fake_client), \
                 patch.object(AIAgent, "_interruptible_api_call", fake_api), \
                 patch.object(AIAgent, "_interruptible_streaming_api_call", fake_api), \
                 patch.object(observer, "build_opener", return_value=FakeHTTP()):
                native.emit_assessment({"session": session, "prompt": "PRIVATE_PROMPT Read app.py and inspect browser.",
                    "telemetry": metadata}, replies, outgoing,
                    {"POSTHOG_API_KEY": "synthetic-export-key", "POSTHOG_HOST": "https://synthetic.posthog.invalid"})
            generations = [event for event in exported if event["event"] == "$ai_generation"]
            spans = [event for event in exported if event["event"] == "$ai_span"]
            assert len(generations) == 3 and len(spans) == 2
            assert all(event["properties"]["$ai_input_tokens"] == 17
                       and event["properties"]["$ai_output_tokens"] == 5 for event in generations)
            assert all(event["properties"]["$ai_trace_id"] == observer.trace_id(metadata) for event in exported)
            for span in spans:
                assert any(span["properties"]["$ai_parent_id"] == generation["properties"]["$ai_span_id"]
                           and span["properties"]["hermes.request_id"] == generation["properties"]["hermes.request_id"]
                           for generation in generations)
            assert not any(private in json.dumps(exported) for private in ["PRIVATE_PROMPT", "synthetic committed source",
                "synthetic screenshot", "image/png", "synthetic-export-key", "app.py", "http://localhost", "findings"])
            for config in [{}, {"POSTHOG_API_KEY": "synthetic-export-key"}]:
                requests.clear()
                exported.clear()
                export_failure[0] = True
                outgoing = io.StringIO()
                replies = io.StringIO(json.dumps({"content": content}) + "\n" + json.dumps({"content": image}) + "\n")
                with patch.object(socket.socket, "connect", side_effect=AssertionError("probe must not use network")), \
                     patch.object(socket.socket, "connect_ex", side_effect=AssertionError("probe must not use network")), \
                     patch.object(agent_init, "_build_client", fake_client), \
                     patch.object(AIAgent, "_interruptible_api_call", fake_api), \
                     patch.object(AIAgent, "_interruptible_streaming_api_call", fake_api), \
                     patch.object(observer, "build_opener", return_value=FakeHTTP()):
                    native.emit_assessment({"session": session, "prompt": "Synthetic review.", "telemetry": metadata},
                                           replies, outgoing, config)
                assert json.loads(outgoing.getvalue().splitlines()[-1]) == {"assessment": {"verdict": "passed", "findings": []}}
                assert len(requests) == 3 and not exported
            checks.extend(["real_native_generation_and_tool_hooks", "review_trace_and_request_parent_hierarchy",
                           "metadata_only_fake_http_export", "native_usage_tokens_17_in_5_out", "flush_before_assessment",
                           "disabled_and_failed_export_preserve_verdict_without_replay"])
            print(json.dumps({"checks": checks, "mode": "installed-sdk-synthetic-no-provider"}))


if __name__ == "__main__":
    if sys.argv[1] == "--inside":
        inside(sys.argv[2], sys.argv[3])
    else:
        root = Path(__file__).resolve().parents[1]
        code = Path(__file__).read_text()
        native = (root / "deploy/independent-reviewer/native_agent.py").read_text()
        observer = (root / "deploy/independent-reviewer/review_observer.py").read_text()
        result = subprocess.run(["/usr/bin/docker", "exec", "--workdir", "/opt/hermes", sys.argv[1],
            "/opt/hermes/.venv/bin/python", "-c", code, "--inside", native, observer], timeout=60)
        sys.exit(result.returncode)
