import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { boundedNativeOwnerRecovery, type InvocationExecutionContext, type NativeOwnerStop, type NativeOwnerRecovery } from './invocation-journal';
import { NonRetryableBridgeExecutionError } from './errors';

const PROFILE = '/tmp/factory-native-profile';

/** Public checked-in assets only; no request/job value becomes Python source. */
export function nativeOwnerBootstrap(): string {
  const directory = ['/usr/local/share/driftward-factory/native-owner',
    path.resolve(__dirname, '../../../../../deploy/experimental-native-executor')]
    .find((candidate) => existsSync(path.join(candidate, 'owner.py')) && existsSync(path.join(candidate, 'main.py')));
  if (!directory) throw failure('hermes_process_spawn_failed');
  const owner = readFileSync(path.join(directory, 'owner.py'), 'utf8');
  const main = readFileSync(path.join(directory, 'main.py'), 'utf8');
  if (owner.length + main.length > 128 * 1024) throw failure('hermes_process_spawn_failed');
  return `import sys,types,os,stat\nassert os.getpid()==1\nsys.path.insert(0,'/opt/hermes')\n` +
    `root=${JSON.stringify(PROFILE + '/factory-native-owners')}\nos.makedirs(root,mode=0o700,exist_ok=True)\n` +
    `info=os.lstat(root)\nassert stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077\n` +
    `module=types.ModuleType('owner')\nsys.modules['owner']=module\nexec(${JSON.stringify(owner)},module.__dict__)\n` +
    `exec(compile(${JSON.stringify(main)},'factory_native_owner.py','exec'),{'__name__':'__main__'})`;
}

export function nativeOwnerJournal(context: InvocationExecutionContext): string {
  return PROFILE + '/factory-native-owners/' + createHash('sha256').update(context.binding.invocation_id).digest('hex');
}

type Options = {
  arguments: string[]; environment: NodeJS.ProcessEnv; timeout: number; maxBuffer: number;
  spawnProcess?: typeof spawn;
};

function failure(reason: string): NonRetryableBridgeExecutionError {
  return new NonRetryableBridgeExecutionError(reason, 'Experimental native owner did not return a verified Factory receipt',
    { failureDomain: 'hermes_process', failureStage: 'hermes_exit', retryable: false });
}

/** One child pipe per journal reservation. A PID is never used as cancellation authority. */
export function runNativeOwner(context: InvocationExecutionContext, launch: Record<string, unknown>, options: Options): Promise<string> {
  let child: ReturnType<typeof spawn> | undefined;
  let registered = false;
  let transportClosed = false;
  let ownerExitAnnounced = false;
  let ownerExitFrameSeen = false;
  let ownerExitObserved = false;
  let recovery: NativeOwnerRecovery | undefined;
  let cancelled = false;
  let stop: NativeOwnerStop | undefined;
  let candidate: string | undefined;
  let finishStop!: (value: NativeOwnerStop) => void;
  const stopReply = new Promise<NativeOwnerStop>((resolve) => { finishStop = resolve; });
  const persist = (reason: NativeOwnerStop['reason'], quiesced: boolean | null = null) => {
    const value: NativeOwnerStop = { binding: context.binding, state: 'unknown', ownerRegistered: registered,
      ownerExitObserved, dispatchQuiesced: quiesced, reason, ...(recovery ? { recovery } : {}) };
    try { context.persistStop(value); } catch { /* Evidence failure never disables an exact owner stop. */ }
    stop = value;
    finishStop(value);
    return value;
  };
  const send = (value: unknown) => {
    if (child?.stdin?.writable) child.stdin.write(JSON.stringify(value) + '\n');
  };
  context.registerCancel(async () => {
    if (cancelled) return stop || stopReply;
    cancelled = true;
    // A pre-registration cancel withholds start authorization. The interpreter
    // can register ownership but cannot admit tools until the host sends start.
    if (registered) send({ cancel: context.binding });
    const deadline = setTimeout(() => { if (!stop) persist(transportClosed ? 'owner_crashed' : 'owner_timeout'); }, 5000);
    try { return await stopReply; } finally { clearTimeout(deadline); }
  });
  return new Promise<string>((resolve, reject) => {
    let pending = Buffer.alloc(0);
    let received = 0;
    let failed: NonRetryableBridgeExecutionError | undefined;
    const abort = (reason: string) => {
      if (failed) return;
      failed = failure(reason);
      persist(reason === 'hermes_process_timeout' ? 'owner_timeout' : 'native_protocol_invalid');
      // Closing the private input requests native cleanup; the container wrapper
      // also enforces an exact-container stop when this protocol fails.
      child?.stdin?.end();
      reject(failed);
    };
    const deadline = setTimeout(() => abort('hermes_process_timeout'), options.timeout);
    try {
      child = (options.spawnProcess || spawn)('docker', options.arguments, {
        env: options.environment, stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch {
      clearTimeout(deadline);
      persist('owner_unavailable');
      reject(failure('hermes_process_spawn_failed'));
      return;
    }
    child.stdin?.on('error', () => abort('hermes_process_exit_nonzero'));
    child.on('error', () => abort('hermes_process_spawn_failed'));
    // Never retain or expose native/Docker diagnostics, even on failure.
    child.stderr?.on('data', () => undefined);
    child.stdout?.on('data', (chunk: Buffer) => {
      received += chunk.length;
      if (received > options.maxBuffer) { abort('hermes_process_output_limit_exceeded'); return; }
      pending = Buffer.concat([pending, chunk]);
      if (pending.length > 65536 && !pending.includes(10)) { abort('hermes_protocol_result_invalid'); return; }
      while (pending.includes(10)) {
        const end = pending.indexOf(10);
        const line = pending.subarray(0, end);
        pending = pending.subarray(end + 1);
        if (line.length > 65536) { abort('hermes_protocol_result_invalid'); return; }
        try {
          const value = JSON.parse(line.toString('utf8'));
          // Failure is final for work, but the still-owned pipe can deliver the
          // supervisor's later bounded cleanup evidence after EOF/timeout.
          if (failed && value.event !== 'owner_reaped') continue;
          if (recovery) throw new Error(); // Reaping is the final supervisor frame.
          if (value.event === 'registered') {
            const owner = value.ownership;
            if (registered || !owner || owner.invocation_id !== context.binding.invocation_id
                || owner.epoch !== context.binding.epoch || owner.fingerprint !== context.binding.fingerprint
                || !Number.isSafeInteger(owner.pid) || owner.pid < 1 || typeof owner.runtime_incarnation !== 'string') throw new Error();
            registered = true;
            send(cancelled ? { cancel: context.binding } : { start: context.binding });
          } else if (value.event === 'result') {
            if (!registered || candidate !== undefined || !value.candidate || typeof value.candidate !== 'object') throw new Error();
            candidate = JSON.stringify(value.candidate);
            child?.stdin?.end();
          } else if (value.event === 'stop') {
            const receipt = value.receipt;
            if (!registered || !receipt || receipt.invocation_id !== context.binding.invocation_id
                || receipt.epoch !== context.binding.epoch || receipt.fingerprint !== context.binding.fingerprint
                || receipt.state !== 'unknown' || typeof receipt.dispatch_quiesced !== 'boolean') throw new Error();
            persist('native_cleanup_observed', receipt.dispatch_quiesced);
            if (cancelled) child?.stdin?.end();
          } else if (value.event === 'execution_failed' || value.event === 'refused') {
            abort('hermes_process_exit_nonzero');
          } else if (value.event === 'owner_reaped') {
            const receipt = value.receipt;
            if (!receipt || receipt.schema !== 1 || receipt.invocation_id !== context.binding.invocation_id
                || receipt.epoch !== context.binding.epoch || receipt.fingerprint !== context.binding.fingerprint
                || receipt.completion !== 'not_established' || receipt.profile_reusable !== false) throw new Error();
            const observed = { ownerExitCode: receipt.owner_exit_code,
              descendantsDrained: receipt.descendants_drained, errors: receipt.errors };
            if (!boundedNativeOwnerRecovery(observed)) throw new Error();
            recovery = observed;
            persist(observed.ownerExitCode === 0 ? 'native_cleanup_observed' : 'owner_crashed', stop?.dispatchQuiesced ?? null);
          } else if (value.event === 'owner_exiting') {
            if (!registered || ownerExitFrameSeen || JSON.stringify(value.binding) !== JSON.stringify(context.binding)
                || typeof value.worker_exited !== 'boolean' || typeof value.dispatch_quiesced !== 'boolean'
                || !Array.isArray(value.errors) || value.errors.length) throw new Error();
            ownerExitFrameSeen = true;
            ownerExitAnnounced = value.worker_exited && value.dispatch_quiesced;
          } else throw new Error();
        } catch { abort('hermes_protocol_result_invalid'); }
      }
    });
    child.on('close', (code) => {
      clearTimeout(deadline);
      transportClosed = true;
      ownerExitObserved = code === 0 && ownerExitAnnounced && recovery?.ownerExitCode === 0
        && recovery.descendantsDrained && recovery.errors.length === 0;
      if (stop) persist(stop.reason, stop.dispatchQuiesced);
      else if (cancelled || code !== 0 || !ownerExitObserved || candidate === undefined) persist('owner_crashed');
      if (failed) return;
      if (code === 0 && ownerExitObserved && candidate !== undefined && pending.length === 0) resolve(candidate);
      else reject(failure('hermes_process_exit_nonzero'));
    });
    send({ ...launch, binding: context.binding });
  });
}
