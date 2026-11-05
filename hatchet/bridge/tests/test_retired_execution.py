from organization_fixtures import configure
setUpModule, tearDownModule = configure()
import json
import unittest
from hatchet_hermes_bridge import retired_execution, store, interrupted_resolution as recovery
import test_interrupted_resolution as base


class RetiredExecutionTests(unittest.TestCase):
    inspection=base.InterruptedResolutionTests.inspection
    preview=base.InterruptedResolutionTests.preview
    command=base.InterruptedResolutionTests.command
    def setUp(self):
        base.InterruptedResolutionTests.setUp(self)
        self.runtime={'schema':1,'backend':'hermes','kind':'docker','instance_id':'d'*64,'image_digest':'sha256:'+'e'*64,'incarnation':'sha256:'+'f'*64}
        self.evidence['runtime_ref']=self.runtime['instance_id']
        with self.conn:
            store._append_factory_attempt_event(self.conn,self.aid,'execution_runtime_bound',{'lease_id':self.lease['lease_id'],'epoch':self.lease['epoch'],'runtime':self.runtime})

    def descriptor(self):
        return retired_execution.descriptors(self.conn,fingerprint_job=lambda job:'a'*64)

    def resolved(self):
        recovery.resolve(self.conn,self.aid,**self.command())

    def test_only_committed_root_retirement_yields_read_only_bound_descriptor(self):
        self.assertEqual(self.descriptor(),[])
        self.resolved();before=self.conn.total_changes
        value=self.descriptor()
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(len(value),1)
        self.assertEqual(value[0]['runtime'],self.runtime)
        self.assertEqual(value[0]['lease_id'],self.lease['lease_id'])
        self.assertNotIn('fence_token',value[0]);self.assertEqual(value[0]['fingerprint_version'],2)

    def test_revoked_authority_is_not_retired_runtime(self):
        self.evidence['containment_method']='write_authority_revoked'
        self.resolved();self.assertEqual(self.descriptor(),[])

    def test_tampered_evidence_and_audit_refuse(self):
        self.resolved()
        original=self.conn.execute('SELECT evidence_json FROM factory_recovery_requests').fetchone()[0]
        with self.conn:self.conn.execute("UPDATE factory_recovery_requests SET evidence_json='{}'")
        with self.assertRaisesRegex(ValueError,'digest'):self.descriptor()
        with self.conn:
            self.conn.execute('UPDATE factory_recovery_requests SET evidence_json=?',(original,))
            self.conn.execute("UPDATE factory_attempt_events SET details_json='{}' WHERE event_type=?",(recovery.EVENT,))
        with self.assertRaisesRegex(ValueError,'audit'):self.descriptor()

    def test_exact_runtime_lease_and_failed_unknown_state_required(self):
        self.resolved()
        for sql,params in [("UPDATE bridge_jobs SET reason_code='ordinary_failure'",()),
                           ('UPDATE factory_execution_leases SET released_at=NULL',()),
                           ("UPDATE factory_execution_leases SET effect_observed_at='2026-11-05T00:00:00Z'",())]:
            self.conn.execute('SAVEPOINT tamper')
            self.conn.execute(sql,params)
            with self.assertRaisesRegex(ValueError,'failed unknown'):self.descriptor()
            self.conn.execute('ROLLBACK TO tamper');self.conn.execute('RELEASE tamper')
        with self.conn:self.conn.execute("UPDATE factory_attempt_events SET details_json=? WHERE event_type='execution_runtime_bound'",(json.dumps({'lease_id':self.lease['lease_id'],'epoch':self.lease['epoch'],'runtime':{**self.runtime,'instance_id':'c'*64}}),))
        with self.assertRaisesRegex(ValueError,'exact runtime'):self.descriptor()

    def retry(self):
        control = store.get_factory_issue_control(self.conn, 'ENG-600')
        result = store.return_factory_issue_control(self.conn, work_item_key='ENG-600',
            request_id='resolved-retry', actor='operator', reason='reviewed fresh attempt',
            expected_revision=control['revision'], expected_snapshot_digest=control['snapshot_digest'],
            payload={**base.verifier_payload('example/widgets'), 'trusted_snapshot_digest':'a'*64},
            prior_attempt_id=self.aid)
        return result['attempt'], result['job']

    def test_legitimate_retry_preserves_root_retirement_through_success(self):
        self.resolved(); expected=self.descriptor()
        successor, job=self.retry()
        self.assertEqual(store.get_factory_attempt(self.conn,self.aid)['status'],'superseded')
        self.assertEqual(self.descriptor(),expected)
        # The descriptor uses historical retirement authority, regardless of successor progress.
        with self.conn:
            self.conn.execute("UPDATE factory_attempts SET status='verified_ready' WHERE attempt_id=?",(successor['attempt_id'],))
            self.conn.execute("UPDATE bridge_jobs SET state='ready_for_human_review' WHERE job_id=?",(job['job_id'],))
        before=self.conn.total_changes
        self.assertEqual(self.descriptor(),expected)
        self.assertEqual(self.conn.total_changes,before)

    def test_superseded_summary_without_legitimate_successor_refuses(self):
        self.resolved()
        with self.conn:self.conn.execute("UPDATE factory_attempts SET status='superseded' WHERE attempt_id=?",(self.aid,))
        with self.assertRaisesRegex(ValueError,'failed unknown'):self.descriptor()

    def test_retry_must_keep_original_resolution_and_exact_audit(self):
        self.resolved();self.retry()
        for sql,params,error in [
            ("UPDATE factory_recovery_requests SET evidence_json='{}'",(),'digest'),
            ("DELETE FROM factory_attempt_events WHERE event_type=?",(recovery.EVENT,),'audit'),
            ("UPDATE bridge_jobs SET reason_code='ordinary_failure' WHERE job_id=?",(self.job['job_id'],),'failed unknown')]:
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT tamper');self.conn.execute(sql,params)
                with self.assertRaisesRegex(ValueError,error):self.descriptor()
                self.conn.execute('ROLLBACK TO tamper');self.conn.execute('RELEASE tamper')

    def test_successor_lineage_payload_and_events_must_agree(self):
        self.resolved();successor,job=self.retry()
        for sql,params in [
            ("DELETE FROM factory_attempt_events WHERE attempt_id=? AND event_type='superseded'",(self.aid,)),
            ("DELETE FROM factory_attempt_events WHERE attempt_id=? AND event_type='retry_requested'",(successor['attempt_id'],)),
            ("UPDATE factory_attempts SET prior_attempt_id=NULL WHERE attempt_id=?",(successor['attempt_id'],)),
            ("UPDATE factory_attempts SET work_item_key='ENG-601' WHERE attempt_id=?",(successor['attempt_id'],)),
            ("UPDATE factory_attempts SET mode='dry-run' WHERE attempt_id=?",(successor['attempt_id'],)),
            ("UPDATE bridge_jobs SET requested_action='read_only_review' WHERE job_id=?",(job['job_id'],)),
            ("UPDATE bridge_jobs SET branch='unrelated-branch' WHERE job_id=?",(job['job_id'],)),
            ("UPDATE factory_attempts SET generation=0 WHERE attempt_id=?",(successor['attempt_id'],)),
            ("UPDATE bridge_jobs SET payload_json=json_set(payload_json,'$.factory_retry.prior_attempt_id','unrelated') WHERE job_id=?",(job['job_id'],)),
            ("UPDATE bridge_jobs SET payload_json=json_set(payload_json,'$.factory_retry.generation',99) WHERE job_id=?",(job['job_id'],)),
            ("UPDATE bridge_jobs SET payload_json=json_set(payload_json,'$.repo','unrelated/repository') WHERE job_id=?",(job['job_id'],))]:
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT tamper');self.conn.execute(sql,params)
                with self.assertRaisesRegex(ValueError,'failed unknown'):self.descriptor()
                self.conn.execute('ROLLBACK TO tamper');self.conn.execute('RELEASE tamper')

    def test_missing_resolution_never_yields_retirement_exemption_after_retry(self):
        self.resolved();self.retry()
        with self.conn:self.conn.execute('DELETE FROM factory_recovery_requests')
        self.assertEqual(self.descriptor(),[])

    def test_ambiguous_supersession_event_refuses(self):
        self.resolved();successor,_=self.retry()
        with self.conn:store._append_factory_attempt_event(self.conn,self.aid,'superseded',{'by_attempt_id':successor['attempt_id'],'supersede_pr_url':None})
        with self.assertRaisesRegex(ValueError,'failed unknown'):self.descriptor()
