from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy/independent-reviewer"
sys.path.insert(0, str(RUNNER))
import runner
import source_server
import native_agent


class IndependentReviewerTests(unittest.TestCase):
    def test_auth_root_is_finite_and_both_native_modes_select_it(self):
        root = "/opt/data/profiles/factory-independent-reviewer"
        self.assertEqual(native_agent.validated_auth_root(root), Path(root))
        for invalid in ["/opt/data", root + "/", "/tmp/reviewer", root + "/../other"]:
            with self.assertRaises(ValueError):
                native_agent.validated_auth_root(invalid)
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                native_agent.validated_auth_root(root)
        for stop in [True, False]:
            args = runner.native_arguments("runtime", "factory-reviewer:" + "a" * 36, stop=stop, auth_root=root)
            self.assertEqual(args[-3:], ["--stop" if stop else "--review", root, "factory-reviewer:" + "a" * 36])

    def test_model_role_has_no_shell_and_uses_exact_astra(self):
        args = runner.native_arguments("hermes-driftward-hermes-1", "factory-reviewer:" + "a" * 36)
        self.assertEqual(args[:5], ["/usr/bin/docker", "exec", "-i", "--workdir", "/opt/hermes"])
        self.assertNotIn("codex", args)
        self.assertEqual(native_agent.MODEL, "gpt-6-astra")
        self.assertEqual(native_agent.CONFIG["plugins"], {"enabled": []})
        self.assertEqual(native_agent.CONFIG["mcp_servers"], {})
        self.assertEqual(native_agent.CONFIG["auth"], {"adopt_external_logins": False})
        self.assertEqual(set(native_agent.PROPERTIES), set(source_server.TOOLS))
        names = {native_agent.PREFIX + name for name in native_agent.PROPERTIES}
        agent = SimpleNamespace(model=native_agent.MODEL, provider="openai-codex", api_mode="codex_responses",
            enabled_toolsets=[native_agent.TOOLSET], valid_tool_names=names,
            tools=[{"function": {"name": n}} for n in names], _fallback_chain=[], compression_enabled=False)
        native_agent.guard(agent)
        for key, value in [("model", "gpt-6.1-sol"), ("api_mode", "codex_app_server"),
                           ("enabled_toolsets", ["all"]), ("valid_tool_names", names | {"terminal"}),
                           ("_fallback_chain", [{"model": "other"}]), ("compression_enabled", True)]:
            with patch.object(agent, key, value), self.assertRaises(RuntimeError):
                native_agent.guard(agent)

    def test_exact_native_termination_preserves_other_process_and_ambiguous_marker(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"HERMES_HOME": temporary}):
            session = "factory-reviewer:" + "a" * 36
            profile = native_agent.profile_path(session)
            profile.mkdir(parents=True)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", session], start_new_session=True)
            try:
                marker = {"pid": process.pid, "start_ticks": native_agent.start_ticks(process.pid), "session": session}
                (profile / "invocation.json").write_text(json.dumps({**marker, "start_ticks": "wrong"}))
                with self.assertRaises(RuntimeError):
                    native_agent.stop(session)
                self.assertIsNone(process.poll())
                self.assertTrue(profile.exists())
                (profile / "invocation.json").write_text(json.dumps(marker))
                native_agent.stop(session)
                self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
                self.assertIsNone(unrelated.poll())
                self.assertFalse(profile.exists())
                with self.assertRaises(FileNotFoundError):
                    native_agent.stop(session)
            finally:
                for child in [process, unrelated]:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)

    def test_partial_relay_line_cannot_hide_deadline(self):
        child = subprocess.Popen([sys.executable, "-c", "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(30)"],
                                 stdout=subprocess.PIPE)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.relay_line(child.stdout, bytearray(), time.monotonic() + 0.05)
        finally:
            child.kill()
            child.communicate(timeout=5)

    def test_request_frame_unicode_and_byte_limit(self):
        session = "factory-reviewer:" + "a" * 36
        for description in ["界" * 12000, "😀" * 6000]:
            prompt = (RUNNER / "runner.py").read_text() + json.dumps({"title": "😀" * 160,
                "description": description})
            frame = native_agent.request_frame(session, prompt)
            self.assertGreater(len(frame), 64001)
            self.assertEqual(native_agent.read_request(io.BytesIO(frame))["prompt"], prompt)
        overhead = len(native_agent.request_frame(session, ""))
        frame = native_agent.request_frame(session, "a" * (native_agent.REQUEST_LIMIT - overhead))
        self.assertEqual(len(frame), native_agent.REQUEST_LIMIT)
        self.assertEqual(len(native_agent.read_request(io.BytesIO(frame))["prompt"]), native_agent.REQUEST_LIMIT - overhead)
        with self.assertRaises(ValueError):
            native_agent.request_frame(session, "a" * (native_agent.REQUEST_LIMIT - overhead + 1))
        with self.assertRaises(ValueError):
            native_agent.read_request(io.BytesIO(frame[:-1]))
        probe = [{"type": "text", "text": json.dumps({"head_sha": "a" * 40,
            "base_sha": "b" * 40, "browser_probe": "passed"})}]
        with patch.object(runner, "command", return_value=json.dumps(probe)), \
             patch.object(runner.subprocess, "Popen") as launch:
            receipt = runner.run("a" * 40, "b" * 40, {"title": "Synthetic", "description": "界" * 12000},
                                 "source", "hermes", session, "a" * native_agent.REQUEST_LIMIT)
            self.assertEqual(receipt["reason"], "review_budget_exhausted")
            launch.assert_not_called()

    def test_delayed_source_checkout_cannot_pass_inspection_before_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "source-ready.json"
            with patch.object(source_server, "READY", ready), \
                 patch.object(source_server, "command", return_value="a" * 40) as command, \
                 patch.object(source_server, "browser_probe") as browser:
                # The immutable database can already be mounted while startup checkout is pending.
                with self.assertRaises(FileNotFoundError):
                    source_server.call_tool("inspect_source", {}, "a" * 40, "b" * 40)
                command.assert_not_called()
                browser.assert_not_called()
                ready.write_text(json.dumps({"head_sha": "a" * 40, "base_sha": "b" * 40}))
                self.assertEqual(json.loads(source_server.call_tool("inspect_source", {}, "a" * 40,
                    "b" * 40)[0]["text"])["browser_probe"], "passed")
                with patch.object(source_server, "command", return_value="c" * 40), self.assertRaises(ValueError):
                    source_server.call_tool("inspect_source", {}, "a" * 40, "b" * 40)

    def test_source_commands_are_credential_free_and_bounded(self):
        self.assertEqual(source_server.checked_argv(["python3", "-m", "unittest"]), ["python3", "-m", "unittest"])
        for argv in [["/root/run"], ["docker", "run"], [], ["python3", "a\x00b"]]:
            with self.assertRaises(ValueError):
                source_server.checked_argv(argv)
        self.assertFalse(any("TOKEN" in key or "AUTH" in key or "KEY" in key or "PROXY" in key for key in source_server.ENV))
        self.assertFalse("CODEX_HOME" in source_server.ENV)

    def test_model_cannot_author_receipt_or_traversal_findings(self):
        self.assertEqual(runner.model_result({"verdict": "passed", "findings": []})["verdict"], "passed")
        for value in [{"verdict": "passed", "findings": [], "isolation": True},
                      {"verdict": "changes_requested", "findings": []},
                      {"verdict": "passed", "findings": [{"priority": 1, "file": "../auth.json", "line": 1,
                                                              "title": "bad", "body": "bad"}]}]:
            with self.assertRaises(ValueError):
                runner.model_result(value)

    def test_checkout_exact_immutable_base_head_and_findings_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            git = lambda *args: subprocess.check_output(["git", *args], cwd=source, stderr=subprocess.DEVNULL, text=True).strip()
            git("init", "-q", "-b", "main")
            git("config", "user.name", "Synthetic")
            git("config", "user.email", "synthetic@example.invalid")
            (source / "counter.py").write_text("count = 0\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            git("checkout", "-qb", "candidate")
            (source / "counter.py").write_text("count = 1\n")
            git("commit", "-qam", "head")
            head = git("rev-parse", "HEAD")
            bundle = root / "source.bundle"
            git("bundle", "create", str(bundle), "main", "candidate")
            checkout = root / "checkout"
            runner.checkout(bundle, checkout, head, base)
            self.assertEqual(runner.command(["git", "rev-parse", "HEAD"], cwd=checkout), head)
            self.assertEqual((checkout / "counter.py").read_text(), "count = 1\n")
            self.assertEqual(runner.command(["git", "show", f"{base}:counter.py"], cwd=checkout), "count = 0")
            prior = source_server.REPO
            prior_git = source_server.IMMUTABLE_GIT
            source_server.REPO = checkout
            source_server.IMMUTABLE_GIT = ["git", "--git-dir", str(checkout / ".git")]
            try:
                finding = {"priority": 1, "file": "counter.py", "line": 1, "title": "Counter", "body": "Concrete defect"}
                source_server.validate_findings([finding], head)
                with self.assertRaises(ValueError):
                    source_server.validate_findings([{**finding, "line": 99}], head)
                with self.assertRaises(ValueError):
                    source_server.call_tool("browser_check", {"url": "https://github.com"}, head, base)
                with self.assertRaises(ValueError):
                    source_server.call_tool("read_file", {"path": "../auth.json"}, head, base)
            finally:
                source_server.REPO = prior
                source_server.IMMUTABLE_GIT = prior_git
            with self.assertRaises(subprocess.CalledProcessError):
                runner.checkout(bundle, root / "wrong", "f" * 40, base)


if __name__ == "__main__":
    unittest.main()
