import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("safe_down_recovery", ROOT / "bridge" / "safe_down_recovery.py")
safe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = safe
SPEC.loader.exec_module(safe)


class SafeDownFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state"; self.state.mkdir()
        self.backups = root / "backups"; self.backups.mkdir()
        self.release = root / "release"; self.release.mkdir()
        self.etc = root / "etc"; self.etc.mkdir()
        self.repository = root / "repository"; self.repository.mkdir()
        for command in (("git", "-C", str(self.repository), "init", "--quiet"), ("git", "-C", str(self.repository), "config", "user.email", "recovery-test@invalid"), ("git", "-C", str(self.repository), "config", "user.name", "recovery test")):
            subprocess.run(command, check=True)
        (self.repository / "release.txt").write_text("v1\n")
        subprocess.run(("git", "-C", str(self.repository), "add", "release.txt"), check=True)
        subprocess.run(("git", "-C", str(self.repository), "commit", "--quiet", "-m", "v1"), check=True)
        self.lock = root / "maintenance.lock"; self.lock.touch()
        self.marker = self.state / "recovery-blocked"
        self.marker.write_bytes(safe.MARKER_TEXT); self.marker.chmod(0o600)
        self.backup = self.backups / "bridge-20260817T061607Z-before-9342db181033ff783d645973e5d4c7a54ae4c6f9.sqlite"
        self._make_db(self.backup)
        self.backup.chmod(0o600)
        self.db = self.state / "bridge.db"; shutil.copy2(self.backup, self.db)
        digest = hashlib.sha256(self.backup.read_bytes()).hexdigest()
        self.checksum = self.backup.with_suffix(self.backup.suffix + ".sha256")
        self.checksum.write_text(f"{digest}  {self.backup.name}\n"); self.checksum.chmod(0o600)
        self.targets = (str(self.etc / "hatchet-verifier-policy.json"), str(self.etc / "hatchet-routing-registry.json"), str(self.release / "current"))
        self.paths = safe.Paths(state=self.state, backups=self.backups, db=self.db, marker=self.marker, archive=self.state / "incidents", transition=self.state / "transition.json", release_root=self.release, routing=self.etc / "hatchet-routing-registry.json", lock=self.lock, targets=self.targets, repository=self.repository, expected_uid=os.getuid(), expected_gid=os.getgid())
        self.fd = os.open(self.lock, os.O_RDWR); os.dup2(self.fd, 3); fcntl.flock(3, fcntl.LOCK_EX)
        self.code = "a" * 40

    def tearDown(self):
        for fd in (3, 4, 5, 6, 7):
            try: os.close(fd)
            except OSError: pass
        self.temp.cleanup()

    @staticmethod
    def _make_db(path):
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE bridge_jobs (state TEXT)")
        c.execute("CREATE TABLE factory_attempts (status TEXT)")
        c.execute("CREATE TABLE factory_execution_leases (released_at TEXT)")
        c.execute("CREATE TABLE factory_attempt_outbox (acknowledged_at TEXT)")
        c.execute("CREATE TABLE factory_inspection_outbox (acknowledged_at TEXT)")
        c.execute("CREATE TABLE factory_attempt_events (event TEXT)")
        c.execute("CREATE TABLE factory_import_conflicts (reason TEXT)")
        c.commit(); c.close()

    def prepare(self):
        return safe.prepare(str(self.backup), hashlib.sha256(self.backup.read_bytes()).hexdigest(), hashlib.sha256(self.marker.read_bytes()).hexdigest(), str(int(self.marker.stat().st_mtime)), paths=self.paths, code_commit=self.code)

    def _sidecar_text(self, filename, *, digest=None, binary=False):
        digest = digest or hashlib.sha256(self.backup.read_bytes()).hexdigest()
        return f"{digest} {'*' if binary else ' '}{filename}\n"

    def _commit_successor(self, name: str) -> str:
        (self.repository / "release.txt").write_text(name + "\n")
        subprocess.run(("git", "-C", str(self.repository), "add", "release.txt"), check=True)
        subprocess.run(("git", "-C", str(self.repository), "commit", "--quiet", "-m", name), check=True)
        return subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", "HEAD"), text=True).strip()

    @staticmethod
    def _verified_release(pr, commit, approved_head, local_parent, local_parent_tree, _paths):
        tree = subprocess.check_output(("git", "-C", str(_paths.repository), "rev-parse", f"{commit}^{{tree}}"), text=True).strip()
        base = subprocess.check_output(("git", "-C", str(_paths.repository), "rev-parse", f"{commit}^"), text=True).strip()
        base_tree = subprocess.check_output(("git", "-C", str(_paths.repository), "rev-parse", f"{base}^{{tree}}"), text=True).strip()
        assert base == local_parent and base_tree == local_parent_tree
        return {"number": pr, "url": f"https://github.com/{safe.SOURCE_REPOSITORY}/pull/{pr}", "state": "MERGED", "base": "main", "base_repo": safe.SOURCE_REPOSITORY, "head_repo": safe.SOURCE_REPOSITORY, "base_sha": base, "base_tree": base_tree, "head": approved_head, "merge": commit, "merge_tree": tree, "merge_parents": [base], "current": commit, "merged_at": "2026-08-17T00:00:00Z"}

    def _replace_adoption_transition_fd(self):
        fd = os.open(self.paths.transition, os.O_RDONLY)
        os.dup2(fd, 4); os.close(fd)

    def _present_pre_marker_target(self) -> Path:
        target = Path(self.targets[0]); target.write_text("pre-marker proof\n"); target.chmod(0o640)
        # The production proof requires the target to predate the marker.  A
        # future marker mtime models that ordering without relying on sleeps.
        future = target.stat().st_ctime_ns + 10_000_000_000
        os.utime(self.marker, ns=(future, future))
        return target

    def _write_reconciled_v1_journal(self, record):
        journal = self.state / f"publication-{record['code_commit']}.journal"
        rows = (
            {"status": "prepared", "transaction": record["code_commit"], "commit": record["code_commit"], "snapshot": {}},
            {"status": "rolled_back", "recovery": "safe_down_retry_fresh_install", "publication_boundary": "committed"},
        )
        journal.write_bytes(b"".join(safe._record_bytes(row) for row in rows)); journal.chmod(0o600)
        return journal

    def _supersede(self, parent, successor, *, pr=170, **overrides):
        body = safe._record_bytes(parent)
        return safe.supersede(
            parent_sha256=overrides.pop("parent_sha256", safe._record_hash(body)), parent_generation=overrides.pop("parent_generation", parent["version"]),
            parent_code_commit=overrides.pop("parent_code_commit", parent["code_commit"]), backup_arg=overrides.pop("backup_arg", str(self.backup)),
            checksum=overrides.pop("checksum", hashlib.sha256(self.backup.read_bytes()).hexdigest()), marker_sha=overrides.pop("marker_sha", hashlib.sha256(self.marker.read_bytes()).hexdigest()),
            marker_mtime=overrides.pop("marker_mtime", str(int(self.marker.stat().st_mtime))), successor_commit=successor, successor_pr=pr, approved_pr_head=overrides.pop("approved_pr_head", "b" * 40),
            paths=self.paths, release_verifier=overrides.pop("release_verifier", self._verified_release), **overrides,
        )

    def _rewrite_marker_in_place(self, contents):
        before = self.marker.stat()
        fd = os.open(self.marker, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            os.write(fd, contents); os.fsync(fd)
        finally:
            os.close(fd)
        os.utime(self.marker, ns=(before.st_atime_ns + 2_000_000_000, before.st_mtime_ns + 2_000_000_000))
        self.assertEqual(self.marker.stat().st_ino, before.st_ino)
        return {"sha256": hashlib.sha256(contents).hexdigest(), "dev": self.marker.stat().st_dev, "ino": self.marker.stat().st_ino, "mtime_ns": self.marker.stat().st_mtime_ns}

    def _replace_adoption_marker_fd(self):
        fd = os.open(self.marker, os.O_RDONLY)
        os.dup2(fd, 5); os.close(fd)

    def test_current_incident_proof_archives_and_adopts_capabilities(self):
        self.prepare()
        record = safe.adopt(paths=self.paths, code_commit=self.code)
        self.assertEqual(record["decision"], "safe_down_fresh_install")
        self.assertTrue((self.paths.archive / record["incident"] / "marker").exists())
        self.assertEqual(record["publication_targets"]["states"], [{"path": str(self.etc / "hatchet-verifier-policy.json"), "state": "absent"}, {"path": str(self.etc / "hatchet-routing-registry.json"), "state": "absent"}, {"path": str(self.release / "current"), "state": "absent"}, {"path": str(self.release / "9342db181033ff783d645973e5d4c7a54ae4c6f9"), "state": "absent"}])

    def test_prepare_rehands_existing_v1_without_reconstruction(self):
        self.prepare()
        before, body = safe._read_record(self.paths.transition, "active transition", self.paths)
        info = self.paths.transition.stat()
        self.assertEqual(self.prepare(), 4)
        after, after_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        self.assertEqual((after, after_body), (before, body))
        self.assertEqual((self.paths.transition.stat().st_dev, self.paths.transition.stat().st_ino), (info.st_dev, info.st_ino))
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=self.code)["version"], 1)

    def test_prepare_rehands_synthetic_descendant_v2_with_strict_adoption(self):
        self.prepare()
        v1, v1_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        current = self._rewrite_marker_in_place(safe.ROLLBACK_MARKER_TEXT)
        successor = self._commit_successor("descendant-v2-prepare")
        v2 = self._supersede(v1, successor, descendant_attestation={"old_marker": v1["marker"], "current_marker": current, "reason": safe.ROLLBACK_MARKER_REASON})
        self.code = successor
        before, before_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        before_info = self.paths.transition.stat()
        self.assertEqual(before, v2)
        self.assertEqual(self.prepare(), 4)
        after, after_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        self.assertEqual((after, after_body), (before, before_body))
        self.assertEqual((self.paths.transition.stat().st_dev, self.paths.transition.stat().st_ino), (before_info.st_dev, before_info.st_ino))
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=successor)["version"], 2)
        self.assertEqual((self.paths.archive / v1["incident"] / "transition.json").read_bytes(), v1_body)

    def test_prepare_existing_transition_rejects_explicit_evidence_mismatch_without_mutation(self):
        self.prepare()
        v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2-evidence-mismatch")
        self._supersede(v1, successor)
        protected = (self.marker.read_bytes(), self.db.read_bytes(), self.paths.transition.read_bytes(), self.paths.transition.stat().st_ino)
        with self.assertRaises(safe.ProofError):
            safe.prepare(str(self.backup), "0" * 64, hashlib.sha256(self.marker.read_bytes()).hexdigest(), str(int(self.marker.stat().st_mtime)), paths=self.paths, code_commit=successor)
        self.assertEqual((self.marker.read_bytes(), self.db.read_bytes(), self.paths.transition.read_bytes(), self.paths.transition.stat().st_ino), protected)

    def test_prepare_and_adopt_accept_only_literal_canonical_sidecar_names(self):
        for form, binary in (("basename", False), ("absolute", False), ("absolute", True)):
            with self.subTest(form=form, binary=binary):
                self.tearDown(); self.setUp()
                filename = self.backup.name if form == "basename" else str(self.backup)
                self.checksum.write_text(self._sidecar_text(filename, binary=binary))
                self.prepare()
                self.assertEqual(safe.adopt(paths=self.paths, code_commit=self.code)["backup"]["path"], str(self.backup))

    def test_production_incident_absolute_sidecar_path_is_accepted_without_opening_it(self):
        backup = Path("/root/driftward-backups/hatchet/bridge-20260817T061607Z-before-9342db181033ff783d645973e5d4c7a54ae4c6f9.sqlite")
        digest = "1d4dec662e055c074d88bbb133305e4485dfab84e3bd66c14cd1b69fe4990839"
        self.assertTrue(safe._matches_checksum_sidecar(f"{digest}  {backup}\n".encode(), digest, backup))

    def test_prepare_rejects_untrusted_sidecar_names_and_grammar(self):
        digest = hashlib.sha256(self.backup.read_bytes()).hexdigest()
        alias = self.backups.parent / "backup-alias"
        alias.symlink_to(self.backups, target_is_directory=True)
        wrong_names = (
            f"/elsewhere/{self.backup.name}",
            str(self.backups / "other.sqlite"),
            str(self.backups / ".." / self.backups.name / self.backup.name),
            f"./{self.backup.name}",
            str(self.backup).replace(f"{self.backups}/", f"{self.backups}//"),
            str(alias / self.backup.name),
            str(self.backup) + ".copy",
            self.backup.name + ".copy",
            str(self.backup) + "*",
            self.backup.name + "*",
        )
        malformed = (
            "",
            "not-a-checksum\n",
            self._sidecar_text(self.backup.name, digest="0" * 64),
            self._sidecar_text(self.backup.name) + self._sidecar_text(self.backup.name),
            self._sidecar_text(self.backup.name) + "extra\n",
            self._sidecar_text(self.backup.name) + "\n",
        )
        for contents in (*[self._sidecar_text(name) for name in wrong_names], *malformed):
            with self.subTest(contents=contents):
                self.tearDown(); self.setUp()
                self.checksum.write_text(contents)
                with self.assertRaises(safe.ProofError): self.prepare()

    def test_adopt_rechecks_untrusted_sidecar_names_and_grammar(self):
        self.prepare()
        alias = self.backups.parent / "backup-alias"
        alias.symlink_to(self.backups, target_is_directory=True)
        invalid = (
            self._sidecar_text(f"/other/{self.backup.name}"),
            self._sidecar_text(str(self.backups / "other.sqlite")),
            self._sidecar_text(str(self.backups / ".." / self.backups.name / self.backup.name)),
            self._sidecar_text(f"./{self.backup.name}"),
            self._sidecar_text(str(self.backup).replace(f"{self.backups}/", f"{self.backups}//")),
            self._sidecar_text(str(alias / self.backup.name)),
            self._sidecar_text(str(self.backup) + ".copy"),
            self._sidecar_text(self.backup.name + ".copy"),
            self._sidecar_text(str(self.backup) + "*"),
            "not-a-checksum\n",
            self._sidecar_text(self.backup.name, digest="0" * 64),
            self._sidecar_text(self.backup.name) + self._sidecar_text(self.backup.name),
            self._sidecar_text(self.backup.name) + "extra\n",
            self._sidecar_text(self.backup.name) + "\n",
            b"",  # empty sidecar must not be interpreted as a filename.
        )
        for contents in invalid:
            with self.subTest(contents=contents):
                self.checksum.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
                with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)

    def test_each_partial_publication_target_blocks(self):
        for index in range(len(self.targets)):
            with self.subTest(target=index):
                # A fresh fixture keeps marker timestamps before any target.
                self.tearDown(); self.setUp()
                path = Path(self.targets[index])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("partial"); path.chmod(0o640)
                if path.name.endswith(".env"): path.chmod(0o600)
                with self.assertRaises(safe.ProofError): self.prepare()

    def test_backup_traversal_symlink_and_checksum_mismatch_block(self):
        with self.assertRaises(safe.ProofError):
            safe.prepare(str(self.backups / ".." / self.backups.name / self.backup.name), "0" * 64, "0" * 64, "0", paths=self.paths, code_commit=self.code)
        link = self.backups / "bridge-20260817T061608Z-before-9342db181033ff783d645973e5d4c7a54ae4c6f9.sqlite"
        link.symlink_to(self.backup)
        with self.assertRaises(safe.ProofError):
            safe.prepare(str(link), hashlib.sha256(self.backup.read_bytes()).hexdigest(), hashlib.sha256(self.marker.read_bytes()).hexdigest(), str(int(self.marker.stat().st_mtime)), paths=self.paths, code_commit=self.code)
        self.checksum.write_text("0" * 64 + "  wrong.sqlite\n")
        with self.assertRaises(safe.ProofError): self.prepare()

    def test_backup_and_sidecar_hardlinks_block(self):
        backup_link = self.backups / "backup-hardlink.sqlite"
        os.link(self.backup, backup_link)
        with self.assertRaises(safe.ProofError): self.prepare()
        self.tearDown(); self.setUp()
        sidecar_link = self.backups / "backup-sidecar-hardlink.sha256"
        os.link(self.checksum, sidecar_link)
        with self.assertRaises(safe.ProofError): self.prepare()

    def test_direct_adoption_without_transition_capabilities_fails(self):
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)

    def test_each_missing_or_forged_fd3_through_fd7_blocks_adoption(self):
        self.prepare()
        for target in (3, 4, 5, 6, 7):
            with self.subTest(missing=target):
                saved = os.dup(target)
                os.close(target)
                try:
                    with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)
                finally:
                    os.dup2(saved, target); os.close(saved)
        forged = os.open(self.marker, os.O_RDONLY)
        saved = os.dup(4); os.dup2(forged, 4); os.close(forged)
        try:
            with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)
        finally:
            os.dup2(saved, 4); os.close(saved)

    def test_production_ownership_policy_rejects_test_fixture(self):
        production = safe.Paths(state=self.state, backups=self.backups, db=self.db, marker=self.marker, archive=self.state / "incidents", transition=self.state / "transition.json", release_root=self.release, routing=self.etc / "hatchet-routing-registry.json", lock=self.lock, targets=self.targets, expected_uid=os.getuid() + 1, expected_gid=os.getgid())
        with self.assertRaises(safe.ProofError):
            safe.prepare(str(self.backup), hashlib.sha256(self.backup.read_bytes()).hexdigest(), hashlib.sha256(self.marker.read_bytes()).hexdigest(), str(int(self.marker.stat().st_mtime)), paths=production, code_commit=self.code)

    def test_changed_live_ledger_and_tampered_transition_block_adoption(self):
        self.prepare()
        con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('running')"); con.commit(); con.close()
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)

    def test_supersession_never_uses_a_publication_journal_to_skip_parent_live_proof(self):
        self.prepare()
        parent, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2")
        journal = self.state / f"publication-{parent['code_commit']}.journal"
        journal.write_text(json.dumps({"status": "prepared", "commit": parent["code_commit"]}) + "\n"); journal.chmod(0o600)
        self.assertTrue(safe._matching_publication_journal(self.paths, parent))
        con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('changed')"); con.commit(); con.close()
        with self.assertRaises(safe.ProofError): self._supersede(parent, successor)

        self.tearDown(); self.setUp(); self.prepare()
        parent, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2-target")
        journal = self.state / f"publication-{parent['code_commit']}.journal"
        journal.write_text(json.dumps({"status": "prepared", "commit": parent["code_commit"]}) + "\n"); journal.chmod(0o600)
        target = Path(self.targets[0]); target.write_text("tempting journal shortcut\n"); target.chmod(0o640)
        with self.assertRaises(safe.ProofError): self._supersede(parent, successor)

    def test_only_prepare_explicitly_allows_its_own_publication_journal_replay(self):
        self.prepare()
        record, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        journal = self.state / f"publication-{record['code_commit']}.journal"
        journal.write_text(json.dumps({"status": "prepared", "commit": record["code_commit"]}) + "\n"); journal.chmod(0o600)
        con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('publication-in-progress')"); con.commit(); con.close()
        with self.assertRaises(safe.ProofError): safe._validate_live_proof(record, self.paths)
        self.assertEqual(self.prepare(), 4)

    def test_v1_to_v2_adoption_and_v2_to_v3_preserve_immutable_lineage(self):
        self.prepare()
        v1, v1_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        marker_before = self.marker.read_bytes(); db_before = self.db.read_bytes()
        v2_commit = self._commit_successor("v2")
        v2 = self._supersede(v1, v2_commit)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["parent"]["sha256"], safe._record_hash(v1_body))
        self.assertEqual(v2["reviewed_release"]["head"], "b" * 40)
        self.assertNotEqual(v2["reviewed_release"]["head"], v2["code_commit"])
        evidence = {key: v2["reviewed_release"][key] for key in ("number", "url", "state", "base", "base_repo", "head_repo", "base_sha", "base_tree", "head", "merge", "merge_tree", "merge_parents", "current", "merged_at")}
        self.assertEqual(v2["reviewed_release"]["github_evidence_sha256"], hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(self.marker.read_bytes(), marker_before)
        self.assertEqual(self.db.read_bytes(), db_before)
        self.assertEqual(safe._read_record(self.paths.archive / v1["incident"] / "transition.json", "v1 archive", self.paths)[1], v1_body)
        self._replace_adoption_transition_fd()
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=v2_commit)["version"], 2)
        v3_commit = self._commit_successor("v3")
        v3 = self._supersede(v2, v3_commit, pr=171)
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["parent"]["generation"], 2)
        self._replace_adoption_transition_fd()
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=v3_commit)["version"], 3)

    def test_supersession_rejects_stale_tampered_dirty_and_bad_provenance_without_side_effects(self):
        self.prepare()
        parent, parent_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2")
        protected = (self.marker.read_bytes(), self.db.read_bytes(), parent_body)
        for kwargs in (
            {"parent_sha256": "0" * 64}, {"parent_generation": 2}, {"parent_code_commit": "b" * 40},
            {"checksum": "0" * 64}, {"marker_sha": "0" * 64}, {"marker_mtime": "0" * 10},
            {"release_verifier": lambda *_: {"number": 170}},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(safe.ProofError): self._supersede(parent, successor, **kwargs)
                self.assertEqual((self.marker.read_bytes(), self.db.read_bytes(), safe._read_record(self.paths.transition, "active transition", self.paths)[1]), protected)
        (self.repository / "untracked").write_text("no\n")
        with self.assertRaises(safe.ProofError): self._supersede(parent, successor)
        (self.repository / "untracked").unlink()
        initial = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", "HEAD~1"), text=True).strip()
        with self.assertRaises(safe.ProofError): safe._clean_commit(initial, self.paths)
        os.unlink(self.paths.transition)
        self.paths.transition.write_text('{"decision":"forged"}\n'); self.paths.transition.chmod(0o600)
        with self.assertRaises(safe.ProofError): self._supersede(parent, successor)
        self.assertEqual(safe._read_record(self.paths.archive / parent["incident"] / "transition.json", "v1 archive", self.paths)[1], parent_body)
        # The archive is append-only: a replaced active path is not the held
        # transition capability and must not be accepted on a later invocation.
        os.unlink(self.paths.transition)
        self.paths.transition.write_text('{"decision":"forged"}\n'); self.paths.transition.chmod(0o600)
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=self.code)

    def test_supersession_rejects_every_bound_pr_provenance_field(self):
        self.prepare()
        parent, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2")
        parent_commit = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", f"{successor}^"), text=True).strip()
        parent_tree = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", f"{parent_commit}^{{tree}}"), text=True).strip()
        valid = self._verified_release(170, successor, "b" * 40, parent_commit, parent_tree, self.paths)
        corruptions = {
            "url": f"https://github.com/{safe.SOURCE_REPOSITORY}/pull/999", "state": "OPEN", "base": "release", "base_repo": "attacker/host", "head_repo": "fork/host",
            "base_sha": "c" * 40, "base_tree": "c" * 40, "head": "c" * 40,
            "merge": "0" * 40, "merge_tree": "0" * 40, "merge_parents": [], "current": "0" * 40,
            "merged_at": "", "number": 171,
        }
        for key, value in corruptions.items():
            with self.subTest(key=key):
                evidence = dict(valid); evidence[key] = value
                with self.assertRaises(safe.ProofError): self._supersede(parent, successor, release_verifier=lambda *_args, evidence=evidence: evidence)

    def test_github_provenance_rejects_forks_wrong_base_and_commit_tree_receipts(self):
        successor = self._commit_successor("github-merge")
        base = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", f"{successor}^"), text=True).strip()
        tree = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", f"{successor}^{{tree}}"), text=True).strip()
        base_tree = subprocess.check_output(("git", "-C", str(self.repository), "rev-parse", f"{base}^{{tree}}"), text=True).strip()
        pull = {"state": "closed", "merged": True, "merged_at": "2026-08-17T00:00:00Z", "html_url": f"https://github.com/{safe.SOURCE_REPOSITORY}/pull/170", "merge_commit_sha": successor, "base": {"ref": "main", "sha": base, "repo": {"full_name": safe.SOURCE_REPOSITORY}}, "head": {"sha": successor, "repo": {"full_name": safe.SOURCE_REPOSITORY}}}
        merge = {"sha": successor, "tree": {"sha": tree}, "parents": [{"sha": base}]}
        base_commit = {"sha": base, "tree": {"sha": base_tree}, "parents": []}

        def evidence_for(pr_payload, merge_payload=merge, base_payload=base_commit):
            def fake_check_output(command, **_kwargs):
                endpoint = command[-1]
                if endpoint.endswith("/pulls/170"): return json.dumps(pr_payload)
                if endpoint.endswith(f"/git/commits/{successor}"): return json.dumps(merge_payload)
                if endpoint.endswith(f"/git/commits/{base}"): return json.dumps(base_payload)
                raise AssertionError(endpoint)
            with mock.patch.object(safe.subprocess, "check_output", side_effect=fake_check_output):
                return safe._github_release_evidence(170, successor, successor, base, base_tree, self.paths)

        receipt = evidence_for(pull)
        self.assertEqual(receipt["merge_tree"], tree)
        for mutate in (
            lambda item: item["base"]["repo"].update(full_name="attacker/host"),
            lambda item: item["head"]["repo"].update(full_name="fork/host"),
            lambda item: item["base"].update(ref="release"),
            lambda item: item.update(html_url=f"https://github.com/{safe.SOURCE_REPOSITORY}/pull/999"),
            lambda item: item["head"].update(sha="c" * 40),
            lambda item: item["base"].update(sha="c" * 40),
            lambda item: item.update(merged=False),
            lambda item: item.update(merge_commit_sha="0" * 40),
        ):
            bad = json.loads(json.dumps(pull)); mutate(bad)
            with self.assertRaises(safe.ProofError): evidence_for(bad)
        with self.assertRaises(safe.ProofError): evidence_for(pull, {**merge, "tree": {"sha": "not-a-tree"}})
        with self.assertRaises(safe.ProofError): evidence_for(pull, merge, {**base_commit, "sha": "0" * 40})

    def test_v2_adoption_rechecks_clean_checkout_and_recorded_reviewed_tree(self):
        self.prepare()
        v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        v2_commit = self._commit_successor("v2")
        v2 = self._supersede(v1, v2_commit)
        (self.repository / "untracked-v2").write_text("no\n")
        self._replace_adoption_transition_fd()
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v2_commit)
        (self.repository / "untracked-v2").unlink()

        archive = next((self.paths.archive / v2["incident"]).glob("transition-v2-*.json"))
        tampered = dict(v2); reviewed = dict(v2["reviewed_release"]); reviewed["tree"] = "0" * 40; tampered["reviewed_release"] = reviewed
        os.unlink(self.paths.transition); os.unlink(archive)
        archive.write_bytes(safe._record_bytes(tampered)); archive.chmod(0o600); os.link(archive, self.paths.transition)
        self._replace_adoption_transition_fd()
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v2_commit)

    def test_v2_adoption_strictly_rejects_every_publication_journal_with_changed_live_proof(self):
        journal_rows = {
            "prepared": '{"commit":"%s","status":"prepared"}\n',
            "committed": '{"commit":"%s","status":"prepared"}\n{"status":"committed"}\n',
            "malformed": '{"commit":"%s","status":"prepared"}',
        }
        for journal_kind, template in journal_rows.items():
            for change in ("ledger", "target"):
                with self.subTest(journal=journal_kind, change=change):
                    self.tearDown(); self.setUp(); self.prepare()
                    v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
                    v2_commit = self._commit_successor(f"v2-{journal_kind}-{change}")
                    self._supersede(v1, v2_commit)
                    journal = self.state / f"publication-{v2_commit}.journal"
                    journal.write_text(template % v2_commit); journal.chmod(0o600)
                    if change == "ledger":
                        con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('journal-bypass')"); con.commit(); con.close()
                    else:
                        target = Path(self.targets[0]); target.write_text("journal-bypass\n"); target.chmod(0o640)
                    self._replace_adoption_transition_fd()
                    with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v2_commit)

    def test_v1_adoption_never_treats_a_publication_journal_as_live_proof(self):
        journal_rows = {
            "prepared": '{"commit":"%s","status":"prepared"}\n',
            "committed": '{"commit":"%s","status":"prepared"}\n{"status":"committed"}\n',
            "malformed": '{"commit":"%s","status":"prepared"}',
        }
        for journal_kind, template in journal_rows.items():
            for change in ("ledger", "target"):
                with self.subTest(journal=journal_kind, change=change):
                    self.tearDown(); self.setUp(); self.prepare()
                    v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
                    journal = self.state / f"publication-{v1['code_commit']}.journal"
                    journal.write_text(template % v1["code_commit"]); journal.chmod(0o600)
                    if change == "ledger":
                        con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('journal-bypass')"); con.commit(); con.close()
                    else:
                        target = Path(self.targets[0]); target.write_text("journal-bypass\n"); target.chmod(0o640)
                    with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v1["code_commit"])

    def test_v1_reconciled_rollback_ignores_only_ctime_and_v2_remains_exact(self):
        target = self._present_pre_marker_target()
        self.prepare()
        v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        self._write_reconciled_v1_journal(v1)
        before = target.stat()
        target.chmod(0o600); target.chmod(0o640)  # ctime changes; content and mtime do not.
        after = target.stat()
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=v1["code_commit"])["version"], 1)

        self.tearDown(); self.setUp()
        target = self._present_pre_marker_target()
        self.prepare()
        v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        self._write_reconciled_v1_journal(v1)
        before = target.stat()
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v1["code_commit"])

        self.tearDown(); self.setUp()
        target = self._present_pre_marker_target()
        self.prepare()
        v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2-exact-ctime")
        v2 = self._supersede(v1, successor)
        before = target.stat()
        target.chmod(0o600); target.chmod(0o640)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        self._replace_adoption_transition_fd()
        with self.assertRaises(safe.ProofError): safe.adopt(paths=self.paths, code_commit=v2["code_commit"])

    def test_supersession_rechecks_checkout_after_provenance_before_persistence(self):
        self.prepare()
        parent, parent_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        successor = self._commit_successor("v2-checkout-race")

        def mutate_after_receipt(*args):
            evidence = self._verified_release(*args)
            (self.repository / "untracked-after-receipt").write_text("race\n")
            return evidence

        with self.assertRaises(safe.ProofError): self._supersede(parent, successor, release_verifier=mutate_after_receipt)
        self.assertEqual(safe._read_record(self.paths.transition, "active transition", self.paths)[1], parent_body)
        incident = self.paths.archive / parent["incident"]
        self.assertEqual(list(incident.glob("transition-v2-*.json")), [])
        self.assertEqual(list(incident.glob("supersession-v2-*.journal")), [])

    def test_supersession_journal_requires_the_exact_canonical_two_record_sequence(self):
        prepared = {"status": "prepared", "parent_sha256": "a" * 64, "successor_sha256": "b" * 64, "generation": 2}
        committed = {"status": "active_link_committed", "parent_sha256": "a" * 64, "successor_sha256": "b" * 64, "generation": 2}
        path = self.state / "supersession.journal"
        invalid = (
            [committed, prepared], [prepared, prepared], [prepared, committed, committed],
            [{**prepared, "extra": True}], [{**prepared, "generation": 3}], [prepared, {**committed, "successor_sha256": "c" * 64}],
        )
        for rows in invalid:
            with self.subTest(rows=rows):
                path.write_bytes(b"".join(safe._record_bytes(row) for row in rows)); path.chmod(0o600)
                with self.assertRaises(safe.ProofError): safe._supersession_journal_state(path, prepared, committed, self.paths)
        path.write_bytes(safe._record_bytes(prepared)[:-1]); path.chmod(0o600)
        with self.assertRaises(safe.ProofError): safe._supersession_journal_state(path, prepared, committed, self.paths)

    def test_descendant_attestation_models_in_place_terminal_marker_and_v2_adopts(self):
        self.prepare()
        v1, v1_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        archive = self.paths.archive / v1["incident"] / "transition.json"
        archive_info = archive.stat()
        current = self._rewrite_marker_in_place(safe.ROLLBACK_MARKER_TEXT)
        successor_commit = self._commit_successor("descendant-v2")
        v2 = self._supersede(
            v1, successor_commit, marker_sha=current["sha256"], marker_mtime=str(current["mtime_ns"] // 1_000_000_000),
            descendant_attestation={"old_marker": v1["marker"], "current_marker": current, "reason": safe.ROLLBACK_MARKER_REASON},
        )
        self.assertEqual(v2["marker"], current)
        self.assertIn("marker_descendant", v2)
        self.assertEqual(archive.read_bytes(), v1_body)
        self.assertEqual(archive.stat().st_ino, archive_info.st_ino)
        self.assertGreaterEqual(self.paths.transition.stat().st_nlink, 2)
        safe._validate_chain(v2, safe._record_bytes(v2), self.paths)
        safe._validate_live_proof(v2, self.paths)
        self._replace_adoption_transition_fd(); self._replace_adoption_marker_fd()
        self.assertEqual(safe.adopt(paths=self.paths, code_commit=successor_commit)["version"], 2)

    def test_descendant_attestation_rejects_wrong_reason_identity_and_unattested_same_inode_rewrite(self):
        for mutation in ("wrong-reason", "wrong-old", "wrong-current", "missing"):
            with self.subTest(mutation=mutation):
                self.tearDown(); self.setUp(); self.prepare()
                v1, body = safe._read_record(self.paths.transition, "active transition", self.paths)
                current = self._rewrite_marker_in_place(safe.ROLLBACK_MARKER_TEXT)
                successor = self._commit_successor("descendant-" + mutation)
                if mutation == "missing":
                    request = None
                else:
                    request = {"old_marker": v1["marker"], "current_marker": current, "reason": safe.ROLLBACK_MARKER_REASON}
                    if mutation == "wrong-reason": request["reason"] = "arbitrary-terminal-text"
                    if mutation == "wrong-old": request["old_marker"] = {**v1["marker"], "sha256": "0" * 64}
                    if mutation == "wrong-current": request["current_marker"] = {**current, "mtime_ns": current["mtime_ns"] + 1}
                with self.assertRaises(safe.ProofError):
                    self._supersede(v1, successor, descendant_attestation=request)
                self.assertEqual(safe._read_record(self.paths.transition, "active transition", self.paths)[1], body)
                self.assertEqual(list((self.paths.archive / v1["incident"]).glob("transition-v2-*.json")), [])

    def test_descendant_attestation_reproves_db_backup_and_publications_before_writing(self):
        for attack in ("db", "backup", "target"):
            with self.subTest(attack=attack):
                self.tearDown(); self.setUp(); self.prepare()
                v1, _ = safe._read_record(self.paths.transition, "active transition", self.paths)
                current = self._rewrite_marker_in_place(safe.ROLLBACK_MARKER_TEXT)
                if attack == "db":
                    con = sqlite3.connect(self.db); con.execute("INSERT INTO bridge_jobs VALUES ('running')"); con.commit(); con.close()
                elif attack == "backup":
                    with self.backup.open("ab") as handle: handle.write(b"tamper")
                else:
                    target = Path(self.targets[0]); target.write_text("published\n"); target.chmod(0o640)
                with self.assertRaises(safe.ProofError): self._supersede(
                    v1, self._commit_successor("attack-" + attack), marker_sha=current["sha256"], marker_mtime=str(current["mtime_ns"] // 1_000_000_000),
                    descendant_attestation={"old_marker": v1["marker"], "current_marker": current, "reason": safe.ROLLBACK_MARKER_REASON},
                )
                self.assertEqual(list((self.paths.archive / v1["incident"]).glob("marker-descendant-*.json")), [])

    def test_future_atomic_marker_lineage_replays_and_allows_strict_successor(self):
        self.prepare()
        v1, v1_body = safe._read_record(self.paths.transition, "active transition", self.paths)
        original_marker = self.marker.read_bytes(); original_db = self.db.read_bytes()
        with mock.patch.object(safe, "_revalidate_barrier_evidence"):
            committed = safe.update_marker_lineage(safe.ROLLBACK_MARKER_REASON, paths=self.paths, barrier_evidence=True)
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(self.marker.read_bytes(), safe.ROLLBACK_MARKER_TEXT)
        self.assertNotEqual(committed["new_marker"], v1["marker"])
        self.assertEqual(safe._read_record(self.paths.transition, "active transition", self.paths)[1], v1_body)
        self.assertEqual(self.db.read_bytes(), original_db)
        # Exact replay is idempotent and the immutable archive lets ordinary
        # supersession prove the new marker without an operator attestation.
        with mock.patch.object(safe, "_revalidate_barrier_evidence"):
            self.assertEqual(safe.update_marker_lineage(safe.ROLLBACK_MARKER_REASON, paths=self.paths, barrier_evidence=True), committed)
            second = safe.update_marker_lineage("safe-down second-failure", paths=self.paths, barrier_evidence=True)
        incident = self.paths.archive / v1["incident"]
        self.assertTrue((incident / f"marker-lineage-v1-{safe._record_hash(safe._record_bytes(v1))}-g1.journal").is_file())
        self.assertTrue((incident / f"marker-lineage-v1-{safe._record_hash(safe._record_bytes(v1))}-g2.journal").is_file())
        self.assertEqual(second["generation"], 2)
        v2 = self._supersede(v1, self._commit_successor("automatic-lineage"), marker_sha=second["new_marker"]["sha256"], marker_mtime=str(second["new_marker"]["mtime_ns"] // 1_000_000_000))
        self.assertIn("marker_lineage", v2)
        self.assertEqual(self.marker.read_bytes(), b"rollback recovery blocked: safe-down second-failure\n")
        self.assertNotEqual(original_marker, self.marker.read_bytes())
