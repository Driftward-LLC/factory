import { HATCHET_EVENTS, type BridgeClient, type BridgeJob } from '@driftward/hatchet-worker-shared';
import type { ReconciliationFinding } from './cross-provider-reconciliation';
import { recoverableStaleVerificationJobs } from './verification-recovery';

type RecoveryEventPusher = (event: string, data: Record<string, unknown>, options: Record<string, unknown>) => Promise<unknown>;

/** Reserve before dispatch so duplicate reconciliation scans cannot run verification twice. */
export async function redispatchStaleVerification(findings: ReconciliationFinding[], jobs: Map<string, BridgeJob>, bridge: Pick<BridgeClient, 'acquireVerificationLease'>, pushEvent: RecoveryEventPusher): Promise<number> {
  let failures = 0;
  for (const job of recoverableStaleVerificationJobs(findings, jobs)) {
    try {
      const verificationAttemptId = `${job.idempotency_key}:verify:${job.commit_sha || 'unknown'}`;
      const lease = await bridge.acquireVerificationLease(job.job_id, verificationAttemptId);
      if (!lease.acquired) continue;
      await pushEvent(HATCHET_EVENTS.HERMES_JOB_ACCEPTED, {
        jobId: job.job_id, idempotencyKey: job.idempotency_key, recovery: 'verification_resume', verificationAttemptId,
      }, { additionalMetadata: { source: 'reconciliation-worker', job_id: job.job_id, recovery: 'verification_resume' } });
    } catch {
      failures += 1;
    }
  }
  return failures;
}
