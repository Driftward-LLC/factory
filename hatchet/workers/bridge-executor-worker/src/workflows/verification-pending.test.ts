import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { transitionToVerificationPending } from './verification-pending';

function job(state: BridgeJob['state'], context: BridgeJob['verification_context'] = null): BridgeJob {
  return {
    job_id: 'job-race', idempotency_key: 'factory:job-race', state, requested_action: 'create_draft_pr',
    payload: { repo: 'Driftward-LLC/capykit', dry_run: false }, payload_hash: 'payload-hash',
    branch: 'codex/job-race', commit_sha: 'abcdef1', pr_url: 'https://github.com/Driftward-LLC/capykit/pull/81',
    hermes_session_id: 'session-81', hatchet_workflow_run_id: null, hatchet_step_run_id: null,
    verification_context: context, verification_observed_context: null, verification_receipt: null,
    verification_digest: null, reason_code: null, error: null,
    created_at: '2026-09-13T00:00:00Z', updated_at: '2026-09-13T00:00:01Z',
  };
}

test('verification handoff accepts an exact concurrent receipt-runner transition', async () => {
  const context = { local_commands: [{ command: 'npm test', exit_code: 0 }], secret_check: { command: 'scan', exit_code: 0 } };
  const original = job('effect_committed');
  const concurrent = job('verification_pending', context);
  const calls: string[] = [];
  const result = await transitionToVerificationPending({
    async transitionJob() { calls.push('transition'); throw new Error('HTTP 409 conflict'); },
    async getJob() { calls.push('reread'); return concurrent; },
  }, original, context);
  assert.deepEqual(calls, ['transition', 'reread']);
  assert.equal(result.transitioned, false);
  assert.equal(result.job, concurrent);
});

test('verification handoff rejects a conflicting receipt or context', async () => {
  const context = { local_commands: [], secret_check: {} };
  const conflict = new Error('HTTP 409 conflict');
  await assert.rejects(
    transitionToVerificationPending({
      async transitionJob() { throw conflict; },
      async getJob() { return { ...job('verification_pending', { different: true }), commit_sha: '7654321' }; },
    }, job('effect_committed'), context),
    (error: unknown) => error === conflict,
  );
});
