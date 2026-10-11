import { createHash, randomUUID } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { boundedHermesDraftPrReceipt, type BoundedHermesDraftPrReceipt } from './bounded-evidence';
import { ArtifactObservationUnavailable, NonRetryableBridgeExecutionError } from './errors';
import { boundedArtifactObservationContext, type ArtifactObservationContext } from './artifact-observation';
import { boundedExecutionOutcome, executionOutcome, type ExecutionOutcome } from './execution-outcome';

// Match the bridge's CORRELATION_PAYLOAD_KEYS; all other intent remains immutable.
const CORRELATION_PAYLOAD_KEYS = new Set(['source_event_key', 'source_delivery_id', 'source_event_keys', 'source_delivery_ids']);

export type InvocationLease = { lease_id: string; owner: string; epoch: number; fence_token: string };
export type InvocationStatus = {
  state: 'not_found' | 'running' | 'outcome_unknown' | 'completed' | 'incomplete' | 'failed';
  cancellationRequested: boolean;
  cancellationAcknowledged: boolean;
  receipt?: BoundedHermesDraftPrReceipt;
  lease?: InvocationLease;
  outcome?: ExecutionOutcome;
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
  private observing = new Map<string, Promise<BoundedHermesDraftPrReceipt>>();
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
    if (existsSync(resultPath)) {
      const receipt = boundedHermesDraftPrReceipt(job, JSON.parse(readFileSync(resultPath, 'utf8')));
      if (!receipt) throw invalid('Invocation receipt does not match its job');
      return { state: 'completed', cancellationRequested, cancellationAcknowledged: cancellationRequested, receipt, lease };
    }
    const outcomePath = path.join(directory, 'outcome.json');
    if (existsSync(outcomePath)) {
      const outcome = boundedExecutionOutcome(job, JSON.parse(readFileSync(outcomePath, 'utf8')));
      if (!outcome) throw invalid('Invocation outcome does not match its job');
      return { state: outcome.state, cancellationRequested, cancellationAcknowledged: false, outcome, lease };
    }
    return { state: this.active.has(job.job_id) ? 'running' : 'outcome_unknown', cancellationRequested, cancellationAcknowledged: false };
  }

  async inspect(job: BridgeJob, cancel: boolean,
    observe: (job: BridgeJob, context: ArtifactObservationContext) => Promise<unknown>): Promise<InvocationStatus> {
    const current = this.status(job, cancel);
    const context = current.state === 'outcome_unknown' ? this.observationContext(job) : null;
    if (!context) return current;
    let pending = this.observing.get(job.job_id);
    if (!pending) {
      pending = this.settle(job, () => observe(job, context));
      this.observing.set(job.job_id, pending);
    }
    try { await pending; }
    catch (error) {
      if (!['completed', 'incomplete', 'failed'].includes(this.status(job).state)) throw error;
    } finally {
      if (this.observing.get(job.job_id) === pending) this.observing.delete(job.job_id);
    }
    return this.status(job);
  }

  private observationContext(job: BridgeJob): ArtifactObservationContext | null {
    const file = path.join(this.location(job), 'observation.json');
    if (!existsSync(file)) return null;
    const context = boundedArtifactObservationContext(job, JSON.parse(readFileSync(file, 'utf8')));
    if (!context) throw invalid('Artifact observation checkpoint does not match its job');
    return context;
  }

  async execute(job: BridgeJob, claim: InvocationLease,
    run: (checkpoint: (context: ArtifactObservationContext) => void) => Promise<unknown>): Promise<BoundedHermesDraftPrReceipt> {
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
    const pending = this.settle(job, () => run((raw) => {
      const context = boundedArtifactObservationContext(job, raw);
      const file = path.join(directory, 'observation.json');
      if (!context || existsSync(file)) throw invalid('Invalid or duplicate artifact observation checkpoint');
      this.persist(file, context);
    }));
    this.active.set(job.job_id, pending);
    try { return await pending; }
    finally { this.active.delete(job.job_id); }
  }

  private settle(job: BridgeJob, run: () => Promise<unknown>): Promise<BoundedHermesDraftPrReceipt> {
    const directory = this.location(job);
    return Promise.resolve().then(run).then((value) => {
      const receipt = value && typeof value === 'object' && !Array.isArray(value) ? boundedHermesDraftPrReceipt(job, value) : null;
      if (!receipt) throw invalid('Executor did not return a bounded invocation receipt');
      this.persist(path.join(directory, 'result.json'), receipt);
      return receipt;
    }).catch((error) => {
      // Record code-owned exit evidence even when agent output is prose or missing.
      // Never retain raw output/errors, and never turn an uncertain effect into a safe retry.
      if (!existsSync(path.join(directory, 'result.json'))) {
        const context = this.observationContext(job);
        // A saved process exit permits only read-only inspection after restart.
        if (!(context && error instanceof ArtifactObservationUnavailable)) {
          const outcome = executionOutcome(job, error);
          if (context) outcome.finished_at = context.finished_at;
          this.persist(path.join(directory, 'outcome.json'), outcome);
        }
      }
      throw error;
    });
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
