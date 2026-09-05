import { webcrypto } from 'node:crypto';
import {
  BridgeClient,
  HATCHET_EVENTS,
  type BridgeJob,
  type JsonObject,
  type JsonValue
} from '@driftward/hatchet-worker-shared';
import {
  NonRetryableBridgeExecutionError,
  dispatchHermesCreateDraftPrReceipt,
  realCreateDraftPrEnabled,
  verificationCompletionPlan,
  hermesReceiptEventExpression,
  hermesReceiptEventScope,
  hermesReceiptWaitDuration,
  inspectDraftPrState,
  preflightHermesCreateDraftPr,
  resumeHermesDraftPrResult,
  waitForHermesDraftPrVerification,
  type HermesDraftPrResult
} from '../hermes/create-draft-pr';
import { Or, SleepCondition, UserEventCondition } from '@hatchet-dev/typescript-sdk/v1/conditions';
import { hatchet } from '../hatchet-client';
import { emitBestEffort } from './event-delivery';
import { validateFactoryArtifactFence } from './factory-artifact-fence';
import { recoverClaimedVerification } from './verification-recovery';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto as Crypto;
}

export type BridgeJobAcceptedInput = {
  jobId?: string;
  idempotencyKey?: string;
  verificationAttemptId?: string;
};

export type BridgeExecutorOutput = {
  'execute-create-draft-pr': {
    executed: boolean;
    blockedReason?: string;
    job: BridgeJob;
    prUrl?: string;
    dryRun?: boolean;
    reused?: boolean;
  };
  'await-hermes-receipt': {
    executed: boolean;
    blockedReason?: string;
    job: BridgeJob;
    prUrl?: string;
    dryRun?: boolean;
    reused?: boolean;
  };
};

export const bridgeExecutorCreateDraftPr = hatchet.workflow<BridgeJobAcceptedInput, BridgeExecutorOutput>({
  name: 'bridge-executor-create-draft-pr',
  onEvents: [HATCHET_EVENTS.HERMES_JOB_ACCEPTED]
});

const executeCreateDraftPrTask = bridgeExecutorCreateDraftPr.task({
  name: 'execute-create-draft-pr',
  executionTimeout: '25m',
  fn: async (input, ctx) => {
    const ref = input.jobId || input.idempotencyKey;
    if (!ref) {
      throw new Error('jobId or idempotencyKey is required');
    }

    const bridge = new BridgeClient();
    const job = await bridge.getJob(ref);
    const retryAttempt = await bridge.getFactoryAttemptForJob(job.job_id);
    if (retryAttempt && (retryAttempt.status === 'canceled' || retryAttempt.status === 'canceled_after_effect')) {
      return { executed: false, blockedReason: 'factory_attempt_canceled', job, dryRun: Boolean(job.payload.dry_run), reused: true };
    }

    if (job.requested_action === 'inspect_draft_pr_state') {
      if (job.state === 'completed') {
        return { executed: false, blockedReason: 'inspection_already_completed', job, dryRun: false, reused: true };
      }
      const inspection = job.payload.inspection;
      const requestKey = job.payload.inspection_request_key;
      const inspectionCapability = job.payload.inspection_capability;
      if (!inspection || typeof inspection !== 'object' || Array.isArray(inspection) || typeof requestKey !== 'string' || typeof inspectionCapability !== 'string' || typeof inspection.repository !== 'string' || (typeof inspection.url !== 'string' && typeof inspection.number !== 'number')) {
        return { executed: false, blockedReason: 'invalid_inspection_request', job };
      }
      const running = job.state === 'accepted' ? await ensureRunning(bridge, job, 'hermes') : job;
      const reference = typeof inspection.url === 'string' ? inspection.url : inspection.number;
      if (typeof reference !== 'string' && typeof reference !== 'number') return { executed: false, blockedReason: 'invalid_inspection_request', job };
      const result = await inspectDraftPrState(inspection.repository, reference);
      await bridge.recordPrInspection(running.job_id, requestKey, inspectionCapability, result as unknown as JsonObject);
      const completed = await bridge.getJob(running.job_id);
      return { executed: true, job: completed, dryRun: false, reused: false };
    }

    if (!isDraftPrEffectAction(job.requested_action)) {
      return {
        executed: false,
        blockedReason: `unsupported action ${job.requested_action}`,
        job
      };
    }

    if (job.state === 'completed' || job.state === 'ready_for_human_review') {
      return {
        executed: false,
        blockedReason: job.state === 'ready_for_human_review' ? 'already_verified_ready' : 'already_completed',
        job,
        prUrl: job.pr_url || undefined,
        dryRun: Boolean(job.payload.dry_run)
      };
    }

    if (job.state === 'failed') {
      return {
        executed: false,
        blockedReason: job.reason_code || 'already_failed',
        job,
        dryRun: Boolean(job.payload.dry_run)
      };
    }

    if (job.state === 'effect_committed' || job.state === 'verification_pending') {
      const attemptId = input.verificationAttemptId || `${job.idempotency_key}:verify:${job.commit_sha || 'unknown'}`;
      const recovered = await recoverClaimedVerification({
        bridge, job, attemptId, attemptReserved: Boolean(input.verificationAttemptId),
        ensurePending: () => ensureVerificationPending(bridge, job),
        resume: resumeHermesDraftPrResult,
        verify: (pending, result, claimedAttemptId) => finishVerification(bridge, pending, result, true, claimedAttemptId, true),
        emitFailure: async (failed) => {
          await emitBestEffort(() => emitVerificationFailed(failed));
          await emitBestEffort(() => emitFailed(failed, false));
        },
      });
      return { executed: Boolean(recovered.executed), blockedReason: recovered.blockedReason, job: recovered.job, prUrl: recovered.prUrl || recovered.job.pr_url || undefined, dryRun: false, reused: recovered.reused !== false };
    }

    if (job.payload.dry_run === true) {
      const completed = await completeDryRunJob(bridge, job);
      await emitCommitted(completed, true);
      await emitCompleted(completed, true);
      await emitPrResult(completed, String(job.payload.repo || ''), 0, true);

      return {
        executed: true,
        job: completed,
        prUrl: completed.pr_url || undefined,
        dryRun: true
      };
    }

    // Real effects are never a legacy fallback.  A supported legacy job is
    // imported to generation one during predeploy; anything without that
    // durable attempt lineage is an operator-required terminal failure.
    if (!retryAttempt) {
      const failed = await failBeforeExternalEffect(bridge, job, {
        reasonCode: 'factory_attempt_required',
        error: 'real create_draft_pr requires a valid factory attempt lineage'
      });
      await emitFailed(failed, false);
      return { executed: false, blockedReason: 'factory_attempt_required', job: failed, dryRun: false };
    }

    if (!realCreateDraftPrEnabled()) {
      const failed = await failBeforeExternalEffect(bridge, job, {
        reasonCode: 'real_create_draft_pr_disabled',
        error: 'Set DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED=1 to enable real PR creation'
      });
      await emitFailed(failed, false);
      return {
        executed: false,
        blockedReason: 'real_create_draft_pr_disabled',
        job: failed,
        dryRun: false
      };
    }

    let running: BridgeJob;
    try {
      await preflightHermesCreateDraftPr(job);
      running = await ensureRunning(bridge, job, 'hermes');
    } catch (error) {
      if (error instanceof NonRetryableBridgeExecutionError) {
        const failed = await failBeforeExternalEffect(bridge, job, {
          reasonCode: error.reasonCode,
          error: error.message
        });
        await emitFailed(failed, false);
        return {
          executed: false,
          blockedReason: error.reasonCode,
          job: failed,
          dryRun: false
        };
      }
      throw error;
    }

    // Supersession evidence is durable in the attempt/job payload but may have
    // changed remotely since reservation.  Re-observe immediately before the
    // execution claim; any reopen, merge, branch, SHA, or base change blocks.
    const fenceValidation = await validateFactoryArtifactFence(running);
    if (!fenceValidation.ok) {
      const failed = await failBeforeExternalEffect(bridge, running, {
        reasonCode: fenceValidation.reasonCode,
        error: fenceValidation.error
      });
      await emitFailed(failed, false);
      return { executed: false, blockedReason: fenceValidation.reasonCode, job: failed, dryRun: false, reused: true };
    }

    // Validation and the durable running transition are pre-effect work. Claim
    // only after they succeed, so every active lease represents a process that
    // could be at the Hermes boundary.
    const execution = await bridge.claimFactoryExecution(running.job_id, `bridge-executor:${ctx.taskRunExternalId()}`);
    if (!execution.claimed) {
      return { executed: false, blockedReason: execution.reason || 'factory_execution_in_progress', job: running, dryRun: false, reused: true };
    }
    const executionLease = execution.lease;

    try {
      // This is the irreversible, single-use provider authority. Cancellation
      // or expiry before it prevents the call; after it, only this exact fence
      // may invoke Hermes and later record the provider receipt.
      if (executionLease && (!await bridge.renewFactoryExecution(running.job_id, executionLease) || !await bridge.authorizeFactoryEffect(running.job_id, executionLease))) {
        await bridge.settleFactoryExecution(running.job_id, executionLease, false);
        return { executed: false, blockedReason: 'factory_attempt_canceled', job: running, dryRun: false, reused: true };
      }
      if (!executionLease) {
        return { executed: false, blockedReason: 'factory_execution_recovery_required', job: running, dryRun: false, reused: true };
      }
      const dispatch = dispatchHermesCreateDraftPrReceipt(running, executionLease);
      ctx.logger.info('Dispatched Hermes receipt runner', { jobId: running.job_id, dispatchId: dispatch.dispatchId, pid: dispatch.pid });
      return { executed: false, blockedReason: 'waiting_for_hermes_receipt', job: running, dryRun: false, reused: false };
    } catch (error) {
      // Once Hermes has been invoked, an error cannot prove that no provider
      // effect happened. If async dispatch itself failed after authorization,
      // move the attempt to explicit recovery-required state.
      if (executionLease) await bridge.settleFactoryExecution(running.job_id, executionLease, false);
      if (error instanceof NonRetryableBridgeExecutionError) return { executed: false, blockedReason: error.reasonCode, job: running, dryRun: false, reused: true };
      throw error;
    }
  }
});

bridgeExecutorCreateDraftPr.durableTask({
  name: 'await-hermes-receipt',
  parents: [executeCreateDraftPrTask],
  executionTimeout: '6h',
  fn: async (input, ctx) => {
    const ref = input.jobId || input.idempotencyKey;
    if (!ref) {
      throw new Error('jobId or idempotencyKey is required');
    }
    const bridge = new BridgeClient();
    let job = await bridge.getJob(ref);
    if (!isDraftPrEffectAction(job.requested_action)) {
      return { executed: false, blockedReason: `unsupported action ${job.requested_action}`, job };
    }
    if (job.payload.dry_run === true || job.state === 'completed' || job.state === 'ready_for_human_review') {
      return { executed: false, blockedReason: job.state === 'ready_for_human_review' ? 'already_verified_ready' : 'already_completed', job, prUrl: job.pr_url || undefined, dryRun: Boolean(job.payload.dry_run), reused: true };
    }
    if (job.state === 'failed') {
      return { executed: false, blockedReason: job.reason_code || 'already_failed', job, dryRun: false, reused: true };
    }
    let receiptWaitResult: unknown;
    if (job.state !== 'effect_committed' && job.state !== 'verification_pending') {
      const started = await ctx.now();
      const since = new Date(started.getTime() - 5 * 60 * 1000).toISOString();
      const expression = hermesReceiptEventExpression(job);
      const scope = hermesReceiptEventScope(job);
      receiptWaitResult = await ctx.waitFor(
        Or(
          new UserEventCondition(HATCHET_EVENTS.HERMES_JOB_EFFECT_COMMITTED, expression, 'hermes-effect-committed', undefined, scope, since),
          new UserEventCondition(HATCHET_EVENTS.HERMES_JOB_FAILED, expression, 'hermes-failed', undefined, scope, since),
          new SleepCondition(hermesReceiptWaitDuration(), 'hermes-receipt-timeout')
        ),
        'waiting for Hermes receipt'
      );
      job = await bridge.getJob(job.job_id);
    }
    if (job.state === 'effect_committed' || job.state === 'verification_pending') {
      const result = resumeHermesDraftPrResultOrReceipt(job, receiptWaitResult);
      const pending = await ensureVerificationPending(bridge, job, result.validation);
      return finishVerification(bridge, pending, result, true);
    }
    return { executed: false, blockedReason: 'factory_execution_recovery_required', job, dryRun: false, reused: true };
  }
});

function resumeHermesDraftPrResultOrReceipt(job: BridgeJob, waitResult: unknown): HermesDraftPrResult {
  try {
    return resumeHermesDraftPrResult(job);
  } catch (error) {
    const receipt = findHermesReceiptPayload(waitResult, job);
    if (!receipt) throw error;
    const prUrl = typeof receipt.prUrl === 'string' ? receipt.prUrl : job.pr_url;
    const branch = typeof receipt.branch === 'string' ? receipt.branch : job.branch;
    const prNumber = typeof receipt.prNumber === 'number' ? receipt.prNumber : Number(prUrl?.match(/\/pull\/(\d+)$/)?.[1] || 0);
    if (!prUrl || !branch || !prNumber) throw error;
    return {
      status: receipt.status === 'created' ? 'created' : 'reused',
      prUrl,
      prNumber,
      commitSha: typeof receipt.commitSha === 'string' ? receipt.commitSha : job.commit_sha,
      branch,
      hermesSessionId: typeof receipt.hermesSessionId === 'string' ? receipt.hermesSessionId : job.hermes_session_id,
      summary: typeof receipt.summary === 'string' ? receipt.summary : 'resumed verification from Hermes receipt event',
      validation: isJsonValue(receipt.validation) ? receipt.validation : null
    };
  }
}

function findHermesReceiptPayload(value: unknown, job: BridgeJob): Record<string, unknown> | null {
  if (!value || typeof value !== 'object') return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findHermesReceiptPayload(item, job);
      if (found) return found;
    }
    return null;
  }
  const record = value as Record<string, unknown>;
  if (record.jobId === job.job_id && record.idempotencyKey === job.idempotency_key) return record;
  for (const item of Object.values(record)) {
    const found = findHermesReceiptPayload(item, job);
    if (found) return found;
  }
  return null;
}

function isJsonValue(value: unknown): value is JsonValue {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value) || Array.isArray(value) || (typeof value === 'object' && value !== null);
}

function isDraftPrEffectAction(action: string): action is 'create_draft_pr' | 'repair_draft_pr' {
  return action === 'create_draft_pr' || action === 'repair_draft_pr';
}

async function finishVerification(bridge: BridgeClient, pending: BridgeJob, result: HermesDraftPrResult, reused: boolean, reservedAttemptId?: string, alreadyClaimed: boolean = false): Promise<BridgeExecutorOutput['execute-create-draft-pr']> {
  const attemptId = reservedAttemptId || `${pending.idempotency_key}:verify:${pending.commit_sha || 'unknown'}`;
  try {
    const claimed = alreadyClaimed || (reservedAttemptId
      ? await bridge.claimVerificationLease(pending.job_id, attemptId)
      : await acquireAndClaimVerificationLease(bridge, pending.job_id, attemptId));
    if (!claimed) {
      return { executed: false, blockedReason: 'verification_attempt_in_progress', job: pending, prUrl: result.prUrl, dryRun: false, reused: true };
    }
    const verification = await waitForHermesDraftPrVerification(pending, result);
    const completion = verificationCompletionPlan(pending, verification);
    if (completion.recordObservation) {
      await bridge.recordVerificationObservation(pending.job_id, attemptId, completion.receipt);
    }
    const ready = await bridge.completeVerifiedReady(pending.job_id, attemptId, completion.receipt);
    await emitBestEffort(() => emitReadyForHumanReview(ready.job));
    return { executed: true, job: ready.job, prUrl: result.prUrl, dryRun: false, reused };
  } catch (error) {
    if (error instanceof NonRetryableBridgeExecutionError) {
      const failed = await bridge.completeVerificationFailure(pending.job_id, attemptId, error.reasonCode, error.message);
      await emitBestEffort(() => emitVerificationFailed(failed.job));
      await emitBestEffort(() => emitFailed(failed.job, false));
      return { executed: false, blockedReason: error.reasonCode, job: failed.job, prUrl: result.prUrl, dryRun: false };
    }
    throw error;
  }
}

async function acquireAndClaimVerificationLease(bridge: BridgeClient, jobId: string, attemptId: string): Promise<boolean> {
  const lease = await bridge.acquireVerificationLease(jobId, attemptId);
  return Boolean(lease.acquired && (await bridge.claimVerificationLease(jobId, attemptId)).claimed);
}

async function completeDryRunJob(bridge: BridgeClient, job: BridgeJob): Promise<BridgeJob> {
  if (job.state === 'effect_committed') {
    const completed = await bridge.transitionJob(job.job_id, {
      state: 'completed',
      details: {
        reason_code: 'dry_run_completed'
      }
    });
    return completed.job;
  }

  const running = await ensureRunning(bridge, job, 'dry_run');
  const syntheticPrUrl = `dry-run://github/pr/${running.job_id}`;
  const committed = await bridge.transitionJob(running.job_id, {
    state: 'effect_committed',
    details: {
      commit_sha: 'dry-run',
      pr_url: syntheticPrUrl,
      reason_code: 'dry_run_effect'
    }
  });
  const completed = await bridge.transitionJob(committed.job.job_id, {
    state: 'completed',
    details: {
      reason_code: 'dry_run_completed'
    }
  });
  return completed.job;
}

async function ensureVerificationPending(bridge: BridgeClient, job: BridgeJob, initialContext?: unknown): Promise<BridgeJob> {
  if (job.state === 'verification_pending') {
    return job;
  }
  if (!job.pr_url) {
    throw new Error(`effect_committed bridge job ${job.job_id} is missing pr_url`);
  }
  const pending = await bridge.transitionJob(job.job_id, {
    state: 'verification_pending',
    details: {
      reason_code: 'draft_pr_verification_pending',
      ...(initialContext && typeof initialContext === 'object' && !Array.isArray(initialContext) ? { verification_context: initialContext as JsonObject } : {}),
    }
  });
  await emitBestEffort(() => emitVerificationPending(pending.job));
  return pending.job;
}


async function ensureRunning(bridge: BridgeClient, job: BridgeJob, mode: 'dry_run' | 'hermes'): Promise<BridgeJob> {
  if (job.state === 'running') {
    return job;
  }
  if (job.state !== 'accepted') {
    throw new Error(`cannot start ${job.job_id} from bridge state ${job.state}`);
  }
  const running = await bridge.transitionJob(job.job_id, {
    state: 'running',
    details: {
      executor: 'bridge-executor-worker',
      mode
    }
  });
  return running.job;
}

async function failBeforeExternalEffect(
  bridge: BridgeClient,
  job: BridgeJob,
  input: {
    reasonCode: string;
    error: string;
  }
): Promise<BridgeJob> {
  if (job.state !== 'accepted' && job.state !== 'running' && job.state !== 'effect_committed') {
    return job;
  }
  const failed = await bridge.transitionJob(job.job_id, {
    state: 'failed',
    details: {
      reason_code: input.reasonCode,
      error: input.error
    }
  });
  return failed.job;
}

async function emitCommitted(job: BridgeJob, dryRun: boolean): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_EFFECT_COMMITTED, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    prUrl: job.pr_url,
    dryRun
  }, {
    additionalMetadata: {
      source: 'bridge-executor-worker',
      job_id: job.job_id
    }
  });
}

async function emitCompleted(job: BridgeJob, dryRun: boolean): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_COMPLETED, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    prUrl: job.pr_url,
    dryRun
  }, {
    additionalMetadata: {
      source: 'bridge-executor-worker',
      job_id: job.job_id
    }
  });
}

async function emitVerificationPending(job: BridgeJob): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_VERIFICATION_PENDING, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    prUrl: job.pr_url,
    dryRun: false
  }, { additionalMetadata: { source: 'bridge-executor-worker', job_id: job.job_id } });
}

async function emitVerificationFailed(job: BridgeJob): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_VERIFICATION_FAILED, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    prUrl: job.pr_url,
    reasonCode: job.reason_code,
    dryRun: false
  }, { additionalMetadata: { source: 'bridge-executor-worker', job_id: job.job_id } });
}

async function emitReadyForHumanReview(job: BridgeJob): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_READY_FOR_HUMAN_REVIEW, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    prUrl: job.pr_url,
    dryRun: false
  }, { additionalMetadata: { source: 'bridge-executor-worker', job_id: job.job_id } });
}

async function emitFailed(job: BridgeJob, dryRun: boolean): Promise<void> {
  await hatchet.events.push(HATCHET_EVENTS.HERMES_JOB_FAILED, {
    jobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    reasonCode: job.reason_code,
    error: job.error,
    dryRun
  }, {
    additionalMetadata: {
      source: 'bridge-executor-worker',
      job_id: job.job_id
    }
  });
}

async function emitPrResult(job: BridgeJob, repo: string, prNumber: number, dryRun: boolean): Promise<void> {
  const eventName = dryRun ? HATCHET_EVENTS.GITHUB_PR_CREATION_SIMULATED : HATCHET_EVENTS.GITHUB_PR_CREATED;
  await hatchet.events.push(eventName, {
    repo,
    prNumber,
    prUrl: job.pr_url,
    bridgeJobId: job.job_id,
    dryRun
  }, {
    additionalMetadata: {
      source: 'bridge-executor-worker',
      job_id: job.job_id
    }
  });
}
