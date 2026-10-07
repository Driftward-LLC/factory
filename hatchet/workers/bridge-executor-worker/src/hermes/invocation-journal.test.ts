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
  const start = async () => {
    server = createServer(target, async () => { calls++; await new Promise((resolve) => setTimeout(resolve, 150)); return receipt; });
    await new Promise<void>((resolve, reject) => server.bindAsync(target, grpc.ServerCredentials.createInsecure(), (error) => error ? reject(error) : resolve()));
  };
  let calls = 0;
  t.after(() => {
    server?.forceShutdown();
    for (const [key, value] of [['DRIFTWARD_HERMES_GRPC_TARGET', previous.target], ['DRIFTWARD_HERMES_INVOCATION_DIRECTORY', previous.journal], ['DRIFTWARD_HERMES_GRPC_AUTH_TOKEN', previous.token]]) {
      if (value === undefined) delete process.env[key!]; else process.env[key!] = value;
    }
    rmSync(directory, { recursive: true, force: true });
  });
  await start();
  await assert.rejects(runHermesCreateDraftPrViaGrpc(correlatedJob, 60, lease));
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal((await inspectHermesInvocation(job)).state, 'completed');
  await new Promise<void>((resolve) => server.tryShutdown(() => resolve()));
  await start();
  const recovered = await inspectHermesInvocation(updatedJob, true);
  assert.deepEqual(recovered.receipt, receipt);
  assert.deepEqual(recovered.lease, lease);
  assert.equal(recovered.cancellationAcknowledged, true);
  assert.deepEqual(await runHermesCreateDraftPrViaGrpc(updatedJob, 1000, lease), receipt);
  await assert.rejects(runHermesCreateDraftPrViaGrpc(updatedJob, 1000, { ...lease, epoch: 2 }));
  await assert.rejects(inspectHermesInvocation({ ...updatedJob, payload: { ...updatedJob.payload, instructions: 'different' } }));
  assert.equal(calls, 1);
});
