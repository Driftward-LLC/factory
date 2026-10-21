"""Metadata identity checks for the reviewed native executor hook plugin."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_observer():
    source = Path(__file__).resolve().parents[1] / "deploy/factory-observers/posthog/__init__.py"
    spec = importlib.util.spec_from_file_location("factory_executor_observer", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'requests': SimpleNamespace()}):
        spec.loader.exec_module(module)
    return module


class ExecutorObserverTests(unittest.TestCase):
    def setUp(self):
        self.guards = SimpleNamespace(_check_all_guards=lambda *args: {"approved": True})
        self.environments = SimpleNamespace(build_subprocess_env=lambda **kwargs: {"FIXTURE": "scoped"})
        self.native = patch.dict(sys.modules, {"tools.terminal_tool": self.guards,
                                              "tools.environments.local": self.environments,
                                              "tools.interrupt": SimpleNamespace(is_interrupted=lambda: False)})
        self.native.start()
        self.addCleanup(self.native.stop)

    def test_native_profile_binds_role_without_changing_attempt_trace(self):
        source = Path(__file__).resolve().parents[1] / "deploy/factory-observers/posthog/__init__.py"
        spec = importlib.util.spec_from_file_location("factory_executor_observer", source)
        module = importlib.util.module_from_spec(spec)
        # Identity/payload filtering does not exercise HTTP delivery. Keep that
        # external boundary explicit so repo unit tests need no Hermes SDK deps.
        with patch.dict(sys.modules, {'requests': SimpleNamespace()}):
            spec.loader.exec_module(module)
        attempt = "11111111-2222-4333-8444-555555555555"
        with patch.dict(os.environ, {"FACTORY_OBSERVABILITY_ATTEMPT_ID": attempt}, clear=True):
            for home, role in [("/opt/data", False), ("/opt/data/profiles/factory-executor", True)]:
                with patch.dict(sys.modules, {"hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path(home))}):
                    properties = module._factory_properties()
                    self.assertEqual(properties.get("factory.role"), "executor" if role else None)
                    self.assertEqual(properties.get("factory.profile"), "factory-executor" if role else None)
                    event = module._event({"api_request_id": "request", "model": "gpt-6.1-sol",
                                           "prompt": "must not export", "completion": "must not export"})
                    self.assertNotIn("must not export", str(event))
                    if not role:
                        trace = event["properties"]["$ai_trace_id"]
                    else:
                        self.assertEqual(event["properties"]["$ai_trace_id"], trace)

    def test_github_invocation_parent_status_and_secrecy(self):
        observer = load_observer()
        events = []
        ids = dict(session_id="session", task_id="task", turn_id="turn", api_request_id="request", tool_call_id="call")
        secret = "PRIVATE_REPO_BODY_TOKEN_EXCEPTION"
        argv = ["pr", "create", "--repo", secret, "--body", secret]
        def execute(actual, stdin, workdir, timeout, environment):
            self.assertEqual(actual, ["gh", *argv])
            self.assertEqual(stdin, secret)
            self.assertEqual(workdir, "/tmp")
            self.assertEqual(environment, {"FIXTURE": "scoped"})
            return {"exit_code": 3, "stdout": secret + "\n", "stderr": "stderr\n"}, "nonzero_exit"
        home = SimpleNamespace(get_hermes_home=lambda: Path("/opt/data/profiles/factory-executor"))
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"hermes_constants": home}), \
                patch.object(observer, "_github_available", return_value=True), \
                patch.object(observer, "_enqueue", side_effect=events.append), \
                patch.object(observer, "_run_github", side_effect=execute) as run:
            result = json.loads(observer._github_context(tool_name="factory_github",
                args={"args": argv, "stdin": secret, "workdir": "/tmp"}, next_call=observer._github_tool, **ids))
            observer.on_post_tool_call(tool_name="factory_github", status="ok", **ids)
            observer.on_post_api_request(**ids)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["stdout"], secret + "\n")
        self.assertEqual(result["stderr"], "stderr\n")
        child, tool, generation = [event["properties"] for event in events]
        self.assertEqual(child["$ai_parent_id"], tool["$ai_span_id"])
        self.assertEqual(tool["$ai_parent_id"], generation["$ai_span_id"])
        self.assertEqual(child["$ai_trace_id"], generation["$ai_trace_id"])
        self.assertEqual(child["github.cli_operation"], "pr.create")
        self.assertEqual(child["github.cli_status"], "nonzero_exit")
        self.assertTrue(child["github.process_started"])
        self.assertEqual(child["github.effect_status"], "unverified")
        self.assertEqual(tool["factory.external_operation_coverage"], "controlled_github_cli")
        self.assertNotIn(secret, json.dumps(events))
        self.assertEqual(observer._TOOL_CONTEXT.get(), {})

    def test_github_failures_output_bounds_and_no_telemetry_retry(self):
        observer = load_observer()
        for outcome, expected in [(0, "success"), (-9, "timeout"), (None, "spawn_error")]:
            events = []
            def execute(*args, **kwargs):
                return {"exit_code": outcome, "stdout": "x" * 100000, "stdout_truncated": True}, expected
            with patch.object(observer, "_github_available", return_value=True), \
                    patch.object(observer, "_factory_properties", return_value={}), \
                    patch.object(observer, "_enqueue", side_effect=events.append), \
                    patch.object(observer, "_run_github", side_effect=execute) as run:
                result = json.loads(observer._github_context(tool_name="factory_github", args={"args": ["api", "SECRET"]},
                    next_call=observer._github_tool, session_id="session", tool_call_id="call"))
                self.assertEqual(run.call_count, 1)
            if expected != "spawn_error":
                self.assertEqual(len(result["stdout"]), 100000)
                self.assertTrue(result["stdout_truncated"])
            properties = events[0]["properties"]
            self.assertEqual(properties["github.cli_status"], expected)
            self.assertEqual(properties["github.process_started"], expected != "spawn_error")
            self.assertNotIn("SECRET", json.dumps(events))
        with patch.object(observer, "_github_available", return_value=True), \
                patch.object(observer, "_factory_properties", return_value={}), \
                patch.object(observer, "_enqueue", side_effect=RuntimeError("SECRET")), \
                patch.object(observer, "_run_github", return_value=({"exit_code": 0}, "success")) as run:
            result = json.loads(observer._github_context(tool_name="factory_github", args={"args": ["pr", "view"]},
                next_call=observer._github_tool, tool_call_id="call"))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(run.call_count, 1)

    def test_github_validation_context_and_classifier(self):
        observer = load_observer()
        with patch.object(observer, "_github_available", return_value=True), patch.object(observer, "_run_github") as run:
            for args in [{"args": []}, {"args": ["pr", 1]}, {"args": ["pr\0"]}, {"args": ["pr"] * 257},
                         {"args": ["pr"], "stdin": "x" * 1048577}, {"args": ["pr"], "timeout": True},
                         {"args": ["pr"], "workdir": "\0"}, {"args": ["pr"], "stdin": "\ud800"},
                         {"args": ["pr"], "stdin": "😀" * 262145}, {"args": ["pr", "view"]}]:
                self.assertIn("error", json.loads(observer._github_tool(args)))
            run.assert_not_called()
        for args, expected in [(["pr", "create", "--help"], "help_or_version"), (["--version"], "help_or_version"),
                               (["issue", "create", "--title", "help"], "issue.create"),
                               (["pr", "create", "--body", "version"], "pr.create"),
                               (["pr", "view", "help"], "pr.view"), (["help", "pr"], "help_or_version"),
                               (["version"], "help_or_version"), (["pr", "--help"], "help_or_version"),
                               (["api", "--help"], "help_or_version"),
                               (["api", "graphql", "--help"], "help_or_version"),
                               (["api", "repos/PRIVATE/PRIVATE", "--help"], "help_or_version"),
                               (["pr", "create", "--body", "--help"], "unknown"),
                               (["pr", "create", "--title", "value", "--help"], "unknown"),
                               (["--repo", "SECRET", "pr", "merge"], "unknown"), (["alias", "SECRET"], "unknown"),
                               (["pr", "SECRET"], "unknown"), (["api", "graphql"], "api.graphql"),
                               (["api", "repos/SECRET/SECRET"], "api")]:
            self.assertEqual(observer._github_operation(args), expected)

    def test_interrupt_before_spawn_and_during_both_wait_paths(self):
        observer = load_observer()
        stopped = threading.Event()
        with patch.dict(sys.modules, {"tools.interrupt": SimpleNamespace(is_interrupted=stopped.is_set)}):
            stopped.set()
            with patch.object(observer.subprocess, "Popen") as spawn:
                response, status = observer._run_github(["gh", "pr", "create"], "", "/tmp", 10, {})
                self.assertEqual((status, response["exit_code"]), ("interrupted_before_spawn", None))
                spawn.assert_not_called()
            for close_pipes in (False, True):
                stopped.clear()
                outcome = []
                script = "import os,time; " + ("os.close(1);os.close(2); " if close_pipes else "") + "time.sleep(2);os._exit(7)"
                worker = threading.Thread(target=lambda: outcome.append(observer._run_github([sys.executable, "-c", script], "", "/tmp", 10, {})))
                started = time.monotonic()
                worker.start()
                time.sleep(0.2)
                stopped.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - started, 1.2)
                response, status = outcome[0]
                self.assertEqual((status, response["exit_code"]), ("interrupted", -9))
                with patch.object(observer, "_factory_properties", return_value={}):
                    properties = observer._github_event({"tool_call_id": "call"}, "pr.create", status, response["exit_code"], started)["properties"]
                self.assertTrue(properties["github.process_started"])
                self.assertEqual(properties["github.effect_status"], "unverified")
                self.assertTrue(properties["$ai_is_error"])

    def test_native_guard_denial_and_failure_do_not_spawn(self):
        observer = load_observer()
        for guard in [lambda *a: {"approved": False}, lambda *a: (_ for _ in ()).throw(RuntimeError("SECRET"))]:
            self.guards._check_all_guards = guard
            with patch.object(observer, "_github_available", return_value=True), patch.object(observer, "_run_github") as run:
                response = observer._github_context(tool_name="factory_github", args={"args": ["pr", "merge"]},
                    next_call=observer._github_tool, tool_call_id="call")
                self.assertIn("error", json.loads(response))
                self.assertNotIn("SECRET", response)
                run.assert_not_called()

    def test_real_subprocess_streams_exit_timeout_and_output_limit(self):
        observer = load_observer()
        script = "import sys; value=sys.stdin.buffer.read(); sys.stdout.buffer.write(value); sys.stderr.write('stderr\\n'); sys.exit(7)"
        response, status = observer._run_github([sys.executable, "-c", script], "stdin\n" * 20000, "/tmp", 5, {})
        self.assertEqual(status, "output_limit")
        self.assertEqual(len(response["stdout"].encode()), 100000)
        response, status = observer._run_github([sys.executable, "-c", script], "stdin\n", "/tmp", 5, {})
        self.assertEqual((status, response["exit_code"], response["stdout"], response["stderr"]),
                         ("nonzero_exit", 7, "stdin\n", "stderr\n"))
        response, status = observer._run_github([sys.executable, "-c", "import time; time.sleep(10)"], "", "/tmp", 1, {})
        self.assertEqual(status, "timeout")
        self.assertEqual(response["exit_code"], -9)
        response, status = observer._run_github(["/not/a/gh/executable"], "", "/tmp", 1, {})
        self.assertEqual((status, response["exit_code"]), ("spawn_error", None))

    def test_github_registration_only_for_local_executor(self):
        observer = load_observer()
        for home, backend, environment, gh, enabled in [
            ("/opt/data", "local", "local", "gh", False),
            ("/opt/data/profiles/factory-independent-reviewer", "local", "local", "gh", False),
            ("/opt/data/profiles/factory-executor", "ssh", "local", "gh", False),
            ("/opt/data/profiles/factory-executor", "local", "docker", "gh", False),
            ("/opt/data/profiles/factory-executor", "local", "local", None, False),
            ("/opt/data/profiles/factory-executor", "local", "local", "gh", True),
        ]:
            tools, middleware, prompts = [], [], []
            ctx = SimpleNamespace(register_hook=lambda *a: None, register_tool=lambda **kw: tools.append(kw),
                register_middleware=lambda *a: middleware.append(a), register_system_prompt_section=lambda *a, **kw: prompts.append(a))
            with patch.dict(sys.modules, {
                "hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path(home)),
                "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: {"terminal": {"backend": backend}}),
                "gateway.session_context": SimpleNamespace(get_session_env=lambda *a: environment),
            }), patch.object(observer.shutil, "which", return_value=gh):
                observer.register(ctx)
            self.assertEqual(bool(tools), enabled)
            self.assertEqual(bool(middleware), enabled)
            self.assertEqual(bool(prompts), enabled)

    def test_optional_tool_discovery_failure_preserves_observer_hooks_and_unknown_coverage(self):
        observer = load_observer()
        hooks, events = [], []
        with patch.dict(sys.modules, {
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path('/opt/data/profiles/factory-executor')),
            "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: {"terminal": None}),
            "gateway.session_context": SimpleNamespace(get_session_env=lambda *a: "local"),
        }):
            observer.register(SimpleNamespace(register_hook=lambda *a: hooks.append(a)))
        self.assertEqual(len(hooks), 4)
        with patch.object(observer, "_enqueue", side_effect=events.append), patch.object(observer, "_factory_properties", return_value={}):
            for tool in ("terminal", "execute_code", "process", "unrecognized"):
                observer.on_post_tool_call(tool_name=tool, args={"command": "SECRET"}, result="SECRET")
        self.assertTrue(all(e['properties']['factory.external_operation_coverage']=='unknown' for e in events))
        self.assertNotIn("SECRET", json.dumps(events))
