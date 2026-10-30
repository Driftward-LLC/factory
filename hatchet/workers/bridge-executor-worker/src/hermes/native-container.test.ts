import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { withNativeContainer, execNativeRead, nativeContainerScope } from './native-container';
import type { InvocationExecutionContext, NativeContainerRecord, NativeOwnerStop } from './invocation-journal';

const binding = { invocation_id: '00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-000000000002:1',
  epoch: 1, fingerprint: 'a'.repeat(64) };
const script = `#!${process.execPath}
const fs=require('node:fs'),path=require('node:path');
const args=process.argv.slice(2),root=process.env.NATIVE_CONTAINER_TEST_ROOT,mode=process.env.NATIVE_CONTAINER_TEST_MODE;
const emit=v=>process.stdout.write(JSON.stringify(v));
const file=path.join(root,'state'),save=v=>fs.writeFileSync(file,JSON.stringify(v)),read=()=>JSON.parse(fs.readFileSync(file));
fs.appendFileSync(path.join(root,'calls'),JSON.stringify(args)+'\\n');
if(args[0]==='volume')emit([{Name:process.env.DRIFTWARD_HERMES_NATIVE_SEED,Driver:'local',Labels:{'com.driftward.factory.role':'native-executor-seed','com.driftward.factory.fresh-credentials':'true'}}]);
else if(args[0]==='image')emit([{Id:process.env.DRIFTWARD_HERMES_NATIVE_IMAGE,Config:{}}]);
else if(args[0]==='create'){
 const nonce=args[args.indexOf('--label')+1].split('=')[1],id='c'.repeat(64),persistent=args[1]==='-i';
 save({Id:id,Image:process.env.DRIFTWARD_HERMES_NATIVE_IMAGE,Config:{User:'10000:10000',Labels:{'com.driftward.factory.native-owner':nonce}},
 HostConfig:{PidMode:'',Init:false,Privileged:false,ReadonlyRootfs:true,RestartPolicy:{Name:'no'},CapDrop:['ALL'],SecurityOpt:['no-new-privileges:true']},
 Mounts:[{Type:'volume',Name:process.env.DRIFTWARD_HERMES_NATIVE_SEED,Destination:'/run/factory-native-seed',RW:false},...(persistent?[{Type:'volume',Name:'private',Destination:'/tmp',RW:true}]:[])],State:{Running:false,Status:'created',ExitCode:0}});
 process.stdout.write(mode==='lost-create'?'unavailable':id);
}else if(args[0]==='inspect'){const row=read();if(mode==='mismatch')row.Image='sha256:'+'b'.repeat(64);if(args.includes('{{json .Id}} {{json .Image}}'))process.stdout.write(JSON.stringify(row.Id)+' '+JSON.stringify(row.Image));else emit([row]);}
else if(args[0]==='start'){
 const row=read();row.State={Running:true,Status:'running',ExitCode:0};save(row);
 if(args.includes('--attach')){row.State={Running:false,Status:'exited',ExitCode:0};save(row);process.stdout.write('42');}
 if(mode==='lost-start')process.exit(1);
 if(mode==='cancel-start')setTimeout(()=>{},200);
}else if(args[0]==='kill'){
 const row=read();if(!row.State.Running)process.exit(1);
 row.State={Running:false,Status:'exited',ExitCode:137};save(row);
}else if(args[0]==='exec'){if(args.at(-1)==='observe')process.stdout.write('sha256:'+'d'.repeat(64));else if(args.at(-1)!=='true')process.stdout.write('42');}
else if(args[0]==='rm'){fs.unlinkSync(file);}
else process.exit(1);
`;

test('container reservation survives startup ambiguity, journal faults and cancellation without shared-runtime fallback', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-container-test-'));
  const env = { PATH: directory + path.delimiter + process.env.PATH,
    DRIFTWARD_HERMES_PROFILE: 'factory-native-executor', DRIFTWARD_HERMES_NATIVE_IMAGE: 'sha256:' + 'a'.repeat(64),
    DRIFTWARD_HERMES_NATIVE_SEED: 'factory-native-executor-seed-' + 'a'.repeat(32),
    DRIFTWARD_HERMES_INVOCATION_DIRECTORY: directory + '/journal', NATIVE_CONTAINER_TEST_ROOT: directory,
    NATIVE_CONTAINER_TEST_MODE: '', DRIFTWARD_HERMES_RUNTIME_MODE: '' };
  const previous = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
  Object.assign(process.env, env);
  writeFileSync(path.join(directory, 'docker'), script, { mode: 0o700 });
  try {
    for (const mode of ['success', 'reserved-write', 'created-write', 'lost-create', 'lost-start', 'mismatch', 'pre-cancel', 'cancel-start']) {
      process.env.NATIVE_CONTAINER_TEST_MODE = mode;
      for (const file of ['calls', 'state']) rmSync(path.join(directory, file), { force: true });
      const records: NativeContainerRecord[] = [];
      let cancel!: () => Promise<NativeOwnerStop>;
      let entered = false;
      const context: InvocationExecutionContext = { binding, bindRuntime: async (runtime) => { assert.equal(runtime.instance_id, 'c'.repeat(64)); }, persistStop: () => undefined,
        persistContainer: (record) => {
          if ((mode === 'reserved-write' && record.state === 'reserved') || (mode === 'created-write' && record.state === 'created')) throw Error('disk unavailable');
          records.push(JSON.parse(JSON.stringify(record)));
        }, registerCancel: (value) => { cancel = value; if (mode === 'pre-cancel') void value(); } };
      const pending = withNativeContainer(context, async () => {
        entered = true;
        assert.equal(nativeContainerScope()?.id, 'c'.repeat(64));
        assert.equal((await execNativeRead(['python3', '-c', 'print(42)'], 1000, 4096)).stdout, '42');
        return 42;
      });
      const rejected = mode === 'success' ? undefined : assert.rejects(pending);
      if (mode === 'cancel-start') {
        const deadline = Date.now() + 3000;
        while (Date.now() < deadline && (!existsSync(path.join(directory, 'state'))
          || !JSON.parse(readFileSync(path.join(directory, 'state'), 'utf8')).State.Running)) await new Promise((resolve) => setTimeout(resolve, 5));
        await Promise.all([cancel(), cancel()]);
      }
      if (rejected) await rejected;
      else assert.equal(await pending, 42);
      assert.equal(entered, mode === 'success');
      const calls = readFileSync(path.join(directory, 'calls'), 'utf8').trim().split('\n').map((line) => JSON.parse(line) as string[]);
      assert.equal(calls.some((args) => args[0] === 'compose' || args.includes('/var/run/docker.sock')), false);
      assert.equal(calls.some((args) => args[0] === 'rm'), false, 'Invocation state must be retained');
      if (['reserved-write', 'pre-cancel'].includes(mode)) assert.equal(calls.some((args) => args[0] === 'create'), false);
      if (['lost-create', 'mismatch'].includes(mode)) assert.equal(calls.some((args) => args[0] === 'kill'), false);
      if (['success', 'lost-start', 'cancel-start'].includes(mode)) {
        assert.equal(records.at(-1)?.state, 'stopped');
        assert.equal(calls.filter((args) => args[0] === 'kill').length, 1);
      }
      for (const args of calls.filter((args) => ['kill', 'start'].includes(args[0]))) assert.equal(args.at(-1), 'c'.repeat(64));
    }
    process.env.NATIVE_CONTAINER_TEST_MODE = 'success';
    assert.equal((await execNativeRead(['python3', '-c', 'print(42)'], 1000, 4096)).stdout, '42');
    const reads = readdirSync(env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY).filter((name) => name.startsWith('read-'));
    assert.equal(reads.length, 1);
    const read = JSON.parse(readFileSync(path.join(env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY, reads[0]), 'utf8'));
    assert.equal(read.state, 'removed');
    assert.equal(read.containerId, 'c'.repeat(64));
    process.env.DRIFTWARD_HERMES_RUNTIME_MODE = 'local-executor';
    await assert.rejects(execNativeRead(['true'], 1000, 4096));
  } finally {
    for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    rmSync(directory, { recursive: true, force: true });
  }
});
