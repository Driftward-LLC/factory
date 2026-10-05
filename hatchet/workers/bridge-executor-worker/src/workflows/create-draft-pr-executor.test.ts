import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { runInNewContext } from 'node:vm';
import { HATCHET_EVENTS, withExecutionSpan, type BridgeJob } from '@driftward/hatchet-worker-shared';
import { emitBestEffort } from './event-delivery';

test('event push failure cannot undo a durable bridge transition', async () => {
  let attempts = 0;
  await emitBestEffort(async () => {
    attempts += 1;
    throw new Error('Hatchet event transport temporarily unavailable');
  });
  await emitBestEffort(async () => { attempts += 1; });
  assert.equal(attempts, 2);
});

for (const taskName of ['execute-create-draft-pr', 'await-hermes-receipt']) {
  test(`${taskName} uses a fresh bounded UUID owner for each verification attempt`, async () => {
    const first = executorFixture();
    const second = executorFixture();
    for (const fixture of [first, second]) {
      const result = await fixture.tasks.get(taskName)!({ jobId: fixture.job.job_id });
      assert.equal(result.executed, true);
      assert.equal(fixture.acquired.length, 1);
      assert.match(fixture.acquired[0], /^verify:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.deepEqual(fixture.claimed, fixture.acquired);
      assert.deepEqual(fixture.observed, fixture.acquired);
      assert.deepEqual(fixture.completed, fixture.acquired);
    }
    assert.notEqual(first.acquired[0], second.acquired[0]);
  });

  test(`${taskName} blocks canceled, superseded, terminal and unknown-outcome attempts before a lease`, async () => {
    for (const status of ['canceled', 'canceled_after_effect', 'cancellation_requested', 'superseded', 'failed', 'completed', 'verified_ready', 'effect_outcome_unknown']) {
      const fixture = executorFixture(status);
      const result = await fixture.tasks.get(taskName)!({ jobId: fixture.job.job_id });
      assert.equal(result.executed, false, status);
      assert.ok(result.blockedReason, status);
      assert.deepEqual(fixture.acquired, [], status);
      assert.deepEqual(fixture.claimed, [], status);
      assert.deepEqual(fixture.completed, [], status);
    }
  });
}

test('a legacy event with a reserved verification owner still claims that exact owner', async () => {
  const fixture = executorFixture();
  const legacyOwner = 'hatchet:legacy:verify:existing-head';
  const result = await fixture.tasks.get('execute-create-draft-pr')!({ jobId: fixture.job.job_id, verificationAttemptId: legacyOwner });
  assert.equal(result.executed, true);
  assert.deepEqual(fixture.acquired, []);
  assert.deepEqual(fixture.claimed, [legacyOwner]);
  assert.deepEqual(fixture.observed, [legacyOwner]);
  assert.deepEqual(fixture.completed, [legacyOwner]);
});

type Handler = (input: { jobId: string; verificationAttemptId?: string }) => Promise<{ executed: boolean; blockedReason?: string }>;

test('running authorized and canceled invocations route only to receipt recovery', async () => {
  for (const status of ['effect_authorized', 'effect_outcome_unknown', 'cancellation_requested']) {
    const fixture = executorFixture(status);
    fixture.job.state = 'running';
    const result = await fixture.tasks.get('execute-create-draft-pr')!({ jobId: fixture.job.job_id });
    assert.equal(result.executed, false);
    assert.deepEqual(fixture.recovery, [status === 'cancellation_requested']);
    assert.deepEqual(fixture.acquired, []);
  }
});

function executorFixture(status = 'verification_pending') {
  const tasks = new Map<string, Handler>();
  const acquired: string[] = []; const claimed: string[] = []; const observed: string[] = []; const completed: string[] = [];
  const recovery: boolean[] = [];
  const job: BridgeJob = {
    job_id: 'same-job', idempotency_key: 'long-idempotency-key:'.repeat(25), state: 'verification_pending', requested_action: 'create_draft_pr',
    payload: { dry_run: false }, payload_hash: 'hash', branch: 'codex/same-branch', commit_sha: 'a'.repeat(40), pr_url: 'https://github.com/Driftward-LLC/factory/pull/1', hermes_session_id: 'same-session',
    hatchet_workflow_run_id: null, hatchet_step_run_id: null, verification_context: {}, verification_observed_context: null, verification_receipt: null, verification_digest: null,
    reason_code: null, error: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  };
  const bridge = {
    getJob: async () => job,
    getFactoryAttemptForJob: async () => ({ status }),
    acquireVerificationLease: async (_jobId: string, owner: string) => { acquired.push(owner); return { acquired: true }; },
    claimVerificationLease: async (_jobId: string, owner: string) => { claimed.push(owner); return { claimed: true }; },
    recordVerificationObservation: async (_jobId: string, owner: string) => { observed.push(owner); },
    completeVerifiedReady: async (_jobId: string, owner: string) => { completed.push(owner); return { job: { ...job, state: 'ready_for_human_review' } }; },
  };
  const register = (definition: { name: string; fn: Handler }) => { tasks.set(definition.name, definition.fn); return {}; };
  const dependencies: Record<string, unknown> = {
    '@driftward/hatchet-worker-shared': { HATCHET_EVENTS, withExecutionSpan, BridgeClient: function () { return bridge; } },
    '../hatchet-client': { hatchet: { workflow: () => ({ task: register, durableTask: register }), events: { push: async () => undefined } } },
    '@hatchet-dev/typescript-sdk/v1/conditions': {},
    './factory-artifact-fence': {},
    './execution-recovery': { recoverExecutionReceipt: async (_bridge: unknown, _job: unknown, cancel: boolean) => { recovery.push(cancel); return null; } },
    '../hermes/create-draft-pr': {
      NonRetryableBridgeExecutionError: class extends Error {},
      resumeHermesDraftPrResult: () => ({ prUrl: job.pr_url, validation: {} }),
      waitForHermesDraftPrVerification: async () => ({}),
      verificationCompletionPlan: () => ({ recordObservation: true, receipt: {} }),
    },
  };
  // Exercise the registered task functions without starting Hatchet or acquiring
  // credentials. All provider and bridge effects are local fakes.
  const filename = path.join(__dirname, 'create-draft-pr-executor.js');
  runInNewContext(readFileSync(filename, 'utf8'), { exports: {}, require: (name: string) => dependencies[name] ?? require(name) }, { filename });
  return { tasks, job, acquired, claimed, observed, completed, recovery };
}
