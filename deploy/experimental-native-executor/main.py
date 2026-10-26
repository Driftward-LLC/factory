"""Opt-in experimental owner; private inherited stdin is its only control channel.

Referenced by a default-disabled gateway route and packaged as public installer
assets. JSON lines carry launch, exact-bound start and cancellation frames.
stdout contains only bounded protocol metadata and filtered result candidates.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import re
import stat
import sys
import threading
import time
from types import SimpleNamespace

from owner import Owner, Refused, binding, guarded_agent


_PROTOCOL = None
_PROTOCOL_LOCK = threading.Lock()


def isolate_native_output():
    """Retain one non-inherited protocol writer; discard all native output.

    Redirect OS descriptors before importing Hermes: quiet_mode does not suppress
    every native error, and Python-only redirection misses os.write/C/child output.
    """
    global _PROTOCOL
    protocol_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(protocol_fd, False)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    finally:
        os.close(null_fd)
    _PROTOCOL = os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def frame(stream):
    line = stream.readline(65537)
    if not line or len(line) > 65536 or not line.endswith("\n"):
        raise Refused("invalid bounded frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise Refused("invalid frame")
    return value


def claim_profile(home):
    """Serialize cooperating owners without changing Hermes checkpoint policy."""
    fd = os.open(Path(home) / "factory-native-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused("invalid native profile admission lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd  # Held until this dedicated interpreter exits; never unlinked.
    except BaseException:
        os.close(fd)
        raise Refused("native profile already owned or unavailable")


def emit(value):
    with _PROTOCOL_LOCK:
        print(json.dumps(value, separators=(",", ":")), file=_PROTOCOL, flush=True)


def fixture(owner, native):
    """Real native resources, deterministic sleeps only, no model/provider."""
    from tools.environments.local import LocalEnvironment
    command = shlex.quote(sys.executable) + " -I -c 'import time; time.sleep(90)'"
    resources = []
    for persistent in (False, True):
        with owner.dispatch(owner.session, owner.raw_owner, ["terminal"]):
            if not resources:
                emit({"event": "fixture_spawn_admitted"})
                # Deterministic disposable window: cancellation must wait for
                # this already-admitted native spawn and then include it.
                time.sleep(0.15)
            process = native.registry.spawn_local(command, cwd=str(owner.root), task_id="shared-environment",
                                                  owner_task_id=owner.raw_owner, persist_on_release=persistent)
            resources.append({"pid": process.pid, "persistent": persistent})
    with owner.dispatch(owner.session, owner.raw_owner, ["terminal"]):
        environment = LocalEnvironment(cwd=str(owner.root), timeout=90)
        def ready():
            from tools.environments.base import _live_foreground, _live_foreground_cond
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with _live_foreground_cond:
                    live = list(_live_foreground.values())
                marker = owner.root / "fixture-child.pid"
                if live and marker.exists():
                    emit({"event": "fixture_ready", "background": resources,
                          "foreground_pid": live[0][1].pid, "escaped_pid": int(marker.read_text())})
                    return
                time.sleep(0.01)
        threading.Thread(target=ready, daemon=True).start()
        foreground_code = ("import subprocess,sys,time,pathlib; "
                           "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(90)'],start_new_session=True); "
                           "pathlib.Path(" + repr(str(owner.root / "fixture-child.pid")) + ").write_text(str(child.pid)); time.sleep(90)")
        environment.execute(shlex.quote(sys.executable) + " -I -c " + shlex.quote(foreground_code), timeout=90)


def conversation(owner, request):
    from run_agent import AIAgent
    from hermes_cli.config import load_config_readonly
    from tools.terminal_scope import build_profile_terminal_scope, reset_terminal_scope, set_terminal_scope
    config = load_config_readonly() or {}
    if config.get("platform_toolsets", {}).get("cli") != ["terminal"]:
        raise Refused("explicit local-terminal-only profile required")
    if config.get("tools", {}).get("tool_search", {}).get("enabled") != "off":
        raise Refused("explicit direct native tool dispatch required")
    from agent.skill_commands import build_preloaded_skills_prompt
    required_skills = ['driftward-engineering:implement-change', 'driftward-engineering:verify-app',
                       'driftward-engineering:prepare-pr']
    skills_prompt, loaded, missing = build_preloaded_skills_prompt(required_skills, task_id=owner.session)
    if missing or set(loaded) != set(required_skills) or not skills_prompt:
        raise Refused("mandatory Factory engineering skills unavailable")
    policy = build_profile_terminal_scope(os.environ["HERMES_HOME"])
    if policy.get("TERMINAL_ENV", "local") != "local":
        raise Refused("remote terminal backend unsupported")
    token = set_terminal_scope(policy)
    try:
        cls = guarded_agent(AIAgent, owner)
        agent = cls(model=request["model"], provider=request["provider"],
                    session_id=owner.session, enabled_toolsets=["terminal"],
                    max_iterations=20, run_budget_seconds=120, fallback_model=[],
                    quiet_mode=True, save_trajectories=False, verbose_logging=False,
                    ephemeral_system_prompt=skills_prompt, skip_context_files=False, load_soul_identity=True, skip_memory=True,
                    skip_background_review=True, checkpoints_enabled=False, session_db=None,
                    cwd=os.getcwd())
        try:
            from hermes_cli.plugins import get_plugin_manager
            plugin = get_plugin_manager()._plugins.get('observability/posthog')
            callback = getattr(getattr(plugin, 'module', None), 'observe_owner', None)
            if plugin and plugin.enabled and not plugin.error and callable(callback):
                owner.observer = callback
                owner.observe('registered')
        except Exception:
            pass  # Optional telemetry never changes execution or stop authority.
        if set(agent.valid_tool_names) != {"terminal", "process_manage"}:
            raise Refused("native tool surface unsupported")
        owner.attach(agent)
        try:
            result = agent.run_conversation(request["prompt"], task_id=owner.raw_owner)
            if result.get("interrupted") or result.get("failed") or not result.get("completed"):
                raise Refused("native conversation did not complete")
            return bounded_candidate(result.get("final_response"), request["receipt_policy"])
        finally:
            agent.close()
    finally:
        reset_terminal_scope(token)


def bounded_candidate(text, policy):
    """Only immutable-policy receipt fields cross the private result channel."""
    if not isinstance(text, str) or len(text) > 65536 or not isinstance(policy, dict):
        raise Refused("invalid result candidate")
    value = json.loads(text)
    repo, branch = policy.get("repo"), policy.get("branch")
    if (not isinstance(value, dict) or not isinstance(repo, str) or not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or len(branch) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or value.get("status") not in {"created", "reused"}
            or value.get("branch", branch) != branch
            or not isinstance(value.get("pr_url"), str)
            or not re.fullmatch(re.escape("https://github.com/" + repo + "/pull/") + r"[1-9][0-9]{0,14}", value["pr_url"])):
        raise Refused("invalid result candidate identity")
    commit = value.get("commit_sha")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", commit)):
        raise Refused("invalid result candidate revision")
    validation = None
    expected = policy.get("verification_policy")
    raw = value.get("validation")
    if isinstance(expected, dict) and isinstance(raw, dict):
        commands = expected.get("required_local_commands")
        secret = expected.get("secret_check_command")
        rows = raw.get("local_commands")
        def command(item, name):
            return (isinstance(item, dict) and item.get("command") == name
                    and type(item.get("exit_code")) is int and -(2**31) <= item["exit_code"] < 2**31)
        if (isinstance(commands, list) and 0 < len(commands) <= 32
                and all(isinstance(c, str) and 0 < len(c) <= 4096 for c in commands)
                and isinstance(secret, str) and 0 < len(secret) <= 4096 and isinstance(rows, list)
                and command(raw.get("secret_check"), secret)):
            selected = [[row for row in rows if command(row, name)] for name in commands]
            if all(len(matches) == 1 for matches in selected):
                validation = {"local_commands": [{"command": name, "exit_code": matches[0]["exit_code"]}
                                                  for name, matches in zip(commands, selected)],
                              "secret_check": {"command": secret, "exit_code": raw["secret_check"]["exit_code"]}}
    return {"status": value["status"], "pr_url": value["pr_url"], "branch": branch,
            "commit_sha": commit, "validation": validation}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experimental-local-terminal", action="store_true", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--controlled", action="store_true")
    args = parser.parse_args()
    launch_request = frame(sys.stdin)
    expected = {"binding", "backend", "enabled_tools"}
    if not args.fixture:
        expected |= {"model", "provider", "prompt", "receipt_policy"}
    if set(launch_request) != expected:
        raise Refused("invalid launch fields")
    # Only an explicitly selected profile home is permitted, never root fallback
    # by an omitted environment variable. Tests supply a newly created temp home.
    if not os.environ.get("HERMES_HOME"):
        raise Refused("explicit Hermes profile home required")
    if args.controlled or not args.fixture:
        profile_lock = claim_profile(os.environ["HERMES_HOME"])
    owner = Owner(args.journal, launch_request["binding"], backend=launch_request["backend"],
                  enabled_tools=launch_request["enabled_tools"])
    from tools.process_registry import process_registry
    from tools.environments.base import kill_live_foreground_processes
    native = SimpleNamespace(registry=process_registry, kill_foreground=kill_live_foreground_processes)
    emit({"event": "registered", "ownership": owner.record})
    errors = []
    def run():
        try:
            if args.fixture:
                fixture(owner, native)
            else:
                candidate = conversation(owner, launch_request)
                emit({"event": "result", "candidate": candidate})
        except Exception:
            errors.append("execution_failed_or_refused")
            emit({"event": "execution_failed", "reason": "execution_failed_or_refused"})
    worker = threading.Thread(target=run, daemon=True)
    started = not args.controlled
    if started:
        worker.start()
    try:
        while True:
            control_frame = frame(sys.stdin)
            if args.controlled and set(control_frame) == {"start"}:
                if started or owner.fenced or binding(control_frame["start"]) != owner.binding:
                    emit({"event": "refused", "reason": "start_not_authorized"})
                else:
                    started = True
                    worker.start()
                continue
            if set(control_frame) != {"cancel"}:
                emit({"event": "refused", "reason": "unsupported_frame"})
                continue
            try:
                emit({"event": "stop", "receipt": owner.cancel(control_frame["cancel"], native)})
            except (Refused, ValueError, TypeError):
                emit({"event": "refused", "reason": "binding_or_owner_state_mismatch"})
    except (Refused, ValueError, TypeError):
        # Pipe closure is not permission to orphan work. Use the in-memory
        # binding, never infer an old process's ownership from the filesystem.
        owner.cancel(owner.binding, native)
    if started:
        worker.join(timeout=4)
    emit({"event": "owner_exiting", "binding": owner.binding, "worker_exited": not started or not worker.is_alive(), "errors": errors,
          "completion": "not_established"})


if __name__ == "__main__":
    try:
        isolate_native_output()
    except BaseException:
        # Isolation must succeed before anything can import/initialize Hermes.
        os._exit(1)
    try:
        main()
    except Exception:
        # No prompts, provider errors, tool commands or tracebacks on this channel.
        emit({"event": "refused", "reason": "invalid_launch_or_runtime"})
        sys.exit(1)
