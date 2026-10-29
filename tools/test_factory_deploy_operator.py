"""Audited CLI with disposable root-owned state; GitHub/Docker are explicit fake boundaries."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import hashlib
import io
import json
import os
import sqlite3
import stat
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
    def test_release_selection_is_exact_and_excludes_legacy_run_selector(self):
        parsed = cli.deploy_parser(['preview', '--release', 'v2026.10.29', '--json'])
        self.assertEqual(parsed.release, 'v2026.10.29')
        self.assertIsNone(parsed.run)
        for arguments in (['preview'], ['preview', '--release', 'latest'],
                          ['preview', '--release', '../../main'],
                          ['preview', '--release', 'v2026.10.29', '--run', '34']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.deploy_parser(arguments)

    def test_help_uses_real_entrypoint_without_binding_or_root(self):
        for args in (['--help'], ['deploy', '--help'], ['execution', 'configure', '--help'], *(['deploy', name, '--help'] for name in ('status', 'preview', 'approve', 'apply'))):
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
        fixture_tenant = 'fixture-' + self.root.name
        tenant_lock = Path('/tmp') / ('.factory-tenant-' + fixture_tenant + '.factory-lock')
        self.addCleanup(lambda: tenant_lock.unlink(missing_ok=True))
        actual_lstat = Path.lstat
        def fixture_lstat(path):
            info = actual_lstat(path)
            if path == self.root or self.root in path.parents:
                fields = list(info); fields[4] = 0
                return os.stat_result(fields)
            return info
        actual_fstat = os.fstat
        def fixture_fstat(fd):
            info = actual_fstat(fd)
            path = Path(os.readlink('/proc/self/fd/' + str(fd)))
            if self.root in path.parents or path == tenant_lock:
                fields = list(info); fields[4] = 0
                return os.stat_result(fields)
            return info
        for item in (patch.object(Path, 'lstat', new=fixture_lstat), patch.object(os, 'fstat', new=fixture_fstat),
                     patch.object(os, 'geteuid', return_value=0)):
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
                                       project='synthetic', tenant=fixture_tenant, images={'factory': IMAGE}, service_rows=lambda: [])
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

    def baseline_fixture(self):
        self.installed = {name: ROOT / name for name in manual.INSTALLED_MODES}
        self.target = {name: {'mode': manual.INSTALLED_MODES[name], 'type': 'blob',
                             'sha': manual.blob(path)} for name, path in self.installed.items()}
        directory = manual.records_directory(self.config)
        self.save(directory / 'baseline-anchor.json', {'format': 1, 'commit': BASE,
                  'compatibility': self.config['compatibility']})
        snap = {'commit': TARGET, 'run_id': 34, 'run_attempt': 1, 'artifact': self.artifact}
        identifier = manual.digest(snap)
        self.save(directory / (identifier + '.json'), {'format': 1, 'digest': identifier,
                  'phase': 'complete', 'snapshot': snap, 'result': {'image': IMAGE, 'commit': TARGET}})
        self.save(directory / 'latest.json', {'digest': identifier, 'updated_at': 'fixture'})
        self.save(self.work / 'status.json', {'phase': 'complete'})
        identity = patch.object(manual, 'current_identity', return_value={'current_image': IMAGE, 'current_commit': TARGET})
        identity.start(); self.addCleanup(identity.stop)
        paused = patch.object(auto, 'paused_execution', return_value=['bridge-executor-worker'])
        paused.start(); self.addCleanup(paused.stop)
        flags = patch.object(manual, 'execution_flags', return_value={'execution_enabled': False,
                             'independent_reviewer_enabled': False})
        flags.start(); self.addCleanup(flags.stop)
        return directory

    def baseline(self, action, identifier=None):
        return manual.baseline_run(auto, self.binding, SimpleNamespace(baseline_action=action,
            run=34, preview=identifier, yes=True, reason='Reviewed protected source and installed helpers'), self.installed)

    def test_baseline_adoption_exact_source_durable_schema_and_preserved_execution(self):
        directory = self.baseline_fixture()
        latest = (directory / 'latest.json').read_bytes()
        files = {name: (self.installation / name).read_bytes() for name in ('.env', '.factory-install.json', '.factory-cutover.json')}
        view = self.baseline('preview')
        result = self.baseline('apply', view['preview'])
        config = manual.config_for(auto, self.binding)
        self.assertEqual(config['compatibility'], result['compatibility'])
        self.assertEqual(manual.anchor(auto, directory, config, TARGET)[0], TARGET)
        auto.assert_manual_idle(self.work)
        self.assertEqual((directory / 'latest.json').read_bytes(), latest)
        self.assertEqual(files, {name: (self.installation / name).read_bytes() for name in files})
        self.assertEqual(view['paused_execution'], ['bridge-executor-worker'])

    def test_baseline_helper_mismatch_unchanged_source_and_modes_are_refused(self):
        self.baseline_fixture()
        for change in ({'sha': BASE}, {'mode': '120000'}, {'mode': '100755'}, {'type': 'tree'}):
            name = 'deploy/factory-cli.py'
            original = self.target[name]
            self.target[name] = {**original, **change}
            with self.subTest(change=change), self.assertRaisesRegex(manual.Refusal, 'helpers_differ'):
                self.baseline('preview')
            self.target[name] = original

    def test_baseline_staleness_and_partial_write_fence(self):
        directory = self.baseline_fixture()
        view = self.baseline('preview')
        self.ci['updated_at'] = 'changed'
        with self.assertRaisesRegex(manual.Refusal, 'stale'):
            self.baseline('apply', view['preview'])
        self.ci['updated_at'] = 'fixed-fixture-time'
        original = manual.write
        def fail_anchor(auto, path, value):
            if path.name == 'baseline-anchor.json':
                raise OSError('synthetic interrupted anchor')
            return original(auto, path, value)
        with patch.object(manual, 'write', side_effect=fail_anchor), self.assertRaises(OSError):
            self.baseline('apply', view['preview'])
        self.assertNotEqual(manual.config_for(auto, self.binding)['compatibility'], self.config['compatibility'])
        with self.assertRaisesRegex(auto.Refusal, 'prior_deployment_requires_operator'):
            auto.assert_manual_idle(self.work)
        self.assertEqual(manual.private_json(directory / (view['preview'] + '.json'))['phase'], 'failed')

    def test_baseline_canonical_cli_plain_and_json_preview_and_apply(self):
        self.baseline_fixture()
        self.binding.update(profile_runtime=str(ROOT / 'deploy/factory-profile-runtime.py'),
                            artifact_runtime=str(ROOT / 'deploy/factory-artifact-runtime.py'))
        modules = {'auto_deploy_artifact': policy, 'factory_cli_auto_deploy': auto, 'factory_cli_deploy_operator': manual}
        with patch.object(cli, 'trusted_file'), patch.object(cli, 'checked_hermes_wrapper', side_effect=Path), \
             patch.object(cli, 'installed_module', side_effect=lambda name, _: modules[name]), \
             patch.object(cli, 'DEPLOY_HELPER', ROOT / 'deploy/factory-auto-deploy.py'), \
             patch.object(cli, 'DEPLOY_POLICY', ROOT / 'deploy/auto_deploy_artifact.py'), \
             patch.object(cli, 'DEPLOY_OPERATOR', ROOT / 'deploy/factory_deploy_operator.py'):
            for machine in (False, True):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.deploy_dispatch(self.binding, ['baseline', 'preview', '--run', '34'] + (['--json'] if machine else [])), 0)
                if machine:
                    view = json.loads(output.getvalue())
                else:
                    self.assertIn('Compatibility baseline: preview only', output.getvalue())
                    self.assertIn('Protected fingerprint:', output.getvalue())
            args = ['baseline', 'apply', '--preview', view['preview'], '--reason', 'Reviewed exact source', '--yes']
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.deploy_dispatch(self.binding, args), 0)
            self.assertIn('Compatibility baseline: adopted', output.getvalue())
            complete = {'phase': 'complete', 'baseline_adoption': True, 'commit': TARGET,
                        'compatibility': view['target_compatibility'], 'next_command': 'factory deploy status --json'}
            output = io.StringIO()
            with patch.object(manual, 'baseline_run', return_value=complete), redirect_stdout(output):
                self.assertEqual(cli.deploy_dispatch(self.binding, args + ['--json']), 0)
            self.assertEqual(json.loads(output.getvalue()), complete)

    def test_baseline_preview_positive_bounded_run_validation(self):
        for run in ('0', '-1', str(10**20)):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.deploy_parser(['baseline', 'preview', '--run', run])

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

    def published_release(self):
        fingerprint = manual.tree(auto, TARGET)[1]
        self.release = {'id': 78, 'tag': 'v2026.10.29', 'published_at': 'fixed', 'tag_object': 'e' * 40,
            'asset': {'id': 56, 'name': 'deployment.json', 'size': 512, 'digest': 'sha256:' + 'f' * 64},
            'manifest': {'format': 3, 'release_tag': 'v2026.10.29', 'commit': TARGET,
                         'image': auto.RELEASE_IMAGE + '@' + NEXT_IMAGE, 'image_config': NEXT_IMAGE,
                         'compatibility': fingerprint, 'run_id': 34, 'run_attempt': 1},
            'run_updated_at': 'fixed'}
        item = patch.object(auto, 'release_metadata', side_effect=lambda _: json.loads(json.dumps(self.release)))
        item.start(); self.addCleanup(item.stop)
        item = patch.object(auto, 'current_main', side_effect=AssertionError('A published release may precede main'))
        item.start(); self.addCleanup(item.stop)

    def release_call(self, action, identifier=None):
        return manual.run(auto, self.binding, SimpleNamespace(action=action, run=None, release='v2026.10.29',
                          preview=identifier, reason='Reviewed published release', yes=True), self.installed)

    def test_published_release_applies_locally_without_dispatch_credentials_or_current_main(self):
        self.published_release()
        with patch.object(auto, 'get_release_image', return_value=NEXT_IMAGE) as image, \
             patch.object(auto, 'rollout') as rollout, patch.object(manual, 'queue_deployment') as queue:
            preview = self.release_call('preview')
            self.assertEqual(preview['release'], 'v2026.10.29')
            identifier = preview['preview']
            image.assert_not_called(); rollout.assert_not_called()
            self.release_call('approve', identifier)
            def pull(*args):
                path = self.work / 'operator-deploy' / (identifier + '.json')
                self.assertEqual(manual.private_json(path)['phase'], 'consumed')
                return NEXT_IMAGE
            image.side_effect = pull
            completed = self.release_call('apply', identifier)
            self.assertEqual(completed['phase'], 'complete')
            self.assertEqual(completed['current_image'], NEXT_IMAGE)
            self.assertEqual(rollout.call_args.kwargs, {'release': self.release})
            self.assertEqual(image.call_args.args[1]['compatibility'], self.release['manifest']['compatibility'])
            queue.assert_not_called()
            self.assertEqual(manual.private_json(self.config_path), self.config)
            with self.assertRaisesRegex(ValueError, 'approval_already'):
                self.release_call('apply', identifier)

    def test_changed_published_release_cannot_consume_approval_or_pull(self):
        self.published_release()
        identifier = self.release_call('preview')['preview']
        self.release_call('approve', identifier)
        self.release['asset']['digest'] = 'sha256:' + 'a' * 64
        with patch.object(auto, 'get_release_image') as image, patch.object(auto, 'rollout') as rollout:
            with self.assertRaisesRegex(ValueError, 'preview_stale'):
                self.release_call('apply', identifier)
            image.assert_not_called(); rollout.assert_not_called()
        self.assertEqual(self.release_call('status')['phase'], 'approved')

    def test_failed_public_pull_latches_approval_without_stopping_services(self):
        self.published_release()
        identifier = self.release_call('preview')['preview']
        self.release_call('approve', identifier)
        with patch.object(auto, 'get_release_image', side_effect=auto.Refusal('docker_failed')), \
             patch.object(auto, 'rollout') as rollout, self.assertRaisesRegex(auto.Refusal, 'docker_failed'):
            self.release_call('apply', identifier)
        rollout.assert_not_called()
        self.assertEqual(self.release_call('status')['phase'], 'failed')
        with self.assertRaisesRegex(auto.Refusal, 'prior_deployment'):
            auto.assert_manual_idle(self.work)

    def test_release_baseline_adoption_keeps_exact_completed_release_evidence(self):
        directory = self.baseline_fixture()
        self.published_release()
        latest = manual.private_json(directory / 'latest.json')
        receipt = manual.read_preview(auto, directory, latest['digest'])
        receipt['snapshot'].update(release=self.release, artifact=self.release['asset'])
        identifier = manual.digest(receipt['snapshot'])
        receipt['digest'] = identifier
        self.save(directory / (identifier + '.json'), receipt)
        self.save(directory / 'latest.json', {'digest': identifier, 'updated_at': 'fixture'})
        view = self.baseline('preview')
        self.assertEqual(view['release'], self.release)
        self.release['asset']['id'] = 99
        with self.assertRaisesRegex(auto.Refusal, 'release_changed'):
            self.baseline('apply', view['preview'])

    def test_execution_snapshot_binds_release_receipt_without_legacy_ci_lookup(self):
        self.published_release()
        self.save(self.work / 'status.json', {'phase': 'complete', 'image': NEXT_IMAGE, 'commit': TARGET,
                                             'run_id': 34, 'release': self.release})
        source = 'synthetic guarded source'
        raw = (source + '\n').encode()
        guard = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        tree = {manual.GUARDED_SOURCE: {'mode': '100644', 'type': 'blob', 'sha': guard}}
        readiness = Mock(return_value={'reviewer_routed': True, 'gateway_executor_profile_selected': True,
            'native_runtime_idle': True, 'executor_auth_ready': True, 'gateway_socket_listening': True,
            'gateway_service': {'ActiveState': 'active'}, 'profiles': {
                'executor': {'engineering_enabled': True, 'capabilities_ready': True, 'capabilities_match_default': True},
                'reviewer': {'auth_present': True}}})
        with patch.object(manual, 'current_identity', return_value={'current_image': NEXT_IMAGE, 'current_commit': TARGET}), \
             patch.object(manual, 'tree', return_value=(tree, 'fixture')), patch.object(manual, 'GUARDED_BLOB', guard), \
             patch.object(manual, 'execution_flags', return_value={'execution_enabled': True, 'independent_reviewer_enabled': True}), \
             patch.object(auto, 'command', return_value=source) as command, \
             patch.object(auto, 'api', side_effect=AssertionError('No legacy CI lookup')):
            snap = manual.execution_snapshot(auto, self.binding, self.config, self.install, self.installed, readiness)
            self.assertEqual(snap['release'], self.release)
            self.release['asset']['id'] = 99
            command.reset_mock(); readiness.reset_mock()
            with self.assertRaisesRegex(auto.Refusal, 'release_changed'):
                manual.execution_snapshot(auto, self.binding, self.config, self.install, self.installed, readiness)
            command.assert_not_called(); readiness.assert_not_called()

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

    def configuration_fixture(self, flag=b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='0'\n"):
        self.save(self.work / 'status.json', {'phase': 'complete'})
        directory = self.installation / 'env'; directory.mkdir(mode=0o700)
        self.reviewer_env = directory / 'hatchet-worker-bridge-executor.env'
        self.reviewer_env.write_bytes(b"PRIVATE_FIXTURE='retain byte for byte'\r\n" + flag)
        self.reviewer_env.chmod(0o600)
        self.other_env = directory / 'hatchet-verifier.env'
        self.other_env.write_bytes(b"UNRELATED_FIXTURE='unchanged'\n"); self.other_env.chmod(0o600)
        self.prod.portable.bootstrap = cli.production_module({'production_bundle': str(ROOT / 'deploy')}).portable.bootstrap
        self.unresolved = {'services': {'bridge-executor-worker': {
            'env_file': [{'path': str(self.reviewer_env)}, {'path': str(self.other_env)}], 'environment': {}}}}
        def compose(*args, **kwargs):
            self.assertEqual(args[0], 'config')  # Configuration never starts/stops a service.
            if '--no-env-resolution' in args:
                return json.dumps(self.unresolved)
            values = self.prod.portable.bootstrap.read_env(self.reviewer_env)
            return json.dumps({'services': {'bridge-executor-worker': {'environment': {
                'DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED': '1', **values}}}})
        self.install.compose = Mock(side_effect=compose)
        snap = {'installation': str(self.installation), 'paused_execution': ['bridge-executor-worker']}
        item = patch.object(manual, 'execution_snapshot', return_value=snap)
        item.start(); self.addCleanup(item.stop)
        return self.reviewer_env.read_bytes()

    def configure(self, action, **values):
        args = SimpleNamespace(action='configure', configure_action=action, snapshot_digest=None,
                               reason=None, yes=False)
        args.__dict__.update(values)
        return manual.execution_run(auto, self.binding, args, self.installed, Mock())

    def test_configure_preview_apply_preserves_private_bytes_and_never_starts(self):
        for flag in (b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='0'\n", b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='false'\r\n", b''):
            with self.subTest(flag=flag):
                # Each subcase gets its own existing installation and audit state.
                if hasattr(self, 'reviewer_env'):
                    self.reviewer_env.write_bytes(b"PRIVATE_FIXTURE='retain byte for byte'\r\n" + flag)
                    receipt = self.work / 'operator-execution-configure/receipt.json'
                    receipt.unlink()
                    receipt.parent.rmdir()
                else:
                    self.configuration_fixture(flag)
                before = self.reviewer_env.read_bytes()
                owner = self.reviewer_env.stat()
                view = self.configure('preview')
                self.assertEqual(self.reviewer_env.read_bytes(), before)
                self.assertNotIn('PRIVATE_FIXTURE', json.dumps(view))
                result = self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)
                self.assertEqual(result['phase'], 'complete')
                after = self.reviewer_env.read_bytes()
                expected = before.replace(flag, flag.replace(b"'0'", b"'1'").replace(b"'false'", b"'1'")) if flag else before + b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='1'\n"
                self.assertEqual(after, expected)
                self.assertEqual(self.reviewer_env.stat().st_mode & 0o777, 0o600)
                self.assertEqual((self.reviewer_env.stat().st_uid, self.reviewer_env.stat().st_gid), (owner.st_uid, owner.st_gid))
                self.assertEqual(self.other_env.read_bytes(), b"UNRELATED_FIXTURE='unchanged'\n")
                auto.assert_manual_idle(self.work)
                with self.assertRaisesRegex(manual.Refusal, 'already_enabled'):
                    self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)

    def test_configure_parser_requires_fixed_subcommand(self):
        self.assertEqual(cli.execution_parser(['configure', 'preview', '--json']).configure_action, 'preview')
        for argv in (['configure'], ['configure', 'enable'], ['resume', 'apply']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.execution_parser(argv)

    def test_configure_private_environment_pending_and_shared_sources_refuse(self):
        before = self.configuration_fixture()
        self.reviewer_env.chmod(0o640)
        with self.assertRaisesRegex(manual.Refusal, 'unsafe_execution_environment'):
            self.configure('preview')
        self.reviewer_env.chmod(0o600)
        pending = self.reviewer_env.with_suffix('.pending'); pending.touch(mode=0o600)
        with self.assertRaisesRegex(manual.Refusal, 'interrupted_execution_configuration'):
            self.configure('preview')
        pending.unlink()
        self.unresolved['services']['unexpected'] = {'env_file': [{'path': str(self.reviewer_env)}]}
        with self.assertRaisesRegex(manual.Refusal, 'shared_environment'):
            self.configure('preview')
        self.assertEqual(self.reviewer_env.read_bytes(), before)

    def test_configure_preserves_rollout_guard_and_role_readiness_refusals(self):
        before = self.configuration_fixture()
        for reason in ('terminal_inspection_guard_unverified', 'execution_roles_not_ready'):
            with patch.object(manual, 'execution_snapshot', side_effect=manual.Refusal(reason)) as snap:
                with self.assertRaisesRegex(manual.Refusal, reason): self.configure('preview')
                self.assertTrue(snap.call_args.kwargs['configuring'])
        self.save(self.work / 'status.json', {'phase': 'upgrading'})
        with self.assertRaisesRegex(manual.Refusal, 'deployment_not_complete'): self.configure('preview')
        self.assertEqual(self.reviewer_env.read_bytes(), before)

    def test_configure_stale_file_duplicate_and_override_refuse(self):
        before = self.configuration_fixture()
        view = self.configure('preview')
        self.reviewer_env.write_bytes(before + b"NEW_FIXTURE='different'\n")
        with self.assertRaisesRegex(manual.Refusal, 'stale_or_consent'):
            self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)
        self.assertFalse((self.work / 'operator-execution-configure').exists())
        self.reviewer_env.write_bytes(before + b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='0'\n")
        with self.assertRaises(ValueError): self.configure('preview')
        self.reviewer_env.write_bytes(before)
        self.unresolved['services']['bridge-executor-worker']['environment']['DRIFTWARD_FACTORY_REVIEWER_ENABLED'] = '0'
        with self.assertRaisesRegex(manual.Refusal, 'overridden'): self.configure('preview')
        self.unresolved['services']['bridge-executor-worker']['environment'].clear()
        self.other_env.write_bytes(b"DRIFTWARD_FACTORY_REVIEWER_ENABLED='0'\n")
        with self.assertRaisesRegex(manual.Refusal, 'overridden'): self.configure('preview')

    def test_configure_atomic_failure_latches_configure_resume_and_deploy(self):
        before = self.configuration_fixture()
        view = self.configure('preview')
        with patch.object(os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)
        self.assertEqual(self.reviewer_env.read_bytes(), before)
        with self.assertRaisesRegex(auto.Refusal, 'configuration_requires_operator'):
            self.configure('preview')
        args = SimpleNamespace(action='resume')
        with self.assertRaisesRegex(auto.Refusal, 'configuration_requires_operator'):
            manual.execution_run(auto, self.binding, args, self.installed, Mock())
        with self.assertRaisesRegex(auto.Refusal, 'configuration_requires_operator'):
            auto.assert_manual_idle(self.work)

    def test_configure_effective_mismatch_latches_after_mutation(self):
        self.configuration_fixture()
        view = self.configure('preview')
        original = self.install.compose.side_effect
        def mismatch(*args, **kwargs):
            value = json.loads(original(*args, **kwargs))
            if b"ENABLED='1'" in self.reviewer_env.read_bytes():
                value['services']['unexpected-service'] = {}
            return json.dumps(value)
        self.install.compose.side_effect = mismatch
        with self.assertRaisesRegex(manual.Refusal, 'effective_mismatch'):
            self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)
        self.assertIn(b"ENABLED='1'", self.reviewer_env.read_bytes())
        self.assertEqual(manual.private_json(self.work / 'operator-execution-configure/receipt.json')['phase'], 'failed')
        with self.assertRaisesRegex(auto.Refusal, 'configuration_requires_operator'):
            auto.assert_manual_idle(self.work)

    def test_configure_interruption_after_replace_retains_consumed_latch(self):
        self.configuration_fixture()
        view = self.configure('preview')
        replace = os.replace
        def interrupted(source, target):
            if target == self.reviewer_env:
                self.assertEqual(manual.private_json(self.work / 'operator-execution-configure/receipt.json')['phase'], 'consumed')
                replace(source, target)
                raise SystemExit('simulated interruption')
            return replace(source, target)
        with patch.object(os, 'replace', side_effect=interrupted), self.assertRaises(SystemExit):
            self.configure('apply', snapshot_digest=view['snapshot_digest'], reason='reviewed', yes=True)
        self.assertIn(b"ENABLED='1'", self.reviewer_env.read_bytes())
        self.assertEqual(manual.private_json(self.work / 'operator-execution-configure/receipt.json')['phase'], 'consumed')
        with self.assertRaisesRegex(auto.Refusal, 'configuration_requires_operator'):
            auto.assert_manual_idle(self.work)

    def test_guard_source_mismatch_is_refused_before_native_readiness(self):
        self.save(self.work / 'status.json', {'phase': 'complete', 'image': IMAGE, 'commit': TARGET, 'run_id': 34})
        tree = {manual.GUARDED_SOURCE: {'mode': '100644', 'type': 'blob', 'sha': manual.GUARDED_BLOB}}
        readiness = Mock()
        with patch.object(manual, 'current_identity', return_value={'current_image': IMAGE, 'current_commit': TARGET}), \
             patch.object(manual, 'tree', return_value=(tree, 'fixture')), \
             patch.object(auto, 'command', return_value='unguarded image source'):
            with self.assertRaisesRegex(manual.Refusal, 'terminal_inspection_guard_unverified'):
                manual.execution_snapshot(auto, self.binding, self.config, self.install, self.installed, readiness)
        readiness.assert_not_called()

    def test_record_directory_parent_is_synced_on_creation_and_recovery_but_not_status(self):
        with patch.object(manual, 'sync_parent') as sync:
            directory = manual.records_directory(self.config)
            manual.records_directory(self.config)
            self.assertEqual(sync.call_args_list, [((directory,),), ((directory,),)])
            sync.reset_mock()
            manual.records_directory(self.config, create=False)
            sync.assert_not_called()

    def test_execution_parent_sync_failure_refuses_before_receipt_or_start(self):
        self.save(self.work / 'status.json', {'phase': 'complete'})
        snap = {'installation': str(self.installation), 'paused_execution': ['bridge-executor-worker']}
        args = SimpleNamespace(action='resume', snapshot_digest=manual.digest(snap), reason='reviewed', yes=True)
        self.install.compose = Mock()
        with patch.object(manual, 'execution_snapshot', return_value=snap), \
             patch.object(manual, 'sync_parent', side_effect=OSError('synthetic sync failure')):
            for _ in range(2):
                with self.assertRaises(OSError):
                    manual.execution_run(auto, self.binding, args, self.installed, Mock())
        self.assertFalse((self.work / 'operator-execution' / 'receipt.json').exists())
        self.install.compose.assert_not_called()

    def test_fast_ci_callback_can_finish_before_dispatch_returns_without_lost_receipt(self):
        identifier = self.preview(); self.call('approve', preview=identifier)
        args = SimpleNamespace(action='apply', preview=identifier, yes=True)
        original_api, command = self.api, self.command
        def api(path):
            if path == 'actions/workflows/factory-deploy.yml':
                return {'id': 90, 'path': '.github/workflows/factory-deploy.yml'}
            if path.startswith('actions/workflows/factory-deploy.yml/runs?'):
                return {'total_count': 1, 'workflow_runs': [{'id': 78, 'workflow_id': 90,
                    'path': '.github/workflows/factory-deploy.yml', 'event': 'workflow_run',
                    'head_branch': 'main', 'head_sha': TARGET, 'status': 'completed',
                    'repository': {'full_name': auto.REPO}, 'head_repository': {'full_name': auto.REPO}}]}
            return original_api(path)
        def request(*values, **kwargs):
            if values[:3] == ('gh', 'api', '--method'):
                # A synchronous callback attempts the real same nonblocking locks.
                self.assertEqual(self.call('apply', preview=identifier)['phase'], 'complete')
                return ''
            return command(*values, **kwargs)
        with patch.object(auto, 'api', side_effect=api), patch.object(auto, 'command', side_effect=request), \
             patch.object(auto, 'get_image', return_value=NEXT_IMAGE), patch.object(auto, 'rollout'):
            result = manual.run(auto, self.binding, args, self.installed)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(result['current_image'], NEXT_IMAGE)
        value = manual.private_json(self.work / 'operator-deploy' / (identifier + '.json'))
        self.assertEqual(value['result']['image'], NEXT_IMAGE)

    def test_execution_resume_holds_writer_barrier_and_consumes_receipt_once(self):
        self.save(self.work / 'status.json', {'phase': 'complete'})
        database = self.root / 'bridge.db'
        with sqlite3.connect(database) as connection:
            connection.executescript('CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);')
        snap = {'installation': str(self.installation), 'paused_execution': ['bridge-executor-worker']}
        args = SimpleNamespace(action='resume', snapshot_digest=manual.digest(snap), reason='reviewed', yes=True)
        def compose(*values, **kwargs):
            self.assertEqual(manual.private_json(self.work / 'operator-execution' / 'receipt.json')['phase'], 'consumed')
            with sqlite3.connect(database, timeout=0) as racing:
                with self.assertRaises(sqlite3.OperationalError):
                    racing.execute("INSERT INTO factory_attempts VALUES ('executing')")
        self.install.compose = Mock(side_effect=compose)
        with patch.object(manual, 'execution_snapshot', return_value=snap), patch.object(auto, 'healthy'), \
             patch.object(auto, 'command', return_value=json.dumps([{'Mountpoint': str(self.root)}])):
            result = manual.execution_run(auto, self.binding, args, self.installed, Mock())
            self.assertEqual(result['phase'], 'complete')
            with self.assertRaisesRegex(manual.Refusal, 'execution_requires_operator'):
                manual.execution_run(auto, self.binding, args, self.installed, Mock())
        self.install.compose.assert_called_once()

    def test_activation_exception_accepts_only_exact_reviewed_blob(self):
        name, sha = next(iter(manual.ACTIVATION_BLOBS.items()))
        target = {name: {'mode': '100644', 'type': 'blob', 'sha': sha}}
        self.assertFalse(manual.protected_changes(auto, {}, target, {})[1])
        target[name]['sha'] = TARGET
        self.assertEqual(manual.protected_changes(auto, {}, target, {})[1], [name])

    def test_preserved_host_toolchain_requires_exact_installed_and_target_blobs(self):
        for name, (host_sha, target_sha) in manual.HOST_TARGET_BLOB_PAIRS.items():
            with self.subTest(path=name):
                installed = {name: ROOT / name}
                before = {'mode': '100644', 'type': 'blob', 'sha': BASE}
                after = {'mode': '100644', 'type': 'blob', 'sha': target_sha}
                baseline, target = {name: before}, {name: after}
                with patch.object(manual, 'blob', return_value=host_sha):
                    changes, blocked = manual.protected_changes(auto, baseline, target, installed)
                self.assertEqual(changes, [{'path': name, 'before': before, 'after': after}])
                self.assertEqual(blocked, [])
                for origin in (baseline, {}):
                    with patch.object(manual, 'blob', return_value=host_sha):
                        self.assertEqual(manual.protected_changes(auto, origin, target, installed)[1], [])
                    with patch.object(manual, 'blob', return_value='0' * 40):
                        self.assertEqual(manual.protected_changes(auto, origin, target, installed)[1], [name])
                    for key, value in [('sha', TARGET), ('mode', '100755'), ('mode', '120000'), ('type', 'tree')]:
                        changed = {name: {**after, key: value}}
                        self.assertEqual(manual.protected_changes(auto, origin, changed, installed)[1], [name])
                    self.assertEqual(manual.protected_changes(auto, origin, target, {})[1], [name])
                for key, value in [('mode', '100755'), ('mode', '120000'), ('type', 'tree')]:
                    changed = {name: {**before, key: value}}
                    self.assertEqual(manual.protected_changes(auto, changed, target, installed)[1], [name])
                self.assertEqual(manual.protected_changes(auto, baseline, {}, installed)[1], [name])

    def test_installed_artifact_helper_preserves_actual_executable_source_mode(self):
        name = 'deploy/factory-artifact-runtime.py'
        mode = format(stat.S_IFREG | stat.S_IMODE((ROOT / name).stat().st_mode), 'o')
        target = {name: {'mode': mode, 'type': 'blob', 'sha': manual.blob(ROOT / name)}}
        self.assertEqual(mode, '100755')
        self.assertFalse(manual.protected_changes(auto, {}, target, {name: ROOT / name})[1])
        target[name]['mode'] = '100644'
        self.assertEqual(manual.protected_changes(auto, {}, target, {name: ROOT / name})[1], [name])

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

    def test_reviewed_host_pair_does_not_bypass_snapshot_staleness(self):
        name = 'deploy/factory-auto-deploy.py'
        target_sha = manual.blob(ROOT / name)
        helper = self.root / 'preserved-auto-deploy.py'
        content = (ROOT / name).read_bytes()
        helper.write_bytes(content)
        self.installed[name] = helper
        self.target[name] = {'mode': '100644', 'type': 'blob', 'sha': target_sha}
        baseline = {name: {'mode': '100644', 'type': 'blob', 'sha': BASE}}
        fingerprint = policy.compatibility_entries([f'100644 blob {BASE}\t{name}'.encode()])
        self.config['compatibility'] = fingerprint
        self.save(self.config_path, self.config)
        real_tree = manual.tree
        def tree(auto, commit):
            return (baseline, fingerprint) if commit == BASE else real_tree(auto, commit)
        with patch.object(manual, 'tree', side_effect=tree):
            view = self.call('preview')
            self.assertTrue(view['eligible'])
            for change in ('host', 'target'):
                with self.subTest(change=change):
                    if change == 'host':
                        helper.write_bytes(content + b'\n# changed\n')
                    else:
                        self.target[name]['sha'] = '0' * 40
                    with self.assertRaisesRegex(ValueError, 'preview_stale'):
                        self.call('approve', preview=view['preview'])
                    helper.write_bytes(content)
                    self.target[name]['sha'] = target_sha

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

    def test_status_reconciles_new_automatic_release_and_preserves_manual_history(self):
        identifier = self.preview()
        path = self.work / 'operator-deploy' / (identifier + '.json')
        value = manual.private_json(path)
        value.update(phase='complete', result={'image': NEXT_IMAGE, 'commit': TARGET})
        self.save(path, value)
        current = {'current_image': IMAGE, 'current_commit': 'f' * 40}
        self.save(self.work / 'status.json', {'phase': 'complete', 'image': IMAGE, 'commit': 'f' * 40, 'run_id': 99})
        with patch.object(manual, 'current_identity', return_value=current):
            result = self.call('status')
        self.assertEqual((result['phase'], result['target_commit'], result['run_id']), ('complete', 'f' * 40, 99))
        self.assertEqual(result['historical_manual_deployment']['run_id'], 34)
        self.assertNotIn('artifact_id', result)
        self.assertEqual(result['current_deployment']['commit'], 'f' * 40)
        self.assertEqual(result['configured_compatibility'], self.config['compatibility'])
        for change, reason in (({'commit': TARGET}, 'identity_differs'), ({'image': NEXT_IMAGE}, 'identity_differs'),
                               ({'run_id': True}, 'invalid_deployment_status'), ({'image': 'invalid'}, 'invalid_deployment_status')):
            self.save(self.work / 'status.json', {'phase': 'complete', 'image': IMAGE, 'commit': 'f' * 40, 'run_id': 99, **change})
            with self.subTest(change=change), patch.object(manual, 'current_identity', return_value=current), self.assertRaisesRegex(manual.Refusal, reason):
                self.call('status')

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
