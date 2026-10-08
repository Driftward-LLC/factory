"""Deployment trust, compatibility, archive and execution-stop boundaries."""
from contextlib import contextmanager
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
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

    def test_compatibility_changes_require_operator_review(self):
        good = {'format': 1, 'commit': COMMIT, 'image': IMAGE, 'compatibility': 'd' * 64, 'archive_sha256': 'e' * 64}
        self.assertEqual(auto.checked_manifest(good, COMMIT, 'd' * 64), good)
        for change in ({'compatibility': 'f' * 64}, {'commit': 'c' * 40}, {'image': 'factory:latest'},
                       {'archive_sha256': 'wrong'}, {'command': 'do not execute'}, {'format': 2}):
            with self.subTest(change=change), self.assertRaises(auto.Refusal):
                auto.checked_manifest({**good, **change}, COMMIT, 'd' * 64)
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
                bundle.writestr('factory-image.tar', b'synthetic image')
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
        image_bytes = b'synthetic image archive'
        tree = {'truncated': False, 'tree': [
            {'mode': '040000', 'type': 'tree', 'sha': 'c' * 40, 'path': 'deploy'},
            {'mode': '100644', 'type': 'blob', 'sha': 'd' * 40, 'path': 'deploy/Dockerfile'}]}
        compatibility = artifact.compatibility_entries([b'100644 blob ' + b'd' * 40 + b'\tdeploy/Dockerfile'])
        manifest = {'format': 1, 'commit': COMMIT, 'image': IMAGE, 'compatibility': compatibility,
                    'archive_sha256': hashlib.sha256(image_bytes).hexdigest()}
        with zipfile.ZipFile(source, 'w') as bundle:
            bundle.writestr('factory-image.tar', image_bytes)
            bundle.writestr('deployment.json', json.dumps(manifest))
        item = {'id': 56, 'name': 'factory-production-' + COMMIT, 'expired': False,
                'size_in_bytes': source.stat().st_size, 'digest': 'sha256:' + auto.sha256(source),
                'workflow_run': {'id': 34, 'head_sha': COMMIT}}
        loaded = []
        def command(*args, output=None):
            if args[0] == 'gh':
                output.write(source.read_bytes()); return ''
            if args[:3] == ('docker', 'image', 'load'):
                loaded.append(args); return ''
            return json.dumps([{'Id': IMAGE, 'Architecture': 'amd64', 'Os': 'linux', 'Config': {'Labels': {
                'org.opencontainers.image.revision': COMMIT, 'org.opencontainers.image.source': 'https://github.com/' + auto.REPO}}}])
        for corrupted in [True, False]:
            metadata = {**item, 'digest': 'sha256:' + '0' * 64} if corrupted else item
            with tempfile.TemporaryDirectory(dir=self.directory) as output, patch.object(auto, 'api', side_effect=[tree, {'total_count': 1, 'artifacts': [metadata]}]), patch.object(auto, 'command', side_effect=command):
                if corrupted:
                    with self.assertRaisesRegex(auto.Refusal, 'artifact_digest_mismatch'):
                        auto.get_image(34, COMMIT, {'compatibility': compatibility}, Path(output))
                    self.assertEqual(loaded, [])
                else:
                    self.assertEqual(auto.get_image(34, COMMIT, {'compatibility': compatibility}, Path(output)), IMAGE)
                    self.assertEqual(len(loaded), 1)

        # Even an artifact claiming the approved fingerprint cannot approve a
        # changed protected source file; reject before downloading/loading it.
        changed = {'truncated': False, 'tree': [dict(tree['tree'][1], sha='e' * 40)]}
        for rejected, reason in [(changed, 'compatibility_review_required'),
                                 ({**tree, 'truncated': True}, 'incomplete_source_tree')]:
            with patch.object(auto, 'api', return_value=rejected) as api, patch.object(auto, 'command') as run:
                with self.assertRaisesRegex(auto.Refusal, reason):
                    auto.get_image(34, COMMIT, {'compatibility': compatibility}, self.directory)
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

    def rollout_fixture(self):
        self.database()
        calls = []
        install = SimpleNamespace(project='synthetic', directory=self.directory, images={'factory': IMAGE}, services=['bridge'])
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
                patch.object(auto, 'resume', side_effect=lambda _: calls.append(('resume',))):
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
