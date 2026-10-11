import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import * as grpc from '@grpc/grpc-js';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { InvocationJournal } from './invocation-journal';
import { ArtifactObservationUnavailable, NonRetryableBridgeExecutionError } from './errors';
import { boundedArtifactObservationContext, type ArtifactObservationContext } from './artifact-observation';
import { boundedExecutionOutcome } from './execution-outcome';
import { createServer } from './hermes-gateway-server';
import { inspectHermesInvocation, runHermesCreateDraftPrViaGrpc } from './hermes-gateway-client';

const job = { job_id: '00000000-0000-4000-8000-000000000010', idempotency_key: 'test:intent:1',
  requested_action: 'create_draft_pr', branch: 'codex/test-receipt', state: 'running',
  payload: { repo: 'example/project', dry_run: false, implementation_prompt: 'PRIVATE_TEST_PROMPT' },
} as unknown as BridgeJob;
const lease = { lease_id: '00000000-0000-4000-8000-000000000011', owner: 'test-executor', epoch: 1, fence_token: 'test-original-fence' };
const receipt = { status: 'created' as const, prUrl: 'https://github.com/example/project/pull/1', prNumber: 1,
  commitSha: 'a'.repeat(40), branch: job.branch!, hermesSessionId: null, summary: 'draft_pr_created' as const, validation: null };
const correlatedJob = { ...job, payload: { ...job.payload,
  source_event_key: 'event-1', source_event_keys: ['event-1'],
  source_delivery_id: 'delivery-1', source_delivery_ids: ['delivery-1'],
} };
const updatedJob = { ...correlatedJob, payload: { ...correlatedJob.payload,
  source_event_keys: ['event-1', 'event-2'], source_delivery_ids: ['delivery-1', 'delivery-2'],
} };

const observation: ArtifactObservationContext = {
  schema: 'factory-artifact-observation-v1', finished_at: '2026-10-11T00:00:00.000Z',
  before: { repository: 'example/project', head_branch: job.branch!, reason: 'ok', pull_requests: [] }, report: null,
};

test('saved observation retries after restart with the original fence, never replaying execution', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-observation-retry-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  let executions = 0;
  await assert.rejects(new InvocationJournal(directory).execute(job, lease, async (checkpoint) => {
    executions++;
    checkpoint(observation);
    throw new ArtifactObservationUnavailable();
  }), ArtifactObservationUnavailable);
  const restarted = new InvocationJournal(directory);
  assert.equal(restarted.status(job).state, 'outcome_unknown');
  assert.deepEqual(JSON.parse(readFileSync(path.join(directory, job.job_id, 'intent.json'), 'utf8')).lease, lease);
  await assert.rejects(restarted.inspect(updatedJob, true, async () => { throw new ArtifactObservationUnavailable(); }));
  assert.equal(restarted.status(job).cancellationAcknowledged, false);
  await assert.rejects(restarted.execute(job, lease, async () => { executions++; return receipt; }), /already reserved/);
  await assert.rejects(restarted.inspect({ ...job, branch: 'codex/wrong' }, false, async () => assert.fail('wrong intent')), /intent differs/);
  let reads = 0;
  let release!: () => void;
  const wait = new Promise<void>((resolve) => { release = resolve; });
  const inspect = async (_job: BridgeJob, context: ArtifactObservationContext) => {
    reads++;
    assert.deepEqual(context, observation);
    await wait;
    return receipt;
  };
  const first = restarted.inspect(job, false, inspect);
  const second = restarted.inspect(updatedJob, true, inspect);
  release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(reads, 1);
  assert.equal(executions, 1);
  assert.deepEqual(a, b);
  assert.equal(a.state, 'completed');
  assert.deepEqual(a.lease, lease);
  assert.deepEqual(a.receipt, receipt);
  assert.equal((await restarted.inspect(job, false, async () => assert.fail('already settled'))).state, 'completed');
  assert.equal(statSync(path.join(directory, job.job_id, 'observation.json')).mode & 0o777, 0o600);
});

test('post-checkpoint crash permits only inspection, while missing or invalid checkpoints stay fenced', async (t) => {
  for (const checkpointed of [true, false]) {
    const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-observation-crash-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const child = spawnSync(process.execPath, ['-e', `
      const { InvocationJournal } = require(${JSON.stringify(require.resolve('./invocation-journal'))});
      new InvocationJournal(${JSON.stringify(directory)}).execute(${JSON.stringify(job)}, ${JSON.stringify(lease)}, async (checkpoint) => {
        if (${checkpointed}) checkpoint(${JSON.stringify(observation)});
        process.kill(process.pid, 'SIGKILL');
      });`]);
    assert.equal(child.signal, 'SIGKILL');
    const restarted = new InvocationJournal(directory);
    assert.equal(restarted.status(job).state, 'outcome_unknown');
    await assert.rejects(restarted.execute(job, lease, async () => assert.fail('must not replay')), /already reserved/);
    if (!checkpointed) {
      assert.equal((await restarted.inspect(job, true, async () => assert.fail('no saved exit'))).state, 'outcome_unknown');
    } else {
      const file = path.join(directory, job.job_id, 'observation.json');
      writeFileSync(file, JSON.stringify({ ...observation, before: { ...observation.before, head_branch: 'wrong' } }));
      await assert.rejects(restarted.inspect(job, true, async () => assert.fail('invalid checkpoint')), /checkpoint does not match/);
      writeFileSync(file, JSON.stringify(observation));
      const status = await restarted.inspect(job, true, async () => {
        throw new NonRetryableBridgeExecutionError('hermes_execution_incomplete', 'no validation', {
          processExitCode: 0, observedArtifact: { branch: job.branch!, pr_url: receipt.prUrl, commit_sha: receipt.commitSha },
        });
      });
      assert.equal(status.state, 'incomplete');
      assert.equal(status.cancellationAcknowledged, false);
      assert.equal(status.outcome?.finished_at, observation.finished_at);
      assert.equal(status.outcome?.containment, 'unconfirmed');
      assert.deepEqual(status.lease, lease);
    }
  }
});

test('observation checkpoint strips raw output and binds validation claims to policy and artifact', () => {
  const boundJob = { ...job, payload: { ...job.payload, verification_policy: {
    required_local_commands: ['npm test'], secret_check_command: 'secret-check',
  } } };
  const report = { pr_url: receipt.prUrl, branch: job.branch, commit_sha: receipt.commitSha,
    summary: 'PRIVATE_OUTPUT', stdout: 'PRIVATE_OUTPUT', validation: {
      local_commands: [{ command: 'npm test', exit_code: 0, output: 'PRIVATE_OUTPUT' }],
      secret_check: { command: 'secret-check', exit_code: 0, stdout: 'PRIVATE_OUTPUT' },
    } };
  const context = boundedArtifactObservationContext(boundJob, { ...observation, report, raw_output: 'PRIVATE_OUTPUT',
    before: { ...observation.before, pull_requests: [{ url: receipt.prUrl, body: 'PRIVATE_OUTPUT' }] } });
  assert.ok(context?.report?.validation);
  assert.doesNotMatch(JSON.stringify(context), /PRIVATE_OUTPUT|stdout|summary|body/);
  for (const change of [{ branch: 'wrong' }, { commit_sha: 'short' }, { pr_url: 'https://github.com/other/repo/pull/1' },
    { validation: { local_commands: [], secret_check: {} } }]) {
    assert.equal(boundedArtifactObservationContext(boundJob, { ...observation, report: { ...report, ...change } })?.report, null);
  }
  assert.equal(boundedArtifactObservationContext(job, { ...observation, before: { ...observation.before, repository: 'other/repo' } }), null);
});

test('incomplete and failed invocations persist bounded outcomes across restart, without replay or containment claims', async (t) => {
  for (const error of [
    new NonRetryableBridgeExecutionError('hermes_execution_incomplete', 'PRIVATE_OUTPUT', { processExitCode: 0 }),
    new NonRetryableBridgeExecutionError('hermes_process_timeout', 'PRIVATE_OUTPUT', { timedOut: true, processSignal: 'SIGTERM' }),
    new Error('PRIVATE_OUTPUT ghp_FAKE_SECRET'),
  ]) {
    const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-terminal-outcome-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    await assert.rejects(new InvocationJournal(directory).execute(job, lease, async () => { throw error; }));
    const restarted = new InvocationJournal(directory);
    const status = restarted.status(updatedJob, true);
    assert.equal(status.state, error instanceof NonRetryableBridgeExecutionError && error.reasonCode === 'hermes_execution_incomplete' ? 'incomplete' : 'failed');
    assert.equal(status.cancellationAcknowledged, false);
    assert.equal(status.outcome?.effect_outcome, 'unknown');
    assert.equal(status.outcome?.containment, 'unconfirmed');
    assert.equal(status.outcome?.job_id, job.job_id);
    assert.deepEqual(status.lease, lease);
    const raw = readFileSync(path.join(directory, job.job_id, 'outcome.json'), 'utf8');
    assert.doesNotMatch(raw, /PRIVATE|FAKE_SECRET|fence_token|implementation_prompt/);
    assert.equal(statSync(path.join(directory, job.job_id, 'outcome.json')).mode & 0o777, 0o600);
    await assert.rejects(restarted.execute(job, lease, async () => assert.fail('must not replay')), /already reserved/);
    for (const change of [{ job_id: 'other-job' }, { containment: 'confirmed' }, { state: 'completed' },
      { process_signal: 'private text' }, { raw_output: 'private' }, { process_exit_code: true }, { timed_out: 'yes' }]) {
      assert.equal(boundedExecutionOutcome(job, { ...status.outcome, ...change }), null);
    }
  }
});

test('correlation updates preserve running, interrupted and completed invocation identity without replay', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-invocation-correlation-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const journal = new InvocationJournal(directory);
  let finish!: () => void;
  const wait = new Promise<void>((resolve) => { finish = resolve; });
  let calls = 0;
  const pending = journal.execute(correlatedJob, lease, async () => { calls++; await wait; return receipt; });
  const intentPath = path.join(directory, job.job_id, 'intent.json');
  const original = readFileSync(intentPath, 'utf8');
  for (const payload of [job.payload, updatedJob.payload, { ...updatedJob.payload,
    source_event_key: 'another-event', source_delivery_id: 'another-delivery',
  }]) {
    assert.equal(journal.status({ ...job, payload }).state, 'running');
    assert.equal(new InvocationJournal(directory).status({ ...job, payload }).state, 'outcome_unknown');
    await assert.rejects(journal.execute({ ...job, payload }, lease, async () => assert.fail('must not replay')), /already reserved/);
  }
  assert.equal(JSON.parse(original).fingerprint_version, 2);
  assert.equal(readFileSync(intentPath, 'utf8'), original);
  const canceled = journal.status(updatedJob, true);
  assert.equal(canceled.cancellationRequested, true);
  assert.equal(canceled.cancellationAcknowledged, false);
  finish();
  await pending;
  const restarted = new InvocationJournal(directory);
  assert.equal(restarted.status(updatedJob).cancellationAcknowledged, true);
  assert.deepEqual(await restarted.execute(updatedJob, lease, async () => assert.fail('must not replay')), receipt);
  await assert.rejects(restarted.execute(updatedJob, { ...lease, epoch: 2 }, async () => receipt), /fence differs/);
  assert.equal(readFileSync(intentPath, 'utf8'), original);
  assert.equal(calls, 1);
});

test('correlation exclusions are top-level only and every other intent field remains bound', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-invocation-intent-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const journal = new InvocationJournal(directory);
  await journal.execute(correlatedJob, lease, async () => receipt);
  const payloadChanges: BridgeJob['payload'][] = [
    { repo: 'another/project' }, { dry_run: true }, { implementation_prompt: 'different' },
    { instructions: 'different' }, { verification_policy: { id: 'different' } },
    { factory_retry: { attempt_id: 'another-attempt' } }, { factory_repair: { attempt_id: 'another-attempt' } },
    { nested: { source_event_key: 'not-top-level' } }, { source_event_keys_extra: ['not-excluded'] },
  ];
  const changes: Partial<BridgeJob>[] = [
    { idempotency_key: 'another-key' }, { requested_action: 'repair_draft_pr' }, { branch: 'codex/another-branch' },
    ...payloadChanges.map((payload) => ({ payload: { ...updatedJob.payload, ...payload } })),
  ];
  for (const change of changes) {
    assert.throws(() => journal.status({ ...updatedJob, ...change }), /intent differs/);
    await assert.rejects(journal.execute({ ...updatedJob, ...change }, lease, async () => assert.fail('must not execute')),
      /intent differs/);
  }
  const otherId = '00000000-0000-4000-8000-000000000012';
  const otherDirectory = path.join(directory, otherId);
  mkdirSync(otherDirectory, { mode: 0o700 });
  writeFileSync(path.join(otherDirectory, 'intent.json'), readFileSync(path.join(directory, job.job_id, 'intent.json')),
    { mode: 0o600 });
  assert.throws(() => journal.status({ ...updatedJob, job_id: otherId }), /intent differs/);
});

test('legacy reservations remain strict and read-only; unknown versions fail closed', async (t) => {
  // Independent version-one fixture, using the original sorted JSON bytes.
  const fingerprint = createHash('sha256').update(JSON.stringify({ branch: job.branch, idempotency_key: job.idempotency_key,
    job_id: job.job_id, payload: { dry_run: false, implementation_prompt: 'PRIVATE_TEST_PROMPT', repo: 'example/project' },
    requested_action: job.requested_action })).digest('hex');
  for (const completed of [false, true]) {
    const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-invocation-legacy-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const reserved = path.join(directory, job.job_id);
    mkdirSync(reserved, { mode: 0o700 });
    const intentPath = path.join(reserved, 'intent.json');
    const original = JSON.stringify({ fingerprint, lease });
    writeFileSync(intentPath, original, { mode: 0o600 });
    if (completed) writeFileSync(path.join(reserved, 'result.json'), JSON.stringify(receipt), { mode: 0o600 });
    const journal = new InvocationJournal(directory);
    assert.equal(journal.status(job).state, completed ? 'completed' : 'outcome_unknown');
    assert.throws(() => journal.status(updatedJob, true), /intent differs/);
    assert.equal(journal.status(job).cancellationRequested, false);
    if (completed) {
      assert.deepEqual(await journal.execute(job, lease, async () => assert.fail('must not execute')), receipt);
      await assert.rejects(journal.execute(job, { ...lease, fence_token: 'another-fence' }, async () => receipt), /fence differs/);
    } else await assert.rejects(journal.execute(job, lease, async () => assert.fail('must not execute')), /already reserved/);
    assert.equal(readFileSync(intentPath, 'utf8'), original);
    for (const fingerprint_version of [null, 0, 3, '2']) {
      writeFileSync(intentPath, JSON.stringify({ fingerprint, lease, fingerprint_version }));
      assert.throws(() => journal.status(job, true), /fingerprint version/);
      await assert.rejects(journal.execute(job, lease, async () => assert.fail('must not execute')), /fingerprint version/);
    }
    // Even without correlation fields, a legacy hash cannot pass as version two.
    writeFileSync(intentPath, JSON.stringify({ fingerprint, lease, fingerprint_version: 2 }));
    assert.throws(() => journal.status(job, true), /intent differs/);
    writeFileSync(intentPath, original);
    assert.equal(journal.status(job).cancellationRequested, false);
  }
});

test('durable journal deduplicates, binds intent and fence, and acknowledges draining only after completion', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-invocations-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const journal = new InvocationJournal(directory);
  assert.equal(journal.status(job, true).state, 'not_found');
  let finish!: () => void;
  const wait = new Promise<void>((resolve) => { finish = resolve; });
  let calls = 0;
  const running = journal.execute(job, lease, async () => { calls++; await wait; return { ...receipt, summary: 'PRIVATE_SUMMARY', stdout: 'PRIVATE_OUTPUT' }; });
  assert.equal(journal.status(job).state, 'running');
  const cancellation = journal.status(job, true);
  assert.equal(cancellation.cancellationRequested, true);
  assert.equal(cancellation.cancellationAcknowledged, false);
  await assert.rejects(journal.execute(job, lease, async () => receipt), /already reserved/);
  assert.throws(() => journal.status({ ...job, payload: { ...job.payload, implementation_prompt: 'different' } }), /intent differs/);
  finish();
  await running;
  const restarted = new InvocationJournal(directory);
  assert.equal(restarted.status(job).cancellationAcknowledged, true);
  assert.deepEqual(await restarted.execute(job, lease, async () => { calls++; return receipt; }), receipt);
  await assert.rejects(restarted.execute(job, { ...lease, epoch: 2 }, async () => receipt), /fence differs/);
  assert.equal(calls, 1);
  for (const name of ['intent.json', 'result.json']) {
    const file = path.join(directory, job.job_id, name);
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.doesNotMatch(readFileSync(file, 'utf8'), /PRIVATE_TEST_PROMPT|PRIVATE_SUMMARY|PRIVATE_OUTPUT/);
  }
});

test('process death preserves completed receipts and fences interrupted execution', async (t) => {
  for (const completed of [false, true]) {
    const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-invocation-crash-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const code = `const { InvocationJournal } = require(${JSON.stringify(path.join(__dirname, 'invocation-journal.js'))});
      new InvocationJournal(process.argv[1]).execute(${JSON.stringify(job)}, ${JSON.stringify(lease)}, async () => {
        ${completed ? `return ${JSON.stringify(receipt)};` : 'process.exit(97);'}
      }).then(() => process.exit(98));`;
    const child = spawnSync(process.execPath, ['-e', code, directory], { encoding: 'utf8' });
    assert.equal(child.status, completed ? 98 : 97, child.stderr);
    const journal = new InvocationJournal(directory);
    assert.equal(journal.status(job).state, completed ? 'completed' : 'outcome_unknown');
    if (!completed) {
      assert.equal(journal.status(job, true).cancellationAcknowledged, false);
      await assert.rejects(journal.execute(job, lease, async () => assert.fail('must not reexecute')), /already reserved/);
    }
  }
});

test('lost gRPC response is recovered after gateway restart without executing again', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-gateway-receipt-'));
  const target = `unix://${path.join(directory, 'gateway.sock')}`;
  const previous = { target: process.env.DRIFTWARD_HERMES_GRPC_TARGET, journal: process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY,
    token: process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN };
  process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
  process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY = path.join(directory, 'journal');
  delete process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN;
  let server: grpc.Server;
  const selection = { model_source: 'executor_config', selected_model: 'gpt-6.1-sol', selected_provider: 'openai-codex' } as const;
  const observed: unknown[] = [];
  let finish!: () => void;
  const gate = new Promise<void>((resolve) => { finish = resolve; });
  const start = async () => {
    server = createServer(target, async (_job, _command, _timeout, _buffer, onModelSelected) => {
      calls++;
      await onModelSelected?.(selection);
      await gate;
      return receipt;
    });
    await new Promise<void>((resolve, reject) => server.bindAsync(target, grpc.ServerCredentials.createInsecure(), (error) => error ? reject(error) : resolve()));
  };
  let calls = 0;
  t.after(() => {
    finish();
    server?.forceShutdown();
    for (const [key, value] of [['DRIFTWARD_HERMES_GRPC_TARGET', previous.target], ['DRIFTWARD_HERMES_INVOCATION_DIRECTORY', previous.journal], ['DRIFTWARD_HERMES_GRPC_AUTH_TOKEN', previous.token]]) {
      if (value === undefined) delete process.env[key!]; else process.env[key!] = value;
    }
    rmSync(directory, { recursive: true, force: true });
  });
  await start();
  await assert.rejects(runHermesCreateDraftPrViaGrpc(correlatedJob, 1000, lease, (value) => { observed.push(value); }));
  assert.deepEqual([...observed], [selection]);
  finish();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await inspectHermesInvocation(job)).state, 'completed');
  await new Promise<void>((resolve) => server.tryShutdown(() => resolve()));
  await start();
  const recovered = await inspectHermesInvocation(updatedJob, true);
  assert.deepEqual(recovered.receipt, receipt);
  assert.deepEqual(recovered.lease, lease);
  assert.equal(recovered.cancellationAcknowledged, true);
  assert.deepEqual(await runHermesCreateDraftPrViaGrpc(updatedJob, 1000, lease, (value) => { observed.push(value); }), receipt);
  assert.deepEqual([...observed], [selection]); // Reused receipts never claim today's config was used earlier.
  await assert.rejects(runHermesCreateDraftPrViaGrpc(updatedJob, 1000, { ...lease, epoch: 2 }));
  await assert.rejects(inspectHermesInvocation({ ...updatedJob, payload: { ...updatedJob.payload, instructions: 'different' } }));
  assert.equal(calls, 1);
});

test('gateway exposes a durable partial receipt without acknowledging cancellation or reinvoking Hermes', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-gateway-partial-'));
  const target = `unix://${path.join(directory, 'gateway.sock')}`;
  const names = ['DRIFTWARD_HERMES_GRPC_TARGET', 'DRIFTWARD_HERMES_INVOCATION_DIRECTORY', 'DRIFTWARD_HERMES_GRPC_AUTH_TOKEN'] as const;
  const previous = names.map((name) => process.env[name]);
  process.env[names[0]] = target;
  process.env[names[1]] = path.join(directory, 'journal');
  delete process.env[names[2]];
  let calls = 0;
  const artifact = { branch: job.branch!, pr_url: receipt.prUrl, commit_sha: receipt.commitSha };
  const server = createServer(target, async () => {
    calls++;
    throw new NonRetryableBridgeExecutionError('hermes_execution_incomplete', 'PRIVATE_OUTPUT', { observedArtifact: artifact, processExitCode: 0 });
  });
  t.after(() => {
    server.forceShutdown();
    names.forEach((name, i) => { if (previous[i] === undefined) delete process.env[name]; else process.env[name] = previous[i]; });
    rmSync(directory, { recursive: true, force: true });
  });
  await new Promise<void>((resolve, reject) => server.bindAsync(target, grpc.ServerCredentials.createInsecure(), (error) => error ? reject(error) : resolve()));
  await assert.rejects(runHermesCreateDraftPrViaGrpc(job, 2000, lease));
  const partial = await inspectHermesInvocation(updatedJob, true);
  assert.equal(partial.state, 'incomplete');
  assert.equal(partial.cancellationAcknowledged, false);
  assert.deepEqual(partial.outcome?.artifact, artifact);
  assert.deepEqual(partial.lease, lease);
  assert.doesNotMatch(JSON.stringify(partial.outcome), /PRIVATE_OUTPUT|fence_token/);
  await assert.rejects(runHermesCreateDraftPrViaGrpc(job, 2000, lease));
  assert.equal(calls, 1);
});

test('gateway inspection resumes a checkpoint after restart without calling the agent again', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-gateway-observation-'));
  const target = `unix://${path.join(directory, 'gateway.sock')}`;
  const names = ['DRIFTWARD_HERMES_GRPC_TARGET', 'DRIFTWARD_HERMES_INVOCATION_DIRECTORY', 'DRIFTWARD_HERMES_GRPC_AUTH_TOKEN'] as const;
  const previous = names.map((name) => process.env[name]);
  process.env[names[0]] = target;
  process.env[names[1]] = path.join(directory, 'journal');
  delete process.env[names[2]];
  let calls = 0;
  let available = false;
  let server: grpc.Server;
  const start = async () => {
    server = createServer(target, async (_job, _command, _timeout, _buffer, _model, checkpoint) => {
      calls++;
      checkpoint!(observation);
      throw new ArtifactObservationUnavailable();
    }, async (observedJob, context) => {
      assert.equal(observedJob.job_id, job.job_id);
      assert.deepEqual(context, observation);
      if (!available) throw new ArtifactObservationUnavailable();
      return receipt;
    });
    await new Promise<void>((resolve, reject) => server.bindAsync(target, grpc.ServerCredentials.createInsecure(), (error) => error ? reject(error) : resolve()));
  };
  t.after(() => {
    server?.forceShutdown();
    names.forEach((name, i) => { if (previous[i] === undefined) delete process.env[name]; else process.env[name] = previous[i]; });
    rmSync(directory, { recursive: true, force: true });
  });
  await start();
  await assert.rejects(runHermesCreateDraftPrViaGrpc(job, 2000, lease));
  await new Promise<void>((resolve) => server.tryShutdown(() => resolve()));
  await start();
  available = true;
  const recovered = await inspectHermesInvocation(updatedJob);
  assert.equal(recovered.state, 'completed');
  assert.deepEqual(recovered.receipt, receipt);
  assert.deepEqual(recovered.lease, lease);
  assert.equal(calls, 1);
});
