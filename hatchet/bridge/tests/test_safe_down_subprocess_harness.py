"""Non-root, capability-bound subprocess coverage for safe-down recovery.

This deliberately drives the checked-in wrapper and installer.  It is not a
mocked state machine: only systemd/network holder probes are fake, and only
after the inherited FD 8 capability has validated a private temporary root.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pwd
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parent
POINTS = (
    "archive", "transition", "installer-adoption", "prepared-journal",
    "publication-applied-directories", "publication-applied-routing",
    "publication-applied-units", "publication-applied-release", "publication-applied-current", "committed-journal",
)
SUPERSESSION_POINTS = (
    "supersession-archive", "supersession-journal-prepared", "supersession-link-temp",
    "supersession-active-link", "supersession-journal-committed",
)
DESCENDANT_ATTESTATION_POINTS = (
    "marker-descendant-attestation-write", "marker-descendant-attestation-fsynced",
    *SUPERSESSION_POINTS,
)
MARKER_LINEAGE_POINTS = (
    "marker-lineage-temp-create", "marker-lineage-temp-write", "marker-lineage-temp-fsynced",
    "marker-lineage-temp-fstat", "marker-lineage-prepared", "marker-lineage-rename",
    "marker-lineage-dir-fsynced", "marker-lineage-committed", "marker-lineage-archive",
)
SETUP_TIMEOUT_SECONDS = 30
WRAPPER_TIMEOUT_SECONDS = 30
DIRECT_TIMEOUT_SECONDS = 10


class HarnessTimeout(AssertionError):
    pass


def _bounded_run(command: list[str], *, timeout: int, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run each hermetic child in a killable session; never orphan a fixture."""
    process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Give production TERM/INT handlers one bounded chance to preserve the
        # safe-down barrier, then kill and reap the entire session.  A timeout
        # can never leave a fixture child holding an FD or running detached.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        # The shell may have exited cleanly while a descendant still belongs
        # to its session.  Fence any remaining member before returning the
        # timeout result; a just-exited orphan is then given a bounded chance
        # to be reaped by init before the fixture is removed.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise HarnessTimeout(f"timed out after {timeout}s: {' '.join(command)}\nstdout={stdout}\nstderr={stderr}") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _commit_if_staged(repo: Path) -> bool:
    """Commit the private fixture only when staging changed its exact HEAD."""
    changed = _bounded_run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], timeout=SETUP_TIMEOUT_SECONDS)
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        raise AssertionError(changed.stderr)
    committed = _bounded_run(["git", "-C", str(repo), "commit", "--quiet", "-m", "test: private recovery harness"], timeout=SETUP_TIMEOUT_SECONDS)
    if committed.returncode:
        raise AssertionError(committed.stderr)
    return True


def _identity() -> tuple[int, int]:
    if os.geteuid() != 0: return os.geteuid(), os.getegid()
    entry = pwd.getpwnam("nobody")
    return entry.pw_uid, entry.pw_gid


class SafeDownSubprocessFixture:
    # The matrix makes eleven independent private roots.  A committed template
    # built once per test class keeps those roots isolated without repeatedly
    # cloning and committing the worker tree.
    template_repo: Path | None = None

    def __init__(self, kill_at: str | None = None, signal_at: str | None = None, legacy_conflicts: int = 0):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.base = Path(self.temp.name)
        self.uid, self.gid = _identity()
        self.root = self.base / "private"; self.repo = self.root / "repository"; self.fake = self.root / "fake-bin"
        self.audit = self.root / "audit.jsonl"; self.capability = self.root / "test-capability.json"; self.kill_at = kill_at; self.signal_at = signal_at; self.legacy_conflicts = legacy_conflicts
        self.cap_fd: int | None = None
        self.successor_count = 0

    def _as_test_user(self) -> None:
        if os.geteuid() == 0:
            os.setgid(self.gid); os.setuid(self.uid)

    def _write(self, path: Path, text: str) -> None:
        path.write_text(text); path.chmod(0o755)

    def setup(self) -> None:
        # A private committed copy permits publish-release.sh to archive exactly
        # the current checked-in scripts without relying on a dirty checkout.
        self.root.mkdir(mode=0o700)
        if self.template_repo is None:
            clone = _bounded_run(["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(self.repo)], timeout=SETUP_TIMEOUT_SECONDS)
            if clone.returncode: raise AssertionError(clone.stderr)
            shutil.copytree(ROOT / "bridge", self.repo / "hatchet/bridge", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for command in (["git", "-C", str(self.repo), "config", "user.email", "bridge-test@invalid"], ["git", "-C", str(self.repo), "config", "user.name", "bridge test"], ["git", "-C", str(self.repo), "add", "hatchet/bridge"]):
                result = _bounded_run(command, timeout=SETUP_TIMEOUT_SECONDS)
                if result.returncode: raise AssertionError(result.stderr)
            _commit_if_staged(self.repo)
        else:
            shutil.copytree(self.template_repo, self.repo, copy_function=shutil.copy2)
        version = _bounded_run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], timeout=SETUP_TIMEOUT_SECONDS)
        if version.returncode: raise AssertionError(version.stderr)
        self._release_version = version.stdout.strip()
        for path in (self.root / "var/lib/driftward-hatchet-bridge", self.root / "root/driftward-backups/hatchet", self.root / "etc/systemd/system", self.root / "run/systemd/system", self.root / "run"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fake.mkdir(mode=0o700); self.audit.touch(); self.audit.chmod(0o600)
        self.prebuilt = self.root / "prebuilt/push-accepted-event.js"; self.prebuilt.parent.mkdir(mode=0o700)
        source_trigger = ROOT / "workers/bridge-executor-worker/dist/triggers/push-accepted-event.js"
        if not source_trigger.is_file(): raise AssertionError(f"required worker build artifact is absent: {source_trigger}")
        shutil.copy2(source_trigger, self.prebuilt); self.prebuilt.chmod(0o644)
        self._fake_commands()
        self._incident_fixture()
        if os.geteuid() == 0:
            # The checkout is immutable after its private test commit.  Walking
            # and chowning every Git object for every kill point dominates the
            # matrix without adding a permission proof; the capability checks
            # only require the repository root to be private/user-owned.
            owned = [self.base, self.root, self.repo, self.fake, self.audit]
            for directory, dirs, files in os.walk(self.root):
                current = Path(directory)
                if current == self.root:
                    dirs[:] = [name for name in dirs if current / name != self.repo]
                if current != self.repo:
                    owned.extend(current / name for name in (*dirs, *files))
            # The release/staging paths are checked-in working-tree files and
            # some are intentionally mode 0600/0700.  Grant the fixture user
            # access to that tree, but never traverse the expensive immutable
            # Git object database.
            for directory, dirs, files in os.walk(self.repo):
                current = Path(directory)
                dirs[:] = [name for name in dirs if name != ".git"]
                owned.extend(current / name for name in (*dirs, *files))
            for path in owned:
                try:
                    current = path.stat()
                    if current.st_uid != self.uid or current.st_gid != self.gid: os.chown(path, self.uid, self.gid)
                except FileNotFoundError: pass
            self.base.chmod(0o755); self.root.chmod(0o700)
            for path in (self.repo / "hatchet", self.repo / "hatchet/bridge"):
                path.chmod(0o755)
        self._write_capability()

    def _fake_commands(self) -> None:
        log = self.root / "fake-system.log"; started = self.root / "fake-bridge-started"; mutated = self.root / "fake-system-mutated"; dropins = self.root / "run/systemd/system"
        self._write(self.fake / "systemctl", f'''#!/bin/sh
echo "$*" >> {log!s}
[ "$1" = daemon-reload ] || [ "$1" = stop ] || [ "$1" = start ] || [ "$1" = mask ] || [ "$1" = unmask ] || [ "$1" = reset-failed ] && : > {mutated!s}
[ "$1" = start ] && [ "${{2:-}}" = driftward-hatchet-bridge.service ] && : > {started!s}
case "$1" in
  show) case "$*" in *LoadState*) echo loaded;; *ActiveState*) echo inactive;; *UnitFileState*) echo disabled;; *DropInPaths*) find {dropins!s} -name 90-driftward-bridge-maintenance.conf -type f -print | tr '\\n' ' ';; esac;;
  is-active) exit 1;;
esac
exit 0
''')
        self._write(self.fake / "systemd-analyze", "#!/bin/sh\nexit 1\n")
        self._write(self.fake / "lsof", "#!/bin/sh\nexit 1\n")
        self._write(self.fake / "factory", "#!/bin/sh\n[ \"$1\" = mode ] && echo dry-run\n")
        self._write(self.fake / "curl", f'''#!/bin/sh
case " $* " in *"/health"*)
  [ -f {started!s} ] && printf '%s\\n' '{{"ok":true,"schema_version":5,"release_commit":"{self.release_version}"}}' && exit 0
  ;;
esac
exit 22
''')
        self._write(self.fake / "chown", "#!/bin/sh\nexit 0\n")
        self._write(self.fake / "install", "#!/usr/bin/env python3\nimport os,sys\na=[]; i=0\nwhile i < len(sys.argv)-1:\n v=sys.argv[1+i]\n if v in ('-o','-g','--owner','--group'): i+=2; continue\n a.append(v); i+=1\nos.execv('/usr/bin/install',['install',*a])\n")
        # The parent snapshot also invokes this fake.  Create the log before
        # fixture ownership is handed to the unprivileged recovery process so
        # its strict stderr proof cannot be contaminated by an append error.
        log.touch(mode=0o600); log.chmod(0o600)

    def _incident_fixture(self) -> None:
        env = self.root / "etc/driftward"
        env.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, lines in {
            "hatchet.env": "HATCHET_POSTGRES_PASSWORD=fixture-password-123\nHATCHET_SERVER_AUTH_COOKIE_SECRETS=fixture-cookie-secret-123\nHATCHET_ADMIN_PASSWORD=fixture-admin-password-123\n",
            "hatchet-bridge.env": "HATCHET_HERMES_BRIDGE_TOKEN=fixture-bridge-token-123\nHATCHET_HERMES_INSPECTION_TOKEN=fixture-inspection-token-123\nHATCHET_HERMES_RECOVERY_TOKEN=fixture-recovery-token-123\n",
            "hatchet-workers.env": "HATCHET_CLIENT_TOKEN=fixture-client-token-123\nHATCHET_HERMES_BRIDGE_TOKEN=fixture-workers-bridge-token-123\n",
            "hatchet-worker-bridge-executor.env": "HATCHET_HERMES_INSPECTION_TOKEN=fixture-executor-token-123\n",
            "hatchet-verifier.env": "HATCHET_HERMES_VERIFIER_TOKEN=fixture-verifier-token-123\n",
        }.items():
            path = env / name; path.write_text(lines); path.chmod(0o600)
            if os.geteuid() == 0: os.chown(path, self.uid, self.gid)
        bridge = self.root / "var/lib/driftward-hatchet-bridge/bridge.db"
        import sys
        sys.path.insert(0, str(ROOT / "bridge"))
        from hatchet_hermes_bridge.store import init_db
        con = sqlite3.connect(bridge)
        init_db(con)
        if self.legacy_conflicts:
            self._seed_schema_zero_legacy_conflicts(con, self.legacy_conflicts)
        # This is deliberately a schema-0 image, not a copy of the live
        # ledger: it retains the production-shaped immutable evidence but no
        # schema marker or resolution table for the installer to rely on.
        con.execute("DROP TABLE factory_import_conflict_resolutions")
        con.execute("DROP TABLE bridge_schema_meta")
        con.execute("PRAGMA user_version=0")
        con.commit()
        self.legacy_immutable_hashes = self._legacy_immutable_hashes(con) if self.legacy_conflicts else {}
        con.close()
        # Match the root-owned production bridge ledger before descriptor-safe
        # recovery opens it with O_NOFOLLOW.
        bridge.chmod(0o600)
        self.backup = self.root / f"root/driftward-backups/hatchet/bridge-20260817T061607Z-before-{self.release_version}.sqlite"
        shutil.copy2(bridge, self.backup); self.backup.chmod(0o600)
        self.checksum = hashlib.sha256(self.backup.read_bytes()).hexdigest()
        # Match production ``sha256sum "$backup"`` output: the canonical
        # absolute backup pathname, not merely its basename.
        self.sidecar = self.backup.with_suffix(".sqlite.sha256"); self.sidecar.write_text(f"{self.checksum}  {self.backup}\n"); self.sidecar.chmod(0o600)
        self.marker = self.root / "var/lib/driftward-hatchet-bridge/recovery-blocked"
        self.marker.write_text("rollback recovery blocked: stop-writers restore-services\n"); self.marker.chmod(0o600)
        self.marker_sha = hashlib.sha256(self.marker.read_bytes()).hexdigest(); self.marker_mtime = str(int(self.marker.stat().st_mtime))

    @staticmethod
    def _legacy_immutable_hashes(con: sqlite3.Connection) -> dict[str, str]:
        tables = (
            "bridge_jobs", "bridge_events", "factory_attempts",
            "factory_attempt_events", "factory_import_conflicts",
        )
        return {
            table: hashlib.sha256(json.dumps(con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(), separators=(",", ":"), default=str).encode()).hexdigest()
            for table in tables
        }

    @staticmethod
    def _seed_schema_zero_legacy_conflicts(con: sqlite3.Connection, count: int) -> None:
        """Seed only immutable legacy evidence that migration must preserve."""
        from hatchet_hermes_bridge.legacy_artifacts import deterministic_branch

        if count != 38:
            raise AssertionError("production fixture requires exactly 38 legacy conflicts")
        # Canonical pre-existing timestamps ensure this fixture proves only
        # allowed resolution-marker appends, not an unrelated time backfill.
        now = "2026-08-17T00:00:00.000000Z"
        for index in range(count):
            work_item = "ENG-8" if index == 0 else f"ENG-{100 + index}"
            job_id = f"legacy-eng{index:02d}"
            key = f"hatchet:work-item:linear:{work_item}:create_draft_pr:real:active"
            branch = deterministic_branch(key)
            pr_url = f"https://github.com/Driftward-LLC/capykit/pull/{20 + index}"
            payload = json.dumps({"linear_issue": work_item, "repo": "Driftward-LLC/capykit", "dry_run": False}, sort_keys=True)
            con.execute(
                "INSERT INTO bridge_jobs (job_id,idempotency_key,state,requested_action,payload_json,payload_hash,branch,pr_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, key, "completed", "create_draft_pr", payload, f"fixture-{index:02d}", branch, pr_url, now, now),
            )
            con.execute(
                "INSERT INTO bridge_events (event_id,job_id,event_type,state,details_json,created_at) VALUES (?,?,?,?,?,?)",
                (f"legacy-effect-{index:02d}", job_id, "transition:completed", "completed", json.dumps({"effect": {"pr_url": pr_url, "head_branch": branch}}, sort_keys=True), now),
            )
            attempt_id = f"legacy-{job_id}"
            con.execute(
                "INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,job_id,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, work_item, "create_draft_pr", "real", 1, f"legacy:{job_id}", key, branch, "legacy_completed", job_id, "legacy-import", now, now),
            )
            con.execute(
                "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
                (f"legacy-imported-{index:02d}", attempt_id, "legacy_imported", json.dumps({"job_id": job_id}, sort_keys=True), now),
            )
            con.execute(
                "INSERT INTO factory_import_conflicts (conflict_key,work_item_key,job_id,reason,created_at) VALUES (?,?,?,?,?)",
                (f"{job_id}:legacy_artifact_reference_conflict", work_item, job_id, "legacy_artifact_reference_conflict", now),
            )

    @property
    def release_version(self) -> str:
        return self._release_version

    def _write_capability(self) -> None:
        value = {"mode": "driftward-hatchet-bridge-hermetic-test-v1", "root": str(self.root), "repository": str(self.repo), "fake_bin": str(self.fake), "audit": str(self.audit), "capability_path": str(self.capability), "prebuilt_trigger": str(self.prebuilt), "uid": self.uid, "gid": self.gid, "release_version": self.release_version, "nonce": "a" * 64}
        if self.kill_at: value["kill_at"] = self.kill_at
        if self.signal_at: value["signal_at"] = self.signal_at
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        value["content_sha256"] = hashlib.sha256(canonical).hexdigest()
        self.capability.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"); self.capability.chmod(0o600)
        if os.geteuid() == 0: os.chown(self.capability, self.uid, self.gid)

    def force_staged_python_bytecode(self) -> Path:
        """Remove only the staged CLI import protections in this private fixture."""
        marker = self.root / "staged-cli-import-after-manifest"
        self._write(self.fake / "python3", f'''#!/bin/sh
if [ "${{1:-}}" = -B ] && [ "${{2:-}}" = -m ] && [ "${{3:-}}" = hatchet_hermes_bridge.cli ]; then
  [ -f "$PYTHONPATH/MANIFEST.json" ] && [ -f "$PYTHONPATH/RELEASE_METADATA.json" ] || exit 69
  unset PYTHONDONTWRITEBYTECODE
  shift
  /usr/bin/python3 "$@"
  status=$?
  find "$PYTHONPATH" -type f -name '*.pyc' -print > {marker!s}
  exit "$status"
fi
exec /usr/bin/python3 "$@"
''')
        return marker

    @contextlib.contextmanager
    def inherited_capability(self):
        self.cap_fd = os.open(self.capability, os.O_RDONLY)
        created_fd8 = False
        try:
            os.fstat(8)
        except OSError:
            os.dup2(self.cap_fd, 8); created_fd8 = True
        try: yield self.cap_fd
        finally:
            if created_fd8: os.close(8)
            os.close(self.cap_fd); self.cap_fd = None

    def _with_capability(self, cap_fd: int):
        os.dup2(cap_fd, 8, inheritable=True)
        self._as_test_user()

    def capability_env(self, cap_fd: int) -> dict[str, str]:
        return {
            **os.environ, "HOME": str(self.root),
            "BRIDGE_TEST_CAPABILITY_FD": "8",
            "BRIDGE_TEST_CAPABILITY_INODE": str(os.fstat(cap_fd).st_ino),
            "BRIDGE_TEST_CAPABILITY_NONCE": "a" * 64,
        }

    def run_wrapper(self) -> subprocess.CompletedProcess[str]:
        command = ["bash", str(self.repo / "hatchet/bridge/recover-safe-down.sh"), "--yes", "--backup", str(self.backup), "--checksum", self.checksum, "--marker-sha256", self.marker_sha, "--marker-mtime", self.marker_mtime]
        with self.inherited_capability() as cap_fd:
            return _bounded_run(command, timeout=WRAPPER_TIMEOUT_SECONDS, cwd=self.repo, env=self.capability_env(cap_fd), pass_fds=(cap_fd, 8), preexec_fn=lambda: self._with_capability(cap_fd))

    def run_prepare(self) -> subprocess.CompletedProcess[str]:
        command = ["python3", str(self.repo / "hatchet/bridge/maintenance_lock_exec.py"), "exec", "python3", str(self.repo / "hatchet/bridge/safe_down_recovery.py"), "prepare", "--backup", str(self.backup), "--checksum", self.checksum, "--marker-sha256", self.marker_sha, "--marker-mtime", self.marker_mtime]
        with self.inherited_capability() as cap_fd:
            return _bounded_run(command, timeout=DIRECT_TIMEOUT_SECONDS, cwd=self.repo, env=self.capability_env(cap_fd), pass_fds=(cap_fd, 8), preexec_fn=lambda: self._with_capability(cap_fd))

    def run_marker_lineage(self, reason: str = "safe-down fresh-install-failed") -> subprocess.CompletedProcess[str]:
        self._install_existing_barrier()
        command = ["python3", str(self.repo / "hatchet/bridge/barrier_evidence_exec.py"), "exec", "python3", str(self.repo / "hatchet/bridge/maintenance_lock_exec.py"), "exec", "python3", str(self.repo / "hatchet/bridge/safe_down_recovery.py"), "update-marker-lineage", "--reason", reason]
        with self.inherited_capability() as cap_fd:
            return _bounded_run(command, timeout=DIRECT_TIMEOUT_SECONDS, cwd=self.repo, env={**self.capability_env(cap_fd), "PATH": f"{self.fake}:{os.environ['PATH']}"}, pass_fds=(cap_fd, 8), preexec_fn=lambda: self._with_capability(cap_fd))

    def run_marker_lineage_lock_only(self) -> subprocess.CompletedProcess[str]:
        command = ["python3", str(self.repo / "hatchet/bridge/maintenance_lock_exec.py"), "exec", "python3", str(self.repo / "hatchet/bridge/safe_down_recovery.py"), "update-marker-lineage", "--reason", "safe-down fresh-install-failed"]
        with self.inherited_capability() as cap_fd:
            return _bounded_run(command, timeout=DIRECT_TIMEOUT_SECONDS, cwd=self.repo, env={**self.capability_env(cap_fd), "PATH": f"{self.fake}:{os.environ['PATH']}"}, pass_fds=(cap_fd, 8), preexec_fn=lambda: self._with_capability(cap_fd))

    def commit_successor(self) -> str:
        path = self.repo / "hatchet/bridge/.supersession-release-proof"
        self.successor_count += 1
        path.write_text(f"reviewed successor {self.successor_count}\n")
        for command in (("git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), "add", str(path.relative_to(self.repo))), ("git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), "commit", "--quiet", "-m", "test: reviewed safe-down successor")):
            result = _bounded_run(list(command), timeout=SETUP_TIMEOUT_SECONDS)
            if result.returncode: raise AssertionError(result.stderr)
        successor = _bounded_run(["git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), "rev-parse", "HEAD"], timeout=SETUP_TIMEOUT_SECONDS).stdout.strip()
        self._release_version = successor; self._fake_commands(); self._write_capability()
        return successor

    def rewrite_marker_in_place(self, reason: str = "safe-down fresh-install-failed") -> dict[str, object]:
        before = self.marker.stat()
        body = f"rollback recovery blocked: {reason}\n".encode()
        fd = os.open(self.marker, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            os.write(fd, body); os.fsync(fd)
        finally:
            os.close(fd)
        os.utime(self.marker, ns=(before.st_atime_ns + 2_000_000_000, before.st_mtime_ns + 2_000_000_000))
        after = self.marker.stat()
        if after.st_ino != before.st_ino: raise AssertionError("fixture rewrite must retain inode")
        self.marker_sha = hashlib.sha256(body).hexdigest(); self.marker_mtime = str(after.st_mtime_ns // 1_000_000_000)
        return {"sha256": self.marker_sha, "dev": after.st_dev, "ino": after.st_ino, "mtime_ns": after.st_mtime_ns}

    def run_supersede(self, parent: dict, successor: str, *, existing_barrier: bool = True, descendant: bool = False) -> subprocess.CompletedProcess[str]:
        if existing_barrier:
            self._install_existing_barrier()
        parent_body = (json.dumps(parent, sort_keys=True, separators=(",", ":")) + "\n").encode()
        command = ["bash", str(self.repo / "hatchet/bridge/recover-safe-down.sh"), "--yes", "--supersede", "--parent-transition-sha256", hashlib.sha256(parent_body).hexdigest(), "--parent-generation", str(parent["version"]), "--parent-code-commit", parent["code_commit"], "--backup", str(self.backup), "--checksum", self.checksum, "--marker-sha256", self.marker_sha, "--marker-mtime", self.marker_mtime, "--successor-commit", successor, "--successor-pr", "170", "--approved-pr-head", "b" * 40]
        if descendant:
            current = self.marker.stat(); old = parent["marker"]
            command += ["--attest-descendant-marker", "--old-marker-sha256", old["sha256"], "--old-marker-dev", str(old["dev"]), "--old-marker-ino", str(old["ino"]), "--old-marker-mtime-ns", str(old["mtime_ns"]), "--marker-dev", str(current.st_dev), "--marker-ino", str(current.st_ino), "--marker-mtime-ns", str(current.st_mtime_ns), "--terminal-marker-reason", "safe-down fresh-install-failed"]
        with self.inherited_capability() as cap_fd:
            return _bounded_run(command, timeout=WRAPPER_TIMEOUT_SECONDS, cwd=self.repo, env=self.capability_env(cap_fd), pass_fds=(cap_fd, 8), preexec_fn=lambda: self._with_capability(cap_fd))

    def _install_existing_barrier(self) -> None:
        """Fixture setup only: model a pre-existing host safe-down barrier."""
        units = subprocess.check_output(["python3", str(self.repo / "hatchet/bridge/writer-inventory.py"), "--kind", "all"], text=True, env={**os.environ, "PYTHONPATH": str(self.repo / "hatchet/bridge")}).splitlines()
        root = self.root / "run/driftward-hatchet-bridge"; active = root / "maintenance.active"
        active.write_text(""); active.chmod(0o600)
        created = [active]
        for unit in units:
            path = self.root / "run/systemd/system" / f"{unit}.d" / "90-driftward-bridge-maintenance.conf"
            path.parent.mkdir(parents=True, exist_ok=True); path.parent.chmod(0o755)
            path.write_text(f"[Unit]\nConditionPathExists=!{active}\n"); path.chmod(0o644)
            created.extend((path.parent, path))
        if os.geteuid() == 0:
            for path in created:
                os.chown(path, self.uid, self.gid)
        for path in created:
            if path == active:
                assert path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
            elif path.is_dir():
                assert stat.S_IMODE(path.stat().st_mode) == 0o755
            else:
                assert path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o644

    def remove_active_transition(self) -> None:
        """Fixture setup only: exercise the no-active-transition lineage path."""
        transition = self.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json"
        transition.unlink()

    def unbound_lineage_roots(self) -> list[Path]:
        archive = self.root / "var/lib/driftward-hatchet-bridge/recovery-incidents"
        return sorted(archive.glob("unbound-marker-lineage-*"))

    def standalone_snapshot(self) -> dict[str, object]:
        paths = [self.root / "run/driftward-hatchet-bridge/maintenance.active", self.root / "run/driftward-hatchet-bridge/maintenance.lock", *self.root.glob("run/systemd/system/**/*.conf")]
        units = subprocess.check_output(["python3", str(self.repo / "hatchet/bridge/writer-inventory.py"), "--kind", "all"], text=True, env={**os.environ, "PYTHONPATH": str(self.repo / "hatchet/bridge")}).splitlines()
        states = {}
        for unit in (*units, "driftward-hatchet-bridge-reconciler.service", "driftward-hatchet-bridge-reconciler.timer"):
            states[unit] = tuple(
                subprocess.check_output([str(self.fake / "systemctl"), "show", unit, "-p", prop, "--value"], text=True).strip()
                for prop in ("ActiveState", "UnitFileState", "DropInPaths")
            )
        masks = {
            str(path.relative_to(self.root)): os.readlink(path)
            for path in (self.root / "run/systemd/system").iterdir()
            if path.is_symlink()
        }
        return {
            "files": {str(path.relative_to(self.root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths if path.exists()},
            "masks": masks, "unit_states": states,
            "mutated": (self.root / "fake-system-mutated").exists(),
            "marker": self.marker.read_bytes(), "db": (self.root / "var/lib/driftward-hatchet-bridge/bridge.db").read_bytes(),
        }

    def audit_rows(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def close(self) -> None: self.temp.cleanup()


class SafeDownSubprocessHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._template = tempfile.TemporaryDirectory(dir="/var/tmp")
        template = Path(cls._template.name) / "repository"
        clone = _bounded_run(["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(template)], timeout=SETUP_TIMEOUT_SECONDS)
        if clone.returncode: raise AssertionError(clone.stderr)
        shutil.copytree(ROOT / "bridge", template / "hatchet/bridge", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for command in (["git", "-C", str(template), "config", "user.email", "bridge-test@invalid"], ["git", "-C", str(template), "config", "user.name", "bridge test"], ["git", "-C", str(template), "add", "hatchet/bridge"]):
            result = _bounded_run(command, timeout=SETUP_TIMEOUT_SECONDS)
            if result.returncode: raise AssertionError(result.stderr)
        _commit_if_staged(template)
        SafeDownSubprocessFixture.template_repo = template

    @classmethod
    def tearDownClass(cls):
        SafeDownSubprocessFixture.template_repo = None
        cls._template.cleanup()

    def setUp(self):
        self.fixture: SafeDownSubprocessFixture | None = None
        self.started = time.monotonic()
    def tearDown(self):
        if self.fixture: self.fixture.close()
        print(f"HARNESS_TIMING {self.id()} {time.monotonic() - self.started:.3f}s", flush=True)

    def make(self, kill_at: str | None = None, signal_at: str | None = None, legacy_conflicts: int = 0) -> SafeDownSubprocessFixture:
        self.fixture = SafeDownSubprocessFixture(kill_at, signal_at, legacy_conflicts); self.fixture.setup(); return self.fixture

    def test_nonroot_actual_wrapper_exec_chain_retains_fd3_through_fd7(self):
        fixture = self.make()
        result = fixture.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + fixture.audit.read_text())
        rows = fixture.audit_rows(); events = [row["event"] for row in rows]
        for event in ("maintenance-lock-exec", "recover-wrapper", "safe-down-archive", "safe-down-transition", "safe-down-prepare", "installer-adoption", "prepared-journal", "committed-journal"):
            self.assertIn(event, events)
        prepared = next(row for row in rows if row["event"] == "safe-down-prepare")["fds"]
        adopted = next(row for row in rows if row["event"] == "installer-adoption")["fds"]
        for fd in ("3", "4", "5", "6", "7"):
            self.assertEqual(prepared[fd], adopted[fd], fd)
        self.assertFalse(fixture.marker.exists()); self.assertFalse((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").exists())
        self.assertTrue((fixture.root / f"docker/driftward/hatchet/releases/bridge/{fixture.release_version}").is_dir())

    def test_actual_installer_keeps_the_staged_manifest_exact_with_bytecode_disabled(self):
        fixture = self.make()
        result = fixture.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + fixture.audit.read_text())
        release_root = fixture.root / "docker/driftward/hatchet/releases/bridge"
        release = release_root / fixture.release_version
        self.assertTrue((release_root / "current").is_symlink())
        self.assertEqual((release_root / "current").resolve(), release)
        self.assertFalse(any(path.suffix == ".pyc" or "__pycache__" in path.parts for path in release.rglob("*")))
        verified = _bounded_run(
            ["python3", str(ROOT / "bridge/release_hygiene.py"), "verify", "--tree", str(release), "--commit", fixture.release_version, "--source-tree", fixture.release_version],
            timeout=DIRECT_TIMEOUT_SECONDS,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_actual_installer_blocks_publication_when_staged_import_writes_bytecode(self):
        fixture = self.make()
        import_marker = fixture.force_staged_python_bytecode()
        result = fixture.run_wrapper()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr + fixture.audit.read_text())
        self.assertTrue(import_marker.is_file())
        self.assertRegex(import_marker.read_text(), r"/release/(?:hatchet_hermes_bridge/)?__pycache__/.*\.pyc\n")
        self.assertIn("release-tree:denied-component:", result.stderr)
        release_root = fixture.root / "docker/driftward/hatchet/releases/bridge"
        self.assertFalse((release_root / "current").exists() or (release_root / "current").is_symlink())
        self.assertFalse((release_root / fixture.release_version).exists())

    def test_actual_wrapper_adopts_synthetic_descendant_v2_without_reconstruction(self):
        """The normal wrapper must pass the reviewed v2 link, not rebuild v1."""
        fixture = self.make(legacy_conflicts=38)
        self.assertEqual(fixture.run_prepare().returncode, 0)
        transition = fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json"
        v1 = json.loads(transition.read_text())
        fixture.rewrite_marker_in_place()
        successor = fixture.commit_successor()
        self.assertEqual(fixture.run_supersede(v1, successor, descendant=True).returncode, 0)
        v2_body = transition.read_bytes(); v2 = json.loads(v2_body); v2_info = transition.stat()
        result = fixture.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + fixture.audit.read_text())
        self.assertIn("safe-down-prepare-existing", [row["event"] for row in fixture.audit_rows()])
        incident = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents" / v2["incident"]
        self.assertEqual((incident / v2["parent"]["archive"]).read_bytes(), (json.dumps(v1, sort_keys=True, separators=(",", ":")) + "\n").encode())
        archived_v2 = incident / f"transition-v2-{v2['parent']['sha256']}.json"
        self.assertEqual(archived_v2.read_bytes(), v2_body)
        self.assertEqual((archived_v2.stat().st_dev, archived_v2.stat().st_ino), (v2_info.st_dev, v2_info.st_ino))
        self.assertTrue((incident / "completion.json").is_file())
        self.assertFalse(transition.exists()); self.assertFalse(fixture.marker.exists())
        self.assertTrue((fixture.root / f"docker/driftward/hatchet/releases/bridge/{successor}").is_dir())

    def test_v2_publication_journal_never_uses_v1_rollback_exception(self):
        fixture = self.make()
        self.assertEqual(fixture.run_prepare().returncode, 0)
        transition = fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json"
        v1 = json.loads(transition.read_text()); successor = fixture.commit_successor()
        self.assertEqual(fixture.run_supersede(v1, successor).returncode, 0)
        v2_body = transition.read_bytes()
        journal = fixture.root / f"var/lib/driftward-hatchet-bridge/publication-{successor}.journal"
        journal.write_text(json.dumps({"status": "prepared", "transaction": successor, "commit": successor, "snapshot": {}}, sort_keys=True, separators=(",", ":")) + "\n"); journal.chmod(0o600)
        result = fixture.run_wrapper()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(transition.read_bytes(), v2_body)
        self.assertTrue(fixture.marker.exists())
        self.assertNotIn("start driftward-hatchet-bridge.service", (fixture.root / "fake-system.log").read_text())

    def test_sigkill_between_existing_transition_decision_and_exec_replays_without_downgrade(self):
        fixture = self.make("prepare-existing")
        self.assertEqual(fixture.run_prepare().returncode, 0)
        transition = fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json"
        v1 = json.loads(transition.read_text()); successor = fixture.commit_successor()
        self.assertEqual(fixture.run_supersede(v1, successor).returncode, 0)
        v2_body = transition.read_bytes()
        killed = fixture.run_wrapper()
        self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
        self.assertEqual(transition.read_bytes(), v2_body)
        fixture.kill_at = None; fixture._write_capability()
        rerun = fixture.run_wrapper()
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertFalse(transition.exists()); self.assertFalse(fixture.marker.exists())

    def test_actual_installer_keeps_one_release_version_across_conflict_proofs(self):
        """Both post-migration proof gates must classify migration's markers."""
        fixture = self.make(legacy_conflicts=38)
        seed = sqlite3.connect(fixture.root / "var/lib/driftward-hatchet-bridge/bridge.db")
        tables = {row[0] for row in seed.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(seed.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertNotIn("bridge_schema_meta", tables)
        self.assertNotIn("factory_import_conflict_resolutions", tables)
        self.assertEqual(seed.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0], 38)
        self.assertEqual(seed.execute("SELECT COUNT(*) FROM factory_attempts WHERE generation=1 AND status='legacy_completed'").fetchone()[0], 38)
        seed.close()
        predeploy = _bounded_run(
            ["python3", str(fixture.repo / "hatchet/bridge/predeploy-check.py"), "--db", str(fixture.root / "var/lib/driftward-hatchet-bridge/bridge.db"), "--routing-registry", str(fixture.repo / "hatchet/workers/routing-registry.json"), "--require-zero-legacy-ready", "--require-zero-import-conflicts", "--validate-legacy-import"],
            timeout=DIRECT_TIMEOUT_SECONDS,
            env={**os.environ, "PYTHONPATH": str(fixture.repo / "hatchet/bridge")},
        )
        self.assertEqual(predeploy.returncode, 0, predeploy.stdout + predeploy.stderr)
        self.assertIn("legacy import conflicts: original=38 resolvable=38 unresolved=0", predeploy.stdout)
        result = fixture.run_wrapper()
        # This passes only when both disposable and post-publication proofs use
        # the exact release version used to append migration markers.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + fixture.audit.read_text())
        db = fixture.root / "var/lib/driftward-hatchet-bridge/bridge.db"
        postdeploy = _bounded_run(
            ["python3", str(fixture.root / f"docker/driftward/hatchet/releases/bridge/{fixture.release_version}/predeploy-check.py"), "--db", str(db), "--routing-registry", str(fixture.root / "etc/driftward/hatchet-routing-registry.json"), "--require-zero-legacy-ready", "--require-zero-import-conflicts", "--validate-legacy-import"],
            timeout=DIRECT_TIMEOUT_SECONDS,
            env={**os.environ, "DRIFTWARD_BRIDGE_RELEASE_VERSION": fixture.release_version, "HATCHET_INTAKE_ROUTING_REGISTRY_PATH": str(fixture.root / "etc/driftward/hatchet-routing-registry.json"), "PYTHONPATH": str(fixture.root / f"docker/driftward/hatchet/releases/bridge/{fixture.release_version}")},
        )
        self.assertEqual(postdeploy.returncode, 0, postdeploy.stdout + postdeploy.stderr)
        self.assertIn("legacy import conflicts: original=38 resolvable=38 unresolved=0", postdeploy.stdout)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT schema_version FROM bridge_schema_meta WHERE singleton=1").fetchone()[0], 5)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_import_conflicts").fetchone()[0], 38)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_import_conflict_resolutions").fetchone()[0], 38)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_import_conflict_resolutions WHERE operator='migration'",).fetchone()[0], 38)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_import_conflict_attestations WHERE producing_release_sha=? AND operator='migration'", (fixture.release_version,)).fetchone()[0], 38)
        self.assertEqual(fixture._legacy_immutable_hashes(con), fixture.legacy_immutable_hashes)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_attempts WHERE generation != 1 OR status != 'legacy_completed'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM factory_attempts WHERE generation=2").fetchone()[0], 0)
        con.close()

    def test_actual_sigkill_at_every_safe_down_boundary_reruns_to_full_new_state(self):
        for point in POINTS:
            with self.subTest(point=point):
                fixture = self.make(point)
                killed = fixture.run_wrapper()
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                self.assertIn("sigkill", [row["event"] for row in fixture.audit_rows()])
                self.assertNotIn("start driftward-hatchet-bridge.service", (fixture.root / "fake-system.log").read_text())
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_wrapper()
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                self.assertFalse(fixture.marker.exists())
                self.assertTrue((fixture.root / f"docker/driftward/hatchet/releases/bridge/{fixture.release_version}").is_dir())
                fixture.close(); self.fixture = None

    def test_standalone_supersession_fails_closed_without_an_existing_barrier_and_never_mutates_system_state(self):
        fixture = self.make()
        self.assertEqual(fixture.run_prepare().returncode, 0)
        v1 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
        successor = fixture.commit_successor()
        before = fixture.standalone_snapshot()
        result = fixture.run_supersede(v1, successor, existing_barrier=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fixture.standalone_snapshot(), before)
        self.assertFalse((fixture.root / "fake-system-mutated").exists())

    def test_archive_replay_is_append_only_and_signal_exit_keeps_safe_down_marker(self):
        fixture = self.make("transition")
        self.assertEqual(fixture.run_wrapper().returncode, -9)
        archive = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents"
        first = [path.name for path in archive.iterdir()]
        fixture.kill_at = None; fixture._write_capability()
        self.assertEqual(fixture.run_wrapper().returncode, 0)
        self.assertEqual(first, [path.name for path in archive.iterdir()])

    def test_actual_sigkill_at_every_supersession_boundary_replays_without_leaving_safe_down(self):
        """Every v1->v2 boundary replays without mutable-host effects."""
        for point in SUPERSESSION_POINTS:
            with self.subTest(point=point):
                fixture = self.make(point)
                prepared = fixture.run_prepare()
                self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
                active = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                successor = fixture.commit_successor()
                fixture._install_existing_barrier(); before = fixture.standalone_snapshot()
                killed = fixture.run_supersede(active, successor)
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                self.assertTrue(fixture.marker.exists())
                self.assertEqual(fixture.standalone_snapshot(), before)
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_supersede(active, successor)
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                successor_record = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                self.assertEqual(successor_record["version"], 2)
                self.assertEqual(successor_record["code_commit"], successor)
                self.assertTrue(fixture.marker.exists())
                self.assertEqual(fixture.standalone_snapshot(), before)
                v2 = successor_record; successor_v3 = fixture.commit_successor()
                fixture._install_existing_barrier(); v3_before = fixture.standalone_snapshot()
                rerun_v3 = fixture.run_supersede(v2, successor_v3)
                self.assertEqual(rerun_v3.returncode, 0, rerun_v3.stdout + rerun_v3.stderr)
                v3 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                self.assertEqual(v3["version"], 3); self.assertEqual(v3["parent"]["sha256"], hashlib.sha256((json.dumps(v2, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest())
                incident = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents" / v3["incident"]
                self.assertTrue((incident / "transition.json").is_file()); self.assertTrue((incident / v3["parent"]["archive"]).is_file())
                self.assertEqual(fixture.standalone_snapshot(), v3_before)
                fixture.close(); self.fixture = None

    def test_descendant_attestation_sigkill_matrix_replays_without_host_mutation(self):
        """The exceptional v1 marker proof is durable before v2 linkage."""
        for point in DESCENDANT_ATTESTATION_POINTS:
            with self.subTest(point=point):
                fixture = self.make(point)
                self.assertEqual(fixture.run_prepare().returncode, 0)
                v1 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                fixture.rewrite_marker_in_place()
                successor = fixture.commit_successor()
                fixture._install_existing_barrier(); before = fixture.standalone_snapshot()
                killed = fixture.run_supersede(v1, successor, descendant=True)
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                self.assertEqual(fixture.standalone_snapshot(), before)
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_supersede(v1, successor, descendant=True)
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                v2 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                self.assertEqual(v2["version"], 2); self.assertIn("marker_descendant", v2)
                incident = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents" / v2["incident"]
                self.assertTrue((incident / v2["marker_descendant"]["path"]).is_file())
                self.assertEqual(fixture.standalone_snapshot(), before)
                fixture.close(); self.fixture = None

    def test_future_marker_lineage_sigkill_matrix_replays_without_db_or_service_effect(self):
        for point in MARKER_LINEAGE_POINTS:
            with self.subTest(point=point):
                fixture = self.make(point)
                self.assertEqual(fixture.run_prepare().returncode, 0)
                fixture._install_existing_barrier()
                before = fixture.standalone_snapshot()
                killed = fixture.run_marker_lineage()
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                # A kill may land after the atomic marker rename; every other
                # safe-down invariant remains identical and replay finalizes
                # the same marker/lineage rather than touching the runtime.
                after = fixture.standalone_snapshot()
                self.assertEqual(after["db"], before["db"])
                self.assertEqual(after["masks"], before["masks"])
                self.assertEqual(after["unit_states"], before["unit_states"])
                self.assertEqual(after["mutated"], before["mutated"])
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_marker_lineage()
                if point in {"marker-lineage-temp-write", "marker-lineage-temp-fsynced", "marker-lineage-temp-fstat"}:
                    self.assertNotEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                    fixture.close(); self.fixture = None
                    continue
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                self.assertEqual(fixture.marker.read_bytes(), b"rollback recovery blocked: safe-down fresh-install-failed\n")
                final = fixture.standalone_snapshot()
                self.assertEqual(final["db"], before["db"])
                self.assertEqual(final["masks"], before["masks"])
                self.assertEqual(final["unit_states"], before["unit_states"])
                self.assertEqual(final["mutated"], before["mutated"])
                fixture.close(); self.fixture = None

    def test_marker_lineage_rejects_lock_only_direct_invocation(self):
        fixture = self.make()
        self.assertEqual(fixture.run_prepare().returncode, 0)
        before = fixture.standalone_snapshot()
        result = fixture.run_marker_lineage_lock_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fixture.standalone_snapshot(), before)

    def test_no_active_marker_lineage_chains_immutable_unbound_incidents(self):
        fixture = self.make()
        self.assertEqual(fixture.run_prepare().returncode, 0)
        fixture.remove_active_transition()
        fixture._install_existing_barrier()
        before = fixture.standalone_snapshot()
        first = fixture.run_marker_lineage("reason one")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        roots_after_first = fixture.unbound_lineage_roots()
        self.assertEqual(len(roots_after_first), 1)
        first_files = {path.name: path.read_bytes() for path in roots_after_first[0].iterdir()}
        first_stat = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in roots_after_first[0].iterdir()}

        second = fixture.run_marker_lineage("reason two")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        roots_after_second = fixture.unbound_lineage_roots()
        self.assertEqual(len(roots_after_second), 2)
        self.assertEqual({path.name: path.read_bytes() for path in roots_after_first[0].iterdir()}, first_files)
        self.assertEqual({path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in roots_after_first[0].iterdir()}, first_stat)
        self.assertEqual(fixture.marker.read_bytes(), b"rollback recovery blocked: reason two\n")
        after = fixture.standalone_snapshot()
        for key in ("db", "masks", "unit_states", "mutated"):
            self.assertEqual(after[key], before[key])
        fixture.close(); self.fixture = None

    def test_no_active_same_reason_after_publication_uses_a_new_incident(self):
        fixture = self.make()
        self.assertEqual(fixture.run_prepare().returncode, 0)
        fixture.remove_active_transition()
        self.assertEqual(fixture.run_marker_lineage("same reason").returncode, 0)
        first_root = fixture.unbound_lineage_roots()[0]
        first_files = {path.name: path.read_bytes() for path in first_root.iterdir()}
        self.assertEqual(fixture.run_marker_lineage("same reason").returncode, 0)
        roots = fixture.unbound_lineage_roots()
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0].name, roots[1].name)
        self.assertEqual({path.name: path.read_bytes() for path in first_root.iterdir()}, first_files)
        fixture.close(); self.fixture = None

    def test_no_active_marker_lineage_rejects_prepared_tampered_extra_untracked_and_stale_provenance(self):
        with self.subTest("prepared prior event"):
            fixture = self.make("marker-lineage-prepared")
            self.assertEqual(fixture.run_prepare().returncode, 0)
            fixture.remove_active_transition()
            self.assertEqual(fixture.run_marker_lineage("prepared reason").returncode, -9)
            fixture.kill_at = None; fixture._write_capability()
            self.assertNotEqual(fixture.run_marker_lineage("different reason").returncode, 0)
            fixture.close(); self.fixture = None

        for case in ("tampered", "extra", "untracked", "stale"):
            with self.subTest(case=case):
                fixture = self.make()
                self.assertEqual(fixture.run_prepare().returncode, 0)
                fixture.remove_active_transition()
                self.assertEqual(fixture.run_marker_lineage("provenance reason").returncode, 0)
                root = fixture.unbound_lineage_roots()[0]
                if case == "tampered":
                    journal = next(root.glob("*.journal"))
                    journal.write_bytes(journal.read_bytes() + b"{}\n")
                elif case == "extra":
                    (root / "extra.journal").write_bytes(b"{}\n")
                elif case == "untracked":
                    fixture.rewrite_marker_in_place("untracked marker")
                else:
                    current = fixture.marker.stat()
                    os.utime(fixture.marker, ns=(current.st_atime_ns + 1_000_000_000, current.st_mtime_ns + 1_000_000_000))
                self.assertNotEqual(fixture.run_marker_lineage("next reason").returncode, 0)
                fixture.close(); self.fixture = None

    def test_no_active_marker_lineage_replays_after_durable_prepare_and_atomic_publication(self):
        for point in ("marker-lineage-prepared", "marker-lineage-rename", "marker-lineage-dir-fsynced", "marker-lineage-committed", "marker-lineage-archive"):
            with self.subTest(point=point):
                fixture = self.make(point)
                self.assertEqual(fixture.run_prepare().returncode, 0)
                fixture.remove_active_transition()
                fixture._install_existing_barrier()
                before = fixture.standalone_snapshot()
                killed = fixture.run_marker_lineage("replay reason")
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_marker_lineage("replay reason")
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                self.assertEqual(fixture.marker.read_bytes(), b"rollback recovery blocked: replay reason\n")
                after = fixture.standalone_snapshot()
                for key in ("db", "masks", "unit_states", "mutated"):
                    self.assertEqual(after[key], before[key])
                fixture.close(); self.fixture = None

    def test_actual_sigkill_at_every_v2_to_v3_boundary_preserves_the_full_immutable_chain(self):
        """The same replay guarantee applies at every v2->v3 write boundary."""
        for point in SUPERSESSION_POINTS:
            with self.subTest(point=point):
                fixture = self.make()
                prepared = fixture.run_prepare()
                self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
                v1 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                successor_v2 = fixture.commit_successor()
                made_v2 = fixture.run_supersede(v1, successor_v2)
                self.assertEqual(made_v2.returncode, 0, made_v2.stdout + made_v2.stderr)
                v2 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                v1_body = (json.dumps(v1, sort_keys=True, separators=(",", ":")) + "\n").encode()
                v2_body = (json.dumps(v2, sort_keys=True, separators=(",", ":")) + "\n").encode()
                v1_archive = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents" / v2["incident"] / v2["parent"]["archive"]
                self.assertEqual(v2["parent"]["sha256"], hashlib.sha256(v1_body).hexdigest())
                v1_archive_before = v1_archive.read_bytes()
                successor_v3 = fixture.commit_successor()
                fixture.kill_at = point; fixture._write_capability()
                fixture._install_existing_barrier(); before = fixture.standalone_snapshot()
                killed = fixture.run_supersede(v2, successor_v3)
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                self.assertEqual(fixture.standalone_snapshot(), before)
                self.assertEqual(v1_archive.read_bytes(), v1_archive_before)
                fixture.kill_at = None; fixture._write_capability()
                rerun = fixture.run_supersede(v2, successor_v3)
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                v3 = json.loads((fixture.root / "var/lib/driftward-hatchet-bridge/safe-down-transition.json").read_text())
                self.assertEqual(v3["version"], 3)
                self.assertEqual(v3["parent"]["sha256"], hashlib.sha256(v2_body).hexdigest())
                incident = fixture.root / "var/lib/driftward-hatchet-bridge/recovery-incidents" / v3["incident"]
                self.assertEqual((incident / v3["parent"]["archive"]).read_bytes(), v2_body)
                self.assertEqual(v1_archive.read_bytes(), v1_archive_before)
                self.assertTrue(fixture.marker.exists())
                self.assertEqual(fixture.standalone_snapshot(), before)
                fixture.close(); self.fixture = None

    def test_actual_sigterm_runs_installer_exit_cleanup_and_keeps_safe_down(self):
        fixture = self.make(signal_at="writer-quiesce")
        result = fixture.run_wrapper()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(fixture.marker.exists())
        self.assertIn("signal", [row["event"] for row in fixture.audit_rows()])
        self.assertNotIn("start driftward-hatchet-bridge.service", (fixture.root / "fake-system.log").read_text())

    def test_forged_missing_and_wrong_test_capability_fd_cannot_enable_paths(self):
        fixture = self.make()
        script = fixture.repo / "hatchet/bridge/test_capability.py"
        missing = _bounded_run(["python3", str(script), "assert"], timeout=DIRECT_TIMEOUT_SECONDS, preexec_fn=fixture._as_test_user)
        self.assertNotEqual(missing.returncode, 0)
        with fixture.inherited_capability() as cap_fd:
            wrong = _bounded_run(["python3", str(script), "assert"], timeout=DIRECT_TIMEOUT_SECONDS, env=fixture.capability_env(cap_fd), pass_fds=(cap_fd, 8), preexec_fn=lambda: (os.dup2(os.open("/dev/null", os.O_RDONLY), 8), fixture._as_test_user()))
        self.assertNotEqual(wrong.returncode, 0)

    def test_unrelated_inherited_fd8_is_not_a_test_capability(self):
        fixture = self.make()
        script = fixture.repo / "hatchet/bridge/test_capability.py"
        unrelated = _bounded_run(
            ["python3", str(script), "maybe-shell"], timeout=DIRECT_TIMEOUT_SECONDS,
            env={**os.environ, "HOME": str(fixture.root)}, pass_fds=(8,),
            preexec_fn=lambda: (os.dup2(os.open("/dev/null", os.O_RDONLY), 8), fixture._as_test_user()),
        )
        self.assertEqual(unrelated.returncode, 1, unrelated.stdout + unrelated.stderr)
        self.assertEqual(unrelated.stdout, "")

    def test_timeout_kills_the_complete_harness_process_group(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            pidfile = Path(directory) / "child.pid"
            command = ["bash", "-c", f"sleep 60 & echo $! > {pidfile}; wait"]
            with self.assertRaises(HarnessTimeout): _bounded_run(command, timeout=1)
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail(f"timeout fixture child {pid} was not reaped")
                time.sleep(0.05)

    def test_root_rejects_test_override_names_and_adoption_requires_each_evidence_fd(self):
        import test_capability
        with mock.patch.dict(os.environ, {"HATCHET_BRIDGE_TEST_ROOT": "/tmp/forged"}), mock.patch("os.geteuid", return_value=0):
            with self.assertRaises(test_capability.CapabilityError): test_capability.reject_root_overrides()
        fixture = self.make()
        script = fixture.repo / "hatchet/bridge/safe_down_recovery.py"
        with fixture.inherited_capability() as cap_fd:
            for missing_fd in (3, 4, 5, 6, 7):
                with self.subTest(missing_fd=missing_fd):
                    result = _bounded_run(["python3", str(script), "adopt"], timeout=DIRECT_TIMEOUT_SECONDS, env=fixture.capability_env(cap_fd), pass_fds=(cap_fd, 8), preexec_fn=lambda: fixture._with_capability(cap_fd))
                    self.assertNotEqual(result.returncode, 0)

    def test_template_commit_accepts_an_identical_index(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            repo = Path(directory) / "repo"
            for command in (["git", "init", "--quiet", str(repo)], ["git", "-C", str(repo), "config", "user.email", "bridge-test@invalid"], ["git", "-C", str(repo), "config", "user.name", "bridge test"]):
                result = _bounded_run(command, timeout=SETUP_TIMEOUT_SECONDS)
                if result.returncode: raise AssertionError(result.stderr)
            (repo / "fixture").write_text("unchanged\n")
            for command in (["git", "-C", str(repo), "add", "fixture"], ["git", "-C", str(repo), "commit", "--quiet", "-m", "seed"], ["git", "-C", str(repo), "add", "fixture"]):
                result = _bounded_run(command, timeout=SETUP_TIMEOUT_SECONDS)
                if result.returncode: raise AssertionError(result.stderr)
            self.assertFalse(_commit_if_staged(repo))
