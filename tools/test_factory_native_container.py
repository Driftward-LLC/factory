"""Seed admission keeps historical state and unsafe filesystem objects out."""
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'deploy/experimental-native-executor/container.py'


class NativeContainerSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        tree = ast.parse(SOURCE.read_text())
        tree.body = [node for node in tree.body if not isinstance(node, ast.Try)]
        self.runtime = {}
        exec(compile(tree, str(SOURCE), 'exec'), self.runtime)
        self.seed = self.root / 'seed'
        self.seed.mkdir(mode=0o700)
        self.home = self.root / 'home'
        self.runtime.update(HOME=self.home, SEED=self.seed)
        self.original_lstat = Path.lstat
        def executor_stat(file):
            result = list(self.original_lstat(file))
            # CI fixtures use its runner UID; model the container's provisioned
            # UID without requiring root/chown or relaxing production admission.
            if result[4] == os.getuid():
                result[4] = 10000
            return os.stat_result(result)
        patch = mock.patch.object(Path, 'lstat', executor_stat)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, content='public fixture'):
        file = self.seed / name
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file.write_text(content)
        file.chmod(0o600)
        return file

    def test_only_declared_inputs_are_copied_into_single_use_private_home(self):
        self.write('config.yaml')
        self.write('.env', '')
        self.write('skills/driftward-engineering/implement-change/SKILL.md')
        self.runtime['prepare']()
        self.assertEqual((self.home / 'config.yaml').read_text(), 'public fixture')
        self.assertEqual((self.home / '.env').stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / 'workspace').is_dir())
        self.assertTrue((self.home / 'ready').is_file())
        with self.assertRaises(FileExistsError):
            self.runtime['prepare']()

    def test_unapproved_seed_owner_is_refused(self):
        file = self.write('config.yaml')
        info = list(self.original_lstat(file))
        info[4] = 12345
        with mock.patch.object(Path, 'lstat', return_value=os.stat_result(info)):
            with self.assertRaises(ValueError):
                self.runtime['prepare']()
        self.assertFalse(self.home.exists())

    def test_session_checkpoint_auth_and_hook_inputs_are_refused(self):
        for name in ['auth.json', 'state.db', 'processes.json', 'factory-native-owner.claim', 'hooks', 'plugins']:
            file = self.write(name)
            with self.assertRaises(ValueError):
                self.runtime['prepare']()
            self.assertFalse(self.home.exists())
            file.unlink()

    def test_symlinks_hardlinks_special_files_and_writable_inputs_are_refused(self):
        file = self.write('config.yaml')
        file.chmod(0o666)
        with self.assertRaises(ValueError):
            self.runtime['prepare']()
        file.unlink()
        file.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            self.runtime['prepare']()
        file.unlink()
        os.mkfifo(file)
        with self.assertRaises(ValueError):
            self.runtime['prepare']()
        file.unlink()
        target = self.root / 'outside'
        target.write_text('public')
        os.link(target, file)
        with self.assertRaises(ValueError):
            self.runtime['prepare']()
        self.assertFalse(self.home.exists())


if __name__ == '__main__':
    unittest.main()
