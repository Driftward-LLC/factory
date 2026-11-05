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
