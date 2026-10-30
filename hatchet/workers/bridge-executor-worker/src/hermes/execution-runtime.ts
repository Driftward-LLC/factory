import { AsyncLocalStorage } from 'node:async_hooks';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { executionRuntimeBinding, type ExecutionRuntimeBinding } from '@driftward/hatchet-worker-shared';
import type { InvocationExecutionContext } from './invocation-journal';
import { NonRetryableBridgeExecutionError } from './errors';

const exec = promisify(execFile);
const scope = new AsyncLocalStorage<ExecutionRuntimeBinding>();
// Checked inside the same container process that execs the command. A host-side
// inspect followed by exec alone leaves a restart race between the two calls.
export const RUNTIME_GUARD = `import hashlib,os,sys
identity='sha256:'+hashlib.sha256((open('/proc/sys/kernel/random/boot_id').read().strip()+':'+open('/proc/1/stat').read().rsplit(')',1)[1].split()[19]).encode()).hexdigest()
if sys.argv[1]=='observe': print(identity)
else:
 if identity!=sys.argv[1]: sys.exit(78)
 os.execvp(sys.argv[2],sys.argv[2:])
`;

function refused() {
  return new NonRetryableBridgeExecutionError('hermes_invocation_conflict', 'Exact execution runtime is unavailable',
    { failureDomain: 'hermes_process', failureStage: 'hermes_spawn', retryable: false });
}

export function pinnedRuntime(): ExecutionRuntimeBinding | undefined { return scope.getStore(); }

export function pinnedRuntimeArgs(args: string[], user: string): string[] {
  const runtime = pinnedRuntime();
  if (!runtime) throw refused();
  return ['exec', '--user', user, runtime.instance_id, 'python3', '-c', RUNTIME_GUARD, runtime.incarnation, ...args];
}

export async function withPinnedRuntime<T>(context: InvocationExecutionContext, containerId: string, run: () => Promise<T>): Promise<T> {
  if (!context.bindRuntime || !/^[a-f0-9]{64}$/.test(containerId)) throw refused();
  let runtime: ExecutionRuntimeBinding;
  try {
    const inspected = await exec('docker', ['inspect', '--type=container', '--format', '{{json .Id}} {{json .Image}}', containerId],
      { timeout: 5000, maxBuffer: 4096 });
    const [id, image] = inspected.stdout.trim().split(' ').map((item) => JSON.parse(item));
    if (id !== containerId) throw refused();
    const observed = await exec('docker', ['exec', '--user', '10000', containerId, 'python3', '-c', RUNTIME_GUARD, 'observe'],
      { timeout: 5000, maxBuffer: 4096 });
    runtime = executionRuntimeBinding({ schema: 1, backend: 'hermes', kind: 'docker',
      instance_id: id, image_digest: image, incarnation: observed.stdout.trim() });

  } catch (error) {
    if (error instanceof NonRetryableBridgeExecutionError) throw error;
    throw refused();
  }
  await context.bindRuntime(runtime);
  return scope.run(runtime, run);
}

export async function resolveComposeRuntime(composePrefix: string[]): Promise<string> {
  try {
    const result = await exec('docker', [...composePrefix, 'ps', '--quiet', 'hermes'], { timeout: 5000, maxBuffer: 4096 });
    const id = result.stdout.trim();
    if (!/^[a-f0-9]{64}$/.test(id)) throw refused();
    return id;
  } catch { throw refused(); }
}
