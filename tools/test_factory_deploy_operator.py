"""Audited CLI with disposable root-owned state; GitHub/Docker are explicit fake boundaries."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load('operator_test_policy', 'auto_deploy_artifact.py')
with patch.dict(sys.modules, {'auto_deploy_artifact': policy}):
    auto = load('operator_test_auto', 'factory-auto-deploy.py')
manual = load('operator_test_manual', 'factory_deploy_operator.py')
cli = load('operator_test_cli', 'factory-cli.py')
BASE, TARGET = 'a' * 40, 'b' * 40
IMAGE, NEXT_IMAGE = 'sha256:' + 'c' * 64, 'sha256:' + 'd' * 64


class DeployPolicyTests(unittest.TestCase):
    def test_help_uses_real_entrypoint_without_binding_or_root(self):
        for args in (['--help'], ['deploy', '--help'], *(['deploy', name, '--help'] for name in ('status', 'preview', 'approve', 'apply'))):
            result = subprocess.run([sys.executable, str(ROOT / 'deploy/factory-cli.py'), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('factory', result.stdout)

    def test_real_nonroot_entrypoint_refuses_operations(self):
        if os.geteuid() == 0:
            command = ['runuser', '-u', 'nobody', '--', sys.executable]
        else:
            command = [sys.executable]
        with tempfile.TemporaryDirectory() as directory:
            readable = Path(directory)
            readable.chmod(0o755)
            launcher = readable / 'factory'
            launcher.write_bytes((ROOT / 'deploy/factory-cli.py').read_bytes())
            launcher.chmod(0o644)
            result = subprocess.run([*command, str(launcher), 'deploy', 'status', '--json'],
                                    capture_output=True, text=True, timeout=10)
        diagnostic = ''.join(char if char.isprintable() else ' ' for char in result.stderr)[:512]
        self.assertEqual(result.returncode, 1, diagnostic)
        self.assertEqual(result.stdout, '')
        self.assertTrue('command refused' in result.stderr, diagnostic)

    @unittest.skipUnless(os.geteuid() == 0, 'requires root to model inaccessible checkout and run nobody')
    def test_nonroot_refusal_fixture_handles_inaccessible_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            checkout.chmod(0o700)
            (checkout / 'deploy').mkdir()
            (checkout / 'deploy/factory-cli.py').write_bytes((ROOT / 'deploy/factory-cli.py').read_bytes())
            with patch.dict(globals(), ROOT=checkout):
                self.test_real_nonroot_entrypoint_refuses_operations()

    def test_invalid_arguments_never_echo_supplied_secrets(self):
        result = subprocess.run([sys.executable, str(ROOT / 'deploy/factory-cli.py'), 'deploy', 'status', '--secret-private-token'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('secret-private-token', result.stdout + result.stderr)

    def test_fixed_policy_refuses_unknown_migrations_deletions_modes_and_self_approval(self):
        base = {'deploy/Dockerfile': {'mode': '100644', 'type': 'blob', 'sha': BASE}}
        targets = [dict(base, **{'deploy/Dockerfile': {'mode': '100644', 'type': 'blob', 'sha': TARGET}}),
                   dict(base, **{'new/migration.py': {'mode': '100644', 'type': 'blob', 'sha': TARGET}}),
                   {'factory/factory': {'mode': '120000', 'type': 'blob', 'sha': TARGET}}]
        for target in targets:
            with self.subTest(target=target), patch.dict(sys.modules, {'auto_deploy_artifact': policy}):
                changes, blocked = manual.protected_changes(auto, base, target, {})
                self.assertTrue(changes)
                self.assertTrue(blocked)
        installed = {'deploy/factory-cli.py': ROOT / 'deploy/factory-cli.py'}
        target = {'deploy/factory-cli.py': {'mode': '100644', 'type': 'blob', 'sha': manual.blob(installed['deploy/factory-cli.py'])}}
        with patch.dict(sys.modules, {'auto_deploy_artifact': policy}):
            self.assertFalse(manual.protected_changes(auto, {}, target, installed)[1])
            target['deploy/factory-cli.py']['sha'] = TARGET
            self.assertTrue(manual.protected_changes(auto, {}, target, installed)[1])

    def test_unsafe_json_and_installed_parent_paths_are_rejected(self):
        for value in ('{"phase":"complete","phase":"preview"}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                manual.strict_json(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'helper.py'
            path.write_text('raise AssertionError("never execute")')
            with self.assertRaises(cli.DeployRefusal):
                cli.installed_module('unsafe_helper', path)


class AuditedDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        actual_lstat = Path.lstat
        def fixture_lstat(path):
            info = actual_lstat(path)
            if path == self.root or self.root in path.parents:
                fields = list(info); fields[4] = 0
                return os.stat_result(fields)
            return info
        for item in (patch.object(Path, 'lstat', new=fixture_lstat), patch.object(os, 'geteuid', return_value=0)):
            item.start(); self.addCleanup(item.stop)
        self.work = self.root / 'work'; self.work.mkdir(mode=0o700)
        self.installation = self.root / 'installation'; self.installation.mkdir(mode=0o700)
        self.bundle = self.root / 'bundle'; self.bundle.mkdir(mode=0o700)
        (self.bundle / 'factory-production.py').write_text('# reviewed fixture bundle\n')
        for name in ('.env', '.factory-install.json', '.factory-cutover.json'):
            path = self.installation / name
            path.write_text('private synthetic state\n'); path.chmod(0o600)
        self.config = {'enabled': True, 'installation': str(self.installation), 'production_bundle': str(self.bundle),
                       'work_directory': str(self.work), 'compatibility': policy.compatibility_entries([])}
        self.config_path = self.root / 'auto.json'
        self.save(self.config_path, self.config)
        self.binding = {**{name: self.config[name] for name in ('installation', 'production_bundle')}, 'installation_id': 'synthetic-id'}
        self.installed = {'deploy/factory-cli.py': ROOT / 'deploy/factory-cli.py'}
        self.target = {'factory/factory': {'mode': '100755', 'type': 'blob', 'sha': TARGET}}
        self.ci = {'id': 34, 'workflow_id': 12, 'path': '.github/workflows/hatchet-quality.yml',
                   'event': 'push', 'head_branch': 'main', 'head_sha': TARGET, 'status': 'completed',
                   'conclusion': 'success', 'repository': {'full_name': auto.REPO},
                   'head_repository': {'full_name': auto.REPO}, 'run_attempt': 1, 'updated_at': 'fixed-fixture-time'}
        self.artifact = {'id': 56, 'name': 'factory-production-' + TARGET, 'expired': False, 'size_in_bytes': 100,
                         'digest': 'sha256:' + 'e' * 64, 'workflow_run': {'id': 34, 'head_sha': TARGET}}
        self.install = SimpleNamespace(directory=self.installation, metadata={'installation_id': 'synthetic-id'},
                                       project='synthetic', tenant='fixture-tenant', images={'factory': IMAGE}, service_rows=lambda: [])
        real_production = cli.production_module({'production_bundle': str(ROOT / 'deploy')})
        self.prod = SimpleNamespace(portable=real_production.portable, Production=Mock(return_value=self.install))
        self.patches = [patch.object(auto, 'CONFIG', self.config_path), patch.object(manual, 'production', return_value=self.prod),
                        patch.object(manual, 'check_idle'), patch.object(auto, 'api', side_effect=self.api),
                        patch.object(auto, 'command', side_effect=self.command), patch.object(auto.shutil, 'disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)),
                        patch.dict(sys.modules, {'auto_deploy_artifact': policy})]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def save(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def api(self, path):
        if path == 'actions/runs/34': return dict(self.ci)
        if path == 'actions/workflows/hatchet-quality.yml': return {'id': 12, 'path': '.github/workflows/hatchet-quality.yml'}
        if path == 'git/ref/heads/main': return {'object': {'sha': TARGET}}
        if path == 'actions/runs/34/artifacts?per_page=100': return {'total_count': 1, 'artifacts': [dict(self.artifact)]}
        if path.startswith('git/trees/'):
            selected = {} if path == f'git/trees/{BASE}?recursive=1' else self.target
            return {'truncated': False, 'tree': [{'path': name, **value} for name, value in selected.items()]}
        raise AssertionError(path)

    def command(self, *args, **kwargs):
        self.assertEqual(args, ('docker', 'image', 'inspect', IMAGE))
        return json.dumps([{'Id': IMAGE, 'Config': {'Labels': {'org.opencontainers.image.revision': BASE,
                           'org.opencontainers.image.source': 'https://github.com/' + auto.REPO}}}])

    def call(self, action, **options):
        args = SimpleNamespace(action=action, run=34, preview=options.get('preview'), reason='Reviewed exact synthetic CLI release', yes=True)
        if action == 'apply':
            path = self.work / 'operator-deploy' / (args.preview + '.json')
            value = manual.private_json(path)
            value['dispatch'] = {'phase': 'queued', 'deploy_run_id': 78}
            self.save(path, value)
            args.callback_run, args.callback_commit = 34, TARGET
        return manual.run(auto, self.binding, args, self.installed,
                          credentials={'username': 'fixture', 'token': 'synthetic_token'} if action == 'apply' else None)

    def preview(self):
        return self.call('preview')['preview']

    def test_ci_queue_does_not_consume_approval_and_unknown_dispatch_is_latched(self):
        identifier = self.preview(); self.call('approve', preview=identifier)
        args = SimpleNamespace(action='apply', preview=identifier, yes=True)
        original = self.api
        deploy = {'id': 78, 'workflow_id': 90, 'path': '.github/workflows/factory-deploy.yml',
                  'event': 'workflow_run', 'head_branch': 'main', 'head_sha': TARGET, 'status': 'completed',
                  'repository': {'full_name': auto.REPO}, 'head_repository': {'full_name': auto.REPO}}
        def api(path):
            if path == 'actions/workflows/factory-deploy.yml':
                return {'id': 90, 'path': '.github/workflows/factory-deploy.yml'}
            if path.startswith('actions/workflows/factory-deploy.yml/runs?'):
                return {'total_count': 1, 'workflow_runs': [deploy]}
            return original(path)
        command = self.command
        def request(*values, **kwargs):
            if values[:3] == ('gh', 'api', '--method'):
                raise RuntimeError('unknown network outcome with synthetic credential')
            return command(*values, **kwargs)
        with patch.object(auto, 'api', side_effect=api), patch.object(auto, 'command', side_effect=request), \
             patch.object(auto, 'get_image') as image:
            with self.assertRaisesRegex(manual.Refusal, 'dispatch_outcome_unknown'):
                manual.run(auto, self.binding, args, self.installed)
            image.assert_not_called()
            value = manual.private_json(self.work / 'operator-deploy' / (identifier + '.json'))
            self.assertEqual(value['phase'], 'approved')
            self.assertEqual(value['dispatch'], {'phase': 'unknown', 'deploy_run_id': 78})
            with self.assertRaisesRegex(auto.Refusal, 'prior_deployment'):
                manual.run(auto, self.binding, args, self.installed)

    def test_execution_stale_consent_never_starts_worker(self):
        self.save(self.work / 'status.json', {'phase': 'complete'})
        args = SimpleNamespace(action='resume', snapshot_digest='0' * 64, reason='reviewed', yes=True)
        snap = {'installation': str(self.installation), 'paused_execution': ['bridge-executor-worker']}
        self.install.compose = Mock()
        with patch.object(manual, 'execution_snapshot', return_value=snap):
            with self.assertRaisesRegex(manual.Refusal, 'stale_or_consent'):
                manual.execution_run(auto, self.binding, args, self.installed, Mock())
        self.install.compose.assert_not_called()

    def test_activation_exception_accepts_only_exact_reviewed_blob(self):
        name, sha = next(iter(manual.ACTIVATION_BLOBS.items()))
        target = {name: {'mode': '100644', 'type': 'blob', 'sha': sha}}
        self.assertFalse(manual.protected_changes(auto, {}, target, {})[1])
        target[name]['sha'] = TARGET
        self.assertEqual(manual.protected_changes(auto, {}, target, {})[1], [name])

    def test_preview_no_runtime_changes_and_complete_once_lifecycle(self):
        with patch.object(auto, 'get_image') as image, patch.object(auto, 'rollout') as rollout:
            identifier = self.preview()
            image.assert_not_called(); rollout.assert_not_called()
            self.assertEqual(self.call('approve', preview=identifier)['phase'], 'approved')
            path = self.work / 'operator-deploy' / (identifier + '.json')
            def import_image(*args, **kwargs):
                self.assertEqual(manual.private_json(path)['phase'], 'consumed')
                with self.assertRaisesRegex(auto.Refusal, 'prior_deployment'):
                    auto.assert_manual_idle(self.work)
                return NEXT_IMAGE
            image.side_effect = import_image
            completed = self.call('apply', preview=identifier)
            self.assertEqual(completed['phase'], 'complete')
            self.assertEqual((completed['current_image'], completed['current_commit']), (NEXT_IMAGE, TARGET))
            self.assertEqual((completed['previous_image'], completed['previous_commit']), (IMAGE, BASE))
            image.assert_called_once(); rollout.assert_called_once()
            value = manual.private_json(path)
            self.assertIn('approved_at', value['approval'])
            self.assertIn('consumed_at', value); self.assertIn('completed_at', value)
            with self.assertRaisesRegex(ValueError, 'approval_already'):
                self.call('apply', preview=identifier)
            self.assertEqual(manual.private_json(self.config_path), self.config)
            auto.assert_manual_idle(self.work)

    def test_stale_run_artifact_main_bundle_installation_and_policy_refuse(self):
        identifier = self.preview()
        for change in ('attempt', 'artifact', 'bundle', 'installation', 'target', 'helper'):
            with self.subTest(change=change):
                if change == 'attempt': self.ci['run_attempt'] += 1
                elif change == 'artifact': self.artifact['id'] += 1
                elif change == 'bundle': (self.bundle / 'new.py').write_text('# changed\n')
                elif change == 'installation': (self.installation / '.env').write_text('changed\n')
                elif change == 'target': self.target['factory/factory']['sha'] = 'f' * 40
                else: self.installed['deploy/auto_deploy_artifact.py'] = ROOT / 'deploy/auto_deploy_artifact.py'
                with self.assertRaisesRegex(ValueError, 'preview_stale'):
                    self.call('approve', preview=identifier)
                self.ci['run_attempt'] = 1; self.artifact['id'] = 56
                (self.bundle / 'new.py').unlink(missing_ok=True)
                (self.installation / '.env').write_text('private synthetic state\n')
                self.target['factory/factory']['sha'] = TARGET
                self.installed.pop('deploy/auto_deploy_artifact.py', None)
        with patch.object(auto, 'current_main', side_effect=auto.Refusal('superseded_commit')):
            with self.assertRaisesRegex(auto.Refusal, 'superseded'):
                self.call('approve', preview=identifier)

    def test_hard_block_cannot_be_approved(self):
        self.target['hatchet/bridge/hatchet_hermes_bridge/store.py'] = {'mode': '100644', 'type': 'blob', 'sha': TARGET}
        result = self.call('preview')
        self.assertFalse(result['eligible'])
        with self.assertRaisesRegex(ValueError, 'not_approvable'):
            self.call('approve', preview=result['preview'])

    def test_actual_current_to_target_downgrade_is_blocked_even_with_old_anchor(self):
        self.preview()
        current_commit = 'f' * 40
        original_api = self.api
        def api(path):
            if path == f'git/trees/{current_commit}?recursive=1':
                return {'truncated': False, 'tree': [{'path': 'hatchet/bridge/hatchet_hermes_bridge/store.py',
                        'mode': '100644', 'type': 'blob', 'sha': current_commit}]}
            return original_api(path)
        info = json.loads(self.command('docker', 'image', 'inspect', IMAGE))
        info[0]['Config']['Labels']['org.opencontainers.image.revision'] = current_commit
        with patch.object(auto, 'api', side_effect=api), patch.object(auto, 'command', return_value=json.dumps(info)):
            result = self.call('preview')
            self.assertFalse(result['eligible'])
            self.assertIn('hatchet/bridge/hatchet_hermes_bridge/store.py', result['blocked_paths'])
            with self.assertRaisesRegex(ValueError, 'not_approvable'):
                self.call('approve', preview=result['preview'])

    def test_status_uses_live_image_revision_and_catches_lost_latest_latch(self):
        identifier = self.preview()
        info = json.loads(self.command('docker', 'image', 'inspect', IMAGE))
        info[0]['Config']['Labels']['org.opencontainers.image.revision'] = TARGET
        self.install.images['factory'] = NEXT_IMAGE; info[0]['Id'] = NEXT_IMAGE
        with patch.object(auto, 'command', return_value=json.dumps(info)):
            status = self.call('status')
            self.assertEqual((status['current_image'], status['current_commit']), (NEXT_IMAGE, TARGET))
        self.install.images['factory'] = IMAGE
        directory = self.work / 'operator-deploy'
        older = manual.private_json(directory / (identifier + '.json'))
        older['snapshot']['run_attempt'] = 2
        older['digest'] = manual.digest(older['snapshot']); older['phase'] = 'failed'
        self.save(directory / (older['digest'] + '.json'), older)
        status = self.call('status')
        self.assertEqual(status['phase'], 'blocked')
        self.assertFalse(status['eligible'])
        (directory / 'latest.json').unlink()
        status = self.call('status')
        self.assertEqual(status['phase'], 'blocked')
        self.assertIn('recovery review', status['next_step'])

    def test_human_status_prints_blocked_recovery_guidance_and_enabled(self):
        result = {'phase': 'blocked', 'installation': str(self.installation), 'enabled': False,
                  'blocked_reason': 'prior_deployment_requires_operator', 'next_command': 'factory deploy status',
                  'next_step': 'Preserve the cold backup for an explicit recovery review.'}
        output = io.StringIO()
        modules = {'auto_deploy_artifact': policy, 'factory_cli_auto_deploy': auto, 'factory_cli_deploy_operator': manual}
        with patch.object(cli, 'trusted_file'), patch.object(cli, 'installed_module', side_effect=lambda name, _: modules[name]), \
             patch.object(manual, 'run', return_value=result), redirect_stdout(output):
            self.assertEqual(cli.deploy_dispatch(self.binding, ['status']), 0)
        for text in ('Deployment enabled: False', 'prior_deployment_requires_operator', 'recovery review'):
            self.assertIn(text, output.getvalue())

    def test_canonical_status_blocks_nonterminal_automatic_rollout_with_or_without_latest(self):
        identifier = self.preview(); self.call('approve', preview=identifier)
        modules = {'auto_deploy_artifact': policy, 'factory_cli_auto_deploy': auto, 'factory_cli_deploy_operator': manual}
        latest = self.work / 'operator-deploy' / 'latest.json'
        with patch.object(cli, 'checked_config', return_value=self.binding), patch.object(cli, 'trusted_file'), \
             patch.object(cli, 'installed_module', side_effect=lambda name, _: modules[name]):
            for has_latest in (True, False):
                if not has_latest:
                    latest.unlink()
                for phase in ('quiescing', 'backing_up', 'upgrading', 'starting'):
                    self.save(self.work / 'status.json', {'phase': phase})
                    with self.subTest(latest=has_latest, rollout_phase=phase):
                        for machine_output in (True, False):
                            output = io.StringIO()
                            argv = ['factory', 'deploy', 'status'] + (['--json'] if machine_output else [])
                            with patch.object(sys, 'argv', argv), redirect_stdout(output):
                                self.assertEqual(cli.main(), 0)
                            rendered = output.getvalue()
                            if machine_output:
                                result = json.loads(rendered)
                                self.assertEqual(result['phase'], 'blocked')
                                self.assertEqual(result['rollout_phase'], phase)
                                self.assertFalse(result['eligible'])
                                self.assertEqual(result['blocked_reason'], 'prior_deployment_requires_operator')
                                self.assertNotIn('apply', result['next_command'])
                                self.assertIn('recovery review', result['next_step'])
                            else:
                                self.assertIn('Phase: blocked', rendered)
                                self.assertIn('Rollout phase: ' + phase, rendered)
                                self.assertIn('recovery review', rendered)
                                self.assertNotIn('factory deploy apply', rendered)

    def test_changed_target_after_approval_cannot_import_or_consume(self):
        identifier = self.preview(); self.call('approve', preview=identifier)
        self.artifact['digest'] = 'sha256:' + 'f' * 64
        with patch.object(auto, 'get_image') as image, patch.object(auto, 'rollout') as rollout:
            with self.assertRaisesRegex(ValueError, 'preview_stale'):
                self.call('apply', preview=identifier)
            image.assert_not_called(); rollout.assert_not_called()
        self.assertEqual(self.call('status')['phase'], 'approved')

    def test_corrupt_status_and_baseline_do_not_become_approvals(self):
        identifier = self.preview()
        self.save(self.work / 'status.json', {'phase': 'invented'})
        with self.assertRaisesRegex(ValueError, 'invalid_deployment_status'):
            self.call('approve', preview=identifier)
        (self.work / 'status.json').unlink()
        path = self.work / 'operator-deploy' / 'baseline-anchor.json'
        self.save(path, {'format': 1, 'commit': BASE, 'compatibility': 'f' * 64})
        with self.assertRaisesRegex(ValueError, 'baseline_changed'):
            self.call('approve', preview=identifier)

    def test_baseline_anchor_survives_one_release_without_promotion(self):
        identifier = self.preview()
        self.call('approve', preview=identifier)
        with patch.object(auto, 'get_image', return_value=NEXT_IMAGE), patch.object(auto, 'rollout'):
            self.call('apply', preview=identifier)
        # The current image need not match the old baseline once its verified anchor is durable.
        baseline, _ = manual.anchor(auto, self.work / 'operator-deploy', self.config, TARGET)
        self.assertEqual(baseline, BASE)
        self.assertEqual(manual.private_json(self.config_path)['compatibility'], self.config['compatibility'])

    def test_apply_failure_latches_both_manual_and_automatic_paths(self):
        identifier = self.preview(); self.call('approve', preview=identifier)
        with patch.object(auto, 'get_image', side_effect=auto.Refusal('artifact_digest_mismatch')):
            with self.assertRaisesRegex(auto.Refusal, 'digest_mismatch'):
                self.call('apply', preview=identifier)
        self.assertEqual(self.call('status')['phase'], 'failed')
        for operation in (lambda: self.call('preview'), lambda: auto.assert_manual_idle(self.work)):
            with self.assertRaisesRegex((ValueError, auto.Refusal), 'prior_deployment'):
                operation()
        self.install.preflight = Mock()
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        with patch.object(auto.importlib.util, 'spec_from_file_location', return_value=spec), \
             patch.object(auto.importlib.util, 'module_from_spec', return_value=self.prod), \
             patch.object(auto, 'get_image') as image:
            with self.assertRaisesRegex(auto.Refusal, 'prior_deployment'):
                auto.deploy(self.config, 34, TARGET, {'username': 'fixture', 'token': 'synthetic_token'})
            self.install.preflight.assert_not_called()
            image.assert_not_called()

    def test_disabled_status_needs_no_journal_and_cannot_preview(self):
        self.config['enabled'] = False; self.save(self.config_path, self.config)
        self.assertFalse(self.call('status')['enabled'])
        self.assertFalse((self.work / 'operator-deploy').exists())
        with self.assertRaisesRegex(ValueError, 'automatic_deployment_disabled'):
            self.call('preview')

    def test_unsafe_records_and_pending_writes_fail_closed(self):
        identifier = self.preview()
        directory = self.work / 'operator-deploy'
        path = directory / (identifier + '.json')
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe_deployment_record'):
            self.call('status')
        path.chmod(0o600)
        os.link(path, directory / 'hardlink.json')
        with self.assertRaisesRegex(ValueError, 'unsafe_deployment_record'):
            self.call('status')
        (directory / 'hardlink.json').unlink()
        path.rename(directory / 'original.json'); path.symlink_to('original.json')
        with self.assertRaisesRegex(ValueError, 'unsafe_deployment_record'):
            self.call('status')
        path.unlink(); (directory / 'original.json').rename(path)
        (directory / 'interrupted.pending').touch(mode=0o600)
        for operation in (lambda: self.call('status'), lambda: auto.assert_manual_idle(self.work)):
            with self.assertRaisesRegex((ValueError, auto.Refusal), 'interrupted|prior_deployment'):
                operation()

    def test_missing_latest_target_is_not_idle(self):
        identifier = self.preview()
        (self.work / 'operator-deploy' / (identifier + '.json')).unlink()
        with self.assertRaisesRegex(auto.Refusal, 'unsafe_manual_deployment_journal'):
            auto.assert_manual_idle(self.work)
        with self.assertRaises(FileNotFoundError):
            self.call('status')

    def test_existing_installation_lock_serializes_preview(self):
        with self.prod.portable.installation_lock(self.installation):
            with self.assertRaisesRegex(ValueError, 'another operation'):
                self.preview()

    def test_real_root_entrypoint_json_and_sanitized_failure(self):
        modules = {'auto_deploy_artifact': policy, 'factory_cli_auto_deploy': auto, 'factory_cli_deploy_operator': manual}
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, 'checked_config', return_value=self.binding), patch.object(cli, 'trusted_file'), \
             patch.object(cli, 'installed_module', side_effect=lambda name, _: modules[name]), \
             patch.object(cli, 'DEPLOY_HELPER', ROOT / 'deploy/factory-auto-deploy.py'), \
             patch.object(cli, 'DEPLOY_POLICY', ROOT / 'deploy/auto_deploy_artifact.py'), \
             patch.object(cli, 'DEPLOY_OPERATOR', ROOT / 'deploy/factory_deploy_operator.py'), \
             patch.object(sys, 'argv', ['factory', 'deploy', 'preview', '--run', '34', '--json']), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['phase'], 'preview')
            self.assertEqual(errors.getvalue(), '')
            output.truncate(0); output.seek(0)
            with patch.object(manual, 'run', side_effect=RuntimeError('secret-token\nunsafe')):
                self.assertEqual(cli.main(), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertNotIn('secret-token', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
