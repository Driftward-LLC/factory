import type { BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import { gatewayReasonCode, processSignal } from './bounded-evidence';
import { NonRetryableBridgeExecutionError } from './errors';

/** A stopped invocation is not proof of containment or absence of external effects. */
export type ExecutionOutcome = JsonObject & {
  schema: 'factory-executor-outcome-v1';
  job_id: string;
  state: 'incomplete' | 'failed';
  reason_code: string;
  finished_at: string;
  process_exit_code: number | null;
  process_signal: string | null;
  timed_out: boolean | null;
  buffer_exceeded: boolean | null;
  effect_outcome: 'unknown';
  containment: 'unconfirmed';
  artifact: { branch: string; pr_url: string | null; commit_sha: string | null } | null;
};

export function executionOutcome(job: BridgeJob, error: unknown): ExecutionOutcome {
  const failure = error instanceof NonRetryableBridgeExecutionError ? error : null;
  const reason = gatewayReasonCode(failure?.reasonCode) || 'hermes_grpc_gateway_failed';
  return {
    schema: 'factory-executor-outcome-v1', job_id: job.job_id,
    state: reason === 'hermes_execution_incomplete' ? 'incomplete' : 'failed',
    reason_code: reason, finished_at: new Date().toISOString(),
    process_exit_code: failure?.processExitCode ?? null, process_signal: failure?.processSignal ?? null,
    timed_out: failure?.timedOut ?? null, buffer_exceeded: failure?.bufferExceeded ?? null,
    effect_outcome: 'unknown', containment: 'unconfirmed',
    artifact: boundedArtifact(job, failure?.observedArtifact),
  };
}

export function boundedExecutionOutcome(job: Pick<BridgeJob, 'job_id' | 'branch' | 'payload'>, raw: unknown): ExecutionOutcome | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const value = raw as ExecutionOutcome;
  const keys = ['schema', 'job_id', 'state', 'reason_code', 'finished_at', 'process_exit_code',
    'process_signal', 'timed_out', 'buffer_exceeded', 'effect_outcome', 'containment', 'artifact'];
  if (Object.keys(value).sort().join() !== keys.sort().join()
    || value.schema !== 'factory-executor-outcome-v1' || value.job_id !== job.job_id
    || !['incomplete', 'failed'].includes(value.state) || !gatewayReasonCode(value.reason_code)
    || (value.state === 'incomplete') !== (value.reason_code === 'hermes_execution_incomplete')
    || typeof value.finished_at !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value.finished_at)
    || !Number.isFinite(Date.parse(value.finished_at))
    || (value.process_exit_code !== null && (!Number.isInteger(value.process_exit_code)
      || value.process_exit_code < -(2 ** 31) || value.process_exit_code >= 2 ** 31))
    || (value.process_signal !== null && processSignal(value.process_signal) !== value.process_signal)
    || [value.timed_out, value.buffer_exceeded].some((item) => item !== null && typeof item !== 'boolean')
    || value.effect_outcome !== 'unknown' || value.containment !== 'unconfirmed'
    || (value.artifact !== null && boundedArtifact(job, value.artifact) === null)) return null;
  return value;
}

function boundedArtifact(job: Pick<BridgeJob, 'branch' | 'payload'>, raw: unknown): ExecutionOutcome['artifact'] {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const item = raw as NonNullable<ExecutionOutcome['artifact']>;
  const url = typeof item.pr_url === 'string' ? item.pr_url.match(/^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)$/) : null;
  if (Object.keys(item).sort().join() !== 'branch,commit_sha,pr_url' || item.branch !== job.branch
    || (item.pr_url !== null && (!url || url[1] !== job.payload.repo || !Number.isSafeInteger(Number(url[2]))))
    || (item.commit_sha !== null && (typeof item.commit_sha !== 'string' || !/^[0-9a-f]{40}$/.test(item.commit_sha)))) return null;
  return { branch: item.branch, pr_url: item.pr_url, commit_sha: item.commit_sha };
}
