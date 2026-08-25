import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { recoverableStaleVerificationJobs } from './verification-recovery';
import { redispatchStaleVerification } from './verification-lease-recovery';
import type { ReconciliationFinding } from './cross-provider-reconciliation';

test('recovery redispatches only the original real PR verification work item', () => {
  const job = fixture();
  const recovered = recoverableStaleVerificationJobs([{ kind: 'stale_active_bridge_job', jobId: job.job_id }], new Map([[job.job_id, job]]));
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].idempotency_key, job.idempotency_key);
  assert.equal(recovered[0].branch, job.branch);
  const dryRun = { ...job, payload: { ...job.payload, dry_run: true } };
  const noPr = { ...job, pr_url: null };
  assert.equal(recoverableStaleVerificationJobs([{ kind: 'stale_active_bridge_job', jobId: job.job_id }], new Map([[job.job_id, dryRun]])).length, 0);
  assert.equal(recoverableStaleVerificationJobs([{ kind: 'stale_active_bridge_job', jobId: job.job_id }], new Map([[job.job_id, noPr]])).length, 0);
});

test('concurrent reconciliation redelivery emits one resume event only after a lease is acquired', async () => {
  const job = fixture();
  let acquires = 0;
  let pushes = 0;
  const bridge = {
    async acquireVerificationLease() {
      acquires += 1;
      return { acquired: acquires === 1, lease: {} };
    },
  };
  const push = async () => { pushes += 1; };
  const findings: ReconciliationFinding[] = [{
    findingKey: 'reconciliation:v1:stale_active_bridge_job:job-recover', kind: 'stale_active_bridge_job', jobId: job.job_id,
    severity: 'warning', subject: `bridge:${job.job_id}`, reason: 'stale', expected: {}, observed: {},
  }];
  const jobs = new Map([[job.job_id, job]]);
  const [first, duplicate] = await Promise.all([
    redispatchStaleVerification(findings, jobs, bridge, push),
    redispatchStaleVerification(findings, jobs, bridge, push),
  ]);
  assert.equal(first + duplicate, 0);
  assert.equal(acquires, 2);
  assert.equal(pushes, 1);
});

function fixture(): BridgeJob {
  return { job_id: 'job-recover', idempotency_key: 'hatchet:work-item:linear:ENG-61:create_draft_pr:real:active', state: 'verification_pending', requested_action: 'create_draft_pr', payload: { repo: 'Driftward-LLC/capykit', dry_run: false }, payload_hash: 'hash', branch: 'codex/recover', commit_sha: 'abc', pr_url: 'https://github.com/Driftward-LLC/capykit/pull/1', hermes_session_id: 'session', hatchet_workflow_run_id: null, hatchet_step_run_id: null, verification_context: null, verification_observed_context: null, verification_receipt: null, verification_digest: null, reason_code: 'draft_pr_verification_pending', error: null, created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T00:00:00Z' };
}
