"""Bounded, opt-in UI verification pilot. This is not a Factory completion writer."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time
import uuid

import bootstrap

HERE = Path(__file__).resolve().parent
LABEL = "io.driftward.factory.ui-run"
SHA = r"[a-f0-9]{40}"
IMAGE = r"(?:sha256:|[a-z0-9][a-z0-9._/:\-]*@sha256:)[a-f0-9]{64}"


class Cancelled(Exception):
    """Do not use InterruptedError: subprocess selectors may swallow EINTR."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_request(value: dict) -> dict:
    required = {"source_commit", "preview_image", "path", "objective", "steps", "model", "timeout_seconds"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("request must contain exactly the documented fields")
    if not re.fullmatch(SHA, str(value["source_commit"])) or not re.fullmatch(IMAGE, str(value["preview_image"])):
        raise ValueError("preview requires an immutable image and exact source commit")
    path = value["path"]
    if not isinstance(path, str) or path.startswith("//") or not re.fullmatch(r"/[A-Za-z0-9/_-]*", path):
        raise ValueError("preview path must be local, without query, fragment or credentials")
    if not isinstance(value["objective"], str) or not 1 <= len(value["objective"]) <= 2000:
        raise ValueError("objective must be 1..2000 characters of synthetic test instructions")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", str(value["model"])):
        raise ValueError("select an explicit model")
    if type(value["timeout_seconds"]) is not int or not 30 <= value["timeout_seconds"] <= 600:
        raise ValueError("timeout must be 30..600 seconds")
    steps = value["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise ValueError("provide 1..30 browser steps")
    fields = {"click": {"action", "role", "name"}, "fill": {"action", "label", "value"},
              "press": {"action", "key"}, "expect": {"action", "text"}}
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get("action"), str)
                or set(step) != fields.get(step["action"])
                or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in step.values())):
            raise ValueError("invalid browser step")
        if step["action"] == "click" and step["role"] not in {"button", "link", "checkbox", "radio", "tab", "menuitem"}:
            raise ValueError("unsupported click role")
        if step["action"] == "press" and step["key"] not in {"Tab", "Shift+Tab", "Enter", "Space", "Escape", "ArrowDown", "ArrowUp"}:
            raise ValueError("unsupported keyboard action")
    if not any(s["action"] == "expect" for s in steps):
        raise ValueError("at least one observable assertion is required")
    return value


def command(args: list[str], *, env: dict | None = None, timeout: int = 60) -> str:
    # Docker invokes Compose as a child process. Stop the whole local process
    # group before container cleanup so a surviving plugin cannot keep launching.
    with subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
            raise
    if process.returncode:
        raise RuntimeError("verification command failed; private output withheld")
    return stdout.strip()


def instructions(plugin: Path) -> tuple[bytes, dict]:
    plugin = bootstrap.checked_path(plugin)
    manifest_path = bootstrap.checked_path(plugin / ".codex-plugin/plugin.json")
    source = bootstrap.checked_path(plugin / "skills/verify-app/SKILL.md")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("name") != "driftward-engineering" or not re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", manifest.get("version", "")):
        raise ValueError("expected a versioned Driftward Engineering plugin")
    raw = source.read_bytes()
    if not 1 <= len(raw) <= 32768:
        raise ValueError("verification skill is empty or too large")
    return raw, {"name": manifest["name"], "version": manifest["version"], "verify_skill_sha256": digest(raw)}


@contextmanager
def lock(directory: Path):
    path = directory / ".ui-verification.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("unsafe verification lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another UI verification is already running") from exc
        yield
    finally:
        os.close(descriptor)


def image_identity(reference: str, *, preview: bool, commit: str | None = None) -> str:
    if not re.fullmatch(IMAGE, reference):
        raise ValueError("runner and preview images must be immutable local IDs or repository digests")
    row = json.loads(command(["docker", "image", "inspect", reference]))[0]
    labels = row["Config"].get("Labels") or {}
    if row["Config"].get("Volumes"):
        raise ValueError("verification images must not create anonymous durable volumes")
    role = "ui-preview" if preview else "ui-verifier"
    if labels.get("io.driftward.factory." + role) != "1":
        raise ValueError("image is not explicitly prepared for its verification role")
    if commit and labels.get("org.opencontainers.image.revision") != commit:
        raise ValueError("preview image source revision differs from the request")
    if row.get("Os") != "linux" or row.get("Architecture") != "amd64" or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["Id"]):
        raise ValueError("pilot images require Linux/amd64")
    return row["Id"]


def safe_json(path: Path, request_hash: str) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
        raise ValueError("invalid verification evidence")
    value = json.loads(path.read_text())
    if value.get("schema") != 1 or value.get("request_sha256") != request_hash:
        raise ValueError("evidence does not match the immutable request")
    return value


def outcome(checks: dict, agent: dict, *, fixture: bool, steps: int) -> str:
    if checks.get("status") != "passed" or checks.get("completed_steps") != steps * 2 or checks.get("viewports") != [1280, 390]:
        return "checks_failed"
    if agent.get("status") != "completed" or type(agent.get("browser_calls")) is not int or agent["browser_calls"] < 1:
        return "agent_blocked"
    if fixture:
        return "fixture_passed"
    if agent.get("assessment") not in {"no_findings", "findings"}:
        return "agent_blocked"
    return "needs_attention" if agent.get("assessment") == "findings" else "observed"


class Pilot:
    def __init__(self, directory: Path, request: dict, runner_image: str, plugin: Path, *, fixture: bool = False):
        self.directory = bootstrap.checked_path(directory)
        self.metadata, _ = bootstrap.validate_directory(self.directory)
        self.request = validate_request(request)
        self.skill, self.plugin = instructions(plugin)
        self.runner_image = image_identity(runner_image, preview=False)
        self.preview_image = image_identity(request["preview_image"], preview=True, commit=request["source_commit"])
        self.fixture = fixture
        self.run_id = str(uuid.uuid4())
        self.project = "factory-ui-" + self.run_id.replace("-", "")
        self.root = self.directory / "ui-verification" / self.run_id
        self.args = ["docker", "compose", "--project-name", self.project, "--project-directory", str(self.root),
                     "--env-file", "/dev/null", "-f", str(HERE / "compose.ui-verifier.yml"), "--profile", "ui-verification"]
        # Do not inherit any deployment's Compose, provider, proxy, or Docker context settings.
        self.env = {k: os.environ[k] for k in ("PATH", "DOCKER_CONFIG") if k in os.environ}
        self.env.update(FACTORY_UI_INSTALLATION=self.metadata["installation_id"], FACTORY_UI_RUN=self.run_id,
                        FACTORY_UI_PREVIEW_IMAGE=self.preview_image, FACTORY_UI_RUNNER_IMAGE=self.runner_image,
                        FACTORY_UI_REQUEST=str(self.root / "request.json"),
                        FACTORY_UI_CHECKS=str(self.root / "checks"), FACTORY_UI_AGENT=str(self.root / "agent"),
                        FACTORY_UI_INSTRUCTIONS=str(self.root / "instructions.md"),
                        FACTORY_UI_CONTROL_SECRET=str(self.root / "secrets/browser-token"),
                        FACTORY_UI_API_KEY=str(self.root / "secrets/openai-api-key"))

    def compose(self, *args: str, timeout: int = 60) -> str:
        return command([*self.args, *args], env=self.env, timeout=timeout)

    def prepare(self, api_key: bytes) -> str:
        parent = bootstrap.checked_path(self.root.parent)
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.stat().st_mode & 0o077:
            raise ValueError("evidence parent must be private")
        self.root.mkdir(mode=0o700)
        for name in ("checks", "agent", "secrets"):
            target = self.root / name
            target.mkdir(mode=0o700)
            os.chown(target, 1000, 1000)
        raw = (json.dumps(self.request, sort_keys=True) + "\n").encode()
        for name, contents in (("request.json", raw), ("instructions.md", self.skill),
                               ("secrets/openai-api-key", api_key),
                               ("secrets/browser-token", secrets.token_hex(32).encode())):
            path = self.root / name
            bootstrap._create_private(path, contents.decode())
            os.chown(path, 1000, 1000)
        return digest(raw)

    def wait_preview(self):
        # The trusted runner image supplies the readiness client, not the app image.
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "node", "checks", "-e",
                     "(async()=>{for(let n=0;n<30;n++){try{const r=await fetch('http://preview:3000/health',"
                     "{signal:AbortSignal.timeout(1000)});if(r.ok)return;}catch{}await new Promise(r=>setTimeout(r,1000));}"
                     "process.exit(1)})()", timeout=45)

    def assert_ownership(self):
        ids = command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project], env=self.env).splitlines()
        if ids:
            rows = json.loads(command(["docker", "inspect", *ids], env=self.env))
            if any(row["Config"]["Labels"].get(LABEL) != self.run_id or
                   row["Config"]["Labels"].get("io.driftward.factory.installation") != self.metadata["installation_id"] for row in rows):
                raise ValueError("verification project ownership differs; refusing cleanup")
        network_ids = command(["docker", "network", "ls", "-q", "--filter",
                               "label=com.docker.compose.project=" + self.project], env=self.env).splitlines()
        if network_ids:
            rows = json.loads(command(["docker", "network", "inspect", *network_ids], env=self.env))
            if any(row["Labels"].get(LABEL) != self.run_id or
                   row["Labels"].get("io.driftward.factory.installation") != self.metadata["installation_id"] for row in rows):
                raise ValueError("verification network ownership differs; refusing cleanup")

    def run(self, api_key: bytes) -> dict:
        if os.geteuid() != 0:
            raise ValueError("the pilot launcher requires the host operator (root) for private container-owned evidence")
        with lock(self.directory):
            # An interrupted prior run must be contained before another can start.
            active = command(["docker", "ps", "-aq", "--filter", "label=io.driftward.factory.installation=" + self.metadata["installation_id"],
                              "--filter", "label=" + LABEL], env=self.env)
            if active:
                raise ValueError("prior UI run still has containers; inspect and contain it before retrying")
            request_hash = self.prepare(api_key)
            report = {"schema": 1, "run_id": self.run_id, "installation_id": self.metadata["installation_id"],
                      "mode": "fixture" if self.fixture else "codex", "source_commit": self.request["source_commit"],
                      "request_sha256": request_hash, "preview_image": self.preview_image,
                      "runner_image": self.runner_image, "plugin": self.plugin, "started_at": int(time.time()),
                      "status": "blocked", "reason": "run_incomplete", "cleanup_complete": False,
                      "factory_verified": False, "phase": "preview_start"}
            bootstrap._create_private(self.root / "report.json", json.dumps(report) + "\n")
            previous = {}
            def cancelled(signum, frame):
                raise Cancelled("verification cancelled")
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, cancelled)
            try:
                self.compose("up", "-d", "--no-build", "--pull", "never", "preview")
                self.wait_preview()
                # Complete deterministic checks before the exploratory agent can change app state.
                report["phase"] = "browser_checks"
                self.compose("run", "--rm", "--no-deps", "checks", timeout=360)
                # Restart the disposable app to reset its tmpfs database for exploration.
                report["phase"] = "exploration_start"
                self.compose("rm", "--stop", "--force", "preview")
                self.compose("up", "-d", "--no-build", "--pull", "never", "preview", "browser")
                self.wait_preview()
                report["phase"] = "agent_observation"
                if self.fixture:
                    self.compose("run", "--rm", "--no-deps", "--entrypoint", "node", "codex-verifier",
                                 "/opt/verifier/probe.cjs", timeout=90)
                else:
                    self.compose("run", "--rm", "--no-deps", "codex-verifier", timeout=self.request["timeout_seconds"] + 30)
                checks = safe_json(self.root / "checks/checks.json", request_hash)
                agent = safe_json(self.root / "agent/agent.json", request_hash)
                report["status"] = outcome(checks, agent, fixture=self.fixture, steps=len(self.request["steps"]))
                report["reason"] = "evidence_collected"
                report["phase"] = "complete"
            except Cancelled:
                report["reason"] = "cancelled"
            except subprocess.TimeoutExpired:
                report["reason"] = "timeout"
            except (RuntimeError, ValueError, OSError):
                report["reason"] = "verification_failed"
            finally:
                # Repeated stop requests must not interrupt containment.
                for sig in previous:
                    signal.signal(sig, signal.SIG_IGN)
                try:
                    self.assert_ownership()
                    self.compose("down", "--remove-orphans", "--timeout", "10", timeout=45)
                    remaining = command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project], env=self.env)
                    networks = command(["docker", "network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + self.project], env=self.env)
                    if remaining or networks:
                        raise RuntimeError("verification containers or networks remain")
                    report["cleanup_complete"] = True
                except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                    report["status"], report["reason"] = "blocked", "containment_unconfirmed"
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                # Revoke our copies, not the operator's original credential file.
                for name in ("openai-api-key", "browser-token"):
                    (self.root / "secrets" / name).unlink(missing_ok=True)
                report["finished_at"] = int(time.time())
                report["artifacts"] = {}
                for section in ("checks", "agent"):
                    for path in (self.root / section).iterdir():
                        info = path.lstat()
                        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 32 * 1024 * 1024:
                            report["artifacts"][section + "/" + path.name] = digest(path.read_bytes())
                required = {"checks/checks.json", "agent/agent.json", "checks/viewport-1280.png",
                            "checks/viewport-390.png", "checks/trace-1280.zip", "checks/trace-390.zip"}
                if report["status"] in {"fixture_passed", "observed", "needs_attention"} and not required <= report["artifacts"].keys():
                    report["status"], report["reason"] = "blocked", "evidence_incomplete"
                pending = self.root / "report.pending.json"
                bootstrap._create_private(pending, json.dumps(report, sort_keys=True, indent=2) + "\n")
                os.replace(pending, self.root / "report.json")
            return report
