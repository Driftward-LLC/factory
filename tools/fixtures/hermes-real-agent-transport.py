"""Provider-free test transport; the AIAgent and all tool dispatch remain native."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import shlex
import sys

adapter_path = Path(sys.argv[1])
sys.argv = sys.argv[1:]
sys.path.insert(0, str(adapter_path.parent))
spec = importlib.util.spec_from_file_location("native_adapter", adapter_path)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
adapter.isolate_native_output()

# Native metadata probes are not provider execution and may be attempted; make
# every socket connection impossible, including accidental credentials/network.
def no_network(*_args, **_kwargs):
    raise RuntimeError("network disabled in recorded native fixture")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network



calls = []
proof = Path(os.environ["HERMES_HOME"]) / "transport-proof.json"
marker = Path(os.environ["HERMES_HOME"]) / "native-effect-marker"

def transport(request):
    body = json.loads(request.content)
    calls.append(body)
    skills = all('FIXED_FACTORY_SKILL_' + name in json.dumps(body.get('messages', []))
                 for name in ('implement-change', 'verify-app', 'prepare-pr'))
    if not skills:
        raise RuntimeError('mandatory fixed skills absent from native provider request')
    if len(calls) == 1:
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_recorded_terminal", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": "printf native-real-dispatch-proof > " + shlex.quote(str(marker)) + "; printf native-real-dispatch-proof", "timeout": 5})}}]}
        finish = "tool_calls"
    else:
        results = [json.loads(item["content"]) for item in body.get("messages", []) if item.get("role") == "tool"]
        observed = any("native-real-dispatch-proof" in str(item.get("output"))
                       and item.get("exit_code", item.get("returncode")) == 0 and not item.get("error") for item in results)
        effect = marker.exists() and marker.read_text() == "native-real-dispatch-proof"
        proof.write_text(json.dumps({"native_tool_result_observed": observed, "provider_requests": len(calls),
                                    "filesystem_effect_observed": effect, "mandatory_skills_observed": skills,
                                    "agent_module": "run_agent.AIAgent", "network": "disabled"}))
        if not observed or not effect:
            raise RuntimeError("actual native terminal result missing")
        message = {"role": "assistant", "content": json.dumps({"status": "created", "pr_url": "https://github.com/example/project/pull/1",
                                                               "branch": "codex/disposable", "commit_sha": "a" * 40,
                                                               "validation": None})}
        finish = "stop"
    if body.get("stream"):
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"][0]["index"] = 0
        chunk = {"id": "chatcmpl-recorded", "object": "chat.completion.chunk", "created": 1, "model": "local-test",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    return httpx2.Response(200, json={"id": "chatcmpl-recorded", "object": "chat.completion", "created": 1,
                                    "model": "local-test", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

def recorded_client(**kwargs):
    # Replace the SDK HTTP transport only, not AIAgent, its conversation loop,
    # response parsing, tool definitions, terminal tool or environment backend.
    kwargs["http_client"] = httpx2.Client(transport=httpx2.MockTransport(transport))
    return openai.OpenAI(**kwargs)

# Install the recorded SDK only inside the new owner, after supervisor fork.
original_run_owner = adapter.run_owner
def recorded_run_owner(*args):
    global openai, httpx2
    import httpx2
    import openai
    from agent import process_bootstrap
    process_bootstrap.OpenAI = recorded_client
    return original_run_owner(*args)
adapter.run_owner = recorded_run_owner
original_conversation = adapter.conversation
def observed_conversation(*args):
    try:
        return original_conversation(*args)
    except Exception as error:
        (Path(os.environ["HERMES_HOME"]) / "probe-failure.json").write_text(json.dumps({
            "class": type(error).__name__, "missing_module": getattr(error, "name", None),
            "refusal": str(error) if isinstance(error, adapter.Refused) else None}))
        raise
adapter.conversation = observed_conversation
try:
    adapter.main()
except Exception:
    adapter.emit({"event": "refused", "reason": "recorded_native_fixture_failed"})
    sys.exit(1)
