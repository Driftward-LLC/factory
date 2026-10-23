"""Root CLI helper regression with disposable bridge state and fake service boundaries."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hatchet/bridge'))
from hatchet_hermes_bridge import store

spec = importlib.util.spec_from_file_location('recovery_runtime', ROOT / 'deploy/factory-profile-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
spec = importlib.util.spec_from_file_location('recovery_cli', ROOT / 'deploy/factory-cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class InspectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'bridge.db'
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        store.init_db(self.conn)
        self.request = 'factory:inspect:ENG-1:legacy:example/repo:1'
        self.payload = {'repo': 'example/repo', 'dry_run': False,
                        'inspection': {'repository': 'example/repo', 'number': 1, 'url': 'https://github.com/example/repo/pull/1'},
                        'inspection_request_key': self.request, 'inspection_capability': str(uuid.uuid4())}
        with patch.object(store, '_canonical_enabled_repo', return_value=True):
            self.job, _ = store.submit_job(self.conn, {'idempotency_key': self.request,
                'requested_action': 'inspect_draft_pr_state', 'payload': self.payload}, internal_inspection=True)
        self.job_id = self.job['job_id']
        old = '2026-01-01T00:00:00.000000Z'
        with self.conn:
            self.conn.execute('UPDATE bridge_jobs SET created_at=?,updated_at=? WHERE job_id=?', (old, old, self.job_id))
            self.conn.execute('INSERT INTO factory_inspection_requests (request_key,repository,number,url,job_id,work_item_key,capability,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.request, 'example/repo', 1, self.payload['inspection']['url'], self.job_id, 'ENG-1', self.payload['inspection_capability'], 'pending', old, old))
            self.conn.execute('INSERT INTO factory_inspection_outbox (outbox_key,request_key,job_id,payload_json,state,delivery_attempts,acknowledged_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (self.request + ':accepted', self.request, self.job_id, '{}', 'acknowledged', 1, old, old, old))
        self.running = True
        self.executor_state = 'exited'
        def rows():
            return [{'Service': name, 'State': 'running' if self.running else 'exited'}
                    for name in ('bridge', 'factory-operator', 'outbox-dispatcher', 'execution-dispatch-worker')] + [
                    {'Service': 'bridge-executor-worker', 'State': self.executor_state}]
        def compose(*args, **kwargs):
            self.running = args[0] == 'up'
        self.install = SimpleNamespace(directory=self.root, project='fixture', service_rows=rows,
                                       compose=Mock(side_effect=compose))
        self.gateway = Mock(return_value={'ActiveState': 'activating', 'SubState': 'auto-restart', 'Result': 'exit-code', 'ExecMainStatus': 226})
        def ctl(*args):
            if args[0] == 'stop':
                self.gateway.return_value = {'ActiveState': 'inactive'}
        for patcher in (patch.object(runtime, 'bridge_database', return_value=self.database),
                        patch.object(runtime, 'gateway_status', self.gateway),
                        patch.object(runtime, 'systemctl', side_effect=ctl),
                        patch.object(runtime, 'run_native', return_value={'idle': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def preview(self):
        return runtime.recover_inspection(self.install, '/fixture', [self.job_id, 'preview'])

    def apply_args(self, digest=None):
        return [self.job_id, 'apply', '--snapshot-digest', digest or self.preview()['snapshot_digest'],
                '--reason', 'Expire abandoned inspection before role cutover', '--request-id', str(uuid.uuid4()), '--yes']

    def test_preview_is_read_only_bounded_and_apply_audited_idempotent(self):
        before = self.conn.execute('SELECT * FROM factory_inspection_outbox').fetchone()
        changes = self.conn.total_changes
        preview = self.preview()
        self.assertTrue(preview['read_only'])
        self.assertNotIn(self.payload['inspection_capability'], json.dumps(preview))
        self.assertEqual(self.conn.total_changes, changes)
        self.install.compose.assert_not_called()
        args = self.apply_args(preview['snapshot_digest'])
        result = runtime.recover_inspection(self.install, '/fixture', args)
        self.assertTrue(result['changed'])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM factory_inspection_outbox').fetchone()), dict(before))
        self.assertEqual(self.conn.execute('SELECT state FROM factory_inspection_requests').fetchone()[0], 'expired')
        self.assertEqual(self.conn.execute('SELECT state FROM bridge_jobs').fetchone()[0], 'failed')
        events = self.conn.execute('SELECT * FROM bridge_events ORDER BY created_at').fetchall()
        self.assertEqual(len(events), 2)
        audit = json.loads(events[1]['details_json'])
        self.assertEqual(audit['actor'], 'root')
        self.assertEqual(audit['snapshot_digest'], preview['snapshot_digest'])
        count = self.install.compose.call_count
        self.assertFalse(runtime.recover_inspection(self.install, '/fixture', args)['changed'])
        self.assertEqual(self.install.compose.call_count, count)
        self.assertTrue(self.running)
        self.assertEqual(self.executor_state, 'exited')
        self.assertIn('--no-deps', self.install.compose.call_args.args)
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET error='drift'")
        with self.assertRaisesRegex(ValueError, 'drift'):
            runtime.recover_inspection(self.install, '/fixture', args)

    def test_refuses_changed_snapshot_and_live_executor_without_stopping_services(self):
        args = self.apply_args()
        with self.conn:
            self.conn.execute("UPDATE bridge_jobs SET updated_at='changed'")
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            runtime.recover_inspection(self.install, '/fixture', args)
        self.install.compose.assert_not_called()
        self.executor_state = 'running'
        with self.assertRaisesRegex(ValueError, 'executor already exited'):
            runtime.recover_inspection(self.install, '/fixture', self.apply_args())
        self.install.compose.assert_not_called()

    def test_evidence_and_unexpired_capability_refused(self):
        for column, value in [('state', 'running'), ('commit_sha', 'a' * 40), ('hatchet_workflow_run_id', 'workflow')]:
            with self.subTest(column=column), self.conn:
                original = self.conn.execute(f'SELECT {column} FROM bridge_jobs').fetchone()[0]
                self.conn.execute(f'UPDATE bridge_jobs SET {column}=?', (value,))
                self.conn.commit()
                with self.assertRaises(ValueError):
                    self.preview()
                self.conn.execute(f'UPDATE bridge_jobs SET {column}=?', (original,))
                self.conn.commit()
        with self.conn:
            self.conn.execute("UPDATE factory_inspection_outbox SET delivery_lease_id='claimed'")
        with self.assertRaises(ValueError):
            self.preview()
        with self.conn:
            self.conn.execute('UPDATE factory_inspection_outbox SET delivery_lease_id=NULL')
            self.conn.execute('UPDATE factory_inspection_requests SET created_at=?', (store.utc_now(),))
        with self.assertRaisesRegex(ValueError, 'not expired'):
            self.preview()

    def test_transaction_rechecks_after_native_idle_and_restores_services(self):
        args = self.apply_args()
        def race(*args):
            with self.conn:
                self.conn.execute("UPDATE bridge_jobs SET state='running'")
        with patch.object(runtime, 'run_native', side_effect=race):
            with self.assertRaises(ValueError):
                runtime.recover_inspection(self.install, '/fixture', args)
        self.assertTrue(self.running)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM bridge_events').fetchone()[0], 1)
        self.assertFalse(json.loads((self.root / 'inspection-recovery.json').read_text())['committed'])

    def test_idle_failure_restores_workers_even_if_gateway_restore_fails(self):
        args = self.apply_args()
        def ctl(*args):
            if args[0] == 'stop':
                self.gateway.return_value = {'ActiveState': 'inactive'}
            else:
                raise RuntimeError('SECRET gateway diagnostics')
        with patch.object(runtime, 'systemctl', side_effect=ctl), patch.object(runtime, 'run_native', side_effect=ValueError('SECRET native')):
            with self.assertRaises(ValueError):
                runtime.recover_inspection(self.install, '/fixture', args)
        self.assertTrue(self.running)
        receipt = (self.root / 'inspection-recovery.json').read_text()
        self.assertNotIn('SECRET', receipt)
        self.assertEqual(json.loads(receipt)['errors'], ['native_idle', 'gateway_restore'])
        self.assertEqual(self.conn.execute('SELECT state FROM bridge_jobs').fetchone()[0], 'accepted')

    def test_terminal_prior_lineage_is_supported_but_execution_lease_blocks(self):
        with self.conn:
            self.conn.execute('INSERT INTO factory_attempts (attempt_id,work_item_key,action,mode,generation,request_id,idempotency_key,branch,status,actor,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                              ('prior', 'ENG-1', 'create_draft_pr', 'review', 1, 'original', 'original', 'original', 'verified_ready', 'fixture', 'old', 'old'))
            self.conn.execute("UPDATE factory_inspection_requests SET prior_attempt_id='prior'")
        self.preview()
        with self.conn:
            self.conn.execute('INSERT INTO factory_execution_leases (attempt_id,lease_id,owner,acquired_at,expires_at) VALUES (?,?,?,?,?)', ('prior', 'lease', 'executor', 'old', 'future'))
        with self.assertRaises(ValueError):
            self.preview()
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET released_at='old'")
            self.conn.execute("UPDATE factory_attempts SET status='running'")
        with self.assertRaises(ValueError):
            self.preview()

    def test_committed_expiry_status_survives_gateway_restore_failure(self):
        args = self.apply_args()
        def ctl(*args):
            if args[0] == 'stop':
                self.gateway.return_value = {'ActiveState': 'inactive'}
            else:
                raise RuntimeError('private gateway failure')
        with patch.object(runtime, 'systemctl', side_effect=ctl):
            with self.assertRaises(ValueError):
                runtime.recover_inspection(self.install, '/fixture', args)
        status = runtime.recover_inspection(self.install, '/fixture', [self.job_id, 'status'])
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['inspection_states'], ['expired'])
        self.assertEqual(status['outbox_states'], ['acknowledged'])
        self.assertTrue(status['recovery_audit_present'])
        self.assertEqual(status['recovery'], {'phase': 'requires_operator', 'committed': True, 'errors': ['gateway_restore']})
        self.assertTrue(self.running)
        self.assertNotIn(self.payload['inspection_capability'], json.dumps(status))
        self.assertFalse(runtime.recover_inspection(self.install, '/fixture', args)['changed'])

    def test_missing_approval_and_reused_request_intent_are_refused(self):
        args = self.apply_args()
        with self.assertRaises(ValueError):
            runtime.recover_inspection(self.install, '/fixture', args[:-1])
        self.install.compose.assert_not_called()
        runtime.recover_inspection(self.install, '/fixture', args)
        args[args.index('--reason') + 1] = 'A different operator intent'
        with self.assertRaisesRegex(ValueError, 'reuse'):
            runtime.recover_inspection(self.install, '/fixture', args)

    def test_stop_error_still_restores_initial_running_services(self):
        args = self.apply_args()
        def compose(*args, **kwargs):
            self.running = False
            if args[0] == 'stop':
                raise RuntimeError('private stop failure')
            self.running = True
        self.install.compose.side_effect = compose
        with self.assertRaises(ValueError):
            runtime.recover_inspection(self.install, '/fixture', args)
        self.assertTrue(self.running)
        self.assertEqual(json.loads((self.root / 'inspection-recovery.json').read_text())['errors'], ['writers_stop'])

    def test_benign_stale_diagnostic_is_bound_without_volatile_counter_churn(self):
        with self.conn:
            self.conn.execute('INSERT INTO reconciliation_findings VALUES (?,?,?,?,?,?,?,?)',
                              ('warning', self.job_id, 'bridge:' + self.job_id, 'stale_active_bridge_job', '{}', 'old', 'old', 1))
        args = self.apply_args()
        with self.conn:
            self.conn.execute("UPDATE reconciliation_findings SET last_seen_at='new',occurrence_count=2")
        retained = dict(self.conn.execute('SELECT * FROM reconciliation_findings').fetchone())
        runtime.recover_inspection(self.install, '/fixture', args)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM reconciliation_findings').fetchone()), retained)

    def test_artifact_diagnostic_is_unsupported(self):
        with self.conn:
            self.conn.execute('INSERT INTO reconciliation_findings VALUES (?,?,?,?,?,?,?,?)',
                              ('warning', self.job_id, 'bridge:' + self.job_id, 'failed_job_has_external_effect_refs', '{}', 'old', 'old', 1))
        with self.assertRaises(ValueError):
            self.preview()

    def test_gateway_socket_listener_is_only_a_transport_probe(self):
        path = self.root / 'gateway.sock'
        with patch.object(runtime, 'Path', return_value=path):
            self.assertFalse(runtime.gateway_socket_listening())
            path.write_text('not a socket')
            self.assertFalse(runtime.gateway_socket_listening())
            with patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFSOCK)), \
                 patch.object(runtime.socket, 'socket') as create:
                create.return_value.__enter__.return_value.connect.side_effect = OSError('not listening')
                self.assertFalse(runtime.gateway_socket_listening())
                create.return_value.__enter__.return_value.connect.side_effect = None
                self.assertTrue(runtime.gateway_socket_listening())
                create.return_value.__enter__.return_value.settimeout.assert_called_with(0.5)
                create.return_value.__enter__.return_value.connect.assert_called_with(str(path))
            path.unlink()
            path.symlink_to(self.root / 'missing')
            self.assertFalse(runtime.gateway_socket_listening())

    def test_canonical_cli_dispatches_real_backend_in_isolation(self):
        binding = {'hermes_wrapper': '/fixture', 'profile_runtime': str(ROOT / 'deploy/factory-profile-runtime.py')}
        import contextlib
        import io
        output = io.StringIO()
        original = cli.importlib.util.module_from_spec
        def module(spec):
            backend = original(spec)
            spec.loader.exec_module(backend)
            backend.bridge_database = lambda install: self.database
            backend.gateway_status = runtime.gateway_status
            backend.systemctl = runtime.systemctl
            backend.run_native = runtime.run_native
            spec.loader = SimpleNamespace(exec_module=lambda module: None)
            return backend
        with patch.object(cli, 'installation', return_value=self.install), \
             patch.object(cli, 'checked_hermes_wrapper', side_effect=Path), \
             patch.object(cli.importlib.util, 'module_from_spec', side_effect=module), contextlib.redirect_stdout(output):
            self.assertEqual(cli.dispatch_bound(binding, ['profiles', 'recover-inspection', self.job_id, 'preview']), 0)
            preview = json.loads(output.getvalue())
            output.seek(0)
            output.truncate()
            self.assertEqual(cli.dispatch_bound(binding, ['profiles', 'recover-inspection', *self.apply_args(preview['snapshot_digest'])]), 0)
            self.assertTrue(json.loads(output.getvalue())['recovered'])
            output.seek(0)
            output.truncate()
            self.assertEqual(cli.dispatch_bound(binding, ['profiles', 'recover-inspection', self.job_id, 'status']), 0)
        self.assertEqual(json.loads(output.getvalue())['state'], 'failed')
        self.assertTrue(json.loads(output.getvalue())['recovery_audit_present'])


if __name__ == '__main__':
    unittest.main()
