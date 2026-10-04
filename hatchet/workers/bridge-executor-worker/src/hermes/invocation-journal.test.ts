import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
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
  await assert.rejects(runHermesCreateDraftPrViaGrpc(job, 60, lease));
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal((await inspectHermesInvocation(job)).state, 'completed');
  await new Promise<void>((resolve) => server.tryShutdown(() => resolve()));
  await start();
  const recovered = await inspectHermesInvocation(job, true);
  assert.deepEqual(recovered.receipt, receipt);
  assert.deepEqual(recovered.lease, lease);
  assert.equal(recovered.cancellationAcknowledged, true);
  assert.deepEqual(await runHermesCreateDraftPrViaGrpc(job, 1000, lease), receipt);
  await assert.rejects(runHermesCreateDraftPrViaGrpc(job, 1000, { ...lease, epoch: 2 }));
  assert.equal(calls, 1);
});
