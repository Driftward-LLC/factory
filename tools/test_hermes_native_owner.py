"""Owner protocol units plus opt-in REAL pinned Hermes cleanup, never live Hermes.

Native check: FACTORY_TEST_HERMES_SOURCE=<checkout> and
FACTORY_TEST_HERMES_PYTHON=<isolated python> python3 -m unittest ...
"""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ADAPTER = Path(__file__).resolve().parents[1] / "deploy/experimental-native-executor"
spec = importlib.util.spec_from_file_location("experimental_owner", ADAPTER / "owner.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PIN = "818c13be1dc4fd28987e1e881a9408224afd4535"


def request():
    return {"invocation_id": f"{uuid.uuid4()}:{uuid.uuid4()}:1", "epoch": 1, "fingerprint": "a" * 64}


def native():
    return SimpleNamespace(kill_foreground=Mock(return_value=0),
                           registry=SimpleNamespace(running_owned_by=Mock(return_value=[]), kill_process=Mock()))


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "journal"
        self.binding = request()
        self.owner = module.Owner(self.root, self.binding, backend="local", enabled_tools=["terminal", "process_manage"])

    def test_registration_is_private_exact_and_replay_refused(self):
        record = json.loads((self.root / "ownership.json").read_text())
        self.assertEqual(record["invocation_id"], self.binding["invocation_id"])
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual((self.root.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.root / "ownership.json").stat().st_mode & 0o777), 0o600)
        with self.assertRaises(FileExistsError):
            module.Owner(self.root, self.binding, backend="local", enabled_tools=["terminal", "process_manage"])

    def test_bad_binding_never_calls_cleanup(self):
        backend = native()
        for bad in ({**self.binding, "fingerprint": "b" * 64}, {**self.binding, "epoch": 2}, request()):
            with self.assertRaises((module.Refused, ValueError)):
                self.owner.cancel(bad, backend)
        backend.kill_foreground.assert_not_called()
        self.assertFalse(self.owner.fenced)

    def test_cancel_before_dispatch_and_late_agent_are_fenced(self):
        backend = native()
        receipt = self.owner.cancel(self.binding, backend)
        with self.assertRaises(module.Refused):
            with self.owner.dispatch(self.owner.session, self.owner.raw_owner, ["terminal"]):
                self.fail("tool was admitted after fence")
        agent = SimpleNamespace(session_id=self.owner.session, hard_interrupt=Mock())
        self.owner.attach(agent)
        agent.hard_interrupt.assert_called_once()
        self.assertEqual(receipt["state"], "unknown")
        self.assertEqual(receipt["completion"], "not_established")
        self.assertEqual(self.owner.cancel(self.binding, backend), receipt)
        self.assertEqual(backend.kill_foreground.call_count, 2)

    def test_rotation_persist_failure_fences_before_dispatch(self):
        with patch.object(module, "atomic", side_effect=OSError("disposable failure")):
            with self.assertRaises(OSError):
                with self.owner.dispatch("rotated", self.owner.raw_owner, ["terminal"]):
                    self.fail("unregistered tool dispatch")
        self.assertTrue(self.owner.fenced)
        self.assertEqual(self.owner.inflight, 0)

    def test_rotation_persisted_before_dispatch(self):
        with self.owner.dispatch("rotated", self.owner.raw_owner, ["terminal"]):
            self.assertIn("rotated", json.loads((self.root / "ownership.json").read_text())["sessions"])

    def test_unsupported_tool_backend_batch_owner_refused(self):
        for names, backend, raw in [(["delegate_task"], "local", self.owner.raw_owner),
                                    (["browser_navigate"], "local", self.owner.raw_owner),
                                    (["mcp__write"], "local", self.owner.raw_owner),
                                    (["terminal"], "ssh", self.owner.raw_owner),
                                    (["terminal", "terminal"], "local", self.owner.raw_owner),
                                    (["terminal"], "local", "shared-environment")]:
            self.owner.fenced = False
            with self.assertRaises(module.Refused):
                with self.owner.dispatch(self.owner.session, raw, names, backend=backend):
                    self.fail("unsupported dispatch admitted")
        with self.assertRaises(module.Refused):
            module.Owner(Path(self.temp.name) / "remote", self.binding, backend="ssh", enabled_tools=["terminal"])

    def test_stale_pid_start_namespace_boot_refused_without_cleanup(self):
        for key in ("pid", "start_ticks", "pid_namespace", "boot_id"):
            original = self.owner.record[key]
            self.owner.record[key] = -1 if key == "pid" else "stale"
            backend = native()
            with self.assertRaises(module.Refused):
                self.owner.cancel(self.binding, backend)
            backend.kill_foreground.assert_not_called()
            self.owner.record[key] = original

    def test_missing_corrupted_or_replaced_registration_refuses_cleanup(self):
        path = self.root / "ownership.json"
        original = path.read_bytes()
        for value in (b"{", b"{}", None):
            if value is None:
                path.unlink()
            else:
                path.write_bytes(value)
            backend = native()
            with self.assertRaises((module.Refused, ValueError, OSError)):
                self.owner.cancel(self.binding, backend)
            backend.kill_foreground.assert_not_called()
            path.write_bytes(original)

    def test_nonquiescent_dispatch_returns_unknown_without_background_sweep(self):
        backend = native()
        with self.owner.dispatch(self.owner.session, self.owner.raw_owner, ["terminal"]):
            receipt = self.owner.cancel(self.binding, backend, timeout=0)
        self.assertIn("inflight_dispatch_not_quiescent", receipt["errors"])
        backend.registry.running_owned_by.assert_not_called()

    def test_fifo_registration_is_refused_without_blocking_or_cleanup(self):
        path = self.root / "ownership.json"
        path.unlink()
        os.mkfifo(path, 0o600)
        backend = native()
        completed = threading.Event()
        failures = []
        def cancel():
            try:
                self.owner.cancel(self.binding, backend)
            except Exception as error:
                failures.append(error)
            finally:
                completed.set()
        worker = threading.Thread(target=cancel, daemon=True)
        worker.start()
        bounded = completed.wait(0.5)
        if not bounded:
            # Unstick the old blocking implementation after detecting the bug,
            # so a failing regression cannot leave a test thread hanging.
            writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            os.close(writer)
            worker.join(timeout=0.5)
        self.assertTrue(bounded, "FIFO ownership verification blocked")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], module.Refused)
        backend.kill_foreground.assert_not_called()
        self.assertFalse(self.owner.fenced)

    def test_nonprivate_nonregular_or_oversized_registration_refuses_cleanup(self):
        path = self.root / "ownership.json"
        original = path.read_bytes()
        for replacement in ("symlink", "directory", "public", "oversized", "hardlink"):
            with self.subTest(replacement=replacement):
                path.unlink()
                other = self.root / "other"
                if replacement == "symlink":
                    other.write_bytes(original)
                    path.symlink_to(other)
                elif replacement == "directory":
                    path.mkdir()
                elif replacement == "public":
                    path.write_bytes(original)
                    path.chmod(0o644)
                elif replacement == "oversized":
                    path.write_bytes(b" " * 65537)
                    path.chmod(0o600)
                else:
                    other.write_bytes(original)
                    other.chmod(0o600)
                    os.link(other, path)
                backend = native()
                with self.assertRaises((module.Refused, OSError)):
                    self.owner.cancel(self.binding, backend)
                backend.kill_foreground.assert_not_called()
                if replacement == "directory":
                    path.rmdir()
                else:
                    path.unlink()
                other.unlink(missing_ok=True)
                module.atomic(path, self.owner.record)

    def test_bounded_native_receipt_excludes_output(self):
        backend = native()
        backend.registry.running_owned_by.return_value = [SimpleNamespace(id="native-id")]
        backend.registry.kill_process.return_value = {"status": "killed", "command": "secret", "output": "secret"}
        receipt = self.owner.cancel(self.binding, backend)
        backend.registry.running_owned_by.assert_called_once_with(self.owner.raw_owner)
        backend.registry.kill_process.assert_called_once_with("native-id", source="factory_explicit_cancel", consume_output=True)
        self.assertNotIn("secret", json.dumps(receipt))

    def test_stop_journal_or_telemetry_failure_does_not_disable_native_cleanup(self):
        backend = native()
        self.owner.observer = Mock(side_effect=RuntimeError("observer unavailable"))
        with patch.object(module, "atomic", side_effect=OSError("disk full")):
            receipt = self.owner.cancel(self.binding, backend)
        self.assertEqual(backend.kill_foreground.call_count, 2)
        self.assertEqual(receipt["state"], "unknown")
        self.assertIn("stop_journal_write_failed", receipt["errors"])
        self.assertNotIn("disk full", json.dumps(receipt))


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def descendants(roots):
    parents = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                parents[int(entry.name)] = int(fields[1])
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    found = set(roots)
    while True:
        expanded = found | {pid for pid, parent in parents.items() if parent in found}
        if expanded == found:
            return found
        found = expanded


def read_event(process, timeout=20):
    # Read the descriptor without TextIOWrapper read-ahead: select() alone
    # cannot see complete frames already sitting in its userspace buffer.
    deadline = time.monotonic() + timeout
    encoded = bytearray()
    while len(encoded) <= 65536:
        if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            raise AssertionError("owner protocol timeout")
        value = os.read(process.stdout.fileno(), 1)
        if not value:
            raise AssertionError("owner protocol ended early")
        encoded.extend(value)
        if value == b"\n":
            return json.loads(encoded)
    raise AssertionError("oversized owner event")


class LauncherOutputTests(unittest.TestCase):
    def test_real_launcher_suppresses_import_init_python_fd_and_child_noise(self):
        # Exercise the actual non-fixture launch/control path using provider-free
        # native module stand-ins, not a real AIAgent or provider request.
        with tempfile.TemporaryDirectory(prefix="factory-native-output-") as temporary:
            root = Path(temporary)
            source = root / "source"
            home = root / "profile"
            home.mkdir(mode=0o700)
            modules = {
                "noisy.py": '''
                    import os, pathlib, subprocess, sys
                    def noise(stage):
                        value = "SENSITIVE_NATIVE_" + stage
                        print(value, flush=True)
                        print(value, file=sys.stderr, flush=True)
                        os.write(1, (value + "_FD1\\n").encode())
                        os.write(2, (value + "_FD2\\n").encode())
                        subprocess.run([sys.executable, "-I", "-c", "import os; os.write(1,b'SENSITIVE_CHILD_FD1\\\\n'); os.write(2,b'SENSITIVE_CHILD_FD2\\\\n')"], check=True)
                        with (pathlib.Path(os.environ["HERMES_HOME"]) / "noise-stages").open("a") as output:
                            output.write(stage + "\\n")
                ''',
                "tools/__init__.py": "",
                "tools/environments/__init__.py": "",
                "tools/process_registry.py": '''
                    from noisy import noise
                    noise("registry_import")
                    class Registry:
                        def running_owned_by(self, owner):
                            noise("cleanup_background")
                            return []
                    process_registry = Registry()
                ''',
                "tools/environments/base.py": '''
                    from noisy import noise
                    noise("base_import")
                    def kill_live_foreground_processes(*, now):
                        noise("cleanup_foreground")
                        return 0
                ''',
                "tools/terminal_scope.py": '''
                    def build_profile_terminal_scope(home):
                        return {"TERMINAL_ENV": "local"}
                    def set_terminal_scope(policy):
                        return None
                    def reset_terminal_scope(token):
                        pass
                ''',
                "hermes_cli/__init__.py": "",
                "hermes_cli/config.py": '''
                    def load_config_readonly():
                        return {"platform_toolsets": {"cli": ["terminal"]}, "tools": {"tool_search": {"enabled": "off"}}}
                ''',
                "agent/__init__.py": "",
                "agent/skill_commands.py": '''
                    def build_preloaded_skills_prompt(names, task_id=None):
                        return "disposable fixed skill instructions", names, []
                ''',
                "run_agent.py": '''
                    from noisy import noise
                    noise("agent_import")
                    class AIAgent:
                        def __init__(self, **kwargs):
                            noise("agent_init")
                            self.session_id = kwargs["session_id"]
                            self.valid_tool_names = {"terminal", "process_manage"}
                        def hard_interrupt(self, reason):
                            noise("interrupt")
                        def run_conversation(self, prompt, **kwargs):
                            noise("conversation")
                            print(prompt, flush=True)
                            raise RuntimeError(prompt)
                        def close(self):
                            noise("agent_close")
                ''',
            }
            for name, body in modules.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(body))
            bound = request()
            launch = {"binding": bound, "backend": "local", "enabled_tools": ["terminal", "process_manage"],
                      "model": "disposable", "provider": "disposable", "prompt": "SENSITIVE_PROMPT_ERROR",
                      "receipt_policy": {"repo": "example/project", "branch": "codex/disposable"}}
            result = subprocess.run([sys.executable, "-u", str(ADAPTER / "main.py"),
                                     "--experimental-local-terminal", "--journal", str(root / "journal")],
                                    input=json.dumps(launch) + "\n" + json.dumps({"cancel": bound}) + "\n",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
                                    env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(source), "HERMES_HOME": str(home)}, cwd=root)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            self.assertNotIn("SENSITIVE", result.stdout)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(sorted(event["event"] for event in events), sorted(["registered", "stop", "execution_failed", "owner_exiting"]))
            self.assertEqual(events[-1]["errors"], ["execution_failed_or_refused"])
            stages = set((home / "noise-stages").read_text().splitlines())
            self.assertTrue({"registry_import", "base_import", "agent_import", "agent_init",
                             "conversation", "agent_close", "cleanup_foreground", "cleanup_background"}.issubset(stages))
            for path in (root / "journal").iterdir():
                self.assertNotIn("SENSITIVE", path.read_text())


@unittest.skipUnless(os.environ.get("FACTORY_TEST_HERMES_SOURCE") and os.environ.get("FACTORY_TEST_HERMES_PYTHON"),
                     "native checkout and isolated interpreter explicitly required")
class RealNativeCleanupTests(unittest.TestCase):
    def test_controlled_owners_refuse_same_profile_before_registration_and_native_effects(self):
        with tempfile.TemporaryDirectory(prefix='factory-profile-lock-') as temporary:
            root = Path(temporary)
            home = root / 'shared-profile'
            home.mkdir(mode=0o700)
            environment = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': os.environ['FACTORY_TEST_HERMES_SOURCE'], 'HERMES_HOME': str(home)}
            first = subprocess.Popen([os.environ['FACTORY_TEST_HERMES_PYTHON'], '-u', str(ADAPTER / 'main.py'),
                                      '--experimental-local-terminal', '--fixture', '--controlled', '--journal', str(root / 'first')],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment, cwd=root)
            bound = request()
            launch = {'binding': bound, 'backend': 'local', 'enabled_tools': ['terminal', 'process_manage']}
            try:
                first.stdin.write(json.dumps(launch) + '\n'); first.stdin.flush()
                self.assertEqual(read_event(first)['event'], 'registered')
                # Registration alone cannot execute a tool until host authorization.
                self.assertFalse((root / 'first/fixture-child.pid').exists())
                first.stdin.write(json.dumps({'start': bound}) + '\n'); first.stdin.flush()
                while True:
                    ready = read_event(first)
                    if ready['event'] == 'fixture_ready':
                        break
                checkpoint = (home / 'processes.json').read_bytes()
                second = subprocess.run([os.environ['FACTORY_TEST_HERMES_PYTHON'], '-u', str(ADAPTER / 'main.py'),
                                         '--experimental-local-terminal', '--fixture', '--controlled', '--journal', str(root / 'second')],
                                        input=json.dumps({**launch, 'binding': request()}) + '\n', text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, env=environment, cwd=root)
                self.assertEqual(second.returncode, 1)
                self.assertEqual(json.loads(second.stdout)['event'], 'refused')
                self.assertEqual(second.stderr, '')
                self.assertFalse((root / 'second').exists())
                self.assertEqual((home / 'processes.json').read_bytes(), checkpoint)
                self.assertTrue(alive(ready['foreground_pid']))
            finally:
                first.stdin.close()
                try:
                    first.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    first.kill(); first.wait(timeout=5)
                first.stdout.close(); first.stderr.close()

    def test_real_aiagent_dispatches_terminal_over_recorded_transport(self):
        with tempfile.TemporaryDirectory(prefix="factory-real-aiagent-") as temporary:
            root = Path(temporary)
            home = root / "profile"
            home.mkdir(mode=0o700)
            (home / "config.yaml").write_text("model:\n  default: local-test\n  provider: custom\n  base_url: http://127.0.0.1:1/v1\nplatform_toolsets:\n  cli: [terminal]\nterminal:\n  backend: local\ntools:\n  tool_search:\n    enabled: 'off'\n")
            for name in ('implement-change', 'verify-app', 'prepare-pr'):
                directory = home / 'skills' / ('driftward-engineering:' + name)
                directory.mkdir(parents=True)
                (directory / 'SKILL.md').write_text('---\nname: "driftward-engineering:' + name + '"\ndescription: Disposable native preload proof\n---\nFIXED_FACTORY_SKILL_' + name + '\n')
            bound = request()
            process = subprocess.Popen([os.environ["FACTORY_TEST_HERMES_PYTHON"], "-u",
                                        str(Path(__file__).parent / "fixtures/hermes-real-agent-transport.py"),
                                        str(ADAPTER / "main.py"), "--experimental-local-terminal", "--journal", str(root / "journal")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": os.environ["FACTORY_TEST_HERMES_SOURCE"],
                                            "HERMES_HOME": str(home)}, cwd=root)
            try:
                launch = {"binding": bound, "backend": "local", "enabled_tools": ["terminal", "process_manage"],
                          "model": "local-test", "provider": "custom", "prompt": "Run the disposable terminal check and return its receipt.",
                          "receipt_policy": {"repo": "example/project", "branch": "codex/disposable"}}
                process.stdin.write(json.dumps(launch) + "\n")
                process.stdin.flush()
                events = []
                while True:
                    event = read_event(process, 30)
                    events.append(event)
                    if event["event"] in {"execution_failed", "refused"}:
                        diagnostic = (home / "probe-failure.json")
                        self.fail(diagnostic.read_text() if diagnostic.exists() else "recorded agent failed before diagnostic")
                    if event["event"] == "result":
                        break
                self.assertEqual(event["candidate"]["pr_url"], "https://github.com/example/project/pull/1")
                proof = json.loads((home / "transport-proof.json").read_text())
                self.assertTrue(proof["native_tool_result_observed"])
                self.assertTrue(proof["filesystem_effect_observed"])
                self.assertTrue(proof["mandatory_skills_observed"])
                self.assertEqual(proof["network"], "disabled")
                self.assertEqual(proof["provider_requests"], 2)
                process.stdin.close()
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stderr.read(), "")
                (home / 'skills/driftward-engineering:verify-app/SKILL.md').unlink()
                (home / 'transport-proof.json').unlink()
                refused = subprocess.run([os.environ['FACTORY_TEST_HERMES_PYTHON'], '-u',
                                          str(Path(__file__).parent / 'fixtures/hermes-real-agent-transport.py'),
                                          str(ADAPTER / 'main.py'), '--experimental-local-terminal', '--journal', str(root / 'missing-skill')],
                                         input=json.dumps(launch) + '\n', text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': os.environ['FACTORY_TEST_HERMES_SOURCE'], 'HERMES_HOME': str(home)}, cwd=root, timeout=20)
                self.assertEqual(refused.stderr, '')
                self.assertFalse((home / 'transport-proof.json').exists())
                self.assertEqual(json.loads((home / 'probe-failure.json').read_text())['refusal'], 'mandatory Factory engineering skills unavailable')
            finally:
                if not process.stdin.closed:
                    process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_cancel_during_admitted_native_spawn_is_included_in_cleanup(self):
        source = os.environ["FACTORY_TEST_HERMES_SOURCE"]
        python = os.environ["FACTORY_TEST_HERMES_PYTHON"]
        with tempfile.TemporaryDirectory(prefix="factory-native-spawn-") as temporary:
            home = Path(temporary) / "profile"
            home.mkdir(mode=0o700)
            process = subprocess.Popen([python, "-u", str(ADAPTER / "main.py"), "--experimental-local-terminal",
                                        "--fixture", "--journal", str(Path(temporary) / "journal")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": source,
                                                       "HERMES_HOME": str(home), "TERMINAL_ENV": "local"}, cwd=temporary)
            bound = request()
            try:
                process.stdin.write(json.dumps({"binding": bound, "backend": "local", "enabled_tools": ["terminal", "process_manage"]}) + "\n")
                process.stdin.flush()
                events = []
                while True:
                    event = read_event(process)
                    events.append(event["event"])
                    if event["event"] == "fixture_spawn_admitted":
                        process.stdin.write(json.dumps({"cancel": bound}) + "\n")
                        process.stdin.flush()
                    if event["event"] == "stop":
                        receipt = event["receipt"]
                        break
                self.assertTrue(receipt["dispatch_quiesced"])
                self.assertEqual(receipt["errors"], [])
                self.assertEqual(len(receipt["background"]), 1)
                self.assertEqual(receipt["background"][0]["status"], "killed")
                self.assertFalse(alive(receipt["background"][0]["pid"]))
                self.assertNotIn("fixture_ready", events)
                self.assertEqual(receipt["state"], "unknown")
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_three_owners_native_foreground_background_persistence_and_duplicate_cancel(self):
        source = Path(os.environ["FACTORY_TEST_HERMES_SOURCE"])
        python = os.environ["FACTORY_TEST_HERMES_PYTHON"]
        self.assertEqual(subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(), PIN)
        subprocess.run(["git", "-C", str(source), "diff", "--exit-code", "HEAD", "--", "*.py"], check=True, stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(prefix="factory-native-owner-") as temporary:
            running = []
            def receive(process, expected):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    value = read_event(process, max(0, deadline - time.monotonic()))
                    if value["event"] == expected:
                        return value
                    self.assertNotEqual(value["event"], "refused", value)
                self.fail(f"no {expected} event")
            def send(process, value):
                process.stdin.write(json.dumps(value) + "\n")
                process.stdin.flush()
            try:
                for name in ("target", "concurrent", "reviewer-control"):
                    root = Path(temporary) / name
                    root.mkdir(mode=0o700)
                    # Deliberately model nonparticipating legacy interpreters:
                    # two share a profile without the new admission lock.
                    home = (Path(temporary) / 'legacy-shared-profile') if name != 'reviewer-control' else root / 'profile'
                    home.mkdir(mode=0o700, exist_ok=True)
                    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(source), "HERMES_HOME": str(home), "TERMINAL_ENV": "local"}
                    process = subprocess.Popen([python, "-u", str(ADAPTER / "main.py"), "--experimental-local-terminal",
                                                "--fixture", "--journal", str(root / "journal")],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, env=env, cwd=root)
                    run = {"process": process, "binding": request()}
                    running.append(run)
                    send(process, {"binding": run["binding"], "backend": "local", "enabled_tools": ["terminal", "process_manage"]})
                    registration = receive(process, "registered")
                    run['raw_owner'] = registration['ownership']['raw_owners'][0]
                    self.assertTrue((root / "journal/ownership.json").exists())
                    self.assertEqual(registration["ownership"]["pid"], process.pid)
                    run["ready"] = receive(process, "fixture_ready")
                    run["pids"] = [run["ready"]["foreground_pid"]] + [x["pid"] for x in run["ready"]["background"]]
                    run["pids"].append(run["ready"]["escaped_pid"])
                    self.assertNotEqual(os.getpgid(run["ready"]["escaped_pid"]), os.getpgid(run["ready"]["foreground_pid"]))
                    self.assertTrue(all(alive(pid) for pid in run["pids"]))
                    run["tree"] = descendants(run["pids"])
                target = running[0]
                checkpoint_path = Path(temporary) / 'legacy-shared-profile/processes.json'
                before = json.loads(checkpoint_path.read_text())
                self.assertIn(running[1]['raw_owner'], {row['owner_task_id'] for row in before})
                send(target["process"], {"cancel": {**target["binding"], "fingerprint": "b" * 64}})
                receive(target["process"], "refused")
                self.assertTrue(all(alive(pid) for run in running for pid in run["pids"]))
                send(target["process"], {"cancel": target["binding"]})
                receipt = receive(target["process"], "stop")["receipt"]
                self.assertEqual(receipt["state"], "unknown")
                self.assertTrue(receipt["dispatch_quiesced"])
                self.assertEqual(receipt["errors"], [])
                self.assertEqual(len(receipt["background"]), 2)
                self.assertTrue(all(row["status"] in {"killed", "already_exited"} for row in receipt["background"]))
                deadline = time.monotonic() + 5
                while any(alive(pid) for pid in target["tree"]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(any(alive(pid) for pid in target["tree"]))
                self.assertTrue(all(alive(pid) for run in running[1:] for pid in run["pids"]))
                # Native process liveness is scoped, but native checkpoint writes
                # are process-local replacements. This measured metadata loss
                # is an activation blocker with nonparticipating legacy owners.
                after = json.loads(checkpoint_path.read_text())
                self.assertNotIn(running[1]['raw_owner'], {row['owner_task_id'] for row in after})
                send(target["process"], {"cancel": target["binding"]})
                self.assertEqual(receive(target["process"], "stop")["receipt"], receipt)
                for run in running[1:]:
                    send(run["process"], {"cancel": run["binding"]})
                    receive(run["process"], "stop")
            finally:
                for run in running:
                    process = run["process"]
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        # This is only the fixture Popen we created above, never a
                        # PID discovered in Hermes state or a live runtime.
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
