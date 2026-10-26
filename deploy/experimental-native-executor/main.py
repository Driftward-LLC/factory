"""Opt-in experimental owner; private inherited stdin is its only control channel.

Not referenced by deployment/production commands. JSON lines: first a launch
frame, then exact-bound cancel frames. stdout contains bounded metadata only.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time
from types import SimpleNamespace

from owner import Owner, Refused, guarded_agent


def frame(stream):
    line = stream.readline(65537)
    if not line or len(line) > 65536 or not line.endswith("\n"):
        raise Refused("invalid bounded frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise Refused("invalid frame")
    return value


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


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
    from tools.terminal_scope import build_profile_terminal_scope, reset_terminal_scope, set_terminal_scope
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
                    skip_context_files=True, load_soul_identity=False, skip_memory=True,
                    skip_background_review=True, checkpoints_enabled=False, session_db=None,
                    cwd=str(owner.root))
        if set(agent.valid_tool_names) != {"terminal", "process_manage"}:
            raise Refused("native tool surface unsupported")
        owner.attach(agent)
        try:
            agent.run_conversation(request["prompt"], task_id=owner.raw_owner)
        finally:
            agent.close()
    finally:
        reset_terminal_scope(token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experimental-local-terminal", action="store_true", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    request = frame(sys.stdin)
    expected = {"binding", "backend", "enabled_tools"}
    if not args.fixture:
        expected |= {"model", "provider", "prompt"}
    if set(request) != expected:
        raise Refused("invalid launch fields")
    # Only an explicitly selected profile home is permitted, never root fallback
    # by an omitted environment variable. Tests supply a newly created temp home.
    if not os.environ.get("HERMES_HOME"):
        raise Refused("explicit Hermes profile home required")
    owner = Owner(args.journal, request["binding"], backend=request["backend"],
                  enabled_tools=request["enabled_tools"])
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
                conversation(owner, request)
        except Exception:
            errors.append("execution_failed_or_refused")
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        while True:
            request = frame(sys.stdin)
            if set(request) != {"cancel"}:
                emit({"event": "refused", "reason": "unsupported_frame"})
                continue
            try:
                emit({"event": "stop", "receipt": owner.cancel(request["cancel"], native)})
            except (Refused, ValueError, TypeError):
                emit({"event": "refused", "reason": "binding_or_owner_state_mismatch"})
    except (Refused, ValueError, TypeError):
        # Pipe closure is not permission to orphan work. Use the in-memory
        # binding, never infer an old process's ownership from the filesystem.
        owner.cancel(owner.binding, native)
    worker.join(timeout=4)
    emit({"event": "owner_exiting", "worker_exited": not worker.is_alive(), "errors": errors,
          "completion": "not_established"})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No prompts, provider errors, tool commands or tracebacks on this channel.
        emit({"event": "refused", "reason": "invalid_launch_or_runtime"})
        sys.exit(1)
