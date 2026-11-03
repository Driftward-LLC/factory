import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
spec = importlib.util.spec_from_file_location('provision_observer', ROOT / 'deploy/provision-native-observer.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
sys.path.insert(0, str(ROOT / 'deploy/independent-reviewer'))
import native_agent


class ObserverProvisionTests(unittest.TestCase):
    def fixture(self, root):
        root.chmod(0o700)
        for name, model in zip(observer.ROLES, ('gpt-6.1-sol', 'gpt-6-astra')):
            profile = root / 'profiles' / name
            profile.mkdir(parents=True, mode=0o700)
            config = {'model': {'default': model, 'provider': 'openai-codex', 'extra': 'preserve'},
                      'plugins': {'enabled': ['engineering'] if model.endswith('sol') else []},
                      'mcp_servers': {'factory-composio': {'url': 'https://example.invalid', 'headers': {'secret': '${UNCHANGED}'}}} if model.endswith('sol') else {},
                      'tools': {'connectors': {'enabled': False}}, 'terminal': {'backend': 'local'}}
            (profile / 'config.yaml').write_text(yaml.safe_dump(config))
            (profile / 'auth.json').write_text('private-auth-' + name)
            (profile / '.env').write_text('# retain formatting\nUNRELATED="private-marker"\n')
            (profile / '.env').chmod(0o600)
        source = root / 'collector.env'
        source.write_text("POSTHOG_PROJECT_TOKEN='phc_synthetic_fixture'\nPOSTHOG_TRACES_ENDPOINT='https://us.i.posthog.com/i/v1/traces'\n")
        source.chmod(0o600)
        return source

    def test_preview_apply_preserves_capabilities_models_auth_reviewer_and_unrelated_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            preview = observer.provision(root, source)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            result = observer.provision(root, source, expected_digest=preview['snapshot_digest'])
            self.assertTrue(result['applied'])
            self.assertNotIn('phc_synthetic_fixture', json.dumps(result))
            self.assertNotIn('private-auth', json.dumps(result))
            for name in observer.ROLES:
                profile = root / 'profiles' / name
                old = yaml.safe_load(before[str((profile / 'config.yaml').relative_to(root))])
                current = yaml.safe_load((profile / 'config.yaml').read_bytes())
                if name == observer.ROLES[0]:
                    old['plugins']['enabled'].append(observer.PLUGIN)
                self.assertEqual(current, old)
                self.assertEqual((profile / 'auth.json').read_bytes(), before[str((profile / 'auth.json').relative_to(root))])
                self.assertTrue((profile / '.env').read_bytes().startswith(before[str((profile / '.env').relative_to(root))]))
                self.assertEqual((profile / '.env').stat().st_mode & 0o777, 0o600)
                self.assertIn(b"POSTHOG_HOST='https://us.i.posthog.com'", (profile / '.env').read_bytes())
            self.assertNotIn('UNRELATED', os.environ)

    def test_stale_preview_refuses_all_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            preview = observer.provision(root, source)
            auth = root / 'profiles' / observer.ROLES[1] / 'auth.json'
            auth.write_text('refreshed independently')
            with self.assertRaisesRegex(ValueError, 'preview changed'):
                observer.provision(root, source, expected_digest=preview['snapshot_digest'])
            self.assertFalse((root / 'profiles' / observer.ROLES[0] / 'plugins').exists())

    def test_conflict_and_symlink_refuse_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            env = root / 'profiles' / observer.ROLES[0] / '.env'
            env.write_text('export POSTHOG_HOST=https://attacker.invalid\n')
            with self.assertRaisesRegex(ValueError, 'already configured'):
                observer.provision(root, source)
            env.unlink()
            env.symlink_to(source)
            with self.assertRaisesRegex(ValueError, 'symlinked'):
                observer.provision(root, source)

    def test_only_approved_ingestion_origins_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            for endpoint in ('https://attacker.invalid/i/v1/traces', 'http://us.i.posthog.com/i/v1/traces', 'https://us.i.posthog.com/i/v1/traces?secret=x'):
                source.write_text("POSTHOG_PROJECT_TOKEN='phc_synthetic_fixture'\nPOSTHOG_TRACES_ENDPOINT='" + endpoint + "'\n")
                with self.assertRaises(ValueError):
                    observer.provision(root, source)

    def test_reviewer_env_is_private_and_does_not_interpolate_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / '.env'
            path.write_text('POSTHOG_API_KEY=${PRIVATE_KEY}\nUNRELATED=private\n')
            path.chmod(0o600)
            parser = Mock(return_value={'POSTHOG_API_KEY': '${PRIVATE_KEY}', 'UNRELATED': 'private'})
            with patch.object(native_agent, 'validated_auth_root', side_effect=lambda value: Path(value)), patch.dict(os.environ, {'PRIVATE_KEY': 'do-not-read'}), patch.dict(sys.modules, {'dotenv': SimpleNamespace(dotenv_values=parser)}):
                self.assertEqual(native_agent.observer_config(root)['POSTHOG_API_KEY'], '${PRIVATE_KEY}')
                parser.assert_called_once_with(path, interpolate=False)
                self.assertNotIn('UNRELATED', native_agent.observer_config(root))
                path.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'not private'):
                    native_agent.observer_config(root)


if __name__ == '__main__':
    unittest.main()
