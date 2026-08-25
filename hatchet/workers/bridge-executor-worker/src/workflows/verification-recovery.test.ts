import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { recoverClaimedVerification } from './verification-recovery';

function job(): BridgeJob {
  return {
    job_id: 'job-recovery', idempotency_key: 'hatchet:ENG-61:recovery', state: 'verification_pending', requested_action: 'create_draft_pr',
    payload: { repo: 'Driftward-LLC/capykit', dry_run: false }, payload_hash: 'hash', branch: 'codex/recovery', commit_sha: 'abc', pr_url: 'https://github.com/Driftward-LLC/capykit/pull/61', hermes_session_id: null, hatchet_workflow_run_id: null, hatchet_step_run_id: null,
    verification_context: null, verification_observed_context: null, verification_receipt: null, verification_digest: null, reason_code: null, error: null, created_at: '2026-08-16T00:00:00Z', updated_at: '2026-08-16T00:00:00Z',
  };
}

for (const [name, resume] of [
  ['missing context', () => { throw new Error('missing context'); }],
  ['malformed context', () => { throw new Error('malformed context'); }],
] as const) {
  test(`recovery ${name} claims before resume and completes through verifier failure once`, async () => {
    const calls: string[] = []; let lifecycle = 0; const failed = { ...job(), state: 'failed' as const, reason_code: 'verification_recovery_context_unavailable' };
    const bridge = {
      async acquireVerificationLease() { calls.push('acquire'); return { acquired: true }; },
      async claimVerificationLease() { calls.push('claim'); return { claimed: true }; },
      async completeVerificationFailure(_ref: string, attempt: string) { calls.push(`complete:${attempt}`); return { job: failed }; },
    };
    const result = await recoverClaimedVerification({ bridge, job: job(), attemptId: 'attempt-1', ensurePending: async () => { calls.push('pending'); return job(); }, resume, verify: async () => { throw new Error('verify must not run'); }, emitFailure: async () => { lifecycle += 1; } });
    assert.equal(result.job.state, 'failed'); assert.equal(result.blockedReason, 'verification_recovery_context_unavailable');
    assert.deepEqual(calls, ['acquire', 'claim', 'pending', 'complete:attempt-1']); assert.equal(lifecycle, 1);
    const redelivery = await recoverClaimedVerification({ bridge: { ...bridge, async acquireVerificationLease() { return { acquired: false }; } }, job: job(), attemptId: 'attempt-1', ensurePending: async () => job(), resume, verify: async () => ({ job: failed }), emitFailure: async () => { lifecycle += 1; } });
    assert.equal(redelivery.blockedReason, 'verification_attempt_in_progress'); assert.equal(lifecycle, 1);
  });
}

test('invalid immutable refs stay blocked without verifier completion', async () => {
  let completion = 0;
  const result = await recoverClaimedVerification({
    bridge: { async acquireVerificationLease() { throw new Error('missing immutable refs'); }, async claimVerificationLease() { return { claimed: false }; }, async completeVerificationFailure() { completion += 1; return { job: job() }; } },
    job: { ...job(), pr_url: null }, attemptId: 'attempt-invalid', ensurePending: async () => job(), resume: () => ({}), verify: async () => ({ job: job() }), emitFailure: async () => {},
  });
  assert.equal(result.blockedReason, 'verification_recovery_lease_unavailable'); assert.equal(completion, 0);
});
