import { processSignal, transportStatus } from './bounded-evidence';

export type ExecutionFailureDomain =
  | 'unknown'
  | 'factory'
  | 'executor'
  | 'executor_backend'
  | 'gateway_transport'
  | 'gateway'
  | 'hermes_process'
  | 'hermes_protocol'
  | 'auth'
  | 'provider'
  | 'tool'
  | 'github'
  | 'verification';

export type ExecutionFailureStage =
  | 'unknown'
  | 'preflight'
  | 'artifact_fence'
  | 'effect_authorization'
  | 'receipt_dispatch'
  | 'backend_invocation_fence'
  | 'backend_execution'
  | 'gateway_connect'
  | 'gateway_handler'
  | 'hermes_spawn'
  | 'hermes_exit'
  | 'hermes_timeout'
  | 'result_parse'
  | 'auth_resolve'
  | 'auth_refresh'
  | 'model_request'
  | 'model_stream'
  | 'tool_execution'
  | 'effect_commit'
  | 'verification_checks'
  | 'verification_recovery';

export type ExecutionFailureEvidence = {
  observedArtifact?: { branch: string; pr_url: string | null; commit_sha: string | null } | null;
  failureDomain?: ExecutionFailureDomain;
  failureStage?: ExecutionFailureStage;
  retryable?: boolean | null;
  transportStatus?: string | null;
  processExitCode?: number | null;
  processSignal?: string | null;
  timedOut?: boolean | null;
  bufferExceeded?: boolean | null;
};

export class NonRetryableBridgeExecutionError extends Error {
  readonly observedArtifact: ExecutionFailureEvidence['observedArtifact'];
  readonly reasonCode: string;
  readonly failureDomain: ExecutionFailureDomain;
  readonly failureStage: ExecutionFailureStage;
  readonly retryable: boolean | null;
  readonly transportStatus: string | null;
  readonly processExitCode: number | null;
  readonly processSignal: string | null;
  readonly timedOut: boolean | null;
  readonly bufferExceeded: boolean | null;

  constructor(reasonCode: string, message: string, evidence: ExecutionFailureEvidence = {}) {
    super(message);
    this.name = 'NonRetryableBridgeExecutionError';
    this.observedArtifact = evidence.observedArtifact ?? null;
    this.reasonCode = reasonCode;
    this.failureDomain = evidence.failureDomain || 'unknown';
    this.failureStage = evidence.failureStage || 'unknown';
    this.retryable = evidence.retryable === null || typeof evidence.retryable === 'boolean'
      ? evidence.retryable
      : false;
    this.transportStatus = transportStatus(evidence.transportStatus);
    this.processExitCode = Number.isInteger(evidence.processExitCode)
      && Number(evidence.processExitCode) >= -(2 ** 31)
      && Number(evidence.processExitCode) < 2 ** 31
      ? evidence.processExitCode!
      : null;
    this.processSignal = processSignal(evidence.processSignal);
    this.timedOut = evidence.timedOut === null || typeof evidence.timedOut === 'boolean'
      ? evidence.timedOut
      : null;
    this.bufferExceeded = evidence.bufferExceeded === null || typeof evidence.bufferExceeded === 'boolean'
      ? evidence.bufferExceeded
      : null;
  }
}

/** Retry only GitHub observation; never grant permission to repeat execution. */
export class ArtifactObservationUnavailable extends NonRetryableBridgeExecutionError {
  constructor() {
    super('hermes_artifact_observation_failed', 'GitHub observation is unavailable; inspect the saved invocation again',
      { failureDomain: 'github', failureStage: 'result_parse', retryable: false, processExitCode: 0 });
  }
}
