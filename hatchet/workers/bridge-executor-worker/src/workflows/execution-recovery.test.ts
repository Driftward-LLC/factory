import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob, FactoryAttempt } from '@driftward/hatchet-worker-shared';
import { recoverExecutionReceipt } from './execution-recovery';
import { executionOutcome } from '../hermes/execution-outcome';
import { NonRetryableBridgeExecutionError } from '../hermes/errors';

const job = { job_id: 'test-job', state: 'running', branch: 'codex/test', payload: { repo: 'example/project' } } as unknown as BridgeJob;
const lease = { lease_id: 'original-lease', epoch: 1, owner: 'original-owner', fence_token: 'original-fence' };
const receipt = { status: 'created' as const, prUrl: 'https://github.com/example/project/pull/1', prNumber: 1,
  branch: 'codex/test', commitSha: 'a'.repeat(40), hermesSessionId: null, summary: 'draft_pr_created' as const, validation: null };

test('a durable incomplete receipt is observed without committing success or releasing the effect fence', async () => {
  const outcome = executionOutcome(job, new NonRetryableBridgeExecutionError('hermes_execution_incomplete', 'not published'));
  const observed: unknown[] = [];
  const bridge = {
    recordExecutionReceiptObservation: async (_id: string, value: unknown) => { observed.push(value); },
    commitFactoryExecution: async () => { throw new Error('must not commit'); },
    getJob: async () => job,
  };
  assert.equal(await recoverExecutionReceipt(bridge, job, true, async () => ({
    state: 'incomplete', outcome, lease, cancellationRequested: true, cancellationAcknowledged: false,
  })), null);
  assert.deepEqual(observed, [{ state: 'incomplete', outcome, cancellation_requested: true, cancellation_acknowledged: false }]);
});

test('receipt recovery commits the original fence and preserves manual takeover', async () => {
  let commits = 0;
  const bridge = {
    recordExecutionReceiptObservation: async () => {},
    commitFactoryExecution: async (_id: string, claim: typeof lease) => {
      assert.deepEqual(claim, lease); commits++;
      return { job: { ...job, state: 'effect_committed', pr_url: receipt.prUrl, commit_sha: receipt.commitSha } as BridgeJob };
    },
    getJob: async () => job,
    getFactoryAttemptForJob: async () => ({ status: 'canceled_after_effect' } as FactoryAttempt),
    transitionJob: async () => { throw new Error('must not verify held work'); },
  };
  const result = await recoverExecutionReceipt(bridge, job, true, async (_job, cancel) => {
    assert.equal(cancel, true);
    return { state: 'completed', receipt, lease, cancellationRequested: true, cancellationAcknowledged: true };
  });
  assert.equal(result?.state, 'effect_committed');
  assert.equal(commits, 1);
  for (const state of ['not_found', 'running', 'outcome_unknown'] as const) {
    assert.equal(await recoverExecutionReceipt(bridge, job, true, async () => ({ state, cancellationRequested: true, cancellationAcknowledged: false })), null);
  }
  assert.equal(await recoverExecutionReceipt(bridge, job, true, async () => { throw new Error('unavailable'); }), null);
  assert.equal(commits, 1);
});
