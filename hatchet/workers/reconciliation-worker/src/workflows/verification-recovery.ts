import type { BridgeJob } from '@driftward/hatchet-worker-shared';

export type StaleFinding = { kind: string; jobId: string | null };

/** Only a durable real PR in a verification state can be re-delivered. */
export function recoverableStaleVerificationJobs(findings: StaleFinding[], jobs: Map<string, BridgeJob>): BridgeJob[] {
  const recovered: BridgeJob[] = [];
  for (const item of findings) {
    if (item.kind !== 'stale_active_bridge_job' || !item.jobId) continue;
    const job = jobs.get(item.jobId);
    if (!job || !['effect_committed', 'verification_pending'].includes(job.state) || !job.pr_url || job.payload.dry_run === true) continue;
    recovered.push(job);
  }
  return recovered;
}
