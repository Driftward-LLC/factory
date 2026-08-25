import hashlib, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from hatchet_hermes_bridge import recovery

ROOT = Path(__file__).resolve().parents[1]

class RecoveryTests(unittest.TestCase):
    def test_unknown_recovery_failpoint_rejects_before_any_restore_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / f"bridge-20260817T010203Z-before-{'a'*40}.sqlite"
            live = root / "live.db"
            c = sqlite3.connect(backup); c.execute("CREATE TABLE t (v)"); c.commit(); c.close()
            os.chmod(backup, 0o600)
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "unknown recovery failpoint"):
                recovery.restore_verified_backup(str(backup), str(live), digest, failpoint="not-a-stage")
            self.assertFalse(live.exists())
            self.assertFalse((root / "live.db.restore-journal").exists())

    def test_production_owner_policy_rejects_nonroot_and_private_test_policy_can_inject_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / f"bridge-20260817T010203Z-before-{'a'*40}.sqlite"
            live = Path(directory) / "live.db"
            c=sqlite3.connect(backup); c.execute("CREATE TABLE t (v)"); c.commit(); c.close()
            os.chmod(backup, 0o600); digest=hashlib.sha256(backup.read_bytes()).hexdigest()
            class Reject:
                def accepts(self, stat): return False
            with patch("hatchet_hermes_bridge.recovery._ownership_policy", Reject()):
                with self.assertRaisesRegex(RuntimeError, "checksum or mode"):
                    recovery.restore_verified_backup(str(backup), str(live), digest)
            class Current:
                def accepts(self, stat): return stat.st_uid == os.getuid() and stat.st_gid == os.getgid() and stat.st_mode & 0o777 == 0o600
            with patch("hatchet_hermes_bridge.recovery._ownership_policy", Current()):
                recovery.restore_verified_backup(str(backup), str(live), digest)
    def test_subprocess_crashes_are_recoverable_to_coherent_backup(self):
        for point in ("prepared", "built", "checkpointed", "replaced", "fsynced"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); backup=root/f"bridge-20260817T010203Z-before-{'a'*40}.sqlite"; live=root/'live.db'
                for path,value in ((backup,'old'),(live,'new')):
                    c=sqlite3.connect(path); c.execute('CREATE TABLE state (v TEXT)'); c.execute('INSERT INTO state VALUES (?)',(value,)); c.commit(); c.close()
                os.chmod(backup,0o600); digest=hashlib.sha256(backup.read_bytes()).hexdigest()
                code="from hatchet_hermes_bridge import recovery; import os,sys; recovery._ownership_policy=type('TestOwner',(),{'accepts':lambda self,s:s.st_uid==os.getuid() and s.st_gid==os.getgid() and s.st_mode & 0o777==0o600})(); recovery.restore_verified_backup(sys.argv[1],sys.argv[2],sys.argv[3],failpoint=sys.argv[4])"
                subprocess.run([sys.executable,'-c',code,str(backup),str(live),digest,point], env={**os.environ,'PYTHONPATH':str(ROOT)}, check=False)
                # A subsequent recovery deterministically converges to old.
                subprocess.run([sys.executable,'-c',code,str(backup),str(live),digest,'none'], env={**os.environ,'PYTHONPATH':str(ROOT)}, check=True)
                c=sqlite3.connect(live); self.assertEqual(c.execute('SELECT v FROM state').fetchone()[0],'old'); self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok'); c.close()
