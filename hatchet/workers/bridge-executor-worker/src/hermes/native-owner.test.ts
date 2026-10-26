import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { nativeOwnerBootstrap, runNativeOwner } from './native-owner';
import type { InvocationExecutionContext, NativeOwnerStop } from './invocation-journal';

const binding = { invocation_id: '00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-000000000002:1',
  epoch: 1, fingerprint: 'a'.repeat(64) };
const fixture = `
const fs=require('node:fs'), readline=require('node:readline');
let launch, started=false, cancels=0;
const mode=process.env.MODE, marker=process.env.MARKER;
const emit=(v)=>process.stdout.write(JSON.stringify(v)+'\\n');
const record=()=>fs.writeFileSync(marker,JSON.stringify({started,cancels}));
const stop=()=>emit({event:'stop',receipt:{...launch.binding,state:'unknown',dispatch_quiesced:true}});
readline.createInterface({input:process.stdin}).on('line',line=>{
 const frame=JSON.parse(line);
 if(!launch){launch=frame;
  if(mode==='noise'){process.stdout.write('PRIVATE_PROVIDER_ERROR\\n');process.stderr.write('PRIVATE_PROVIDER_SECRET');return;}
  if(mode==='crash'){process.exit(1);}
  if(mode==='timeout'){return;}
  setTimeout(()=>emit({event:'registered',ownership:{...launch.binding,pid:process.pid,runtime_incarnation:'test-incarnation',
   ...(mode==='wrong-binding'?{fingerprint:'b'.repeat(64)}:{})}}),30);return;
 }
 if(frame.start){started=true;record();
  if(mode==='transport-loss')process.exit(1);
  if(mode==='complete'||mode==='race')emit({event:'result',candidate:{status:'created',pr_url:'https://github.com/example/project/pull/1',branch:'codex/test'}});
 }
 if(frame.cancel){cancels++;record();stop();}
}).on('close',()=>{if(launch){stop();emit({event:'owner_exiting',binding:launch.binding,worker_exited:true});}process.exit(0);});
setTimeout(()=>process.exit(1),3000).unref();
`;

function setup(mode: string, precancel = false) {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-owner-controller-'));
  const marker = path.join(directory, 'frames.json');
  const evidence: NativeOwnerStop[] = [];
  const children: ReturnType<typeof spawn>[] = [];
  let fixtureError = '';
  let cancel!: () => Promise<NativeOwnerStop>;
  const context: InvocationExecutionContext = { binding,
    persistStop: (value) => evidence.push(value),
    registerCancel: (value) => { cancel = value; if (precancel) void value(); },
  };
  const run = () => runNativeOwner(context, { prompt: 'PRIVATE_PROMPT', backend: 'local' }, {
    arguments: ['fixed-runtime-bootstrap'], environment: { ...process.env, MODE: mode, MARKER: marker }, timeout: mode === 'timeout' ? 100 : 2000,
    maxBuffer: 65536, spawnProcess: ((command: string, args: string[], options: Parameters<typeof spawn>[2]) => {
      assert.equal(command, 'docker');
      assert.deepEqual(args, ['fixed-runtime-bootstrap']);
      const child = spawn(process.execPath, ['-e', fixture], options);
      children.push(child);
      child.stderr?.on('data', (value: Buffer) => { fixtureError += value.toString(); });
      if (mode === 'race') child.stdout?.on('data', (chunk: Buffer) => {
        if (chunk.toString().includes('"event":"result"')) void cancel();
      });
      return child;
    }) as typeof spawn,
  });
  return { run, cancel: () => cancel(), evidence,
    frames: () => { assert.equal(fixtureError, '', 'Disposable protocol fixture failed'); return JSON.parse(readFileSync(marker, 'utf8')); },
    async close() { for (const child of children) { child.stdin?.end(); if (child.exitCode === null) child.kill(); }
      rmSync(directory, { recursive: true, force: true }); },
  };
}

test('fixed bootstrap contains only checked-in assets and the existing native runtime/profile', () => {
  const code = nativeOwnerBootstrap();
  assert.match(code, /\/opt\/hermes/);
  assert.match(code, /factory-executor\/factory-native-owners/);
  assert.doesNotMatch(code, /PRIVATE_PROMPT/);
});

test('pre-registration cancellation withholds start and duplicate cancellation uses one exact pipe request', async () => {
  const fixture = setup('cancel', true);
  try {
    const pending = fixture.run();
    const rejected = assert.rejects(pending, /Experimental native owner/);
    const first = await fixture.cancel();
    const second = await fixture.cancel();
    await rejected;
    assert.equal(first.state, 'unknown');
    assert.equal(second.binding, binding);
    assert.deepEqual(fixture.frames(), { started: false, cancels: 1 });
    assert.ok(fixture.evidence.at(-1)?.ownerExitObserved);
    assert.doesNotMatch(JSON.stringify(fixture.evidence), /PRIVATE_/);
  } finally { await fixture.close(); }
});

test('concurrent child pipes remain independently owned while completion races with cancellation', async () => {
  const stopped = setup('cancel', true);
  const completed = setup('race');
  try {
    const failed = assert.rejects(stopped.run());
    const candidate = JSON.parse(await completed.run());
    await failed;
    assert.equal(candidate.status, 'created');
    assert.equal(completed.frames().started, true);
    assert.equal(stopped.frames().started, false);
    assert.equal(completed.evidence.at(-1)?.state, 'unknown');
    assert.equal(completed.evidence.at(-1)?.ownerExitObserved, true);
  } finally { await stopped.close(); await completed.close(); }
});

test('wrong binding, private output, process crash and timeout never become a completion or containment receipt', async () => {
  for (const mode of ['wrong-binding', 'noise', 'crash', 'timeout', 'transport-loss']) {
    const fixture = setup(mode);
    try {
      await assert.rejects(fixture.run(), (error: Error) => {
        assert.doesNotMatch(error.message, /PRIVATE_/);
        return true;
      });
      assert.ok(fixture.evidence.length > 0);
      assert.ok(fixture.evidence.every((item) => item.state === 'unknown'));
      assert.doesNotMatch(JSON.stringify(fixture.evidence), /PRIVATE_/);
      if (mode === 'transport-loss') {
        assert.equal(fixture.evidence.at(-1)?.ownerRegistered, true);
        assert.equal(fixture.evidence.at(-1)?.ownerExitObserved, false);
      }
    } finally { await fixture.close(); }
  }
});
