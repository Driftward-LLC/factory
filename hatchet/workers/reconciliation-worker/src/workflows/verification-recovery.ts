import type { BridgeClient, BridgeJob } from '@driftward/hatchet-worker-shared';

export type StaleFinding = { kind: string; jobId: string | null };

/** Only a durable real PR in a verification state can be re-delivered. */
export function recoverableStaleVerificationJobs(findings: StaleFinding[], jobs: Map<string, BridgeJob>): BridgeJob[] {
  const recovered: BridgeJob[] = [];
  const seen = new Set<string>();
  for (const item of findings) {
    if (item.kind !== 'stale_active_bridge_job' || !item.jobId || seen.has(item.jobId)) continue;
    const job = jobs.get(item.jobId);
    if (!job || !['effect_committed', 'verification_pending'].includes(job.state) || !job.pr_url || job.payload.dry_run === true) continue;
    recovered.push(job);
    seen.add(job.job_id);
  }
  return recovered;
}

/** The bridge revalidates eligibility and queues a same-job wake atomically.
 * Verification leases are acquired by the consumer, never while waiting in an outbox. */
export async function enqueueStaleVerification(findings: StaleFinding[], jobs: Map<string, BridgeJob>, bridge: Pick<BridgeClient, 'enqueueVerificationResume'>, staleAfterMinutes: number) {
  const result = { enqueued: 0, skipped: 0, failed: 0, reasons: {} as Record<string, number> };
  for (const job of recoverableStaleVerificationJobs(findings, jobs)) {
    try {
      const resume = await bridge.enqueueVerificationResume(job.job_id, staleAfterMinutes);
      if (resume.enqueued) result.enqueued += 1;
      else result.skipped += 1;
      result.reasons[resume.reason] = (result.reasons[resume.reason] || 0) + 1;
    } catch {
      result.failed += 1;
    }
  }
  return result;
}

export async function enqueueStaleExecutionReceipts(findings: StaleFinding[], jobs: Map<string, BridgeJob>, bridge: Pick<BridgeClient, 'enqueueExecutionReceiptRecovery'>, staleAfterMinutes: number) {
  const result = { enqueued: 0, skipped: 0, failed: 0 };
  const seen = new Set<string>();
  for (const item of findings) {
    if (item.kind !== 'stale_active_bridge_job' || !item.jobId || seen.has(item.jobId)) continue;
    seen.add(item.jobId);
    const job = jobs.get(item.jobId);
    if (!job || job.state !== 'running' || job.payload.dry_run === true) continue;
    try {
      const wake = await bridge.enqueueExecutionReceiptRecovery(job.job_id, staleAfterMinutes);
      if (wake.enqueued) result.enqueued++; else result.skipped++;
    } catch { result.failed++; }
  }
  return result;
}
