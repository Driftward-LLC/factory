"""Fresh-local activation boundaries, without provider credentials or live effects."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
sys.path.insert(0, str(DEPLOY))
import bootstrap
import local_execution as execution
import portable_state
spec = importlib.util.spec_from_file_location('local_activation_portable', DEPLOY / 'factory-portable.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


class LocalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'
        bootstrap.init_directory(self.root, 'ghcr.io/driftward-llc/factory@sha256:' + 'a' * 64, 'factory-test', apply=True)
        self.write('config/organization.json', json.loads((DEPLOY / 'examples/organization.configured.example.json').read_text()))
        self.route = {'project_id': '44444444-4444-4444-8444-444444444444', 'repo': 'example/private', 'execution_enabled': True}
        self.write('config/routing-registry.json', {'version': 2, 'routes': [self.route]})
        self.write('config/verifier-policy.json', {'version': 1, 'policies': [{
            'id': 'host', 'repo': self.route['repo'], 'expected_base': 'main',
            'allowed_changed_paths': ['tools/'], 'forbidden_changed_paths': ['secrets/'],
            'required_local_commands': ['python3 -m unittest'], 'secret_check_command': 'git diff --check'}]})
        review = {'schema': 'factory-independent-review-policy-v1', 'enabled': True,
                  'automatic_repairs_allowed': False, 'max_passes': 2, 'repositories': [self.route['repo']]}
        self.write('config/independent-review-policy.json', review)
        (self.root / 'reviewer').mkdir(mode=0o700)
        for name in ('review-code', 'verify-app'):
            bootstrap._create_private(self.root / f'reviewer/{name}.md', name)
        self.write('reviewer/provisioning.json', {'schema': 'factory-hermes-source-reviewer-v1',
            'auth_root': '/opt/data/profiles/factory-independent-reviewer',
            'repositories': {self.route['repo']: {'base_ref': 'main'}}, 'policy_digest': execution.digest(review),
            'review_skill_digest': hashlib.sha256(b'review-code').hexdigest(),
            'verify_skill_digest': hashlib.sha256(b'verify-app').hexdigest()})
        identity = dict.fromkeys(execution.IDENTITY, 'factory-bot')
        identity['FACTORY_HERMES_ALLOWED_REPOS'] = self.route['repo']
        bootstrap._create_private(self.root / 'factory-hermes-execution.env', bootstrap.env_text(identity))
        bootstrap._create_private(self.root / 'env/hatchet-client.env', bootstrap.env_text({'HATCHET_CLIENT_TOKEN': 'fixture'}))
        self.install = portable.Installation(self.root)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def receipt(self):
        return {'schema': 1, 'approved_sha256': 'b' * 64, 'configuration': execution.hashes(self.install),
                'deployment': {name: hashlib.sha256((DEPLOY / name).read_bytes()).hexdigest() for name in execution.FILES},
                'route': self.route, 'optional_services': []}

    def enrolled(self):
        metadata = {**self.install.metadata, 'local_execution': self.receipt()}
        self.write(bootstrap.MARKER, metadata)
        return portable.Installation(self.root)

    def test_policy_scope_and_identity_cannot_expand(self):
        self.assertEqual(execution.policies(self.install), self.route)
        for change in ('route', 'identity', 'reviewer'):
            with self.subTest(change=change):
                if change == 'route':
                    self.write('config/routing-registry.json', {'version': 2, 'routes': [self.route, {**self.route, 'repo': 'example/other', 'project_id': '55555555-5555-4555-8555-555555555555'}]})
                elif change == 'identity':
                    with (self.root / execution.CONFIG[0]).open('a') as stream:
                        stream.write("PROVIDER_KEY='must-never-be-shared'\n")
                else:
                    self.write('reviewer/provisioning.json', {})
                with self.assertRaises(ValueError):
                    execution.policies(self.install)
                # Restore fixture inputs for the next independent refusal.
                if change == 'route':
                    self.write('config/routing-registry.json', {'version': 2, 'routes': [self.route]})
                elif change == 'identity':
                    path = self.root / execution.CONFIG[0]
                    path.write_text('\n'.join(line for line in path.read_text().splitlines() if not line.startswith('PROVIDER_KEY')) + '\n')

    def test_receipt_required_and_changes_refuse_start_but_allow_stop(self):
        install = self.enrolled()
        self.assertEqual(execution.validate(install)['route'], self.route)
        self.assertIn(str(DEPLOY / 'compose.local-execution.yml'), install.args)
        self.write('config/verifier-policy.json', {'version': 1, 'policies': []})
        install = portable.Installation(self.root)
        with patch.object(install, 'compose') as compose, self.assertRaisesRegex(ValueError, 'configuration changed'):
            install.up()
        compose.assert_not_called()
        # Even stale approval must not prevent an emergency stop operation.
        with patch.object(install, 'compose') as compose, patch.object(install, 'assert_owned_resources'):
            install.compose('stop')
        compose.assert_called_once_with('stop')

    def test_approved_apply_records_receipt_without_editing_dry_run_env(self):
        value = {'schema': 1, 'configuration': execution.hashes(self.install),
                 'deployment': self.receipt()['deployment'], 'route': self.route,
                 'optional_services': ['linear-webhook-proxy'], 'services': list(self.install.services)}
        original = (self.root / 'env/hatchet-workers.env').read_bytes()
        with patch.object(execution, 'preview', return_value=value), patch.object(execution, 'assert_idle'), \
                patch.object(self.install, 'compose'), patch.object(execution.os, 'chown'), patch.object(execution, 'start') as start:
            execution.activate(self.install, execution.digest(value))
        saved = portable.Installation(self.root)
        self.assertEqual(execution.validate(saved)['approved_sha256'], execution.digest(value))
        self.assertIn('linear-webhook-proxy', saved.services)
        self.assertEqual((self.root / 'env/hatchet-workers.env').read_bytes(), original)
        start.assert_called_once()

    def test_preview_changed_refuses_before_mutation(self):
        with patch.object(execution, 'preview', return_value={'schema': 1}) as preview, patch.object(self.install, 'compose') as compose:
            with self.assertRaisesRegex(ValueError, 'preview changed'):
                execution.activate(self.install, 'c' * 64)
            compose.assert_not_called()
            preview.assert_called_once()

    def test_preview_binds_runtime_configuration_and_deployment(self):
        with patch.object(self.install, 'assert_owned_resources'), patch.object(self.install, 'assert_config'), \
                patch.object(self.install, 'compose', return_value='[]'), patch.object(execution, 'assert_idle'), patch.object(execution, 'runtime', return_value={'runtime': 'first'}):
            first = execution.preview(self.install)
            with patch.object(execution, 'runtime', return_value={'runtime': 'second'}):
                second = execution.preview(self.install)
            self.assertNotEqual(execution.digest(first), execution.digest(second))
            self.assertNotIn('fixture', json.dumps(first))
            self.assertEqual(first['route'], self.route)

    def test_start_preserves_native_hold_and_never_starts_workers(self):
        install = self.enrolled()
        with patch.object(install, 'assert_owned_resources'), patch.object(install, 'assert_config'), \
                patch.object(install, 'compose', side_effect=['idle', RuntimeError('private read failed')]) as compose:
            with self.assertRaises(RuntimeError):
                execution.start(install)
            self.assertFalse(any(call.args[0] == 'up' for call in compose.call_args_list))

    def test_real_native_idle_script_refuses_holds_and_unfinished_journal(self):
        root = Path(self.tmp.name) / 'invocations'
        root.mkdir()
        script = execution.NATIVE_IDLE.replace('/opt/data/factory-invocations', str(root))
        def result():
            return subprocess.run([sys.executable, '-c', script], capture_output=True).returncode
        self.assertEqual(result(), 0)
        hold = root / 'local-admission'
        hold.mkdir()
        (hold / 'incarnation').mkdir()
        self.assertEqual(result(), 73)
        (hold / 'incarnation').rmdir()
        job = root / 'job'
        job.mkdir()
        self.assertEqual(result(), 73)
        (job / 'result.json').write_text('{}')
        self.assertEqual(result(), 0)

    def test_backup_knows_activation_assets_and_restore_never_inherits_approval(self):
        metadata = {**self.install.metadata, 'local_execution': self.receipt()}
        self.assertTrue(set(execution.CONFIG).issubset(portable_state.config_files('portable-dry-run', metadata)))
        self.assertTrue(set(execution.FILES).issubset(portable_state.deployment_files('portable-dry-run', metadata)))
        restored = {**metadata, 'local_execution_restored': True}
        restored.pop('local_execution')
        self.assertTrue(set(execution.CONFIG).issubset(portable_state.config_files('portable-dry-run', restored)))

    def test_overlay_keeps_worker_identity_public_and_socket_read_only(self):
        install = self.enrolled()
        (self.root / 'gateway').mkdir(mode=0o700)
        (self.root / 'runtime').mkdir(mode=0o755)
        config = json.loads(install.compose('config', '--format', 'json', private=True))
        worker = config['services']['bridge-executor-worker']
        self.assertEqual(worker['environment']['FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED'], '1')
        self.assertNotIn('PROVIDER_KEY', worker['environment'])
        mounts = worker['volumes']
        self.assertTrue(any(m['target'] == '/run/factory-gateway' and m['read_only'] for m in mounts))
        self.assertFalse(any(m['target'] == '/opt/data' or 'docker.sock' in m['target'] for m in mounts))
        self.assertEqual(config['services']['intake-worker']['environment']['HATCHET_INTAKE_ALLOWED_REPOS'], self.route['repo'])


if __name__ == '__main__':
    unittest.main()
