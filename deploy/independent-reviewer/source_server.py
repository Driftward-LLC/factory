"""Credential-free source/test/browser MCP endpoint, on a private internal network."""
from __future__ import annotations

import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from urllib.parse import urlsplit

from runner import browser_probe, checkout, command, model_result

REPO = Path("/workspace/repo")
READY = Path("/workspace/source-ready.json")
IMMUTABLE_GIT = ["/usr/bin/git", "--no-replace-objects", "--git-dir", "/run/source.git"]
ALLOWED = {"python3", "node", "npm", "npx", "bash", "sh", "git", "pytest"}
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/workspace/home",
       "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/browsers")}
TOOLS = {
    "inspect_source": {}, "read_file": {"path": {"type": "string"}}, "diff": {},
    "run_check": {"argv": {"type": "array", "items": {"type": "string"}}},
    "start_preview": {"argv": {"type": "array", "items": {"type": "string"}}},
    "browser_check": {"url": {"type": "string"}},
    "validate_findings": {"findings": {"type": "array", "items": {"type": "object"}}},
}


def checked_argv(args: object) -> list[str]:
    if (not isinstance(args, list) or not 1 <= len(args) <= 40 or args[0] not in ALLOWED
            or any(not isinstance(a, str) or len(a) > 4000 or "\x00" in a for a in args)):
        raise ValueError("unsupported bounded check")
    return args


def validate_findings(findings: list, head: str) -> None:
    model_result({"verdict": "changes_requested" if findings else "passed", "findings": findings})
    for f in findings:
        text = command([*IMMUTABLE_GIT, "show", f"{head}:{f['file']}"])
        if f["line"] > len(text.splitlines()):
            raise ValueError("finding line absent from reviewed revision")


def call_tool(name: str, args: dict, head: str, base: str) -> list[dict]:
    if name not in TOOLS or set(args) != set(TOOLS[name]):
        raise ValueError("unsupported tool arguments")
    if name == "inspect_source":
        if json.loads(READY.read_text()) != {"head_sha": head, "base_sha": base}:
            raise ValueError("source checkout startup incomplete")
        if command(["git", "-C", str(REPO), "rev-parse", "HEAD"]) != head:
            raise ValueError("source checkout head mismatch")
        browser_probe()
        value = {"head_sha": command([*IMMUTABLE_GIT, "rev-parse", head + "^{commit}"]), "base_sha": base,
                 "browser_probe": "passed", "write_credentials": False}
    elif name == "read_file":
        location = args["path"]
        if not isinstance(location, str) or location.startswith("/") or any(s in {"..", ""} for s in location.split("/")):
            raise ValueError("invalid source path")
        value = command([*IMMUTABLE_GIT, "show", f"{head}:{location}"])
    elif name == "diff":
        value = command([*IMMUTABLE_GIT, "diff", "--no-ext-diff", "--no-textconv", f"{base}...{head}"])
    elif name in {"run_check", "start_preview"}:
        argv = checked_argv(args["argv"])
        if name == "start_preview":
            process = subprocess.Popen(argv, cwd=REPO, env=ENV, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            value = {"pid": process.pid, "status": "started"}
        else:
            with subprocess.Popen(argv, cwd=REPO, env=ENV, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, start_new_session=True) as process:
                try:
                    raw, _ = process.communicate(timeout=60)
                    value = {"exit_code": process.returncode, "output": raw[:32000].decode("utf-8", errors="replace")}
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                    value = {"exit_code": None, "reason": "check_timeout"}
    elif name == "validate_findings":
        validate_findings(args["findings"], head)
        value = {"status": "valid"}
    else:
        url = args["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
            raise ValueError("browser must stay on a local synthetic preview")
        evidence = json.loads(command(["node", "/opt/factory-reviewer/browser_probe.cjs", url]))
        return [{"type": "text", "text": evidence["text"]}, {"type": "image", "data": evidence["screenshot"], "mimeType": "image/png"}]
    return [{"type": "text", "text": json.dumps(value)[:64000]}]


def handler(head: str, base: str, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            if self.path != "/mcp" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_response(401)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 64000:
                self.send_response(413)
                self.end_headers()
                return
            request = json.loads(self.rfile.read(length))
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            try:
                method = request["method"]
                if method == "initialize":
                    result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "factory-review-source", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "description": name.replace("_", " "),
                              "inputSchema": {"type": "object", "additionalProperties": False,
                                  "properties": props, "required": list(props)}} for name, props in TOOLS.items()]}
                elif method == "tools/call":
                    params = request["params"]
                    result = {"content": call_tool(params["name"], params.get("arguments", {}), head, base)}
                elif method == "ping":
                    result = {}
                else:
                    raise ValueError("unknown method")
                response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            except Exception:
                response = {"jsonrpc": "2.0", "id": request["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": "Source check unavailable; do not report a pass"}]}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
    return Handler


if __name__ == "__main__":
    if sys.argv[1] == "--call-tool":
        print(json.dumps(call_tool(sys.argv[4], json.loads(sys.argv[5]), sys.argv[2], sys.argv[3])))
        sys.exit(0)
    if sys.argv[1] == "--validate-findings":
        validate_findings(json.loads(sys.argv[3]), sys.argv[2])
        sys.exit(0)
    head, base = sys.argv[1:3]
    Path("/workspace/home").mkdir(mode=0o700)
    checkout(Path("/run/source.bundle"), REPO, head, base)
    browser_probe()
    READY.write_text(json.dumps({"head_sha": head, "base_sha": base}))
    ThreadingHTTPServer(("0.0.0.0", 8765), handler(head, base, Path("/run/mcp-token").read_text())).serve_forever()
