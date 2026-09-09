import assert from 'node:assert/strict';
import test from 'node:test';
import { reconciliationInput } from './reconciliation-input';

test('treats an empty cron payload as default reconciliation options', () => {
  assert.deepEqual(reconciliationInput(null), {});
  assert.deepEqual(reconciliationInput(undefined), {});
});
