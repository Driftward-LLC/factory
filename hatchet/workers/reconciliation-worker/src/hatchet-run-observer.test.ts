import assert from 'node:assert/strict';
import test from 'node:test';
import { observeHatchetRun, observeHatchetRuns } from './hatchet-run-observer';

test('normalizes a Hatchet run without exposing input or output payloads', async () => {
  const lookup = await observeHatchetRun({
    getDetails: async () => ({ status: 'COMPLETED', taskRuns: { task: { externalId: 'task-1', input: { secret: 'never-read' } } } }),
  }, 'run-1');
  assert.deepEqual(lookup, { status: 'matched', runStatus: 'COMPLETED', taskExternalIds: ['task-1'] });
});

test('distinguishes absent, authorization, malformed, and unavailable Hatchet runs', async () => {
  assert.deepEqual(await observeHatchetRun({ getDetails: async () => { throw { response: { status: 404 } }; } }, 'run'), { status: 'not_found' });
  assert.deepEqual(await observeHatchetRun({ getDetails: async () => { throw { response: { status: 403 } }; } }, 'run'), { status: 'auth' });
  assert.deepEqual(await observeHatchetRun({ getDetails: async () => ({}) }, 'run'), { status: 'malformed' });
  assert.deepEqual(await observeHatchetRun({ getDetails: async () => { throw new Error('network'); } }, 'run'), { status: 'unavailable' });
});

test('enumerates bounded Hatchet summaries without inputs, outputs, or metadata payloads', async () => {
  const inventory = await observeHatchetRuns({
    getDetails: async () => ({}),
    list: async () => [{ workflowRunExternalId: 'run-1', taskExternalId: 'task-1', workflowName: 'intake-linear-ready-dispatch', status: 'COMPLETED', taskInsertedAt: '2026-08-13T10:00:00Z', input: { secret: 'never-read' } }],
  }, 2);
  assert.deepEqual(inventory, {
    status: 'matched', complete: true,
    runs: [{ workflowRunId: 'run-1', taskExternalId: 'task-1', workflowName: 'intake-linear-ready-dispatch', status: 'COMPLETED', insertedAt: '2026-08-13T10:00:00Z' }],
  });
});

test('marks a full Hatchet page incomplete and applies a deadline', async () => {
  const full = await observeHatchetRuns({
    getDetails: async () => ({}),
    list: async () => [{ workflowRunExternalId: 'run-1', taskExternalId: 'task-1' }],
  }, 1);
  assert.equal(full.status, 'matched');
  if (full.status === 'matched') assert.equal(full.complete, false);
  assert.deepEqual(await observeHatchetRun({ getDetails: async () => new Promise(() => undefined) }, 'run-1', 5), { status: 'timeout' });
});

test('serializes timed-out SDK calls so unresolved requests cannot accumulate', async () => {
  let calls = 0;
  let release: (() => void) | undefined;
  const runs = {
    getDetails: async () => {
      calls += 1;
      await new Promise<void>((resolve) => { release = resolve; });
      return { status: 'COMPLETED', taskRuns: {} };
    },
  };
  const results = await Promise.all(Array.from({ length: 20 }, (_, index) => observeHatchetRun(runs, `run-${index}`, 5)));
  assert.equal(calls, 1);
  assert.equal(results.filter((result) => result.status === 'timeout').length >= 1, true);
  release?.();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 1);
});
