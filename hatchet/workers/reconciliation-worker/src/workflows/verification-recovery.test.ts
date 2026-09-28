import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { enqueueStaleVerification, recoverableStaleVerificationJobs } from './verification-recovery';

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

test('recovery requests a durable same-job wake, coalesces duplicate findings and reports outcomes', async () => {
  const job = fixture();
  let enqueues = 0;
  const bridge = {
    async enqueueVerificationResume(ref: string, staleAfterMinutes: number = 30) {
      assert.equal(ref, job.job_id);
      assert.equal(staleAfterMinutes, 45);
      enqueues += 1;
      return enqueues === 1 ? { enqueued: true, reason: 'enqueued' } : { enqueued: false, reason: 'already_queued' };
    },
  };
  const findings = Array.from({ length: 2 }, () => ({ kind: 'stale_active_bridge_job', jobId: job.job_id }));
  const jobs = new Map([[job.job_id, job]]);
  const [first, duplicate] = await Promise.all([
    enqueueStaleVerification(findings, jobs, bridge, 45),
    enqueueStaleVerification(findings, jobs, bridge, 45),
  ]);
  assert.equal(first.failed + duplicate.failed, 0);
  assert.equal(first.enqueued + duplicate.enqueued, 1);
  assert.equal(duplicate.reasons.already_queued, 1);
  assert.equal(enqueues, 2);
});

test('legacy recovery is explicitly skipped; an enqueue error is a retryable scan failure', async () => {
  const job = fixture();
  const findings = [{ kind: 'stale_active_bridge_job', jobId: job.job_id }];
  const jobs = new Map([[job.job_id, job]]);
  const skipped = await enqueueStaleVerification(findings, jobs, { enqueueVerificationResume: async () => ({ enqueued: false, reason: 'legacy_job' }) }, 30);
  assert.deepEqual(skipped, { enqueued: 0, skipped: 1, failed: 0, reasons: { legacy_job: 1 } });
  const failed = await enqueueStaleVerification(findings, jobs, { enqueueVerificationResume: async () => { throw new Error('bridge unavailable'); } }, 30);
  assert.deepEqual(failed, { enqueued: 0, skipped: 0, failed: 1, reasons: {} });
});

function fixture(): BridgeJob {
  return { job_id: 'job-recover', idempotency_key: 'hatchet:work-item:linear:ENG-61:create_draft_pr:real:active', state: 'verification_pending', requested_action: 'create_draft_pr', payload: { repo: 'Driftward-LLC/capykit', dry_run: false }, payload_hash: 'hash', branch: 'codex/recover', commit_sha: 'abc', pr_url: 'https://github.com/Driftward-LLC/capykit/pull/1', hermes_session_id: 'session', hatchet_workflow_run_id: null, hatchet_step_run_id: null, verification_context: null, verification_observed_context: null, verification_receipt: null, verification_digest: null, reason_code: 'draft_pr_verification_pending', error: null, created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T00:00:00Z' };
}
