import { createHash } from 'node:crypto';
import type { BridgeClient, BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import {
  factoryExecutionAnalyticsIdentity,
  type FactoryExecutionAnalyticsIdentity,
  type FactoryExecutionLease,
} from './create-draft-pr';

type ReceiptSetupBridge = Pick<BridgeClient, 'getJob' | 'settleFactoryExecution'>;

export type ReceiptRunnerPreparation =
  | { ok: true; job: BridgeJob; analytics: FactoryExecutionAnalyticsIdentity }
  | { ok: false; reasonCode: string; settled: boolean };

export async function prepareReceiptRunnerInvocation(
  bridge: ReceiptSetupBridge,
  jobId: string,
  lease: FactoryExecutionLease,
  invocationId: string,
  makeAnalytics: (id: string) => FactoryExecutionAnalyticsIdentity = factoryExecutionAnalyticsIdentity,
): Promise<ReceiptRunnerPreparation> {
  const fallbackAnalytics = conservativeAnalyticsIdentity(invocationId);
  let analytics: FactoryExecutionAnalyticsIdentity;
  try {
    analytics = makeAnalytics(invocationId);
  } catch {
    return settleSetupFailure(
      bridge,
      jobId,
      lease,
      fallbackAnalytics,
      'factory_receipt_analytics_setup_failed',
      'executor',
    );
  }

  let job: BridgeJob;
  try {
    job = await bridge.getJob(jobId);
    if (job.job_id !== jobId || typeof job.idempotency_key !== 'string' || !job.idempotency_key) {
      throw new Error('Factory returned an invalid receipt job');
    }
  } catch {
    return settleSetupFailure(
      bridge,
      jobId,
      lease,
      analytics,
      'factory_receipt_job_load_failed',
      'factory',
    );
  }
  return { ok: true, job, analytics };
}

async function settleSetupFailure(
  bridge: ReceiptSetupBridge,
  jobId: string,
  lease: FactoryExecutionLease,
  analytics: FactoryExecutionAnalyticsIdentity,
  reasonCode: string,
  failureDomain: 'factory' | 'executor',
): Promise<ReceiptRunnerPreparation> {
  const failure: JsonObject = {
    ...analytics,
    failure_domain: failureDomain,
    failure_stage: 'backend_invocation_fence',
    reason_code: reasonCode,
    retryable: null,
    failure_disposition: 'recovery_required',
    operator_intervention_required: true,
    effect_outcome: 'none',
    elapsed_ms: 0,
  };
  try {
    await bridge.settleFactoryExecution(jobId, lease, false, failure, 'execution_dispatch_failed');
    return { ok: false, reasonCode, settled: true };
  } catch {
    return { ok: false, reasonCode, settled: false };
  }
}

function conservativeAnalyticsIdentity(invocationId: string): FactoryExecutionAnalyticsIdentity {
  const boundedInvocationId = invocationId && invocationId.length <= 200
    ? invocationId
    : `receipt.${createHash('sha256').update(invocationId).digest('hex')}`;
  const target = (process.env.DRIFTWARD_HERMES_GRPC_TARGET || '').trim();
  const configuredAuthMode = (process.env.DRIFTWARD_HERMES_AUTH_MODE || '').trim();
  return {
    analytics_schema_version: 1,
    invocation_id: boundedInvocationId,
    execution_backend: 'hermes',
    execution_transport: !target ? 'local_command' : target.startsWith('unix:') ? 'grpc_uds' : 'grpc_tcp',
    backend_version: null,
    configured_auth_mode: configuredAuthMode === 'chatgpt_codex_oauth' || configuredAuthMode === 'api_key'
      ? configuredAuthMode
      : 'unknown',
  };
}
