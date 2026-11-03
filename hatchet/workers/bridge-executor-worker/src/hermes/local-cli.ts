import { execFile, type ChildProcess } from 'node:child_process';
import { lstatSync, mkdirSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { InvocationExecutionContext, NativeOwnerStop } from './invocation-journal';
import { localExecutorEnabled, localRuntimeArguments, pinnedRuntime } from './execution-runtime';
import { NonRetryableBridgeExecutionError } from './errors';

function refused(): NonRetryableBridgeExecutionError {
  return new NonRetryableBridgeExecutionError('hermes_invocation_conflict', 'Local runtime requires reviewed recovery before another invocation',
    { failureDomain: 'hermes_process', failureStage: 'hermes_spawn', retryable: false });
}

function privateDirectory(directory: string): void {
  const info = lstatSync(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid?.() || info.mode & 0o077) throw refused();
}

/** Hold the exact runtime after uncertainty. Restart never settles the original job's effects. */
export async function runLocalCli(context: InvocationExecutionContext, args: string[], timeout: number, maxBuffer: number):
Promise<{ stdout: string; stderr: string }> {
  const runtime = pinnedRuntime();
  if (!localExecutorEnabled() || !runtime || !context.registerCompletion) throw refused();
  const commandArgs = await localRuntimeArguments(['env', 'HERMES_HOME=/opt/data/profiles/factory-executor', ...args]);
  const root = process.env.FACTORY_HERMES_INVOCATION_DIRECTORY || path.join(process.env.HERMES_HOME || '/opt/data', 'factory-invocations');
  const holds = path.join(root, 'local-admission');
  mkdirSync(root, { recursive: true, mode: 0o700 });
  privateDirectory(root);
  mkdirSync(holds, { recursive: true, mode: 0o700 });
  privateDirectory(holds);
  const directory = path.join(holds, runtime.incarnation.slice('sha256:'.length));
  try { mkdirSync(directory, { mode: 0o700 }); } catch { throw refused(); }
  const marker = path.join(directory, 'binding.json');
  const reservation = JSON.stringify({ binding: context.binding, runtime });
  writeFileSync(marker, reservation, { mode: 0o600, flag: 'wx' });
  let child: ChildProcess | undefined;
  let cancelled = false, sent = false, exited = false;
  const observation = (): NativeOwnerStop => ({ binding: context.binding, state: 'unknown', ownerRegistered: false,
    ownerExitObserved: false, dispatchQuiesced: null, reason: 'native_interrupt_requested',
    interruption: { scope: 'gateway_child', signal: 'SIGTERM', sent, childExitObserved: exited } });
  const persist = () => { const value = observation(); context.persistStop(value); return value; };
  context.registerCompletion(() => {
    if (cancelled || readFileSync(marker, 'utf8') !== reservation) throw refused();
    // Only a validated receipt already persisted by the journal releases admission.
    unlinkSync(marker);
    rmdirSync(directory);
  });
  context.registerCancel(async () => {
    if (!cancelled) {
      cancelled = true;
      // This is the gateway's current child handle, never a recovered/caller-supplied PID.
      if (child && !exited) {
        try { sent = child.kill('SIGTERM'); } catch { sent = false; }
      }
    }
    return persist();
  });
  if (cancelled) throw refused();
  const result = await new Promise<{ stdout: string; stderr: string }>((resolve, reject) => {
    child = execFile('python3', commandArgs, { timeout, maxBuffer, env: process.env }, (error, stdout, stderr) => {
      if (error) { Object.assign(error, { stdout, stderr }); reject(error); }
      else if (cancelled) reject(refused());
      else resolve({ stdout, stderr });
    });
    child.once('exit', () => {
      exited = true;
      if (cancelled) { try { persist(); } catch { /* A failed journal write cannot resurrect the child or release its hold. */ } }
    });
  });
  return result;
}
