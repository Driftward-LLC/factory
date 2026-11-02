"""A queued capability belongs to its original configured organization."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from organization_fixtures import configure
setUpModule, tearDownModule = configure()
from hatchet_hermes_bridge import store
from test_issue_control_store import IssueControlStoreTests
from test_store import verifier_payload


class OrganizationQueueTests(unittest.TestCase):
    setUp = IssueControlStoreTests.setUp
    intake = IssueControlStoreTests.intake
    claim = IssueControlStoreTests.claim
    commit = IssueControlStoreTests.commit

    def different_organization(self):
        value = json.loads(Path(os.environ['FACTORY_ORGANIZATION_CONFIG']).read_text())
        value['linear']['workspace_id'] = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        value['linear']['team_id'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        target = Path(self.tmp.name) / 'different-organization.json'
        target.write_text(json.dumps(value))
        return patch.dict(os.environ, {'FACTORY_ORGANIZATION_CONFIG': str(target)})

    def test_same_prefix_changed_workspace_and_team_cannot_resume_or_authorize(self):
        _, job, _ = self.intake()
        claim = self.claim(job)
        with self.different_organization():
            self.assertFalse(store.authorize_factory_effect(self.conn, job['job_id'], claim))
            self.assertFalse(store.execution_may_effect(self.conn, job['job_id'], claim))
            self.assertEqual(store.claim_factory_execution(self.conn, job['job_id'], owner='resume')['reason'], 'organization_binding_mismatch')
            self.assertEqual(store.get_job(self.conn, job['job_id'])['payload']['organization_binding'], job['payload']['organization_binding'])
        self.assertTrue(store.authorize_factory_effect(self.conn, job['job_id'], claim))

    def test_observed_old_identity_is_not_restamped_during_admission(self):
        payload = verifier_payload('example/widgets')
        with self.different_organization():
            with self.assertRaisesRegex(store.BridgeValidationError, 'organization_binding_mismatch'):
                store.reserve_factory_intake(self.conn, work_item_key=self.issue, mode='real', request_id='stale-observation', reason='fixture', actor='intake-worker', payload=payload)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM factory_attempts').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bridge_jobs').fetchone()[0], 0)

    def test_historical_unbound_job_remains_readable_without_new_authority(self):
        attempt, job, _ = self.intake()
        payload = dict(job['payload']); payload.pop('organization_binding')
        with self.conn:
            self.conn.execute('UPDATE bridge_jobs SET payload_json=?,payload_hash=? WHERE job_id=?', (store.canonical_json(payload), store.payload_hash(payload), job['job_id']))
        self.assertEqual(store.get_job(self.conn, job['job_id'])['job_id'], job['job_id'])
        self.assertEqual(store.get_factory_attempt(self.conn, attempt['attempt_id'])['work_item_key'], self.issue)
        self.assertFalse(store.claim_factory_execution(self.conn, job['job_id'], owner='legacy-resume')['claimed'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM factory_execution_leases').fetchone()[0], 0)

    def test_late_observed_receipt_survives_organization_change(self):
        _, job, _ = self.intake()
        claim = self.claim(job, authorize=True)
        with self.different_organization():
            recorded = self.commit(job, claim)
        self.assertEqual(recorded['pr_url'], 'https://github.com/example/widgets/pull/600')
