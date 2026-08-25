import assert from 'node:assert/strict';
import test from 'node:test';
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
