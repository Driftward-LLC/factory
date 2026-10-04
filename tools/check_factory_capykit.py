#!/usr/bin/env python3
"""Prove the shipped Capykit client through native Hermes MCP, offline.

No production mounts, network, accounts or keys. TLS/grant responses below are
fixtures, not verification of Capykit's server-side authorization implementation.
"""
import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


COMMIT = "eadda266ba9990927c30d84b272bc42b6c6f8573"
CONNECTION = "11111111-1111-4111-8111-111111111111"
ACTION = "getIssueInformation"
TOOLS = ["list_connections", "list_connection_actions", "run_connection_action"]


def inside():
    import yaml

    assert os.getuid() == 10000
    assert not Path("/var/run/docker.sock").exists()
    assert Path("/opt/factory/capykit/SOURCE_COMMIT").read_text().strip() == COMMIT
    key = "ck_agent_" + "f" * 43  # Synthetic; never sent outside this container.
    state = {"granted": True, "key_active": True, "oversized": False, "runs": 0}
    expected = {"repository": {"owner": "fixture", "repo": "repo"}, "issue_number": 1}

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            if self.headers.get("Authorization") != "Bearer " + key or not state["key_active"]:
                status, result = 401, {"error": "provider-secret-must-not-leak"}
            elif self.command == "GET" and self.path == "/v1/connections/personal":
                status, result = 200, {"connections": [{"id": CONNECTION, "appId": "github"}]}
            elif self.command == "GET" and self.path == f"/v1/connections/personal/{CONNECTION}/actions":
                status, result = 200, {"actions": [{"name": ACTION}] if state["granted"] else []}
            elif self.command == "POST" and self.path == f"/v1/connections/personal/{CONNECTION}/actions/{ACTION}/run":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not state["granted"]:
                    status, result = 403, {"error": "provider-secret-must-not-leak"}
                elif body != {"input": expected}:
                    status, result = 400, {"error": "unexpected inputs"}
                else:
                    state["runs"] += 1
                    status, result = 200, {"result": {"number": 1, "title": "Fixture", "state": "open"}}
            else:
                status, result = 403, {"error": "write-not-granted"}
            data = json.dumps(result).encode()
            if state["oversized"] and status == 200:
                data = b" " * (1024 * 1024 + 1)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ssl.SSLError):
                pass  # Client deliberately cancels oversized responses.

        do_GET = handle_request
        do_POST = handle_request

    with tempfile.TemporaryDirectory() as directory:
        cert, private = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(private), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, private)
        gateway.socket = context.wrap_socket(gateway.socket, server_side=True)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        os.environ.update(CAPYKIT_BASE_URL=f"https://127.0.0.1:{gateway.server_port}",
                          CAPYKIT_API_KEY=key, OPENAI_API_KEY="provider-only-fixture",
                          GH_TOKEN="github-only-fixture")
        config = {"command": "node", "args": ["/opt/factory/capykit/mcp.mjs", "--remote"],
                  "env": {"CAPYKIT_BASE_URL": "${CAPYKIT_BASE_URL}",
                          "CAPYKIT_API_KEY": "${CAPYKIT_API_KEY}"},
                  "tools": {"include": TOOLS}, "timeout": 60, "connect_timeout": 15}
        Path("/opt/data/config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"capykit": config}}))
        assert key not in Path("/opt/data/config.yaml").read_text()
        # Import after setting up the disposable HOME; use pinned Hermes itself.
        sys.path.insert(0, "/opt/hermes")
        from tools import mcp_tool as mcp

        def call(name, args=None):
            result = json.loads(mcp._make_tool_handler("capykit", name, 60)(args or {}))
            serialized = json.dumps(result)
            assert key not in serialized and "provider-secret-must-not-leak" not in serialized
            return result

        try:
            loaded = mcp._load_mcp_config()["capykit"]
            assert loaded["env"]["CAPYKIT_API_KEY"] == key
            safe = mcp._build_safe_env(loaded["env"])
            assert "OPENAI_API_KEY" not in safe and "GH_TOKEN" not in safe
            names = mcp.discover_mcp_tools()
            assert {"mcp_capykit_" + name for name in TOOLS}.issubset(names), names
            assert "mcp_capykit_run_action" not in names
            assert "error" in call("list_connections")  # Untrusted TLS must fail.
            mcp.shutdown_mcp_servers()
            config["env"]["NODE_EXTRA_CA_CERTS"] = str(cert)  # Test CA, not TLS bypass.
            Path("/opt/data/config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"capykit": config}}))
            mcp.discover_mcp_tools()
            assert call("list_connections")["structuredContent"]["connections"][0]["id"] == CONNECTION
            assert call("list_connection_actions", {"connectionId": CONNECTION})["structuredContent"]["actions"] == [{"name": ACTION}]
            args = {"connectionId": CONNECTION, "action": ACTION, "input": expected}
            assert "current access" in call("run_connection_action", {**args, "action": "create_issue"})["error"]
            assert state["runs"] == 0
            assert call("run_connection_action", args)["structuredContent"]["result"]["number"] == 1
            state["granted"] = False
            assert call("list_connection_actions", {"connectionId": CONNECTION})["structuredContent"]["actions"] == []
            assert "current access" in call("run_connection_action", args)["error"]
            assert state["runs"] == 1
            state["key_active"] = False
            assert "expired or was revoked" in call("list_connections")["error"]
            state.update(key_active=True, granted=True, oversized=True)
            assert "exceeded the limit" in call("list_connections")["error"]
        finally:
            mcp.shutdown_mcp_servers()
            gateway.shutdown()
            gateway.server_close()
            thread.join(timeout=5)
    print("PASS: native MCP discovery, read-only issue fixture, TLS, revocations, response limit and credential filtering")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor-image", default="driftward/factory-executor:ci")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside()
    else:
        subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--tmpfs", "/opt/data:uid=10000,gid=10000,mode=0700",
                        "--entrypoint", "python3", args.executor_image, "-", "--inside"],
                       input=Path(__file__).read_text(), text=True, check=True, timeout=180)


if __name__ == "__main__":
    main()
