import { createHash, randomUUID } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { boundedHermesDraftPrReceipt, type BoundedHermesDraftPrReceipt } from './bounded-evidence';
import { NonRetryableBridgeExecutionError } from './errors';

// Match the bridge's CORRELATION_PAYLOAD_KEYS; all other intent remains immutable.
const CORRELATION_PAYLOAD_KEYS = new Set(['source_event_key', 'source_delivery_id', 'source_event_keys', 'source_delivery_ids']);

export type InvocationLease = { lease_id: string; owner: string; epoch: number; fence_token: string };
export type NativeOwnerBinding = Readonly<{ invocation_id: string; epoch: number; fingerprint: string }>;
export type NativeOwnerRecovery = { ownerExitCode: number; descendantsDrained: boolean; errors: string[] };
const NATIVE_RECOVERY_ERRORS = ['child_wait_failed', 'child_signal_or_identity_failed',
  'descendant_drain_incomplete', 'supervisor_journal_write_failed', 'recovery_journal_write_failed'];
export function boundedNativeOwnerRecovery(value: unknown): value is NativeOwnerRecovery {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const v = value as NativeOwnerRecovery;
  return Object.keys(v).sort().join(',') === 'descendantsDrained,errors,ownerExitCode'
    && Number.isInteger(v.ownerExitCode) && v.ownerExitCode >= -64 && v.ownerExitCode <= 255
    && typeof v.descendantsDrained === 'boolean' && Array.isArray(v.errors)
    && v.errors.length <= NATIVE_RECOVERY_ERRORS.length
    && v.errors.every((error) => NATIVE_RECOVERY_ERRORS.includes(error));
}
export type NativeOwnerStop = {
  binding: NativeOwnerBinding; state: 'unknown'; ownerRegistered: boolean; ownerExitObserved: boolean;
  recovery?: NativeOwnerRecovery;
  dispatchQuiesced: boolean | null; reason: 'native_cleanup_observed' | 'owner_unavailable' | 'owner_timeout' | 'owner_crashed' | 'native_protocol_invalid';
};
export type InvocationExecutionContext = {
  binding: NativeOwnerBinding;
  registerCancel: (cancel: () => Promise<NativeOwnerStop>) => void;
  persistStop: (stop: NativeOwnerStop) => void;
};
export function boundedNativeOwnerStop(value: unknown, binding?: NativeOwnerBinding): NativeOwnerStop | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const v = value as NativeOwnerStop;
  if (Object.keys(v).filter((key) => key !== 'recovery').sort().join(',') !== 'binding,dispatchQuiesced,ownerExitObserved,ownerRegistered,reason,state'
      || ('recovery' in v && !boundedNativeOwnerRecovery(v.recovery))
      || !v.binding || Object.keys(v.binding).sort().join(',') !== 'epoch,fingerprint,invocation_id'
      || !/^[0-9a-f-]{36}:[0-9a-f-]{36}:[1-9][0-9]*$/.test(v.binding.invocation_id)
      || !Number.isSafeInteger(v.binding.epoch) || v.binding.epoch < 1
      || !/^[a-f0-9]{64}$/.test(v.binding.fingerprint) || v.state !== 'unknown'
      || typeof v.ownerRegistered !== 'boolean' || typeof v.ownerExitObserved !== 'boolean'
      || (v.dispatchQuiesced !== null && typeof v.dispatchQuiesced !== 'boolean')
      || !['native_cleanup_observed', 'owner_unavailable', 'owner_timeout', 'owner_crashed', 'native_protocol_invalid'].includes(v.reason)
      || binding && canonical(v.binding) !== canonical(binding)) return undefined;
  return v;
}
export type InvocationStatus = {
  state: 'not_found' | 'running' | 'outcome_unknown' | 'completed';
  cancellationRequested: boolean;
  cancellationAcknowledged: boolean;
  receipt?: BoundedHermesDraftPrReceipt;
  lease?: InvocationLease;
  nativeStop?: NativeOwnerStop;
};

export function invocationLease(value: unknown): InvocationLease {
  const lease = value as InvocationLease | null;
  if (!lease || typeof lease !== 'object' || Object.keys(lease).sort().join(',') !== 'epoch,fence_token,lease_id,owner'
      || !/^[0-9a-f-]{36}$/i.test(lease.lease_id) || typeof lease.owner !== 'string' || !lease.owner || lease.owner.length > 200
      || !Number.isSafeInteger(lease.epoch) || lease.epoch < 1 || typeof lease.fence_token !== 'string'
      || !lease.fence_token || lease.fence_token.length > 200) throw invalid('Invalid invocation fence');
  return lease;
}

function invalid(message: string): NonRetryableBridgeExecutionError {
  return new NonRetryableBridgeExecutionError('hermes_invocation_conflict', message);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}';
  return JSON.stringify(value);
}

/** Executor-private receipts only. Prompts, output and session material are never stored. */
export class InvocationJournal {
  private active = new Map<string, Promise<BoundedHermesDraftPrReceipt>>();
  private nativeOwners = new Map<string, { binding: NativeOwnerBinding; cancel?: () => Promise<NativeOwnerStop> }>();
  constructor(private readonly directory: string) {
    if (!path.isAbsolute(directory)) throw invalid('Invocation journal requires an absolute directory');
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.privateDirectory(directory);
    this.syncDirectory(path.dirname(directory));
  }

  private location(job: BridgeJob): string {
    if (!/^[0-9a-f-]{36}$/i.test(job.job_id)) throw invalid('Invalid invocation job identity');
    return path.join(this.directory, job.job_id);
  }

  private fingerprint(job: BridgeJob, version: 1 | 2): string {
    const payload = version === 1 ? job.payload : Object.fromEntries(
      Object.entries(job.payload).filter(([key]) => !CORRELATION_PAYLOAD_KEYS.has(key)));
    const intent = { job_id: job.job_id, idempotency_key: job.idempotency_key,
      requested_action: job.requested_action, branch: job.branch, payload };
    // Bind the hash format too, so old code cannot mistake new records for legacy ones.
    return createHash('sha256').update(canonical(version === 1 ? intent : { fingerprint_version: 2, ...intent })).digest('hex');
  }

  status(job: BridgeJob, cancel = false): InvocationStatus {
    const directory = this.location(job);
    if (!existsSync(directory)) return { state: 'not_found', cancellationRequested: false, cancellationAcknowledged: false };
    this.privateDirectory(directory);
    // A crash between reservation and intent persistence is unknown, never permission to execute again.
    if (!existsSync(path.join(directory, 'intent.json'))) return { state: 'outcome_unknown', cancellationRequested: false, cancellationAcknowledged: false };
    const intent = JSON.parse(readFileSync(path.join(directory, 'intent.json'), 'utf8'));
    const version = intent.fingerprint_version === undefined ? 1 : intent.fingerprint_version;
    if (version !== 1 && version !== 2) throw invalid('Unsupported invocation fingerprint version');
    if (intent.fingerprint !== this.fingerprint(job, version)) throw invalid('Invocation intent differs from the reserved job');
    const lease = invocationLease(intent.lease);
    const cancellation = path.join(directory, 'cancel');
    if (cancel && !existsSync(cancellation)) this.persist(cancellation, { requested_at: new Date().toISOString() });
    const cancellationRequested = existsSync(cancellation);
    const resultPath = path.join(directory, 'result.json');
    const nativePath = path.join(directory, 'native-stop.json');
    const nativeStop = existsSync(nativePath) ? boundedNativeOwnerStop(JSON.parse(readFileSync(nativePath, 'utf8')),
      { invocation_id: `${job.job_id.toLowerCase()}:${lease.lease_id.toLowerCase()}:${lease.epoch}`, epoch: lease.epoch, fingerprint: intent.fingerprint }) : undefined;
    if (existsSync(resultPath)) {
      const receipt = boundedHermesDraftPrReceipt(job, JSON.parse(readFileSync(resultPath, 'utf8')));
      if (!receipt) throw invalid('Invocation receipt does not match its job');
      return { state: 'completed', cancellationRequested, cancellationAcknowledged: cancellationRequested, receipt, lease, nativeStop };
    }
    return { state: this.active.has(job.job_id) ? 'running' : 'outcome_unknown', cancellationRequested, cancellationAcknowledged: false, lease, nativeStop };
  }

  async cancel(job: BridgeJob, claim: InvocationLease): Promise<InvocationStatus> {
    const lease = invocationLease(claim);
    const current = this.status(job);
    if (!current.lease || canonical(current.lease) !== canonical(lease)) throw invalid('Cancellation fence differs from the reserved owner');
    this.status(job, true);
    const owner = this.nativeOwners.get(job.job_id);
    if (owner?.cancel) await owner.cancel();
    return this.status(job);
  }

  async execute(job: BridgeJob, claim: InvocationLease, run: (context: InvocationExecutionContext) => Promise<unknown>): Promise<BoundedHermesDraftPrReceipt> {
    const lease = invocationLease(claim);
    const current = this.status(job);
    if (current.state === 'completed') {
      if (canonical(current.lease) !== canonical(lease)) throw invalid('Invocation fence differs from the reserved owner');
      return current.receipt!;
    }
    if (current.state !== 'not_found') throw invalid('Invocation already reserved; inspect its outcome instead of executing again');
    const directory = this.location(job);
    // mkdir is the cross-process reservation; even incomplete reservations survive crashes.
    mkdirSync(directory, { mode: 0o700 });
    this.syncDirectory(this.directory);
    this.persist(path.join(directory, 'intent.json'), { fingerprint_version: 2, fingerprint: this.fingerprint(job, 2), lease });
    const binding = Object.freeze({ invocation_id: `${job.job_id.toLowerCase()}:${lease.lease_id.toLowerCase()}:${lease.epoch}`,
      epoch: lease.epoch, fingerprint: this.fingerprint(job, 2) });
    const owner: { binding: NativeOwnerBinding; cancel?: () => Promise<NativeOwnerStop> } = { binding };
    this.nativeOwners.set(job.job_id, owner);
    const context: InvocationExecutionContext = {
      binding,
      registerCancel: (cancel) => {
        if (this.nativeOwners.get(job.job_id) !== owner || owner.cancel) throw invalid('Native owner context is no longer available');
        owner.cancel = cancel;
        if (existsSync(path.join(directory, 'cancel'))) void cancel().catch(() => undefined);
      },
      persistStop: (stop) => {
        const current = this.status(job);
        if (!boundedNativeOwnerStop(stop, binding) || !current.lease || canonical(current.lease) !== canonical(lease)) throw invalid('Native stop owner differs from invocation');
        this.persist(path.join(directory, 'native-stop.json'), stop);
      },
    };
    const pending = Promise.resolve().then(() => run(context)).then((value) => {
      const receipt = value && typeof value === 'object' && !Array.isArray(value) ? boundedHermesDraftPrReceipt(job, value) : null;
      if (!receipt) throw invalid('Executor did not return a bounded invocation receipt');
      this.persist(path.join(directory, 'result.json'), receipt);
      return receipt;
    });
    this.active.set(job.job_id, pending);
    try { return await pending; }
    finally { this.active.delete(job.job_id); this.nativeOwners.delete(job.job_id); }
  }

  private persist(file: string, value: unknown): void {
    const temporary = file + '.' + randomUUID() + '.tmp';
    const fd = openSync(temporary, 'wx', 0o600);
    try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); }
    finally { closeSync(fd); }
    renameSync(temporary, file);
    this.syncDirectory(path.dirname(file));
  }

  private syncDirectory(directory: string): void {
    const fd = openSync(directory, 'r');
    try { fsyncSync(fd); } finally { closeSync(fd); }
  }

  private privateDirectory(directory: string): void {
    const info = lstatSync(directory);
    if (!info.isDirectory() || (info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid())) {
      throw invalid('Invocation journal must be an executor-owned private directory');
    }
  }
}
