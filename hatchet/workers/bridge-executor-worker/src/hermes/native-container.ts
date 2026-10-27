import { AsyncLocalStorage } from 'node:async_hooks';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { InvocationJournal, type InvocationExecutionContext, type NativeContainerRecord, type NativeOwnerStop } from './invocation-journal';
import { nativeOwnerBootstrap, nativeOwnerJournal } from './native-owner';
import { NonRetryableBridgeExecutionError } from './errors';

const scope = new AsyncLocalStorage<{ id: string; context: InvocationExecutionContext }>();
const TMPFS = '/opt/data:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=10000,gid=10000';
const ENV = ['PATH=/opt/hermes/.venv/bin:/usr/bin:/bin', 'HOME=/tmp/factory-native-profile',
  'LANG=C.UTF-8', 'PYTHONUNBUFFERED=1'];
const LABEL = 'com.driftward.factory.native-owner';
function refused(): NonRetryableBridgeExecutionError {
  return new NonRetryableBridgeExecutionError('hermes_invocation_conflict',
    'Isolated native executor unavailable; inspect its retained invocation record',
    { failureDomain: 'hermes_process', failureStage: 'hermes_spawn', retryable: false });
}
export function nativeContainerScope() { return scope.getStore(); }

function bootstrap(): string {
  const directory = ['/usr/local/share/driftward-factory/native-owner',
    path.resolve(__dirname, '../../../../../deploy/experimental-native-executor')]
    .find((candidate) => existsSync(path.join(candidate, 'container.py')));
  if (!directory) throw refused();
  const source = readFileSync(path.join(directory, 'container.py'), 'utf8');
  if (source.length > 32768) throw refused();
  return source;
}
function command(args: string[], existing = false): string[] {
  return ['env', '-i', ...ENV, '/opt/hermes/.venv/bin/python', '-c', bootstrap(),
    existing ? '--existing' : '--fresh', ...args];
}
async function docker(args: string[], timeout = 15000, maxBuffer = 65536): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => execFile('docker', args,
    { timeout, maxBuffer, env: process.env }, (error, stdout) => {
      if (error) reject(refused());
      else resolve({ stdout, stderr: '' });
    }));
}
async function inspect(id: string): Promise<any> {
  const format = '[{"Id":{{json .Id}},"Image":{{json .Image}},"State":{{json .State}},' +
    '"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}},' +
    '"Config":{"User":{{json .Config.User}},"Labels":{{json .Config.Labels}}}}]';
  const rows = JSON.parse((await docker(['inspect', '--type=container', '--format', format, id])).stdout);
  if (!Array.isArray(rows) || rows.length !== 1 || rows[0].Id !== id) throw refused();
  return rows[0];
}
export async function nativeContainerConfiguration(): Promise<{ image: string; seed: string }> {
  const image = process.env.DRIFTWARD_HERMES_NATIVE_IMAGE || '';
  const seed = process.env.DRIFTWARD_HERMES_NATIVE_SEED || '';
  if (process.env.DRIFTWARD_HERMES_RUNTIME_MODE === 'local-executor'
      || process.env.DRIFTWARD_HERMES_PROFILE !== 'factory-native-executor'
      || !/^sha256:[0-9a-f]{64}$/.test(image)
      || !/^factory-native-executor-seed-[0-9a-f]{32}$/.test(seed)) throw refused();
  // A newly provisioned, dedicated role volume is required. No bind paths,
  // shared Hermes volumes, ambient provider environment or root auth fallback.
  const volumes = JSON.parse((await docker(['volume', 'inspect', seed])).stdout);
  if (volumes.length !== 1 || volumes[0].Name !== seed || volumes[0].Driver !== 'local'
      || Object.keys(volumes[0].Options || {}).length
      || volumes[0].Labels?.['com.driftward.factory.role'] !== 'native-executor-seed'
      || volumes[0].Labels?.['com.driftward.factory.fresh-credentials'] !== 'true') throw refused();
  const images = JSON.parse((await docker(['image', 'inspect', image])).stdout);
  const config = images[0]?.Config;
  if (images.length !== 1 || images[0].Id !== image || !config
      || Object.keys(config.Volumes || {}).some((name) => name !== '/opt/data')
      || (config.OnBuild || []).length
      || (config.Env || []).some((item: string) => typeof item !== 'string'
        || /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE/i.test(item.split('=', 1)[0]))) throw refused();
  return { image, seed };
}
export function nativeContainerArguments(config: { image: string; seed: string }, persistent: boolean): string[] {
  return ['--restart=no', '--init=false', '--read-only', '--no-healthcheck', '--user=10000:10000', '--cap-drop=ALL',
    '--security-opt=no-new-privileges:true', '--pids-limit=128', '--memory=2g', '--cpus=2',
    '--ulimit', 'nofile=1024:1024', '--tmpfs', TMPFS,
    ...(persistent ? ['--mount', 'type=volume,destination=/tmp']
      : ['--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777']),
    '--mount', `type=volume,source=${config.seed},destination=/run/factory-native-seed,readonly`,
    '--workdir=/tmp', '--entrypoint=/usr/bin/env', config.image];
}
function verify(row: any, record: Pick<NativeContainerRecord, 'containerId' | 'image' | 'nonce'>, seed: string, persistent = true): void {
  const host = row.HostConfig, config = row.Config;
  const mounts = row.Mounts;
  if (row.Id !== record.containerId || row.Image !== record.image || config.Labels?.[LABEL] !== record.nonce
      || host.PidMode !== '' || host.Init === true || host.RestartPolicy?.Name !== 'no'
      || host.Privileged !== false || host.ReadonlyRootfs !== true || config.User !== '10000:10000'
      || JSON.stringify(host.CapDrop) !== '["ALL"]' || host.CapAdd?.length || host.Binds?.length
      || host.VolumesFrom?.length || host.Devices?.length
      || JSON.stringify(host.SecurityOpt) !== '["no-new-privileges:true"]'
      || !Array.isArray(mounts) || mounts.filter((m: any) => m.Type === 'volume').length !== (persistent ? 2 : 1)
      || !mounts.some((m: any) => m.Type === 'volume' && m.Name === seed
        && m.Destination === '/run/factory-native-seed' && m.RW === false)
      || persistent && !mounts.some((m: any) => m.Type === 'volume' && m.Destination === '/tmp' && m.RW === true)
      || mounts.some((m: any) => !['/tmp', '/opt/data', '/run/factory-native-seed'].includes(m.Destination))) throw refused();
}

/** Typed read operations get the same executor role, never the shared runtime. */
export async function execNativeRead(args: string[], timeout: number, maxBuffer: number) {
  const current = scope.getStore();
  if (current) return docker(['exec', current.id, ...command(args, true)], timeout, maxBuffer);
  const config = await nativeContainerConfiguration();
  const journal = new InvocationJournal(process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY || '/var/lib/driftward-hermes-factory/invocations');
  const { record, persist } = journal.reserveReadContainer(config.image);
  const id = (await docker(['create', '--label', `${LABEL}=${record.nonce}`,
    ...nativeContainerArguments(config, false), ...command(args)])).stdout.trim();
  if (!/^[0-9a-f]{64}$/.test(id)) throw refused();
  record.containerId = id; record.state = 'created';
  try {
    persist();
    verify(await inspect(id), record, config.seed, false);
    record.state = 'started'; persist();
    const result = await docker(['start', '--attach', id], timeout, maxBuffer);
    const ended = await inspect(id);
    verify(ended, record, config.seed, false);
    if (ended.State.Running !== false || ended.State.Status !== 'exited' || ended.State.ExitCode !== 0) throw refused();
    return result;
  } finally {
    const row = await inspect(id);
    verify(row, record, config.seed, false);
    if (row.State.Running) await docker(['kill', '--signal=KILL', id]);
    const ended = await inspect(id);
    verify(ended, record, config.seed, false);
    if (ended.State.Running !== false || !['created', 'exited'].includes(ended.State.Status)) throw refused();
    record.state = 'stopped'; record.exitCode = ended.State.ExitCode; persist();
    await docker(['rm', '--volumes', id]);
    record.state = 'removed'; persist();
  }
}

/** Reserve before create; ambiguous responses never authorize name-based cleanup or retry. */
export async function withNativeContainer<T>(context: InvocationExecutionContext, run: () => Promise<T>, observability: Record<string, string> = {}): Promise<T> {
  if (!context.persistContainer) throw refused();
  const config = await nativeContainerConfiguration();
  const record: NativeContainerRecord = { schema: 1, binding: context.binding, image: config.image,
    nonce: randomUUID(), containerId: null, state: 'reserved', exitCode: null };
  context.persistContainer(record);
  let nativeCancel: (() => Promise<NativeOwnerStop>) | undefined;
  let cancelled = false;
  let stopping = Promise.resolve();
  const stopOnce = async () => {
    if (!record.containerId) return;
    const row = await inspect(record.containerId);
    verify(row, record, config.seed);
    if (row.State.Running) await docker(['kill', '--signal=KILL', record.containerId]);
    const ended = await inspect(record.containerId);
    verify(ended, record, config.seed);
    if (ended.State.Running !== false || !['exited', 'created'].includes(ended.State.Status)
        || !Number.isInteger(ended.State.ExitCode)) throw refused();
    record.state = 'stopped'; record.exitCode = ended.State.ExitCode;
    context.persistContainer!(record);
  };
  const stop = () => {
    stopping = stopping.catch(() => undefined).then(stopOnce);
    return stopping;
  };
  context.registerCancel(async () => {
    cancelled = true;
    try { if (nativeCancel) await nativeCancel(); }
    finally { await stop(); }
    const value: NativeOwnerStop = { binding: context.binding, state: 'unknown', ownerRegistered: false,
      ownerExitObserved: false, dispatchQuiesced: null, reason: 'owner_timeout' };
    return value;
  });
  const adapted: InvocationExecutionContext = { ...context, registerCancel: (cancel) => {
    nativeCancel = cancel;
    if (cancelled) void cancel().catch(() => undefined);
  } };
  try {
    if (cancelled) throw refused();
    const created = (await docker(['create', '-i', '--name', `factory-native-owner-${record.nonce}`,
      '--label', `${LABEL}=${record.nonce}`, ...nativeContainerArguments(config, true),
      ...command(['env', ...Object.entries(observability).map(([key, value]) => `${key}=${value}`),
        '/opt/hermes/.venv/bin/python', '-u', '-c', nativeOwnerBootstrap(),
        '--experimental-local-terminal', '--controlled', '--journal', nativeOwnerJournal(context)])])).stdout.trim();
    if (!/^[0-9a-f]{64}$/.test(created)) throw refused();
    record.containerId = created; record.state = 'created';
    context.persistContainer(record);
    verify(await inspect(created), record, config.seed);
    if (cancelled) throw refused();
    await docker(['start', created]);
    record.state = 'started'; context.persistContainer(record);
    let ready = false;
    for (let i = 0; i < 50 && !cancelled; i++) {
      try { await docker(['exec', created, ...command(['true'], true)], 1000); ready = true; break; }
      catch { await new Promise((resolve) => setTimeout(resolve, 100)); }
    }
    if (!ready || cancelled) throw refused();
    return await scope.run({ id: created, context: adapted }, run);
  } finally {
    // Retain the stopped container and its private volume. Exit is containment
    // evidence only; the invocation/result journal remains the work authority.
    await stop();
  }
}
