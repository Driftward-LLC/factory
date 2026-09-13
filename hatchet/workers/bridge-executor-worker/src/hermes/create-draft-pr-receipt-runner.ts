import { BridgeClient, HATCHET_EVENTS, type BridgeJob, type JsonObject } from '@driftward/hatchet-worker-shared';
import {
  NonRetryableBridgeExecutionError,
  hermesReceiptEventScope,
  jobMatchesHermesDraftPrReceipt,
  runHermesCreateDraftPr,
  sanitizeText,
  type FactoryExecutionLease
} from './create-draft-pr';
import { prepareReceiptRunnerInvocation } from './receipt-runner-setup';

async function main(): Promise<void> {
  const jobId = process.argv[2];
  if (!jobId) throw new Error('job id is required');
  const lease = parseLease(process.env.HATCHET_FACTORY_EXECUTION_LEASE);
  const bridge = new BridgeClient();
  const invocationId = process.env.HATCHET_HERMES_RECEIPT_DISPATCH_ID || `${jobId}:${lease.lease_id}:${lease.epoch}`;
  const prepared = await prepareReceiptRunnerInvocation(bridge, jobId, lease, invocationId);
  if (!prepared.ok) {
    const idempotencyKey = process.env.HATCHET_BRIDGE_IDEMPOTENCY_KEY;
    if (idempotencyKey) {
      await emitFailureEvent(
        { job_id: jobId, idempotency_key: idempotencyKey },
        prepared.reasonCode,
        new NonRetryableBridgeExecutionError(
          prepared.reasonCode,
          'Factory receipt runner could not load its durable pre-invocation context',
          { failureDomain: 'factory', failureStage: 'backend_invocation_fence', retryable: null },
        ),
        invocationId,
      );
    }
    console.error(`${prepared.reasonCode}; durable_settlement=${prepared.settled ? 'recorded' : 'unavailable'}`);
    process.exitCode = 1;
    return;
  }
  const { job, analytics } = prepared;

  let invocationRecorded = false;
  let invocationRegistrationError: unknown = null;
  try {
    invocationRecorded = await bridge.recordFactoryExecutionAnalytics(
      job.job_id,
      lease,
      'execution_backend_invoked',
      analytics,
    );
  } catch (error) {
    invocationRegistrationError = error;
  }
  if (!invocationRecorded) {
    const reasonCode = invocationRegistrationError
      ? 'factory_execution_invocation_registration_failed'
      : 'factory_execution_invocation_already_registered';
    const error = invocationRegistrationError || new Error(reasonCode);
    await settleUnknown(bridge, job.job_id, lease, {
      ...analytics,
      failure_domain: 'factory',
      failure_stage: 'backend_invocation_fence',
      reason_code: reasonCode,
      retryable: null,
      failure_disposition: 'recovery_required',
      operator_intervention_required: true,
      effect_outcome: 'none',
      elapsed_ms: 0,
    }, 'execution_dispatch_failed');
    await emitFailureEvent(job, reasonCode, error, invocationId);
    process.exitCode = 1;
    return;
  }
  const started = process.hrtime.bigint();
  let result: Awaited<ReturnType<typeof runHermesCreateDraftPr>>;
  try {
    result = await runHermesCreateDraftPr(job);
  } catch (error) {
    const classified = error instanceof NonRetryableBridgeExecutionError ? error : null;
    const reasonCode = classified?.reasonCode || 'factory_execution_recovery_required';
    const failure = {
      ...analytics,
      failure_domain: classified?.failureDomain || 'unknown',
      failure_stage: classified?.failureStage || 'unknown',
      reason_code: reasonCode,
      retryable: classified?.retryable ?? null,
      failure_disposition: 'recovery_required',
      operator_intervention_required: true,
      effect_outcome: 'unknown',
      elapsed_ms: elapsedMs(started),
      transport_status: classified?.transportStatus || null,
      process_exit_code: classified?.processExitCode ?? null,
      process_signal: classified?.processSignal || null,
      timed_out: classified?.timedOut ?? null,
      buffer_exceeded: classified?.bufferExceeded ?? null,
    };
    await settleUnknown(bridge, job.job_id, lease, failure);
    await emitFailureEvent(job, reasonCode, error, invocationId);
    process.exitCode = 1;
    return;
  }

  const elapsed = elapsedMs(started);
  await recordCompletionBestEffort(bridge, job.job_id, lease, {
    ...analytics,
    backend_outcome: result.status,
    elapsed_ms: elapsed,
  });

  let committed;
  try {
    committed = await bridge.commitFactoryExecution(job.job_id, lease, {
      commit_sha: result.commitSha,
      pr_url: result.prUrl,
      reason_code: `hermes_${result.status}`,
      branch: result.branch,
      ...analytics,
      backend_outcome: result.status,
      backend_elapsed_ms: elapsed,
    });
  } catch (error) {
    let persisted: BridgeJob | null = null;
    try {
      const current = await bridge.getJob(job.job_id);
      if (jobMatchesHermesDraftPrReceipt(current, result)) persisted = current;
    } catch (readError) {
      console.error('failed to read Factory receipt after commit error', sanitizeText(readError instanceof Error ? readError.message : String(readError)));
    }
    if (!persisted) {
      // Hermes returned a concrete receipt, but the bridge does not hold that
      // exact receipt. Preserve the single authorized generation for recovery
      // without recording another backend outcome for this invocation.
      persisted = await settleCommitUnknown(bridge, job.job_id, lease, analytics, result, elapsed);
      if (!persisted) {
        await emitFailureEvent(job, 'factory_effect_commit_failed', error, invocationId, result);
        process.exitCode = 1;
        return;
      }
    }
    committed = { job: persisted };
  }

  try {
    await (await import('../hatchet-client')).hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_EFFECT_COMMITTED, {
      jobId: committed.job.job_id,
      idempotencyKey: committed.job.idempotency_key,
      prUrl: committed.job.pr_url,
      prNumber: result.prNumber,
      branch: committed.job.branch,
      commitSha: committed.job.commit_sha,
      hermesSessionId: null,
      status: result.status,
      summary: result.summary,
      validation: result.validation,
      dispatchId: invocationId,
      dryRun: false
    }, {
      scope: hermesReceiptEventScope(committed.job),
      additionalMetadata: {
        source: 'bridge-executor-hermes-receipt-runner',
        job_id: committed.job.job_id
      }
    });
  } catch (error) {
    // The bridge receipt is already durable. Keep persisting verification
    // evidence even when the Hatchet wake must be recovered by reconciliation.
    console.error('failed to publish durable Factory effect wake', sanitizeText(error instanceof Error ? error.message : String(error)));
    process.exitCode = 1;
  }

  let pending: { job: BridgeJob };
  try {
    pending = committed.job.state === 'effect_committed'
      && result.validation && typeof result.validation === 'object' && !Array.isArray(result.validation)
      ? await bridge.transitionJob(committed.job.job_id, {
        state: 'verification_pending',
        details: {
          reason_code: 'draft_pr_verification_pending',
          verification_context: result.validation as JsonObject
        }
      })
      : committed;
  } catch (error) {
    let advanced: BridgeJob | null = null;
    try {
      const current = await bridge.getJob(committed.job.job_id);
      if (current.state !== 'effect_committed' && jobMatchesHermesDraftPrReceipt(current, result)) advanced = current;
    } catch (readError) {
      console.error('failed to read Factory state after verification transition error', sanitizeText(readError instanceof Error ? readError.message : String(readError)));
    }
    if (!advanced) {
      console.error('failed after durable Factory effect commit', sanitizeText(error instanceof Error ? error.message : String(error)));
      process.exitCode = 1;
      return;
    }
    pending = { job: advanced };
  }

  try {
    if (pending.job.state === 'verification_pending') {
      await (await import('../hatchet-client')).hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_VERIFICATION_PENDING, {
        jobId: pending.job.job_id,
        idempotencyKey: pending.job.idempotency_key,
        prUrl: pending.job.pr_url,
        dryRun: false
      }, { additionalMetadata: { source: 'bridge-executor-hermes-receipt-runner', job_id: pending.job.job_id } });
    }
    await emitPrCreated(pending.job, result.prNumber);
  } catch (error) {
    // Hatchet event delivery happens after the bridge receipt is durable.  A
    // delivery outage must never be counted as a Hermes/backend failure.
    console.error('failed to publish post-commit Hatchet events', sanitizeText(error instanceof Error ? error.message : String(error)));
    process.exitCode = 1;
  }
}

async function emitPrCreated(job: { job_id: string; pr_url: string | null; payload: JsonObject }, prNumber: number): Promise<void> {
  await (await import('../hatchet-client')).hatchet.events.push(HATCHET_EVENTS.GITHUB_PR_CREATED, {
    repo: String(job.payload.repo || ''),
    prNumber,
    prUrl: job.pr_url,
    bridgeJobId: job.job_id,
    dryRun: false
  }, {
    additionalMetadata: {
      source: 'bridge-executor-hermes-receipt-runner',
      job_id: job.job_id
    }
  });
}

async function recordCompletionBestEffort(
  bridge: BridgeClient,
  jobId: string,
  lease: FactoryExecutionLease,
  details: JsonObject,
): Promise<void> {
  try {
    await bridge.recordFactoryExecutionAnalytics(jobId, lease, 'execution_backend_completed', details);
  } catch (error) {
    console.error('failed to record Factory execution analytics', sanitizeText(error instanceof Error ? error.message : String(error)));
  }
}

async function settleUnknown(
  bridge: BridgeClient,
  jobId: string,
  lease: FactoryExecutionLease,
  failure: JsonObject,
  failureEventType: 'execution_backend_failed' | 'execution_dispatch_failed' = 'execution_backend_failed',
): Promise<void> {
  try {
    await bridge.settleFactoryExecution(jobId, lease, false, failure, failureEventType);
  } catch (error) {
    console.error('failed to settle factory execution after Hermes receipt failure', sanitizeText(error instanceof Error ? error.message : String(error)));
  }
}

async function settleCommitUnknown(
  bridge: BridgeClient,
  jobId: string,
  lease: FactoryExecutionLease,
  analytics: JsonObject,
  receipt: Awaited<ReturnType<typeof runHermesCreateDraftPr>>,
  elapsed: number,
): Promise<BridgeJob | null> {
  try {
    await bridge.settleFactoryExecution(jobId, lease, false, {
      ...analytics,
      failure_domain: 'factory',
      failure_stage: 'effect_commit',
      reason_code: 'factory_effect_commit_failed',
      retryable: null,
      failure_disposition: 'recovery_required',
      operator_intervention_required: true,
      effect_outcome: 'observed',
      elapsed_ms: elapsed,
      observed_pr_url: receipt.prUrl,
      observed_commit_sha: receipt.commitSha,
      observed_branch: receipt.branch,
      observed_hermes_session_id: null,
      backend_outcome: receipt.status,
      verification_context: receipt.validation && typeof receipt.validation === 'object' && !Array.isArray(receipt.validation)
        ? receipt.validation
        : null,
    }, 'execution_effect_commit_failed');
    const current = await bridge.getJob(jobId);
    return jobMatchesHermesDraftPrReceipt(current, receipt) ? current : null;
  } catch (error) {
    console.error('failed to mark uncommitted Factory receipt recovery-required', sanitizeText(error instanceof Error ? error.message : String(error)));
    return null;
  }
}

async function emitFailureEvent(job: { job_id: string; idempotency_key: string }, reasonCode: string, error: unknown, dispatchId: string, receipt?: Awaited<ReturnType<typeof runHermesCreateDraftPr>>): Promise<void> {
  try {
    await (await import('../hatchet-client')).hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_FAILED, {
      jobId: job.job_id,
      idempotencyKey: job.idempotency_key,
      reasonCode,
      error: error instanceof NonRetryableBridgeExecutionError
        ? sanitizeText(error.message)
        : 'execution failed without structured bounded evidence',
      dispatchId,
      ...(receipt ? {
        prUrl: sanitizeText(receipt.prUrl),
        prNumber: receipt.prNumber,
        branch: sanitizeText(receipt.branch),
        commitSha: receipt.commitSha ? sanitizeText(receipt.commitSha) : null,
        hermesSessionId: null,
        backendOutcome: receipt.status,
      } : {}),
      dryRun: false
    }, {
      scope: hermesReceiptEventScope(job),
      additionalMetadata: {
        source: 'bridge-executor-hermes-receipt-runner',
        job_id: job.job_id
      }
    });
  } catch (pushError) {
    console.error('failed to publish Hermes receipt failure', sanitizeText(pushError instanceof Error ? pushError.message : String(pushError)));
  }
}

function elapsedMs(started: bigint): number {
  const value = Number((process.hrtime.bigint() - started) / 1_000_000n);
  return Math.max(0, Math.min(604_800_000, value));
}

function parseLease(raw: string | undefined): FactoryExecutionLease {
  if (!raw) throw new Error('HATCHET_FACTORY_EXECUTION_LEASE is required');
  let value: Partial<FactoryExecutionLease>;
  try {
    value = JSON.parse(raw) as Partial<FactoryExecutionLease>;
  } catch {
    throw new Error('HATCHET_FACTORY_EXECUTION_LEASE is malformed');
  }
  if (typeof value.lease_id !== 'string' || typeof value.owner !== 'string' || typeof value.epoch !== 'number' || typeof value.fence_token !== 'string') {
    throw new Error('HATCHET_FACTORY_EXECUTION_LEASE is malformed');
  }
  return {
    lease_id: value.lease_id,
    owner: value.owner,
    epoch: value.epoch,
    fence_token: value.fence_token
  };
}

if (require.main === module) {
  main().catch((error) => {
    console.error(sanitizeText(error instanceof Error ? error.message : String(error)));
    process.exit(1);
  });
}
