import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('default_profiles', Path(__file__).resolve().parents[1] / 'deploy/hermes-default-profiles.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProfilesTest(unittest.TestCase):
    def exercise(self, existing=(), seed=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home / 'config.yaml').write_text('unchanged root configuration')
        (home / 'auth.json').write_text('synthetic-root-credential')
        names = {'default', *existing}
        modules = {
            'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: home, get_default_hermes_root=lambda: home),
            'hermes_cli': types.ModuleType('hermes_cli'),
            'hermes_cli.config': types.SimpleNamespace(read_user_config_raw=lambda _: seed or {}),
            'hermes_cli.profiles': types.SimpleNamespace(list_profile_names=lambda: list(names), get_active_profile_name=lambda: 'default', launch_model_seed=lambda value: value),
        }
        def create(args, **kwargs):
            self.assertEqual(args[1:4], ['-m', 'hermes_cli.main', 'profile'])
            self.assertNotIn('--clone', args)
            self.assertNotIn('--no-skills', args)
            names.add(args[5])
        return home, names, modules, create

    def test_create_only_missing_and_repeat_without_changes(self):
        home, names, modules, create = self.exercise(existing=['factory-executor'])
        with patch.dict(sys.modules, modules), patch.object(MODULE.subprocess, 'run', side_effect=create) as command:
            MODULE.ensure_profiles()
            MODULE.ensure_profiles()
        self.assertEqual(command.call_count, 1)
        self.assertIn('factory-independent-reviewer', names)
        self.assertEqual((home / 'auth.json').read_text(), 'synthetic-root-credential')
        self.assertEqual((home / 'config.yaml').read_text(), 'unchanged root configuration')

    def test_no_inline_credential_copy(self):
        _, _, modules, _ = self.exercise(seed={'model': {'api_key': 'synthetic-secret'}})
        with patch.dict(sys.modules, modules), patch.object(MODULE.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'inline credentials'):
                MODULE.ensure_profiles()
        command.assert_not_called()

    def test_unknown_existing_directory_is_preserved(self):
        home, _, modules, _ = self.exercise()
        existing = home / 'profiles/factory-executor'
        existing.mkdir(parents=True)
        (existing / 'user.txt').write_text('preserve')
        with patch.dict(sys.modules, modules), patch.object(MODULE.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'existing role profile'):
                MODULE.ensure_profiles()
        command.assert_not_called()
        self.assertEqual((existing / 'user.txt').read_text(), 'preserve')
