import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('artifacts', BASE / 'deploy/factory-artifact-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
spec = importlib.util.spec_from_file_location('artifact_cli', BASE / 'deploy/factory-cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class ArtifactRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.patch = patch.object(runtime, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.before = '2026-10-20T22:35:06Z'
        self.commit = 'a' * 40
        self.metadata = {'format': 1, 'commit': self.commit, 'image': 'sha256:' + 'b' * 64,
                         'compatibility': 'c' * 64, 'archive_sha256': hashlib.sha256(b'image').hexdigest()}
        self.contents = self.bundle(self.metadata)
        self.row = {'id': 1, 'name': 'factory-production-' + self.commit, 'size_in_bytes': len(self.contents),
                    'digest': 'sha256:' + hashlib.sha256(self.contents).hexdigest(),
                    'created_at': '2026-10-19T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z',
                    'run_id': 2, 'commit': self.commit}
        self.run = {'id': 2, 'path': '.github/workflows/hatchet-quality.yml', 'event': 'push',
                    'head_branch': 'main', 'head_sha': self.commit, 'status': 'completed', 'conclusion': 'success',
                    'repository': {'full_name': runtime.REPO}, 'head_repository': {'full_name': runtime.REPO}}
        self.deleted = []
        self.downloads = 0
        self.api_patch = patch.object(runtime, 'gh', side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def bundle(self, metadata, extra=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('deployment.json', json.dumps(metadata))
            archive.writestr('factory-image.tar', b'image')
            if extra:
                archive.writestr('../unsafe', b'bad')
        return output.getvalue()

    def api(self, endpoint, target=None, method='GET'):
        if method == 'DELETE':
            receipt = json.loads((self.root / '1.json').read_text())
            self.assertEqual(receipt['events'][-1]['status'], 'delete_intent')
            self.deleted.append(1)
        elif target:
            self.downloads += 1
            target.write(self.contents)
        elif endpoint.startswith('actions/artifacts?'):
            return {'artifacts': [] if self.deleted else [dict(self.row, expired=False, workflow_run={'id': 2})]}
        elif endpoint == 'actions/runs/2':
            return self.run
        else:
            self.fail(endpoint)

    def command(self, action, digest=None):
        args = [action, '--before', self.before]
        if action != 'preview':
            args += ['--snapshot-digest', digest or self.view()['snapshot_digest'], '--yes']
        # /tmp is deliberately unprotected in production; fixture replaces only root ancestry validation.
        with patch.object(runtime, 'root_checked'):
            return runtime.dispatch(args)

    def view(self):
        return runtime.snapshot(self.before)

    def preserve(self):
        return self.command('preserve')

    def test_preview_is_read_only_and_fixed_scope(self):
        view = self.command('preview')
        self.assertEqual(view['artifacts'], [self.row])
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(ValueError):
            runtime.snapshot('2026-10-21T00:00:00Z')

    def test_preserve_is_idempotent_then_prune_retains_verified_copies_and_audit(self):
        self.preserve()
        self.preserve()
        self.assertEqual(self.downloads, 1)
        self.command('prune')
        self.assertEqual(self.deleted, [1])
        self.assertEqual((self.root / '1.zip').read_bytes(), self.contents)
        audit = json.loads((self.root / '1.json').read_text())
        self.assertEqual(audit['events'][-1]['status'], 'deleted')
        self.assertEqual((self.root / '1.zip').stat().st_mode & 0o777, 0o600)

    def test_prune_requires_copy_and_receipt(self):
        with self.assertRaises(runtime.Refusal):
            self.command('prune')
        self.preserve()
        (self.root / '1.json').unlink()
        with self.assertRaises(runtime.Refusal):
            self.command('prune')
        self.assertEqual(self.deleted, [])

    def test_tamper_permissions_symlink_hardlink_refused(self):
        self.preserve()
        path = self.root / '1.zip'
        for mode in (0o644, 0o660):
            path.chmod(mode)
            with self.assertRaises(ValueError):
                self.command('prune')
        path.chmod(0o600)
        os.link(path, self.root / 'link')
        with self.assertRaises(ValueError):
            self.command('prune')
        (self.root / 'link').unlink()
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b'x')
        with self.assertRaises(ValueError):
            self.command('prune')
        path.unlink()
        path.symlink_to(self.root / 'absent')
        with self.assertRaises(ValueError):
            self.command('prune')
        self.assertEqual(self.deleted, [])

    def test_snapshot_drift_refused_before_download_or_delete(self):
        with self.assertRaises(ValueError):
            self.command('preserve', '0' * 64)
        self.assertEqual(self.downloads, 0)
        self.preserve()
        view = self.view()
        self.row['expires_at'] = '2098-01-01T00:00:00Z'
        with self.assertRaises(ValueError):
            self.command('prune', view['snapshot_digest'])
        self.assertEqual(self.deleted, [])

    def test_untrusted_source_run_rejected(self):
        for field, value in [('event', 'pull_request'), ('head_sha', 'f' * 40), ('conclusion', 'failure'),
                             ('repository', {'full_name': 'attacker/factory'}), ('path', 'another.yml')]:
            with self.subTest(field=field), patch.dict(self.run, {field: value}):
                with self.assertRaises(ValueError):
                    self.view()

    def test_bad_zip_format2_commit_internal_hash_and_members_refused(self):
        for changes, extra in [({'format': 2}, False), ({'archive_sha256': '0' * 64}, False),
                               ({'commit': 'f' * 40}, False), ({}, True)]:
            self.contents = self.bundle(self.metadata | changes, extra)
            self.row.update(size_in_bytes=len(self.contents), digest='sha256:' + hashlib.sha256(self.contents).hexdigest())
            with self.subTest(changes=changes, extra=extra), self.assertRaises(ValueError):
                self.preserve()
            self.assertFalse((self.root / '1.zip').exists())
        self.assertEqual(self.deleted, [])

    def test_download_digest_error_and_disk_preflight(self):
        with patch.object(runtime.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(ValueError):
                self.preserve()
        self.assertEqual(self.downloads, 0)
        self.contents += b'changed'
        with self.assertRaises(ValueError):
            self.preserve()
        self.assertFalse((self.root / '1.zip').exists())

    def test_delete_failure_retains_partial_failure_audit(self):
        self.preserve()
        api = self.api
        def fail(endpoint, target=None, method='GET'):
            if method == 'DELETE':
                raise ValueError('API unavailable')
            return api(endpoint, target, method)
        with patch.object(runtime, 'gh', side_effect=fail), self.assertRaises(ValueError):
            self.command('prune')
        audit = json.loads((self.root / '1.json').read_text())
        self.assertEqual(audit['events'][-1]['status'], 'delete_failed_or_unknown')
        self.assertTrue((self.root / '1.zip').exists())

    def test_canonical_cli_routes_artifact_preview_under_installation_lock(self):
        import contextlib
        events = []
        @contextlib.contextmanager
        def lock(path):
            events.append('locked')
            yield
            events.append('unlocked')
        production = type('Production', (), {'portable': type('Portable', (), {'installation_lock': lock})})
        config = {'installation': '/fixture', 'artifact_runtime': str(BASE / 'deploy/factory-artifact-runtime.py')}
        with patch.object(cli, 'production_module', return_value=production), \
             patch.object(cli, 'installation', return_value=object()), \
             patch.object(cli, 'checked_hermes_wrapper', side_effect=lambda path: Path(path)), \
             patch.object(cli.importlib.util, 'module_from_spec', return_value=runtime), \
             patch.object(cli.importlib.util, 'spec_from_file_location') as loader, \
             patch.object(cli, 'checked_config', return_value=config), \
             patch.object(cli.sys, 'argv', ['factory', 'artifacts', 'preview', '--before', self.before]), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            loader.return_value.loader.exec_module.side_effect = lambda module: None
            self.assertEqual(cli.main(), 0)
        self.assertEqual(events, ['locked', 'unlocked'])
        self.assertEqual(json.loads(output.getvalue())['artifacts'], [self.row])

    def test_cli_reports_only_safe_artifact_refusals(self):
        with patch.object(cli, 'installation'), \
             patch.object(cli, 'checked_hermes_wrapper', return_value=Path('/fixture/helper.py')), \
             patch.object(cli.importlib.util, 'module_from_spec', return_value=runtime), \
             patch.object(cli.importlib.util, 'spec_from_file_location') as loader, \
             patch.object(runtime, 'dispatch', side_effect=runtime.Refusal('artifact snapshot changed; preview again')), \
             patch('sys.stderr', new_callable=io.StringIO) as output:
            loader.return_value.loader.exec_module.side_effect = lambda module: None
            self.assertEqual(cli.dispatch_bound({'artifact_runtime': '/fixture/helper.py'}, ['artifacts', 'prune']), 1)
        self.assertEqual(output.getvalue(), 'factory artifacts: artifact snapshot changed; preview again\n')

    def test_status_is_local_readonly_and_verifies_post_prune_copies(self):
        with patch.object(runtime, 'gh', side_effect=AssertionError('network call')):
            with patch.object(runtime, 'ROOT', self.root / 'absent'):
                self.assertEqual(runtime.dispatch(['status'])['count'], 0)
                self.assertFalse((self.root / 'absent').exists())
        self.preserve()
        self.command('prune')
        with patch.object(runtime, 'gh', side_effect=AssertionError('network call')), patch.object(runtime, 'root_checked'):
            state = runtime.dispatch(['status'])
            self.assertEqual(state['count'], 1)
            self.assertEqual(state['total_bytes'], len(self.contents))
            self.assertEqual(state['artifacts'][0]['delete_status'], 'deleted')
            (self.root / '1.zip').write_bytes(b'corrupted')
            with self.assertRaises(runtime.Refusal):
                runtime.dispatch(['status'])

    def test_prune_rechecks_snapshot_immediately_before_delete(self):
        self.preserve()
        original = runtime.snapshot
        count = 0
        def change(before):
            nonlocal count
            count += 1
            value = original(before)
            if count == 2:
                value['artifacts'] = []
            return value
        snapshot_digest = self.view()['snapshot_digest']
        with patch.object(runtime, 'snapshot', side_effect=change), self.assertRaises(runtime.Refusal):
            self.command('prune', snapshot_digest)
        self.assertEqual(self.deleted, [])


if __name__ == '__main__':
    unittest.main()
