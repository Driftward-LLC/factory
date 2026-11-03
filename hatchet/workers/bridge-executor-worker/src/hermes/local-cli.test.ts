import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { type TestContext } from 'node:test';
import { RUNTIME_GUARD, withLocalRuntime } from './execution-runtime';
import { boundedNativeOwnerStop, type InvocationExecutionContext, type NativeOwnerStop } from './invocation-journal';
import { runLocalCli } from './local-cli';

function fixture(t: TestContext) {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-local-cli-'));
  const previous = { ...process.env };
  t.after(() => { process.env = previous; rmSync(directory, { recursive: true, force: true }); });
  t.mock.method(os, 'hostname', () => 'a'.repeat(12));
  const incarnation = execFileSync('python3', ['-c', RUNTIME_GUARD, 'observe'], { encoding: 'utf8' }).trim();
  process.env.FACTORY_HERMES_RUNTIME_MODE = 'local-executor';
  process.env.FACTORY_INSTALLATION_ID = '00000000-0000-4000-8000-000000000001';
  process.env.FACTORY_HERMES_RUNTIME_BINDING_PATH = path.join(directory, 'runtime.json');
  process.env.FACTORY_HERMES_INVOCATION_DIRECTORY = path.join(directory, 'invocations');
  const runtime = { schema: 1, backend: 'hermes', kind: 'docker', instance_id: 'a'.repeat(64),
    image_digest: 'sha256:' + 'b'.repeat(64), incarnation };
  writeFileSync(process.env.FACTORY_HERMES_RUNTIME_BINDING_PATH, JSON.stringify({ installation_id: process.env.FACTORY_INSTALLATION_ID, runtime }));
  let cancel!: () => Promise<NativeOwnerStop>;
  let complete!: () => void;
  const observations: NativeOwnerStop[] = [];
  const context: InvocationExecutionContext = { binding: {
    invocation_id: '00000000-0000-4000-8000-000000000010:00000000-0000-4000-8000-000000000011:1',
    epoch: 1, fingerprint: 'd'.repeat(64) }, bindRuntime: async () => undefined,
    registerCancel: value => { cancel = value; }, registerCompletion: value => { complete = value; },
    persistStop: value => { assert.ok(boundedNativeOwnerStop(value)); observations.push(value); } };
  return { directory, runtime, context, observations, cancel: () => cancel(), complete: () => complete() };
}

async function waitFor(file: string): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!existsSync(file)) {
    if (Date.now() > deadline) throw new Error('Disposable child did not become ready');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

test('known child receives one graceful signal, foreground stops, control survives, and hold stays unknown', async (t) => {
  const f = fixture(t);
  const control = spawn('python3', ['-c', 'import time; time.sleep(20)']);
  t.after(() => control.kill('SIGKILL'));
  const ready = path.join(f.directory, 'ready');
  const signalled = path.join(f.directory, 'signalled');
  const script = `import os,pathlib,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])
def stop(*_):
 pathlib.Path(${JSON.stringify(signalled)}).write_text('SIGTERM')
 child.terminate();child.wait(timeout=5);sys.exit(130)
signal.signal(signal.SIGTERM,stop)
pathlib.Path(${JSON.stringify(ready)}).write_text(str(child.pid))
time.sleep(20)
`;
  const pending = withLocalRuntime(f.context, () => runLocalCli(f.context, ['python3', '-c', script], 10000, 4096))
    .then(value => ({ value, error: undefined }), error => ({ value: undefined, error }));
  await waitFor(ready);
  const first = await f.cancel();
  assert.equal(first.interruption?.sent, true);
  assert.equal(first.state, 'unknown');
  assert.equal(first.dispatchQuiesced, null);
  assert.ok((await pending).error);
  const duplicate = await f.cancel();
  assert.equal(duplicate.interruption?.sent, true);
  assert.equal(duplicate.interruption?.childExitObserved, true);
  assert.equal(readFileSync(signalled, 'utf8'), 'SIGTERM');
  assert.throws(() => process.kill(Number(readFileSync(ready, 'utf8')), 0), /ESRCH/);
  assert.ok(control.pid); assert.equal(process.kill(control.pid!, 0), true);
  assert.equal(duplicate.ownerExitObserved, false);
  assert.equal(duplicate.ownerRegistered, false);
  const otherJob = { ...f.context, binding: { ...f.context.binding,
    invocation_id: '00000000-0000-4000-8000-000000000099:00000000-0000-4000-8000-000000000098:1' } };
  await assert.rejects(withLocalRuntime(otherJob, () => runLocalCli(otherJob, ['true'], 1000, 4096)), /reviewed recovery/);
  assert.throws(f.complete, /reviewed recovery/);
});

test('prestart cancellation creates a durable hold and never executes the child', async (t) => {
  const f = fixture(t);
  const effect = path.join(f.directory, 'must-not-exist');
  f.context.registerCancel = value => { void value(); };
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context,
    ['python3', '-c', `open(${JSON.stringify(effect)},'w').write('bad')`], 1000, 4096)));
  assert.equal(existsSync(effect), false);
  assert.equal(f.observations[0].interruption?.sent, false);
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context, ['true'], 1000, 4096)));
});

test('normal CLI output retains admission until a durable receipt completion callback', async (t) => {
  const f = fixture(t);
  const result = await withLocalRuntime(f.context, () => runLocalCli(f.context, ['printf', 'receipt'], 1000, 4096));
  assert.equal(result.stdout, 'receipt');
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context, ['true'], 1000, 4096)));
  f.complete();
  await withLocalRuntime(f.context, () => runLocalCli(f.context, ['true'], 1000, 4096));
  f.complete();
});

test('command failure and stale runtime metadata never release or bypass admission', async (t) => {
  const f = fixture(t);
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context, ['false'], 1000, 4096)));
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context, ['true'], 1000, 4096)));
  writeFileSync(process.env.FACTORY_HERMES_RUNTIME_BINDING_PATH!, JSON.stringify({ installation_id: process.env.FACTORY_INSTALLATION_ID,
    runtime: { ...f.runtime, incarnation: 'sha256:' + 'f'.repeat(64) } }));
  await assert.rejects(withLocalRuntime(f.context, () => runLocalCli(f.context, ['true'], 1000, 4096)));
});
