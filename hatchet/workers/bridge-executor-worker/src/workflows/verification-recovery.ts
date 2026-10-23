import type { BridgeJob } from '@driftward/hatchet-worker-shared';

type RecoveryBridge = {
  acquireVerificationLease(ref: string, attemptId: string): Promise<{ acquired?: boolean }>;
  claimVerificationLease(ref: string, attemptId: string): Promise<{ claimed?: boolean }>;
  completeVerificationFailure(ref: string, attemptId: string, reasonCode: string, error: string, classification?: {
    failure_domain: string;
    failure_stage: string;
    retryable: boolean | null;
    failure_disposition: 'retry' | 'terminal' | 'recovery_required';
    operator_intervention_required: boolean | null;
  }): Promise<{ job: BridgeJob }>;
};

export type VerificationRecoveryResult = { blockedReason?: string; job: BridgeJob; completed?: boolean; executed?: boolean; prUrl?: string; dryRun?: boolean; reused?: boolean };

function failureReason(error: unknown): { reasonCode: string; message: string } {
  if (error && typeof error === 'object' && 'reasonCode' in error && typeof error.reasonCode === 'string') {
    return { reasonCode: error.reasonCode, message: error instanceof Error ? error.message : 'verification recovery failed' };
  }
  return { reasonCode: 'verification_recovery_context_unavailable', message: 'stored verification context could not be resumed' };
}

/** Claim before reading durable context so every post-claim failure has one fenced terminal path. */
export async function recoverClaimedVerification<Result>(input: {
  bridge: RecoveryBridge;
  job: BridgeJob;
  attemptId: string;
  attemptReserved?: boolean;
  ensurePending: () => Promise<BridgeJob>;
  completeObserved?: (job: BridgeJob, attemptId: string) => Promise<VerificationRecoveryResult | null>;
  resume: (job: BridgeJob) => Result;
  verify: (job: BridgeJob, result: Result, attemptId: string) => Promise<VerificationRecoveryResult>;
  emitFailure: (job: BridgeJob) => Promise<void>;
}): Promise<VerificationRecoveryResult> {
  let claimed = false;
  try {
    const reserved = input.attemptReserved ? { acquired: true } : await input.bridge.acquireVerificationLease(input.job.job_id, input.attemptId);
    claimed = Boolean(reserved.acquired && (await input.bridge.claimVerificationLease(input.job.job_id, input.attemptId)).claimed);
  } catch {
    return { blockedReason: 'verification_recovery_lease_unavailable', job: input.job };
  }
  if (!claimed) return { blockedReason: 'verification_attempt_in_progress', job: input.job };
  try {
    const pending = await input.ensurePending();
    const observed = input.completeObserved ? await input.completeObserved(pending, input.attemptId) : null;
    if (observed) return observed;
    return await input.verify(pending, input.resume(pending), input.attemptId);
  } catch (error) {
    const failure = failureReason(error);
    const completed = await input.bridge.completeVerificationFailure(
      input.job.job_id,
      input.attemptId,
      failure.reasonCode,
      failure.message,
      {
        failure_domain: 'verification',
        failure_stage: 'verification_recovery',
        retryable: null,
        failure_disposition: 'terminal',
        operator_intervention_required: true,
      },
    );
    await input.emitFailure(completed.job);
    return { blockedReason: failure.reasonCode, job: completed.job, completed: true };
  }
}
