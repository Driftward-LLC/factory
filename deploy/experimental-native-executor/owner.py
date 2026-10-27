"""Experimental, process-local Hermes ownership. Never imported by production.

The private inherited input pipe is the control authority. Disk records are
evidence, not permission to attach to or signal an old process after a crash.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time
import uuid


class Refused(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def binding(value):
    if (not isinstance(value, dict) or set(value) != {"invocation_id", "epoch", "fingerprint"}
            or not isinstance(value["invocation_id"], str) or not isinstance(value["fingerprint"], str)):
        raise Refused("invalid binding fields")
    parts = value["invocation_id"].split(":")
    if len(parts) != 3 or any(str(uuid.UUID(x)) != x for x in parts[:2]):
        raise Refused("invalid invocation identity")
    if type(value["epoch"]) is not int or value["epoch"] < 1 or parts[2] != str(value["epoch"]):
        raise Refused("epoch mismatch")
    if not re.fullmatch(r"[a-f0-9]{64}", value["fingerprint"]):
        raise Refused("invalid fingerprint")
    return dict(value)


def atomic(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Owner:
    """Exactly one invocation per fresh process, no attach/recovery operation."""
    def __init__(self, root, request, *, backend, enabled_tools, observer=None):
        if backend != "local" or set(enabled_tools) != {"terminal", "process_manage"}:
            raise Refused("unsupported capabilities: only native local terminal is supported")
        self.binding = binding(request)
        self.root = Path(root)
        # Exclusive directory creation is replay protection. Never adopt an old
        # journal, even when its process appears absent.
        self.root.mkdir(mode=0o700)
        self.lock = threading.Condition(threading.RLock())
        self.fenced = False
        self.inflight = 0
        self.agent = None
        self.receipt = None
        self.observer = observer
        self.raw_owner = "factory-" + uuid.uuid4().hex
        self.session = "factory-" + uuid.uuid4().hex
        self.record = {
            "schema": 1, **self.binding, "runtime_incarnation": str(uuid.uuid4()),
            "pid": os.getpid(), "start_ticks": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
            "pid_namespace": os.readlink("/proc/self/ns/pid"),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "sessions": [self.session], "raw_owners": [self.raw_owner],
            "backend": "local", "delegation": "unsupported", "created_at": time.time(),
        }
        atomic(self.root / "ownership.json", self.record)

    def observe(self, stage):
        if self.observer is None:
            return
        event = {"event": "factory.experimental_owner", "distinct_id": "driftward-hermes",
                 "properties": {"factory.invocation_id": self.binding["invocation_id"],
                                "factory.lease_epoch": self.binding["epoch"],
                                "factory.experimental": True, "factory.stop.stage": stage,
                                "hermes.session_id": self.session, "hermes.task_id": self.raw_owner,
                                "factory.runtime_incarnation": self.record["runtime_incarnation"],
                                "$process_person_profile": False}}
        try:
            self.observer(event)
        except Exception:
            pass

    def attach(self, agent):
        with self.lock:
            if self.agent is not None or agent.session_id != self.session:
                raise Refused("agent ownership mismatch")
            self.agent = agent
            if self.fenced:
                agent.hard_interrupt("Factory cancellation")

    def verify_process(self):
        if (self.record["pid"] != os.getpid()
                or self.record["start_ticks"] != Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
                or self.record["pid_namespace"] != os.readlink("/proc/self/ns/pid")
                or self.record["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            raise Refused("owner process identity mismatch")
        fd = os.open(self.root / "ownership.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or not 0 < metadata.st_size <= 65536):
                raise Refused("invalid private ownership record")
            encoded = stream.read(65537)
        if len(encoded) > 65536 or json.loads(encoded) != self.record:
            raise Refused("durable ownership mismatch")

    @contextmanager
    def dispatch(self, session, raw_owner, tool_names, *, backend="local"):
        with self.lock:
            self.verify_process()
            if self.fenced:
                raise Refused("dispatch fenced")
            if (backend != "local" or len(tool_names) != 1
                    or tool_names[0] not in {"terminal", "process_manage"} or raw_owner != self.raw_owner):
                self.fenced = True
                raise Refused("unsupported dispatch or owner")
            # Session rotation is registered before admitting this tool batch.
            if not isinstance(session, str) or not session or len(session) > 200:
                self.fenced = True
                raise Refused("invalid native session")
            if session not in self.record["sessions"]:
                if len(self.record["sessions"]) >= 32:
                    self.fenced = True
                    raise Refused("session registration limit")
                self.record["sessions"].append(session)
                try:
                    atomic(self.root / "ownership.json", self.record)
                except BaseException:
                    self.fenced = True
                    raise
            self.inflight += 1
        try:
            yield
        finally:
            with self.lock:
                self.inflight -= 1
                self.lock.notify_all()

    def cancel(self, request, native, *, timeout=3):
        if binding(request) != self.binding:
            raise Refused("cancellation binding mismatch")
        errors = []
        with self.lock:
            self.verify_process()
            if self.receipt is not None:
                return self.receipt
            self.fenced = True
            self.receipt = {
                "schema": 1, **self.binding, "ownership_digest": digest(self.record),
                "runtime_incarnation": self.record["runtime_incarnation"],
                "state": "requested", "requested_at": time.time(),
                "completion": "not_established", "owner_exit": "not_observed",
            }
            try:
                atomic(self.root / "stop.json", self.receipt)
            except OSError:
                errors.append("stop_journal_write_failed")
            self.receipt.update(state="stopping", stopping_at=time.time())
            try:
                atomic(self.root / "stop.json", self.receipt)
            except OSError:
                if "stop_journal_write_failed" not in errors:
                    errors.append("stop_journal_write_failed")
        self.observe("stopping")
        try:
            if self.agent is not None:
                self.agent.hard_interrupt("Factory cancellation")
        except Exception:
            errors.append("native_interrupt_failed")
        foreground = {"graceful_signalled": None, "forced_signalled": None}
        try:
            # Process-global by design: this launcher owns the entire fresh
            # interpreter and never serves another invocation in it.
            # Graceful local cleanup snapshots escaped descendants. The hard
            # exit path alone kills groups and loses that tree evidence.
            foreground["graceful_signalled"] = native.kill_foreground(now=False)
        except Exception:
            errors.append("foreground_cleanup_failed")
        try:
            foreground["forced_signalled"] = native.kill_foreground(now=True)
        except Exception:
            errors.append("foreground_fence_failed")
        with self.lock:
            quiesced = self.lock.wait_for(lambda: self.inflight == 0, timeout=timeout)
        background = []
        if quiesced:
            try:
                sessions = native.registry.running_owned_by(self.raw_owner)
                if len(sessions) > 64:
                    raise Refused("background resource bound exceeded")
                for process in sessions:
                    result = native.registry.kill_process(process.id, source="factory_explicit_cancel", consume_output=True)
                    # Native results may contain command/output. Never persist
                    # those fields in a Factory ownership or stop receipt.
                    status = result.get("status", "unknown")
                    status = status if status in {"killed", "already_exited", "error", "not_found"} else "unknown"
                    background.append({"id": process.id, "status": status,
                                       "pid": getattr(process, "pid", None),
                                       "scope_stop_failed": bool(result.get("scope_stop_failed"))})
            except Exception:
                errors.append("background_cleanup_failed")
        else:
            errors.append("inflight_dispatch_not_quiescent")
        with self.lock:
            self.receipt.update(
                state="unknown", observed_at=time.time(), foreground_signalled=foreground,
                background=background, dispatch_quiesced=quiesced, errors=errors,
                unresolved=["escaped_descendants_unmeasured", "external_effects_unsettled",
                            "foreground_survivor_coverage_incomplete"],
            )
            try:
                atomic(self.root / "stop.json", self.receipt)
            except OSError:
                if "stop_journal_write_failed" not in errors:
                    errors.append("stop_journal_write_failed")
            self.observe("unknown")
            return self.receipt


def guarded_agent(base, owner):
    """Use the same AIAgent subclass boundary as the shipped reviewer adapter."""
    class LocalOnlyAgent(base):
        def _execute_tool_calls(self, message, messages, effective_task_id, api_call_count=0):
            names = [call.function.name for call in message.tool_calls]
            from tools.terminal_scope import terminal_env
            with owner.dispatch(self.session_id, effective_task_id, names, backend=terminal_env("TERMINAL_ENV", "local")):
                return super()._execute_tool_calls(message, messages, effective_task_id, api_call_count)
    return LocalOnlyAgent


def child_ids():
    # Only this dedicated, single-threaded parent's kernel child list. Never a
    # checkpoint, caller PID, process-name search or system-wide /proc scan.
    with open(f"/proc/self/task/{os.getpid()}/children") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise Refused("child enumeration bound exceeded")
    return [int(value) for value in data.split()]


def prepare_supervisor():
    if threading.active_count() != 1 or child_ids():
        raise Refused("dedicated single-threaded supervisor required")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    # PR_SET_CHILD_SUBREAPER is process-local and not inherited across fork.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise Refused("subreaper unavailable")
    enabled = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0 or enabled.value != 1:
        raise Refused("subreaper verification failed")
    # Probe required APIs before any native import or start authorization.
    fd = os.pidfd_open(os.getpid())
    try:
        signal.pidfd_send_signal(fd, 0)
        try:
            os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            pass  # Self is not a child; the syscall/flags were understood.
        else:
            raise Refused("invalid pidfd child check")
    finally:
        os.close(fd)


def drain_children(timeout=5):
    """Reap until ECHILD; only live kernel children authorize each signal."""
    deadline = time.monotonic() + timeout
    reaped = attempts = 0
    errors = set()
    while time.monotonic() < deadline and attempts < 4096:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return {"descendants_drained": True, "reaped": reaped, "errors": sorted(errors)}
        except OSError:
            errors.add("child_wait_failed")
            break
        if pid:
            reaped += 1
            continue
        try:
            children = child_ids()
            for child in children:
                attempts += 1
                if attempts > 4096:
                    break
                fd = os.pidfd_open(child)
                try:
                    # pidfds establish identity, not authority. This check must
                    # succeed for our current child before we send any signal.
                    os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # Exit raced the signal; waitpid still must reap it.
                finally:
                    os.close(fd)
        except (OSError, ValueError, Refused):
            errors.add("child_signal_or_identity_failed")
            break
        time.sleep(0.01)
    errors.add("descendant_drain_incomplete")
    return {"descendants_drained": False, "reaped": reaped, "errors": sorted(errors)}


def supervise(run, claim, request, emit):
    """Fresh parent only; no disk-driven recovery, retry, or profile release."""
    bound = binding(request)
    prepare_supervisor()
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_write)
        try:
            authorized = os.read(ready_read, 1) == b"1"
        finally:
            os.close(ready_read)
        code = 1
        if authorized:
            try:
                run()
                code = 0
            except BaseException:
                try:
                    emit({"event": "refused", "reason": "invalid_launch_or_runtime"})
                except Exception:
                    pass
        # No interpreter/atexit work after the owner-exit announcement. The
        # surviving parent handles every remaining local descendant separately.
        os._exit(code)
    os.close(ready_read)
    errors = []
    record = {"schema": 1, **bound, "supervisor_pid": os.getpid(), "owner_pid": child,
              "supervisor_incarnation": str(uuid.uuid4()), "created_at": time.time(),
              "recovery": "operator_review_required"}
    try:
        atomic(claim / "supervisor.json", record)
        os.write(ready_write, b"1")
    except OSError:
        errors.append("supervisor_journal_write_failed")
    finally:
        os.close(ready_write)
    # The child alone consumes the inherited private control input.
    os.close(0)
    _, status = os.waitpid(child, 0)
    receipt = {"schema": 1, **bound, "owner_exit_code": os.waitstatus_to_exitcode(status),
               **drain_children(), "completion": "not_established", "profile_reusable": False}
    receipt["errors"] += errors
    try:
        atomic(claim / "recovery.json", receipt)
    except OSError:
        receipt["errors"].append("recovery_journal_write_failed")
    try:
        emit({"event": "owner_reaped", "receipt": receipt})
    except (OSError, ValueError):
        pass  # Lost transport cannot disable cleanup or its private evidence.
    return int(receipt["owner_exit_code"] != 0 or not receipt["descendants_drained"] or bool(receipt["errors"]))
