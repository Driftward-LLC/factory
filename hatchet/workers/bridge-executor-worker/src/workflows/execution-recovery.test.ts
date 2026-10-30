import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob, FactoryAttempt } from '@driftward/hatchet-worker-shared';
import { recoverExecutionReceipt } from './execution-recovery';

const job = { job_id: 'test-job', state: 'running', branch: 'codex/test', payload: { repo: 'example/project' } } as unknown as BridgeJob;
const lease = { lease_id: 'original-lease', epoch: 1, owner: 'original-owner', fence_token: 'original-fence' };
const receipt = { status: 'created' as const, prUrl: 'https://github.com/example/project/pull/1', prNumber: 1,
  branch: 'codex/test', commitSha: 'a'.repeat(40), hermesSessionId: null, summary: 'draft_pr_created' as const, validation: null };

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

test('bound recovery rejects another runtime even when the artifact and lease match', async () => {
  const runtime = { schema: 1 as const, backend: 'hermes', kind: 'docker' as const, instance_id: 'a'.repeat(64),
    image_digest: 'sha256:' + 'b'.repeat(64), incarnation: 'sha256:' + 'c'.repeat(64) };
  const bound = { ...job, execution_runtime: { lease_id: lease.lease_id, epoch: lease.epoch, runtime } };
  let commits = 0;
  const bridge = {
    recordExecutionReceiptObservation: async () => {}, getJob: async () => bound,
    commitFactoryExecution: async (_id: string, _lease: typeof lease, details: any) => {
      assert.deepEqual(details.execution_runtime, runtime); commits++;
      return { job: { ...bound, state: 'effect_committed' } as BridgeJob };
    },
  };
  for (const value of [undefined, { ...runtime, instance_id: 'd'.repeat(64) }, { ...runtime, incarnation: 'sha256:' + 'd'.repeat(64) }]) {
    assert.equal(await recoverExecutionReceipt(bridge, bound, false, async () => ({ state: 'completed', receipt, lease,
      runtime: value, cancellationRequested: false, cancellationAcknowledged: false })), null);
  }
  assert.equal(commits, 0);
  await recoverExecutionReceipt(bridge, bound, false, async () => ({ state: 'completed', receipt, lease,
    runtime, cancellationRequested: false, cancellationAcknowledged: false }));
  assert.equal(commits, 1);
});
