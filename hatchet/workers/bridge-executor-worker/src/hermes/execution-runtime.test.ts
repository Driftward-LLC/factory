import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { RUNTIME_GUARD, pinnedRuntimeArgs, resolveComposeRuntime, withPinnedRuntime } from './execution-runtime';
import { executionRuntimeBinding } from '@driftward/hatchet-worker-shared';
import type { InvocationExecutionContext } from './invocation-journal';

test('in-container guard refuses a different incarnation before executing the command', () => {
  const observed = execFileSync('python3', ['-c', RUNTIME_GUARD, 'observe'], { encoding: 'utf8' }).trim();
  assert.match(observed, /^sha256:[a-f0-9]{64}$/);
  assert.equal(execFileSync('python3', ['-c', RUNTIME_GUARD, observed, 'printf', 'permitted'], { encoding: 'utf8' }), 'permitted');
  const refused = spawnSync('python3', ['-c', RUNTIME_GUARD, 'sha256:' + '0'.repeat(64), 'printf', 'must-not-run'], { encoding: 'utf8' });
  assert.equal(refused.status, 78);
  assert.equal(refused.stdout, '');
});

test('runtime reservation pins full container ID and commands never resolve the service again', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-pin-runtime-'));
  const previous = process.env.PATH;
  t.after(() => { process.env.PATH = previous; rmSync(directory, { recursive: true, force: true }); });
  const id = 'a'.repeat(64), image = 'sha256:' + 'b'.repeat(64), incarnation = 'sha256:' + 'c'.repeat(64);
  const log = path.join(directory, 'calls');
  writeFileSync(path.join(directory, 'docker'), `#!${process.execPath}
const fs=require('node:fs');const args=process.argv.slice(2);fs.appendFileSync(${JSON.stringify(log)},JSON.stringify(args)+'\\n');
if(args[0]==='compose')process.stdout.write('${id}');
else if(args[0]==='inspect')process.stdout.write('${JSON.stringify(id)} ${JSON.stringify(image)}');
else if(args[0]==='exec')process.stdout.write('${incarnation}');
else process.exit(1);
`, { mode: 0o700 });
  process.env.PATH = directory + path.delimiter + previous;
  let bound = false;
  const context = { bindRuntime: async (runtime) => {
    assert.equal(runtime.instance_id, id); assert.equal(runtime.image_digest, image); bound = true;
  } } as InvocationExecutionContext;
  await withPinnedRuntime(context, await resolveComposeRuntime(['compose']), async () => {
    assert.equal(bound, true);
    const args = pinnedRuntimeArgs(['hermes', '-z', 'test'], '10000');
    assert.deepEqual(args.slice(0, 4), ['exec', '--user', '10000', id]);
    assert.equal(args.includes(incarnation), true);
    assert.equal(args.includes('compose'), false);
  });
  assert.throws(() => pinnedRuntimeArgs(['true'], '10000'));
  const calls = readFileSync(log, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(calls.filter(args => args[0] === 'compose').length, 1);
});

test('runtime binding rejects coerced fields and extra private data', () => {
  const runtime = { schema: 1, backend: 'hermes', kind: 'docker', instance_id: 'a'.repeat(64),
    image_digest: 'sha256:' + 'b'.repeat(64), incarnation: 'sha256:' + 'c'.repeat(64) };
  for (const key of ['instance_id', 'image_digest', 'incarnation'] as const) {
    assert.throws(() => executionRuntimeBinding({ ...runtime, [key]: [runtime[key]] }));
  }
  assert.throws(() => executionRuntimeBinding({ ...runtime, credential: 'private' }));
});
