import hashlib
from datetime import datetime, timezone
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


class ArtifactInventoryTests(unittest.TestCase):
    def item(self, artifact_id=1, name='driftward-ui-storybook'):
        return {'id': artifact_id, 'name': name, 'size_in_bytes': artifact_id * 100, 'expired': False,
                'created_at': '2026-10-01T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z',
                'workflow_run': {'id': artifact_id + 1000, 'head_branch': 'main', 'head_sha': 'a' * 40}}

    def test_inventory_is_complete_fixed_repo_metadata_only_and_read_only(self):
        rows = [self.item(1), self.item(2), self.item(3, 'factory-production-' + 'a' * 40)]
        with patch.object(runtime, 'gh', return_value={'total_count': 3, 'artifacts': rows}) as api, \
             patch.object(runtime, 'root_checked', side_effect=AssertionError('local write')):
            view = runtime.dispatch(['inventory'])
        self.assertEqual(view['repository'], 'Driftward-LLC/factory')
        self.assertEqual(view['count'], 3)
        self.assertEqual(view['total_bytes'], 600)
        self.assertEqual(view['groups'][0]['total_bytes'], 300)
        api.assert_called_once_with('actions/artifacts?per_page=100&page=1')
        self.assertEqual(view['artifacts'][0]['run_id'], 1001)
        self.assertNotIn('archive_download_url', json.dumps(view))

    def test_inventory_sanitizes_names_and_branches(self):
        row = self.item(name='https://evil.invalid/\x1b[31m\n' + 'x' * 200)
        row['workflow_run']['head_branch'] = '\nhttps://bad/branch'
        with patch.object(runtime, 'gh', return_value={'total_count': 1, 'artifacts': [row]}):
            value = runtime.inventory()['artifacts'][0]
        self.assertEqual(value['id'], 1)
        self.assertLessEqual(len(value['name']), 160)
        self.assertNotIn(':', value['name'])
        self.assertNotIn('\n', value['head_branch'])
        self.assertNotIn('\x1b', value['name'])

    def test_inventory_pages_all_rows_and_totals(self):
        pages = [{'total_count': 101, 'artifacts': [self.item(i) for i in range(1, 101)]},
                 {'total_count': 101, 'artifacts': [self.item(101)]}]
        with patch.object(runtime, 'gh', side_effect=pages) as api:
            self.assertEqual(runtime.inventory()['count'], 101)
        self.assertEqual(api.call_count, 2)

    def test_incomplete_duplicate_changed_or_unbounded_listing_refused(self):
        pages = [({'total_count': 2, 'artifacts': [self.item()]},),
                 ({'total_count': 2, 'artifacts': [self.item(), self.item()]},),
                 ({'total_count': 1001, 'artifacts': []},),
                 ({'artifacts': []},),
                 ({'total_count': 101, 'artifacts': [self.item(i) for i in range(1, 101)]},
                  {'total_count': 100, 'artifacts': []})]
        for responses in pages:
            with self.subTest(responses=len(responses)), patch.object(runtime, 'gh', side_effect=responses), \
                 self.assertRaises(runtime.Refusal):
                runtime.inventory()

    def test_invalid_metadata_refused(self):
        for field, value in [('id', True), ('size_in_bytes', -1), ('created_at', 'bad'), ('workflow_run', {})]:
            with self.subTest(field=field), \
                 patch.object(runtime, 'gh', return_value={'total_count': 1, 'artifacts': [self.item() | {field: value}]}), \
                 self.assertRaises(runtime.Refusal):
                runtime.inventory()


class ArtifactRoutineTests(unittest.TestCase):
    item = ArtifactInventoryTests.item

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.before = '2026-10-10T00:00:00Z'
        self.items = []
        self.runs = {}
        self.deleted = []
        self.add(1, 'main', '2026-10-01T00:00:00Z')
        self.add(2, 'main', '2026-10-02T00:00:00Z')
        self.add(3, 'obsolete-branch', '2026-10-01T00:00:00Z')
        self.add(4, 'feature', '2026-10-19T00:00:00Z')
        self.items += [self.item(5, 'factory-production-' + 'a' * 40), self.item(6, 'factory-deployment-metadata')]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 21, tzinfo=timezone.utc)
        for item in (patch.object(runtime, 'ROOT', self.root), patch.object(runtime, 'datetime', Clock),
                     patch.object(runtime, 'root_checked'), patch.object(runtime, 'gh', side_effect=self.api)):
            item.start()
            self.addCleanup(item.stop)

    def add(self, artifact_id, branch, created, name='driftward-ui-storybook'):
        row = self.item(artifact_id, name)
        row.update(created_at=created, updated_at=created, digest='sha256:' + 'b' * 64)
        row['workflow_run'].update(head_branch=branch, repository_id=1, head_repository_id=1)
        self.items.append(row)
        run_id = row['workflow_run']['id']
        self.runs[run_id] = {'id': run_id, 'name': 'Hatchet Quality', 'path': '.github/workflows/hatchet-quality.yml',
                             'event': 'push' if branch == 'main' else 'pull_request', 'head_branch': branch,
                             'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success',
                             'repository': {'full_name': runtime.REPO, 'id': 1},
                             'head_repository': {'full_name': runtime.REPO, 'id': 1},
                             'run_attempt': 1, 'workflow_id': 123, 'updated_at': created}
        return row

    def api(self, endpoint, target=None, method='GET'):
        self.assertIsNone(target)
        if method == 'DELETE':
            artifact_id = int(endpoint.rsplit('/', 1)[1])
            receipts = list(self.root.glob('routine-*.json'))
            self.assertEqual(len(receipts), 1)
            audit = json.loads(receipts[0].read_text())
            self.assertEqual(audit['events'][-1]['status'], 'delete_intent')
            self.assertEqual(audit['events'][-1]['artifact_id'], artifact_id)
            self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)
            self.deleted.append(artifact_id)
            return None
        if endpoint.startswith('actions/artifacts?'):
            rows = [item for item in self.items if item['id'] not in self.deleted]
            return {'total_count': len(rows), 'artifacts': rows}
        if endpoint.startswith('actions/runs/'):
            return self.runs[int(endpoint.rsplit('/', 1)[1])]
        self.fail(endpoint)

    def preview(self):
        return runtime.dispatch(['routine', 'preview', '--before', self.before])

    def prune(self, snapshot_digest=None):
        return runtime.dispatch(['routine', 'prune', '--before', self.before, '--snapshot-digest',
                                 snapshot_digest or self.preview()['snapshot_digest'], '--yes'])

    def audit(self):
        return json.loads(next(self.root.glob('routine-*.json')).read_text())

    def test_preview_selects_old_routine_only_and_retains_latest_global_and_main(self):
        view = self.preview()
        self.assertEqual([row['id'] for row in view['artifacts']], [1, 3])
        self.assertEqual([row['id'] for row in view['protected_artifacts']], [2, 4])
        self.assertEqual(view['total_bytes'], 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_both_exact_names_qualify_unknown_names_never_fetch_runs_or_delete(self):
        self.add(7, 'old-feature', '2026-10-01T00:00:00Z', 'factory-console-fixtures')
        self.add(8, 'main', '2026-10-02T00:00:00Z', 'factory-console-fixtures')
        self.items += [self.item(9, 'driftward-ui-storybook-evil'), self.item(10, 'factory-release-recovery')]
        self.assertEqual(self.prune()['artifact_ids'], [1, 3, 7])
        self.assertNotIn(5, self.deleted)
        self.assertNotIn(6, self.deleted)
        self.assertNotIn(9, self.deleted)
        self.assertNotIn(10, self.deleted)

    def test_seven_day_boundary_and_invalid_future_cutoff(self):
        for cutoff in ('2026-10-15T00:00:00Z', '2099-01-01T00:00:00Z'):
            with self.subTest(cutoff=cutoff), self.assertRaises(runtime.Refusal):
                runtime.routine_snapshot(cutoff)
        self.items[0]['created_at'] = self.before
        self.assertNotIn(1, [row['id'] for row in self.preview()['artifacts']])

    def test_updated_artifact_or_rerun_inside_window_is_retained(self):
        self.items[0]['updated_at'] = '2026-10-20T00:00:00Z'
        self.runs[1003]['updated_at'] = '2026-10-20T00:00:00Z'
        self.assertEqual(self.preview()['artifacts'], [])

    def test_expired_missing_digest_and_active_runs_protected(self):
        for mutation in ('expired', 'digest', 'active'):
            with self.subTest(mutation=mutation):
                if mutation == 'expired':
                    self.items[0]['expired'] = True
                elif mutation == 'digest':
                    self.items[0]['expired'] = False
                    self.items[0]['digest'] = None
                else:
                    self.items[0]['digest'] = 'sha256:' + 'b' * 64
                    self.runs[1001]['status'] = 'in_progress'
                view = self.preview()
                self.assertNotIn(1, [row['id'] for row in view['artifacts']])
                self.assertIn(1, [row['id'] for row in view['protected_artifacts']])

    def test_fork_wrong_workflow_sha_branch_and_repository_protected(self):
        changes = [('name', 'Other workflow'), ('path', '.github/workflows/other.yml'), ('event', 'workflow_dispatch'),
                   ('head_sha', 'f' * 40), ('head_branch', 'other'),
                   ('head_repository', {'full_name': 'fork/factory', 'id': 1}),
                   ('repository', {'full_name': 'other/factory', 'id': 1})]
        for field, value in changes:
            with self.subTest(field=field), patch.dict(self.runs[1001], {field: value}):
                view = self.preview()
                self.assertNotIn(1, [row['id'] for row in view['artifacts']])

    def test_approval_required_and_digest_drift_refused_before_delete(self):
        for args in (['routine', 'prune', '--before', self.before],
                     ['routine', 'prune', '--before', self.before, '--snapshot-digest', '0' * 64, '--yes']):
            with self.subTest(args=args), self.assertRaises(runtime.Refusal):
                runtime.dispatch(args)
        value = self.preview()['snapshot_digest']
        self.items[0]['size_in_bytes'] += 1
        with self.assertRaises(runtime.Refusal):
            self.prune(value)
        self.assertEqual(self.deleted, [])

    def test_prune_persists_intent_results_and_never_touches_legacy_files(self):
        for name in ('123.json', '123.zip'):
            (self.root / name).write_bytes(b'legacy-evidence')
        self.assertEqual(self.prune()['artifact_ids'], [1, 3])
        audit = self.audit()
        self.assertEqual([event['status'] for event in audit['events']],
                         ['delete_intent', 'deleted', 'delete_intent', 'deleted'])
        self.assertEqual(audit['snapshot']['total_bytes'], 400)
        for name in ('123.json', '123.zip'):
            self.assertEqual((self.root / name).read_bytes(), b'legacy-evidence')

    def test_ambiguous_delete_failure_stops_and_preserves_success_and_failure(self):
        api = self.api
        def fail(endpoint, target=None, method='GET'):
            if method == 'DELETE' and endpoint.endswith('/3'):
                raise OSError('connection lost after possible deletion')
            return api(endpoint, target, method)
        with patch.object(runtime, 'gh', side_effect=fail), self.assertRaises(OSError):
            self.prune()
        self.assertEqual(self.deleted, [1])
        self.assertEqual([event['status'] for event in self.audit()['events']],
                         ['delete_intent', 'deleted', 'delete_intent', 'delete_failed_or_unknown'])

    def test_immediate_revalidation_stops_drift_before_first_and_between_deletes(self):
        original = runtime.routine_snapshot
        for drift_call in (2, 3):
            count = 0
            def drift(before, runs=None):
                nonlocal count
                count += 1
                result = original(before, runs)
                if count == drift_call:
                    result['protected_artifacts'] = []
                return result
            value = self.preview()['snapshot_digest']
            with patch.object(runtime, 'routine_snapshot', side_effect=drift), self.assertRaises(runtime.Refusal):
                self.prune(value)
            self.assertEqual(self.deleted, [] if drift_call == 2 else [1])

    def test_existing_audit_is_never_overwritten(self):
        view = self.preview()
        path = self.root / f"routine-{view['snapshot_digest']}.json"
        path.write_bytes(b'existing-audit')
        with self.assertRaises(runtime.Refusal):
            self.prune(view['snapshot_digest'])
        self.assertEqual(path.read_bytes(), b'existing-audit')
        self.assertEqual(self.deleted, [])

    def test_audit_durability_failure_prevents_delete(self):
        with patch.object(runtime, 'atomic', side_effect=OSError('fsync failed')), self.assertRaises(OSError):
            self.prune()
        self.assertEqual(self.deleted, [])

    def test_api_calls_are_bounded_and_other_runs_cached_only_within_operation(self):
        self.items = []
        self.runs = {}
        for artifact_id in range(1, 101):
            self.add(artifact_id, 'main', '2026-10-01T00:00:00Z')
        view = self.preview()
        with patch.object(runtime, 'gh', side_effect=self.api) as api:
            result = self.prune(view['snapshot_digest'])
        self.assertEqual(len(result['artifact_ids']), 99)
        run_calls = [call for call in api.call_args_list if call.args[0].startswith('actions/runs/')]
        list_calls = [call for call in api.call_args_list if call.args[0].startswith('actions/artifacts?')]
        self.assertEqual(len(run_calls), 100 + 99)
        self.assertEqual(len(list_calls), 1 + 99)
        self.assertEqual(api.call_count, 398)  # Includes 99 explicit deletions.

    def test_current_candidate_run_is_fresh_and_drift_stops_before_deletion(self):
        for field, value in [('run_attempt', 2), ('status', 'in_progress'), ('updated_at', '2026-10-20T00:00:00Z')]:
            original = runtime.routine_snapshot
            calls = 0
            def change(before, runs=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    self.runs[1001][field] = value
                return original(before, runs)
            initial = self.runs[1001][field]
            view = self.preview()
            with self.subTest(field=field), patch.object(runtime, 'routine_snapshot', side_effect=change), \
                 self.assertRaises(runtime.Refusal):
                self.prune(view['snapshot_digest'])
            self.runs[1001][field] = initial
            self.assertEqual(self.deleted, [])

    def test_fixed_batch_bound_reports_remaining_and_actual_deleted_bytes(self):
        with patch.object(runtime, 'MAX_ROUTINE_DELETES', 1):
            view = self.preview()
            self.assertEqual(view['delete_limit'], 1)
            result = self.prune(view['snapshot_digest'])
        self.assertEqual(result['artifact_ids'], [1])
        self.assertEqual(result['remaining_count'], 1)
        self.assertEqual(result['total_bytes'], 100)


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

    def test_parent_is_synced_after_validation_for_new_and_existing_root(self):
        events = []
        original_lstat = Path.lstat
        def metadata(path):
            if path == self.root:
                events.append('root_validated')
                return original_lstat(path)
            return type('Metadata', (), {'st_mode': 0o040755, 'st_uid': 0})()
        with patch.object(Path, 'lstat', autospec=True, side_effect=metadata), \
             patch.object(runtime, 'sync_directory', side_effect=lambda path: events.append(('fsync', path))):
            self.root.rmdir()
            runtime.root_checked(create=True, durable=True)
            self.assertEqual(events, ['root_validated', ('fsync', self.root.parent)])
            events.clear()
            runtime.root_checked(create=False, durable=True)
            self.assertEqual(events, ['root_validated', ('fsync', self.root.parent)])
            events.clear()
            runtime.root_checked()
            self.assertEqual(events, ['root_validated'])

    def test_mutations_require_parent_durability_before_download_or_delete(self):
        for action in ('preserve', 'prune'):
            with self.subTest(action=action), \
                 patch.object(runtime, 'root_checked', side_effect=OSError('parent fsync failed')) as check:
                with self.assertRaises(OSError):
                    runtime.dispatch([action, '--before', self.before,
                                      '--snapshot-digest', self.view()['snapshot_digest'], '--yes'])
                check.assert_called_once_with(create=action == 'preserve', durable=True)
                self.assertEqual(self.downloads, 0)
                self.assertEqual(self.deleted, [])


if __name__ == '__main__':
    unittest.main()
