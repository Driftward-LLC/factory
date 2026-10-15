from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy/independent-reviewer"
sys.path.insert(0, str(RUNNER))
import runner
import source_server


class IndependentReviewerTests(unittest.TestCase):
    def test_model_role_has_no_shell_and_uses_exact_astra(self):
        args = runner.model_arguments(Path("schema"), Path("output"), Path("clean-home"))
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertIn("features.shell_tool=false", args)
        self.assertIn("features.unified_exec=false", args)
        self.assertIn("features.plugins=false", args)
        self.assertIn("features.apps=false", args)
        self.assertIn('mcp_servers.source.url="http://source:8765/mcp"', args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

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
