"""Deployment trust, compatibility, archive and execution-stop boundaries."""
from contextlib import contextmanager
import base64
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifact = load('auto_deploy_artifact', ROOT / 'deploy/auto_deploy_artifact.py')
with patch.dict(sys.modules, {'auto_deploy_artifact': artifact}):
    auto = load('auto_deploy', ROOT / 'deploy/factory-auto-deploy.py')
COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64


class DeploymentTests(unittest.TestCase):
    def test_every_main_push_gets_checks_even_when_only_docs_change(self):
        workflow = (ROOT / '.github/workflows/hatchet-quality.yml').read_text()
        push = workflow.split('\n  push:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('branches: [main]', push)
        self.assertNotIn('paths:', push)
        self.assertNotIn('paths-ignore:', push)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_only_successful_same_repository_main_push_is_authorized(self):
        workflow = {'id': 12, 'path': '.github/workflows/hatchet-quality.yml'}
        run = {'id': 34, 'workflow_id': 12, 'path': workflow['path'], 'event': 'push', 'head_branch': 'main',
               'head_sha': COMMIT, 'status': 'completed', 'conclusion': 'success',
               'repository': {'full_name': auto.REPO}, 'head_repository': {'full_name': auto.REPO}}
        auto.validate_run(run, workflow, 34, COMMIT)
        for key, value in [('id', 35), ('workflow_id', 13), ('path', 'another.yml'), ('event', 'pull_request'),
                           ('event', 'workflow_dispatch'), ('head_branch', 'feature'), ('head_sha', 'c' * 40),
                           ('status', 'in_progress'), ('conclusion', 'failure'),
                           ('repository', {'full_name': 'foreign/repo'}), ('head_repository', {'full_name': 'fork/repo'})]:
            with self.subTest(key=key, value=value), self.assertRaises(auto.Refusal):
                auto.validate_run({**run, key: value}, workflow, 34, COMMIT)
        with patch.object(auto, 'api', return_value={'object': {'sha': 'c' * 40}}), self.assertRaisesRegex(auto.Refusal, 'superseded'):
            auto.current_main(COMMIT)

    def test_manual_failure_written_before_tenant_lock_blocks_automatic_rollout(self):
        config = {'enabled': True, 'work_directory': str(self.directory),
                  'installation': str(self.directory / 'install'), 'production_bundle': str(self.directory)}
        calls = []
        @contextmanager
        def lock(path):
            calls.append(path)
            if len(calls) == 2:
                directory = self.directory / 'operator-deploy'
                directory.mkdir(mode=0o700)
                snapshot = {}
                identifier = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                path = directory / (identifier + '.json')
                path.write_text(json.dumps({'format': 1, 'digest': identifier, 'snapshot': snapshot, 'phase': 'failed'}))
                path.chmod(0o600)
            yield
        production = SimpleNamespace(portable=SimpleNamespace(installation_lock=lock),
                                     Production=Mock(return_value=SimpleNamespace(tenant='fixture', preflight=Mock())))
        actual_stat, actual_lstat = Path.stat, Path.lstat
        def owned(info):
            fields = list(info); fields[4] = 0
            return os.stat_result(fields)
        def fixture_stat(path, **options):
            info = actual_stat(path, **options)
            return owned(info) if path == self.directory or self.directory in path.parents else info
        def fixture_lstat(path):
            info = actual_lstat(path)
            return owned(info) if path == self.directory or self.directory in path.parents else info
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        with patch.object(auto.importlib.util, 'spec_from_file_location', return_value=spec), \
             patch.object(auto.importlib.util, 'module_from_spec', return_value=production), \
             patch.object(Path, 'stat', new=fixture_stat), patch.object(Path, 'lstat', new=fixture_lstat), \
             patch.object(auto, 'get_image') as image, patch.object(auto, 'rollout') as rollout, patch.object(auto, 'api') as api:
            with self.assertRaisesRegex(auto.Refusal, 'prior_deployment_requires_operator'):
                auto.deploy(config, 34, COMMIT, {'username': 'fixture', 'token': 'synthetic_token'})
        self.assertEqual(len(calls), 2)
        image.assert_not_called(); rollout.assert_not_called(); api.assert_not_called()
        production.Production.return_value.preflight.assert_not_called()

    def manifest(self, compatibility='d' * 64):
        return {'format': 2, 'commit': COMMIT, 'image': artifact.REGISTRY_IMAGE + '@' + IMAGE,
                'image_config': 'sha256:' + 'c' * 64, 'compatibility': compatibility, 'run_id': 34, 'run_attempt': 2}

    def test_compatibility_changes_require_operator_review(self):
        good = self.manifest()
        self.assertEqual(auto.checked_manifest(good, COMMIT, 'd' * 64, 34, 2), good)
        for change in ({'compatibility': 'f' * 64}, {'commit': 'c' * 40}, {'image': 'factory:latest'},
                       {'image': 'ghcr.io/foreign/repo@' + IMAGE}, {'image_config': 'wrong'},
                       {'command': 'do not execute'}, {'format': 1}, {'run_id': 35},
                       {'run_attempt': 1}, {'run_id': True}):
            with self.subTest(change=change), self.assertRaises(auto.Refusal):
                auto.checked_manifest({**good, **change}, COMMIT, 'd' * 64, 34, 2)
        for key in good:
            with self.subTest(missing=key), self.assertRaises(auto.Refusal):
                auto.checked_manifest({k: v for k, v in good.items() if k != key}, COMMIT, 'd' * 64, 34, 2)
        for name in ['deploy/Dockerfile', 'deploy/factory-auto-deploy.py', '.github/workflows/hatchet-quality.yml',
                     'hatchet/bridge/hatchet_hermes_bridge/store.py', 'hatchet/event-ingress/hatchet_event_ingress/store.py',
                     'hatchet/factory-lifecycle-writer/factory_lifecycle_writer/outbox.py',
                     'hatchet/workers/bridge-executor-worker/src/hermes/invocation-journal.ts',
                     'hatchet/workers/shared/src/contracts.ts', 'new-unknown-runtime.py']:
            self.assertFalse(artifact.routine_path(name), name)
        self.assertTrue(artifact.routine_path('factory/console/src/App.tsx'))
        self.assertTrue(artifact.routine_path('hatchet/workers/intake-worker/src/workflows/issue.ts'))

    def test_tree_digest_includes_new_files_modes_and_deletions(self):
        base = b'100644 blob one\tdeploy/Dockerfile\0'
        with patch.object(artifact.subprocess, 'check_output', return_value=base):
            expected = artifact.compatibility(self.directory)
        for tree in [base + b'100644 blob two\tnew/migration.py\0', b'', base.replace(b'100644', b'120000')]:
            with patch.object(artifact.subprocess, 'check_output', return_value=tree):
                self.assertNotEqual(artifact.compatibility(self.directory), expected)
        with patch.object(artifact.subprocess, 'check_output', return_value=base + b'100644 blob three\tfactory/console/app.tsx\0'):
            self.assertEqual(artifact.compatibility(self.directory), expected)

    def test_archive_rejects_paths_duplicates_symlinks_and_oversized_metadata(self):
        for bad in ['../outside', '/absolute', 'deployment.json', 'symlink', 'oversized']:
            archive = self.directory / 'artifact.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                if bad == 'symlink':
                    item = zipfile.ZipInfo('deployment.json'); item.external_attr = 0o120777 << 16
                    bundle.writestr(item, b'target')
                else:
                    bundle.writestr('deployment.json', b'x' * 4097 if bad == 'oversized' else b'{}')
                    if bad != 'oversized':
                        bundle.writestr(bad, b'not allowed')
            with self.subTest(bad=bad), tempfile.TemporaryDirectory(dir=self.directory) as output, self.assertRaises(auto.Refusal):
                auto.unpack(archive, Path(output))
        self.assertFalse((self.directory.parent / 'outside').exists())

    def database(self):
        conn = sqlite3.connect(self.directory / 'bridge.db')
        conn.executescript('CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);')
        conn.commit(); conn.close()

    def test_download_digest_and_image_provenance_checked_before_rollout(self):
        source = self.directory / 'source.zip'
        tree = {'truncated': False, 'tree': [
            {'mode': '100644', 'type': 'blob', 'sha': 'd' * 40, 'path': 'deploy/Dockerfile'}]}
        compatibility = artifact.compatibility_entries([b'100644 blob ' + b'd' * 40 + b'\tdeploy/Dockerfile'])
        manifest = self.manifest(compatibility)
        with zipfile.ZipFile(source, 'w') as bundle:
            bundle.writestr('deployment.json', json.dumps(manifest))
        item = {'id': 56, 'name': 'factory-production-' + COMMIT, 'expired': False,
                'size_in_bytes': source.stat().st_size, 'digest': 'sha256:' + auto.sha256(source),
                'workflow_run': {'id': 34, 'head_sha': COMMIT}}
        token = 'ghs_' + '12345_' + '.'.join(['synthetic', 'jwt', 'signature-_"\\`$();:'])
        credentials = {'username': 'octocat', 'token': token}
        helpers = self.directory / 'helpers'
        helpers.mkdir()
        marker = self.directory / 'credential-helper-called'
        for name in ['docker-credential-pass', 'docker-credential-secretservice']:
            helper = helpers / name
            helper.write_text('#!/bin/sh\nprintf called >> "$CREDENTIAL_HELPER_PROBE"\ncat >/dev/null\n')
            helper.chmod(0o700)
        probe_env = {**os.environ, 'PATH': str(helpers) + ':' + os.environ['PATH'],
                     'CREDENTIAL_HELPER_PROBE': str(marker)}
        docker = shutil.which('docker')
        self.assertIsNotNone(docker, 'Docker CLI is required for the credential-store regression')
        empty_config = self.directory / 'empty-config'
        empty_config.mkdir(mode=0o700)
        # Prove the helpers are discoverable: the former empty configuration
        # routes even logout through an external native helper.
        result = subprocess.run([docker, 'logout', 'ghcr.io'],
                                env={**probe_env, 'DOCKER_CONFIG': str(empty_config)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), 'native helper probe must be effective')
        marker.unlink()
        for failure in ['archive', 'config', 'pull', 'repo_digest', 'id', 'revision', 'platform', 'classic', 'native_helper', None]:
            calls = []
            def command(*args, output=None, env=None):
                calls.append(args)
                if args[0] == 'gh':
                    output.write(source.read_bytes()); return ''
                self.assertEqual(env, {'DOCKER_CONFIG': str(Path(output_dir) / 'docker-config')})
                self.assertNotIn(token, str(args))
                config_dir = Path(env['DOCKER_CONFIG'])
                self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)
                if args[:3] == ('docker', 'manifest', 'inspect'):
                    auth_file = config_dir / 'config.json'
                    self.assertEqual(auth_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(json.loads(auth_file.read_text()), {'auths': {'ghcr.io': {
                        'auth': base64.b64encode(('octocat:' + token).encode()).decode()}}})
                    if failure == 'native_helper':
                        # A real Docker CLI must select its file store even
                        # when native credential helpers are discoverable.
                        result = subprocess.run([docker, 'logout', 'ghcr.io'],
                                                env={**probe_env, **env}, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertNotIn(token, result.stdout + result.stderr)
                        self.assertFalse(marker.exists())
                    return json.dumps({'schemaVersion': 2, 'config': {'digest':
                        IMAGE if failure == 'config' else manifest['image_config']}})
                if args[:3] == ('docker', 'image', 'pull'):
                    if failure == 'pull':
                        raise auto.Refusal('docker_failed')
                    return 'synthetic pull result'
                self.assertEqual(args, ('docker', 'image', 'inspect', manifest['image']))
                return json.dumps([{'Id': 'sha256:' + 'f' * 64 if failure == 'id' else
                    manifest['image_config'] if failure == 'classic' else IMAGE,
                    'RepoDigests': [] if failure == 'repo_digest' else [manifest['image']],
                    'Architecture': 'arm64' if failure == 'platform' else 'amd64', 'Os': 'linux',
                    'Config': {'Labels': {'org.opencontainers.image.revision': 'e' * 40 if failure == 'revision' else COMMIT,
                        'org.opencontainers.image.source': 'https://github.com/' + auto.REPO}}}])
            metadata = {**item, 'digest': 'sha256:' + '0' * 64} if failure == 'archive' else item
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.directory) as output_dir, \
                    patch.object(auto, 'api', side_effect=[tree, {'total_count': 1, 'artifacts': [metadata]}]), \
                    patch.object(auto, 'command', side_effect=command):
                if failure not in (None, 'classic', 'native_helper'):
                    with self.assertRaises(auto.Refusal):
                        auto.get_image(34, COMMIT, {'compatibility': compatibility}, Path(output_dir), 2, credentials)
                    if failure == 'archive':
                        self.assertEqual(len(calls), 1)
                else:
                    self.assertEqual(auto.get_image(34, COMMIT, {'compatibility': compatibility},
                                                   Path(output_dir), 2, credentials),
                                     manifest['image_config'] if failure == 'classic' else IMAGE)
                self.assertFalse((Path(output_dir) / 'docker-config').exists())
        changed = {'truncated': False, 'tree': [dict(tree['tree'][0], sha='e' * 40)]}
        for rejected, reason in [(changed, 'compatibility_review_required'),
                                 ({**tree, 'truncated': True}, 'incomplete_source_tree')]:
            with patch.object(auto, 'api', return_value=rejected) as api, patch.object(auto, 'command') as run:
                with self.assertRaisesRegex(auto.Refusal, reason):
                    auto.get_image(34, COMMIT, {'compatibility': compatibility}, self.directory, 2, credentials)
                api.assert_called_once_with(f'git/trees/{COMMIT}?recursive=1')
                run.assert_not_called()

    def test_expired_or_unknown_execution_is_not_quiescence(self):
        self.database()
        with sqlite3.connect(self.directory / 'bridge.db') as conn:
            for value in auto.TERMINAL:
                conn.execute('INSERT INTO factory_attempts VALUES (?)', (value,))
            auto.assert_quiescent(conn)
            for table, column, value in [('factory_attempts', 'status', 'cancellation_requested'),
                                         ('factory_attempts', 'status', 'effect_outcome_unknown'),
                                         ('factory_attempts', 'status', 'executing'),
                                         ('factory_execution_leases', 'released_at', None),
                                         ('bridge_jobs', 'state', 'running'), ('bridge_jobs', 'state', 'accepted')]:
                conn.execute(f'INSERT INTO {table} VALUES (?)', (value,))
                with self.subTest(table=table, value=value), self.assertRaisesRegex(auto.Refusal, 'active_or_unresolved'):
                    auto.assert_quiescent(conn)
                conn.execute(f'DELETE FROM {table} WHERE {column} IS ?', (value,))

    def test_publisher_requires_private_expected_package_without_changing_visibility(self):
        expected = {'visibility': 'private', 'name': 'factory-production', 'package_type': 'container'}
        with patch.dict(os.environ, {'GH_TOKEN': 'synthetic_token'}):
            for changes, success in [({}, True), ({'repository': None}, True),
                                     ({'repository': {'full_name': auto.REPO}}, True),
                                     ({'repository': {'full_name': 'foreign/repo'}}, False),
                                     ({'repository': {}}, False), ({'repository': 'invalid'}, False),
                                     ({'owner': {'login': 'Driftward-LLC'}}, True),
                                     ({'visibility': 'public'}, False), ({'visibility': 'internal'}, False),
                                     ({'name': 'foreign'}, False), ({'package_type': 'npm'}, False),
                                     ({'owner': None}, True), ({'owner': {'login': 'foreign'}}, False)]:
                response = Mock()
                response.__enter__ = Mock(return_value=io.StringIO(json.dumps(expected | changes)))
                response.__exit__ = Mock(return_value=False)
                with patch.object(artifact, 'urlopen', return_value=response) as request:
                    if success:
                        artifact.private_package()
                    else:
                        with self.assertRaises(RuntimeError):
                            artifact.private_package()
                    self.assertEqual(request.call_args.args[0].get_method(), 'GET')
            for body in ['[]', 'null', '{invalid secret response']:
                response = Mock()
                response.__enter__ = Mock(return_value=io.StringIO(body))
                response.__exit__ = Mock(return_value=False)
                with patch.object(artifact, 'urlopen', return_value=response):
                    with self.assertRaises(RuntimeError) as failure:
                        artifact.private_package()
                    self.assertNotIn('secret', str(failure.exception))
            for code in [403, 404, 500]:
                with patch.object(artifact, 'urlopen', side_effect=HTTPError('synthetic', code, 'error', {}, None)):
                    with self.assertRaises(RuntimeError):
                        artifact.private_package()
                    if code == 404:
                        artifact.private_package(allow_missing=True)
                    else:
                        with self.assertRaises(RuntimeError):
                            artifact.private_package(allow_missing=True)

    def test_publisher_records_exact_tested_single_platform_digest_without_image_tar(self):
        for local_id in [IMAGE, 'sha256:' + 'c' * 64]:
            with self.subTest(local_id=local_id), tempfile.TemporaryDirectory(dir=self.directory) as output_dir:
                output = Path(output_dir) / 'metadata'
                def check_output(args, **kwargs):
                    if args[0] == 'git':
                        return COMMIT
                    if args[:3] == ['docker', 'image', 'inspect']:
                        return json.dumps([{'Id': local_id, 'Architecture': 'amd64', 'Os': 'linux', 'Config': {'Labels': {
                            'org.opencontainers.image.revision': COMMIT,
                            'org.opencontainers.image.source': 'https://github.com/' + auto.REPO}}}])
                    if args[:3] == ['docker', 'image', 'push']:
                        self.assertIn('--platform', args)
                        return 'run-34-2: digest: ' + IMAGE + ' size: 1234'
                    self.assertEqual(args, ['docker', 'manifest', 'inspect', artifact.REGISTRY_IMAGE + '@' + IMAGE])
                    return json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + 'c' * 64}})
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '34', 'GITHUB_RUN_ATTEMPT': '2',
                                            'GITHUB_REPOSITORY': auto.REPO, 'GITHUB_EVENT_NAME': 'push',
                                            'GITHUB_REF': 'refs/heads/main'}), \
                        patch.object(artifact.subprocess, 'check_output', side_effect=check_output), \
                        patch.object(artifact.subprocess, 'run') as command, \
                        patch.object(artifact, 'compatibility', return_value='d' * 64), \
                        patch.object(artifact, 'private_package') as private:
                    artifact.package(self.directory, output, 'tested:ci')
                self.assertEqual(list(output.iterdir()), [output / 'deployment.json'])
                self.assertEqual(json.loads((output / 'deployment.json').read_text()), self.manifest())
                command.assert_called_once_with(['docker', 'image', 'tag', local_id,
                                                artifact.REGISTRY_IMAGE + ':run-34-2'], check=True)
                self.assertEqual(private.call_args_list, [unittest.mock.call(allow_missing=True), unittest.mock.call()])
                # A final API/privacy/identity refusal must not leave deployable metadata.
                for refusal in ['HTTP 403', 'expected private', 'unexpected repository']:
                    refused = Path(output_dir) / ('refused-' + refusal.replace(' ', '-'))
                    with patch.dict(os.environ, {'GITHUB_RUN_ID': '34', 'GITHUB_RUN_ATTEMPT': '2',
                                                'GITHUB_REPOSITORY': auto.REPO, 'GITHUB_EVENT_NAME': 'push',
                                                'GITHUB_REF': 'refs/heads/main'}), \
                            patch.object(artifact.subprocess, 'check_output', side_effect=check_output), \
                            patch.object(artifact.subprocess, 'run'), \
                            patch.object(artifact, 'private_package', side_effect=[None, RuntimeError(refusal)]):
                        with self.assertRaises(RuntimeError):
                            artifact.package(self.directory, refused, 'tested:ci')
                    self.assertFalse((refused / 'deployment.json').exists())

    def test_publisher_rejects_untrusted_context_before_writes(self):
        trusted = {'GITHUB_REPOSITORY': auto.REPO, 'GITHUB_EVENT_NAME': 'push',
                   'GITHUB_REF': 'refs/heads/main'}
        for key, value in [('GITHUB_REPOSITORY', 'foreign/repo'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_REF', 'refs/heads/feature')]:
            output = self.directory / 'refused-metadata'
            with patch.dict(os.environ, trusted | {key: value}), \
                    patch.object(artifact.subprocess, 'check_output') as command:
                with self.assertRaises(RuntimeError):
                    artifact.package(self.directory, output, 'tested:ci')
                command.assert_not_called()
                self.assertFalse(output.exists())

    def test_read_only_package_cli_uses_get_without_docker_or_metadata(self):
        program = """
import io, json, os, runpy, sys
from unittest.mock import MagicMock, patch
script = sys.argv[1]
sys.argv = [script, '--verify-package']
response = MagicMock()
response.__enter__.return_value = io.StringIO(json.dumps({
    'visibility': 'private', 'name': 'factory-production', 'package_type': 'container'}))
with patch.dict(os.environ, {'GH_TOKEN': 'synthetic_token'}), \\
        patch('urllib.request.urlopen', return_value=response) as request, \\
        patch('subprocess.check_output') as command, patch('pathlib.Path.write_text') as write:
    runpy.run_path(script, run_name='__main__')
    assert request.call_args.args[0].get_method() == 'GET'
    command.assert_not_called()
    write.assert_not_called()
"""
        result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'deploy/auto_deploy_artifact.py')],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('repository=omitted', result.stdout)
        self.assertNotIn('synthetic_token', result.stdout + result.stderr)

    def test_real_privileged_entrypoint_reads_bounded_credentials_and_keeps_command_grammar(self):
        # Execute main in a separate interpreter with synthetic configuration;
        # no production bundle, registry, sudo or host configuration is touched.
        program = """
import json, runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
scope = runpy.run_path(sys.argv[1])
expected = json.loads(sys.argv[3])
scope['main'].__globals__['private_json'] = lambda _: {'enabled': True}
scope['main'].__globals__['os'].geteuid = lambda: 0
def deploy(config, run_id, commit, credentials):
    print('validated credentials; no rollout')
    assert run_id == 34 and commit == 'a' * 40
    assert credentials == expected
scope['main'].__globals__['deploy'] = deploy
import factory_deploy_operator
factory_deploy_operator.forced_deployment = lambda *args: False
sys.argv = [sys.argv[1], sys.argv[2]]
try:
    scope['main']()
except Exception:
    sys.exit(2)
"""
        valid = json.dumps({'username': 'octocat', 'token': 'synthetic_token'})
        stateless_token = 'ghs_' + '12345_' + '.'.join([
            ''.join(['eyJhbGci', 'OiJSUzI1', 'NiJ9']), 'eyJpYXQiOjF9', 'signature-_'])
        tokens = ['synthetic_token', stateless_token,
                  ''.join(chr(value) for value in range(33, 127)), 'x' * 4096]
        cases = [('deploy 34 ' + COMMIT, json.dumps({'username': 'octocat', 'token': token}), True)
                 for token in tokens]
        for token in ['', 'bad token', 'bad\ttoken', 'bad\ntoken', 'bad\rtoken', 'bad\x00token',
                      'bad\x1ftoken', 'bad\x7ftoken', 'bad\u0085token', 'bad\u00e9token', 'x' * 4097, None, 123]:
            cases.append(('deploy 34 ' + COMMIT, json.dumps({'username': 'octocat', 'token': token}), False))
        for payload in [[], {}, {'username': 'octocat'}, {'token': 'synthetic_token'},
                        {'username': 'octocat', 'token': 'synthetic_token', 'extra': True},
                        {'username': None, 'token': 'synthetic_token'}]:
            cases.append(('deploy 34 ' + COMMIT, json.dumps(payload), False))
        cases += [('deploy 34 ' + COMMIT, valid + ' ' * (8192 - len(valid)), True),
                  ('deploy 34 ' + COMMIT, '{malformed_synthetic_secret', False),
                  ('status', '', True), ('deploy 34 ' + COMMIT, '', False),
                  ('deploy 34 ' + COMMIT, valid + ' ' * 8193, False),
                  ('deploy 34 ' + COMMIT, valid.replace('octocat', '--bad'), False),
                  ('deploy 34 main', valid, False)]
        for command, envelope, success in cases:
            expected = envelope if success and command != 'status' else '{}'
            result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'deploy/factory-auto-deploy.py'), command, expected],
                                    input=envelope, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode == 0, success, result.stderr)
            self.assertEqual('validated credentials; no rollout' in result.stdout, success and command != 'status')
            if not success:
                self.assertEqual(result.stdout + result.stderr, '')
            for token in tokens + ['{malformed_synthetic_secret']:
                self.assertNotIn(token, result.stdout + result.stderr)

    def test_ssh_entrypoint_preserves_stdin_through_fixed_sudo_command(self):
        program = """
import os, runpy, sys
def execv(path, args):
    assert path == '/usr/bin/sudo'
    assert args == ['sudo', '-n', '/usr/local/libexec/factory-auto-deploy', 'deploy 34 ' + 'a' * 40]
    assert sys.stdin.read() == 'synthetic envelope\\n'
    print('fixed command and EOF preserved')
os.execv = execv
runpy.run_path(sys.argv[1])
"""
        result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'deploy/factory-deploy-ssh.py')],
                                env={'SSH_ORIGINAL_COMMAND': 'deploy 34 ' + COMMIT},
                                input='synthetic envelope\n', capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EOF preserved', result.stdout)

    def rollout_fixture(self):
        self.database()
        calls = []
        install = SimpleNamespace(project='synthetic', directory=self.directory, images={'factory': IMAGE}, services=['bridge'], service_rows=lambda: [])
        def compose(*args, **kwargs):
            calls.append(args)
            if args[0] == 'stop':
                with sqlite3.connect(self.directory / 'bridge.db', timeout=0) as racing:
                    with self.assertRaises(sqlite3.OperationalError):
                        racing.execute("INSERT INTO factory_attempts VALUES ('executing')")
        install.compose = compose
        install.record_upgrade = Mock(side_effect=lambda *a, **kw: calls.append(('record-upgrade',)))
        production = SimpleNamespace(state=SimpleNamespace(backup=Mock(side_effect=lambda *a, **kw: calls.append(('backup',)))), Production=Mock(return_value=install))
        def command(*args, **kwargs):
            return json.dumps([{'Config': {'Labels': {'org.opencontainers.image.revision': 'c' * 40}}}]) if args[:3] == ('docker', 'image', 'inspect') else json.dumps([{'Mountpoint': str(self.directory)}])
        return install, production, command, calls

    def test_rollout_serializes_authorization_backup_pin_and_resume(self):
        install, production, command, calls = self.rollout_fixture()
        with patch.object(auto, 'healthy'), patch.object(auto, 'command', side_effect=command), patch.object(auto, 'current_main'), \
                patch.object(auto, 'resume', side_effect=lambda _, paused: calls.append(('resume',))):
            auto.rollout(production, install, IMAGE, COMMIT, 34, self.directory)
        self.assertEqual([call[0] for call in calls], ['stop', 'backup', 'record-upgrade', 'resume'])
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['phase'], 'complete')

    def test_blocked_execution_never_stops_services_and_failure_latches(self):
        install, production, command, calls = self.rollout_fixture()
        with sqlite3.connect(self.directory / 'bridge.db') as conn:
            conn.execute("INSERT INTO factory_attempts VALUES ('effect_outcome_unknown')")
        with patch.object(auto, 'healthy'), patch.object(auto, 'command', side_effect=command), patch.object(auto, 'current_main'), patch.object(auto, 'resume') as resume:
            with self.assertRaisesRegex(auto.Refusal, 'active_or_unresolved'):
                auto.rollout(production, install, IMAGE, COMMIT, 34, self.directory)
            self.assertEqual(calls, []); resume.assert_not_called()
        with sqlite3.connect(self.directory / 'bridge.db') as conn:
            conn.execute('DELETE FROM factory_attempts')
        with patch.object(auto, 'healthy'), patch.object(auto, 'command', side_effect=command), patch.object(auto, 'current_main'), patch.object(auto, 'resume', side_effect=RuntimeError('health failure')):
            with self.assertRaises(RuntimeError):
                auto.rollout(production, install, IMAGE, COMMIT, 34, self.directory)
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['phase'], 'starting')
        with patch.object(auto, 'private_json', return_value={'phase': 'starting'}), self.assertRaisesRegex(auto.Refusal, 'prior_deployment'):
            auto.rollout(production, install, IMAGE, COMMIT, 34, self.directory)

    def test_ssh_entrypoint_rejects_arbitrary_shell_and_injection(self):
        for request in ['', 'bash', 'status; id', 'deploy 1 main', 'deploy 1 ' + COMMIT + '\nwhoami', '-oProxyCommand=id']:
            result = subprocess.run([sys.executable, str(ROOT / 'deploy/factory-deploy-ssh.py')],
                                    env={'SSH_ORIGINAL_COMMAND': request}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Only a verified Factory deployment request', result.stderr)


if __name__ == '__main__':
    unittest.main()
