import assert from 'node:assert/strict';
import test from 'node:test';
import type { ReconciliationFindingRequest } from '@driftward/hatchet-worker-shared';
import { recordFindingBatches } from './finding-batches';

function findings(count: number): ReconciliationFindingRequest[] {
  return Array.from({ length: count }, (_, index) => ({
    finding_key: `reconciliation:v1:ingress_stored_not_published:${String(index).padStart(3, '0')}`,
    job_id: null, subject: `ingress:${index}`, kind: 'ingress_stored_not_published', severity: 'warning',
    details: { expected: { published: true }, observed: { published: false } },
  }));
}

test('chunks more than the bridge endpoint cap in deterministic key order', async () => {
  const batches: string[][] = [];
  const result = await recordFindingBatches({
    recordReconciliationFindings: async (batch) => {
      batches.push(batch.map((item) => item.finding_key));
      return { created_finding_keys: batch.map((item) => item.finding_key), existing_finding_keys: [] };
    },
  }, findings(501).reverse());
  assert.deepEqual(batches.map((batch) => batch.length), [250, 250, 1]);
  assert.equal(batches[0][0].endsWith(':000'), true);
  assert.equal(result.created_finding_keys.length, 501);
  assert.equal(result.failedBatches, 0);
});

test('continues deterministic batches after one write failure', async () => {
  let calls = 0;
  const result = await recordFindingBatches({
    recordReconciliationFindings: async (batch) => {
      calls += 1;
      if (calls === 2) throw new Error('unavailable');
      return { created_finding_keys: batch.map((item) => item.finding_key), existing_finding_keys: [] };
    },
  }, findings(501));
  assert.equal(calls, 3);
  assert.equal(result.created_finding_keys.length, 251);
  assert.equal(result.failedBatches, 1);
});
