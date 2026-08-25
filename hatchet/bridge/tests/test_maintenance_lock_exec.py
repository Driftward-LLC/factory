import ast
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class MaintenanceLockExecTests(unittest.TestCase):
  def test_fixed_lock_helper_is_descriptor_safe_and_exec_bound(self):
    source=(ROOT/"bridge"/"maintenance_lock_exec.py").read_text()
    self.assertIn('O_DIRECTORY|os.O_NOFOLLOW',source); self.assertIn('os.open(NAME',source)
    self.assertIn('fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)',source); self.assertIn('os.dup2(fd,3',source)
    self.assertIn('os.execvp',source); self.assertNotIn('os.environ',source)
  def test_production_scripts_reexec_only_through_fixed_helper(self):
    install=(ROOT/"bridge"/'install.sh').read_text()
    recovery=(ROOT/"bridge"/'recover-safe-down.sh').read_text()
    self.assertIn('maintenance_lock_exec.py" check',install); self.assertIn('maintenance_lock_exec.py" exec',install)
    self.assertIn('maintenance_lock_exec.py" check',recovery)
    self.assertIn('maintenance_lock_exec.py" "$mode"',recovery)
    self.assertIn('exec-existing',recovery)
