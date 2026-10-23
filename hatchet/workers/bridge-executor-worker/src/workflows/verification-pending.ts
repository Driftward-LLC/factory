import { isDeepStrictEqual } from 'node:util';
import type { BridgeClient, BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';

type VerificationPendingBridge = Pick<BridgeClient, 'getJob' | 'transitionJob'>;

function isJsonObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function matchesConcurrentHandoff(original: BridgeJob, current: BridgeJob, expectedContext: JsonObject | null): boolean {
  return current.state === 'verification_pending'
    && current.job_id === original.job_id
    && current.idempotency_key === original.idempotency_key
    && current.requested_action === original.requested_action
    && current.payload_hash === original.payload_hash
    && current.payload.repo === original.payload.repo
    && current.pr_url === original.pr_url
    && current.branch === original.branch
    && current.commit_sha === original.commit_sha
    && current.hermes_session_id === original.hermes_session_id
    && (expectedContext === null
      ? current.verification_context === null || isJsonObject(current.verification_context)
      : isDeepStrictEqual(current.verification_context, expectedContext));
}

/** Advance once, or accept the exact state written by a concurrent receipt runner. */
export async function transitionToVerificationPending(
  bridge: VerificationPendingBridge,
  job: BridgeJob,
  initialContext?: unknown,
): Promise<{ job: BridgeJob; transitioned: boolean }> {
  if (job.state === 'verification_pending') return { job, transitioned: false };
  const context = isJsonObject(initialContext) ? initialContext : null;
  try {
    const pending = await bridge.transitionJob(job.job_id, {
      state: 'verification_pending',
      details: {
        reason_code: 'draft_pr_verification_pending',
        ...(context ? { verification_context: context } : {}),
      },
    });
    return { job: pending.job, transitioned: true };
  } catch (error) {
    try {
      const current = await bridge.getJob(job.job_id);
      if (matchesConcurrentHandoff(job, current, context)) {
        return { job: current, transitioned: false };
      }
    } catch {
      // Preserve the transition conflict; it is the operation being recovered.
    }
    throw error;
  }
}
