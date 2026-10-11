import type { BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import { boundedHermesDraftPrReceipt } from './bounded-evidence';

/** Executor-private checkpoint: no prompt, raw output, PR body or credentials. */
export type ArtifactObservationContext = {
  schema: 'factory-artifact-observation-v1';
  finished_at: string;
  before: { repository: string; head_branch: string; reason: 'ok'; pull_requests: { url: string }[] };
  report: JsonObject | null;
};

export function boundedArtifactObservationContext(job: BridgeJob, raw: unknown): ArtifactObservationContext | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const value = raw as ArtifactObservationContext;
  const before = value.before;
  if (value.schema !== 'factory-artifact-observation-v1'
    || typeof value.finished_at !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value.finished_at)
    || !Number.isFinite(Date.parse(value.finished_at))
    || !before || before.repository !== job.payload.repo || before.head_branch !== job.branch || before.reason !== 'ok'
    || !Array.isArray(before.pull_requests) || before.pull_requests.length > 100
    || before.pull_requests.some((pr) => {
      const match = typeof pr?.url === 'string' && pr.url.match(/^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)$/);
      return !match || match[1] !== job.payload.repo || !Number.isSafeInteger(Number(match[2]));
    })) return null;
  const report = value.report;
  const claim = report && boundedHermesDraftPrReceipt(job, {
    status: job.requested_action === 'repair_draft_pr' ? 'reused' : 'created',
    prUrl: report.pr_url, branch: report.branch, commitSha: report.commit_sha, validation: report.validation,
  });
  return {
    schema: value.schema, finished_at: value.finished_at,
    before: { repository: before.repository, head_branch: before.head_branch, reason: 'ok',
      pull_requests: before.pull_requests.map(({ url }) => ({ url })) },
    report: claim?.validation && /^[0-9a-f]{40}$/.test(claim.commitSha || '')
      ? { pr_url: claim.prUrl, branch: claim.branch, commit_sha: claim.commitSha, validation: claim.validation } : null,
  };
}
