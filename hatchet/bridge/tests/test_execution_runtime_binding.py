import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hatchet_hermes_bridge import store
from test_support import connect_migrate
from test_server import _PostHandler
from test_store import verifier_payload


class ExecutionRuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect_migrate(str(Path(tmp.name) / 'bridge.db'))
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, {'HATCHET_VERIFIER_POLICY_PATH': str(Path(__file__).resolve().parents[2] / 'workers/verifier-policy.json')})
        env.start()
        self.addCleanup(env.stop)
        self.attempt, self.job, _ = store.reserve_factory_retry(
            self.conn, work_item_key='ENG-601', mode='real', request_id='runtime-binding-test', reason='test', actor='test',
            payload=verifier_payload('Driftward-LLC/capykit'))
        store.transition_job(self.conn, self.job['job_id'], state='running')
        self.lease = store.claim_factory_execution(self.conn, self.job['job_id'], owner='test-executor')['lease']
        store.authorize_factory_effect(self.conn, self.job['job_id'], self.lease)
        self.runtime = {'schema': 1, 'backend': 'hermes', 'kind': 'docker', 'instance_id': 'a' * 64,
                        'image_digest': 'sha256:' + 'b' * 64, 'incarnation': 'sha256:' + 'c' * 64}
        self.receipt = {'pr_url': 'https://github.com/Driftward-LLC/capykit/pull/601',
                        'branch': self.job['branch'], 'commit_sha': 'd' * 40}

    def bind(self, runtime=None, lease=None):
        return store.bind_factory_execution_runtime(self.conn, self.job['job_id'], lease or self.lease, runtime or self.runtime)

    def test_binding_is_durable_idempotent_and_visible_without_fence(self):
        self.assertTrue(self.bind())
        self.assertFalse(self.bind())
        expected = {'lease_id': self.lease['lease_id'], 'epoch': self.lease['epoch'], 'runtime': self.runtime}
        self.assertEqual(store.get_job(self.conn, self.job['job_id'])['execution_runtime'], expected)
        lifecycle = store.list_active_factory_attempts(self.conn)[0]
        self.assertEqual(lifecycle['execution_runtime'], expected)
        events = [e for e in store.list_factory_attempt_events(self.conn, self.attempt['attempt_id']) if e['event_type'] == 'execution_runtime_bound']
        self.assertEqual([e['details'] for e in events], [expected])
        self.assertNotIn(self.lease['fence_token'], str(expected))

    def test_replacement_restart_and_stale_fence_cannot_rebind(self):
        self.bind()
        for key in ('instance_id', 'image_digest', 'incarnation'):
            changed = ('sha256:' if key != 'instance_id' else '') + 'e' * 64
            with self.assertRaises(store.BridgeConflict):
                self.bind({**self.runtime, key: changed})
        with self.assertRaises(store.BridgeConflict):
            self.bind(lease={**self.lease, 'epoch': self.lease['epoch'] + 1})

    def test_expired_and_canceled_leases_cannot_bind(self):
        for column, value in [('expires_at', '2000-01-01T00:00:00Z'), ('cancel_requested_at', '2026-01-01T00:00:00Z')]:
            with self.subTest(column=column):
                original = self.conn.execute(f'SELECT {column} FROM factory_execution_leases').fetchone()[0]
                with self.conn:
                    self.conn.execute(f'UPDATE factory_execution_leases SET {column}=?', (value,))
                with self.assertRaises(store.BridgeConflict):
                    self.bind()
                with self.conn:
                    self.conn.execute(f'UPDATE factory_execution_leases SET {column}=?', (original,))

    def test_receipt_requires_original_binding_including_recovery_after_expiry(self):
        self.bind()
        for supplied in [None, {**self.runtime, 'instance_id': 'e' * 64}, {**self.runtime, 'incarnation': 'sha256:' + 'e' * 64}]:
            with self.assertRaises(store.BridgeValidationError):
                store.commit_factory_execution(self.conn, self.job['job_id'], self.lease, {**self.receipt, 'execution_runtime': supplied})
        with self.conn:
            self.conn.execute("UPDATE factory_execution_leases SET expires_at='2000-01-01T00:00:00Z'")
        store.commit_factory_execution(self.conn, self.job['job_id'], self.lease, {**self.receipt, 'execution_runtime': self.runtime})
        self.assertEqual(store.get_job(self.conn, self.job['job_id'])['state'], 'effect_committed')
        self.assertEqual(store.get_execution_runtime(self.conn, self.job['job_id'])['runtime'], self.runtime)

    def test_legacy_execution_stays_unbound_and_cannot_adopt_a_new_runtime(self):
        store.record_factory_execution_analytics(self.conn, self.job['job_id'], self.lease,
            event_type='execution_backend_invoked', details={'analytics_schema_version': 1, 'invocation_id': 'test-invocation',
            'execution_backend': 'hermes', 'execution_transport': 'grpc_uds', 'backend_version': None, 'configured_auth_mode': 'unknown'})
        with self.assertRaises(store.BridgeConflict):
            self.bind()
        self.assertIsNone(store.get_execution_runtime(self.conn, self.job['job_id']))
        with self.assertRaises(store.BridgeValidationError):
            store.commit_factory_execution(self.conn, self.job['job_id'], self.lease, {**self.receipt, 'execution_runtime': self.runtime})
        store.commit_factory_execution(self.conn, self.job['job_id'], self.lease, self.receipt)

    def settle_commit_failure(self, runtime):
        self.bind()
        identity = {'analytics_schema_version': 1, 'invocation_id': 'bound-invocation', 'execution_backend': 'hermes',
                    'execution_transport': 'grpc_uds', 'backend_version': None, 'configured_auth_mode': 'unknown'}
        store.record_factory_execution_analytics(self.conn, self.job['job_id'], self.lease,
            event_type='execution_backend_invoked', details=identity)
        store.settle_factory_execution(self.conn, self.job['job_id'], self.lease, effect_observed=False,
            failure_event_type='execution_effect_commit_failed', failure_details={**identity,
                'failure_domain': 'factory', 'failure_stage': 'effect_commit', 'reason_code': 'factory_effect_commit_failed',
                'retryable': None, 'failure_disposition': 'recovery_required', 'operator_intervention_required': True,
                'effect_outcome': 'observed', 'elapsed_ms': 10, 'observed_pr_url': self.receipt['pr_url'],
                'observed_commit_sha': self.receipt['commit_sha'], 'observed_branch': self.receipt['branch'],
                'observed_hermes_session_id': None, 'backend_outcome': 'created', 'verification_context': None,
                'execution_runtime': runtime})

    def test_commit_failure_cannot_bypass_runtime_fence(self):
        self.settle_commit_failure({**self.runtime, 'incarnation': 'sha256:' + 'e' * 64})
        self.assertEqual(store.get_job(self.conn, self.job['job_id'])['state'], 'running')
        self.assertIsNone(self.conn.execute('SELECT released_at FROM factory_execution_leases').fetchone()[0])
        events = store.list_factory_attempt_events(self.conn, self.attempt['attempt_id'])
        self.assertIn('execution_effect_commit_receipt_rejected', [e['event_type'] for e in events])

    def test_commit_failure_recovers_receipt_from_original_runtime(self):
        self.settle_commit_failure(self.runtime)
        self.assertEqual(store.get_job(self.conn, self.job['job_id'])['state'], 'effect_committed')

    def test_http_rejects_unbounded_runtime_data_and_accepts_exact_fence(self):
        claim = {key: self.lease[key] for key in ('lease_id', 'owner', 'epoch', 'fence_token')}
        for runtime in [{**self.runtime, 'secret': 'private'}, {**self.runtime, 'instance_id': 'factory-hermes'}, {**self.runtime, 'schema': True}]:
            handler = _PostHandler(self.conn, {**claim, 'runtime': runtime}, f"/jobs/{self.job['job_id']}/factory-execution/runtime")
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
        handler = _PostHandler(self.conn, {**claim, 'runtime': self.runtime}, f"/jobs/{self.job['job_id']}/factory-execution/runtime")
        handler.do_POST()
        self.assertEqual(handler.response[0], 201)
