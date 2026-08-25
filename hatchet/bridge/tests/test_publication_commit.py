import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("publication_commit",ROOT/"bridge"/"publication_commit.py")
pubmod=importlib.util.module_from_spec(spec); assert spec.loader; sys.modules[spec.name]=pubmod; spec.loader.exec_module(pubmod)

class PublicationCommitTests(unittest.TestCase):
  def setUp(self):
    self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name); uid=os.getuid(); gid=os.getgid()
    self.etc=root/"etc"; self.systemd=root/"systemd"; self.release=root/"release"
    for p in (self.etc,self.systemd,self.release): p.mkdir(); p.chmod(0o750 if p==self.etc else 0o755)
    self.stage=root/"stage"; (self.stage/"etc/driftward").mkdir(parents=True); (self.stage/"units").mkdir(); (self.stage/"release").mkdir()
    (self.stage/"etc/driftward/hatchet-verifier-policy.json").write_text('{"v":1}')
    (self.stage/"etc/driftward/hatchet-routing-registry.json").write_text('{"v":1}')
    (self.stage/"units/test.service").write_text("[Service]\n")
    self.p=pubmod.Publication(etc=self.etc,systemd=self.systemd,release=self.release,targets=(str(self.etc/"hatchet-verifier-policy.json"),str(self.etc/"hatchet-routing-registry.json"),str(self.systemd/"test.service"),str(self.release/"current")),directories=(str(self.etc),str(self.systemd),str(self.release)),transients=(str(self.release/".current.new"),),uid=uid,gid=gid,state=root/"state")
    self.old=pubmod.INVENTORY; pubmod.INVENTORY={"units":["test.service"]}
  def tearDown(self): pubmod.INVENTORY=self.old; self.tmp.cleanup()
  def test_reproof_blocks_target_mutation_and_absent_race(self):
    snap=pubmod.snapshot(self.p)
    (self.etc/"hatchet-verifier-policy.json").write_text("race")
    with self.assertRaises(pubmod.PublicationError): pubmod.commit(self.p,self.stage,"a"*40,snap)
  def test_commit_uses_atomic_targets_and_removes_temp_names(self):
    snap=pubmod.snapshot(self.p); result=pubmod.commit(self.p,self.stage,"a"*40,snap)
    self.assertTrue((self.release/"current").is_symlink()); self.assertFalse((self.release/".current.new").exists())
    self.assertFalse(list(self.release.glob(".*.publish-*"))); self.assertIn(str(self.release/"current"),result["created"])
  def test_unexpected_release_or_transient_blocks(self):
    snap=pubmod.snapshot(self.p); (self.release/".current.new").symlink_to("bad")
    with self.assertRaises(pubmod.PublicationError): pubmod.commit(self.p,self.stage,"a"*40,snap)
  def test_each_journaled_interruption_rolls_back_to_exact_snapshot(self):
    for point in range(1,6):
      with self.subTest(point=point):
        self.tearDown(); self.setUp()
        snap=pubmod.snapshot(self.p)
        with self.assertRaises(pubmod.PublicationError): pubmod.commit(self.p,self.stage,"b"*40,snap,fail_after=point)
        journals=list(self.p.state.glob("publication-*.journal")); self.assertEqual(len(journals),1)
        pubmod.rollback(self.p,journals[0])
        self.assertEqual(pubmod.snapshot(self.p)["digest"],snap["digest"])
        self.assertIn('"status":"rolled_back"',journals[0].read_text())
  def test_absent_nested_directories_restore_deepest_first(self):
    self.tearDown(); self.setUp()
    nested=self.tmp.name
    import shutil
    shutil.rmtree(self.etc); shutil.rmtree(self.release)
    self.release=Path(nested)/"releases"/"bridge"
    self.p=pubmod.Publication(etc=self.etc,systemd=self.systemd,release=self.release,targets=(str(self.etc/"hatchet-verifier-policy.json"),str(self.etc/"hatchet-routing-registry.json"),str(self.systemd/"test.service"),str(self.release/"current")),directories=(str(self.etc),str(self.release.parent),str(self.release)),transients=(str(self.release/".current.new"),),uid=os.getuid(),gid=os.getgid(),state=Path(nested)/"state")
    snap=pubmod.snapshot(self.p)
    with self.assertRaises(pubmod.PublicationError): pubmod.commit(self.p,self.stage,"c"*40,snap,fail_after=5)
    journal=next(self.p.state.glob("publication-*.journal")); pubmod.rollback(self.p,journal)
    self.assertFalse(self.etc.exists()); self.assertFalse(self.release.parent.exists())
