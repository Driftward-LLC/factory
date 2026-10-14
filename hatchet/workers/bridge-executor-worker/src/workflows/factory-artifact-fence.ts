import type { BridgeJob, JsonObject, JsonValue } from '@driftward/hatchet-worker-shared';
import {
  inspectDraftPrState,
  inspectGitHubHead,
  type DraftPrStateInspection,
  type GitHubHeadInspection
} from '../hermes/create-draft-pr';

export type FactoryArtifactFenceValidation =
  | { ok: true }
  | { ok: false; reasonCode: 'artifact_fence_invalid' | 'artifact_fence_changed'; error: string };

type ArtifactFenceInspectors = {
  inspectDraftPrState?: (repository: string, reference: string | number) => Promise<DraftPrStateInspection>;
  inspectGitHubHead?: (repository: string, headBranch: string) => Promise<GitHubHeadInspection>;
};

export async function validateFactoryArtifactFence(
  job: BridgeJob,
  inspectors: ArtifactFenceInspectors = {}
): Promise<FactoryArtifactFenceValidation> {
  const rawFence = job.payload.factory_artifact_fence;
  if (job.payload.factory_review_repair && (!rawFence || jsonObject(rawFence)?.kind !== 'operator_review_repair_v1')) {
    return blocked('artifact_fence_invalid', 'operator review repair requires its exact artifact fence');
  }
  if (rawFence === undefined || rawFence === null) return { ok: true };

  const fence = jsonObject(rawFence);
  if (!fence) return blocked('artifact_fence_invalid', 'factory_artifact_fence must be a JSON object');

  const kind = stringValue(fence.kind);
  if (kind === 'operator_review_repair_v1') {
    const review = jsonObject(job.payload.factory_review_repair);
    const repair = jsonObject(job.payload.factory_repair);
    const repository = stringValue(job.payload.repo);
    const sha = stringValue(fence.head_sha);
    if (!review || !repair || job.requested_action !== 'repair_draft_pr' || !repository || fence.repository !== repository
      || !sha || !/^[0-9a-f]{40}$/.test(sha) || job.payload.expected_head_sha !== sha
      || review.reviewed_head_sha !== sha || repair.commit_sha !== sha
      || review.pr_url !== fence.prior_pr_url || repair.pr_url !== fence.prior_pr_url
      || review.source_attempt_id !== fence.prior_attempt_id || repair.source_attempt_id !== fence.prior_attempt_id
      || repair.source_job_id !== fence.prior_job_id || review.repository !== repository
      || review.branch !== job.branch || repair.branch !== job.branch || fence.head_branch !== job.branch
      || review.automatic_repairs_allowed !== false || typeof review.feedback !== 'string' || [...review.feedback].length > 2000) {
      return blocked('artifact_fence_invalid', 'operator review repair must bind its approved source, artifact and head');
    }
    const observed = await (inspectors.inspectDraftPrState || inspectDraftPrState)(repository, String(fence.prior_pr_url));
    if (observed.reason !== 'ok' || observed.repository !== repository || observed.url !== fence.prior_pr_url
      || observed.state !== 'open' || observed.merged || !observed.draft || observed.head_branch !== job.branch
      || observed.head_sha !== sha || observed.base_ref !== fence.base_ref || observed.base_ref !== job.payload.expected_base) {
      return blocked('artifact_fence_changed', 'reviewed draft head changed before execution');
    }
    return { ok: true };
  }
  if (kind === 'github_branch_pr_absence_v1') {
    return validateAbsenceFence(job, fence, inspectors.inspectGitHubHead || inspectGitHubHead);
  }
  if (kind) {
    return blocked('artifact_fence_invalid', `unsupported factory_artifact_fence kind ${kind}`);
  }
  if (stringValue(fence.prior_pr_url)) {
    return validateSupersededPrFence(job, fence, inspectors.inspectDraftPrState || inspectDraftPrState);
  }
  return blocked('artifact_fence_invalid', 'factory_artifact_fence did not include a supported fence shape');
}

async function validateAbsenceFence(
  job: BridgeJob,
  fence: JsonObject,
  inspect: (repository: string, headBranch: string) => Promise<GitHubHeadInspection>
): Promise<FactoryArtifactFenceValidation> {
  const repository = stringValue(job.payload.repo);
  const fenceRepository = stringValue(fence.repository);
  const headBranch = stringValue(fence.head_branch);
  if (!repository || fenceRepository !== repository || !headBranch) {
    return blocked('artifact_fence_invalid', 'absence fence must bind to the bridge job repository and head branch');
  }

  const requiredStrings = ['prior_attempt_id', 'prior_job_id', 'lease_id', 'effect_authorized_at', 'lease_expires_at', 'observed_at', 'evidence_digest'];
  if (requiredStrings.some((key) => !stringValue(fence[key]))) {
    return blocked('artifact_fence_invalid', 'absence fence omitted required lineage or evidence fields');
  }
  if (fence.branch_exists !== false || fence.pull_request_count !== 0 || typeof fence.lease_epoch !== 'number') {
    return blocked('artifact_fence_invalid', 'absence fence must record an absent branch, absent PRs, and the prior lease epoch');
  }

  const absenceRetry = jsonObject(job.payload.factory_absence_retry);
  if (!absenceRetry || !absenceRetryMatchesFence(absenceRetry, fence)) {
    return blocked('artifact_fence_invalid', 'factory_absence_retry must match the immutable absence fence');
  }

  const observed = await inspect(repository, headBranch);
  if (
    observed.reason !== 'ok' ||
    observed.kind !== 'github_branch_pr_absence_v1' ||
    observed.repository !== repository ||
    observed.head_branch !== headBranch ||
    observed.branch_exists ||
    observed.pull_requests.length > 0
  ) {
    return blocked('artifact_fence_changed', 'absence fence no longer verifies: branch or pull request evidence changed');
  }
  return { ok: true };
}

async function validateSupersededPrFence(
  job: BridgeJob,
  fence: JsonObject,
  inspect: (repository: string, reference: string | number) => Promise<DraftPrStateInspection>
): Promise<FactoryArtifactFenceValidation> {
  const repository = stringValue(job.payload.repo);
  const priorUrl = stringValue(fence.prior_pr_url);
  const expectedBranch = stringValue(fence.head_branch);
  const expectedSha = stringValue(fence.head_sha);
  const expectedBase = stringValue(fence.base_ref);
  if (!repository || !priorUrl || !expectedBranch || !expectedSha || !expectedBase) {
    return blocked('artifact_fence_invalid', 'superseded PR fence omitted required PR identity fields');
  }

  const observed = await inspect(repository, priorUrl);
  if (
    observed.reason !== 'ok' ||
    observed.state !== 'closed' ||
    observed.merged ||
    observed.url !== priorUrl ||
    observed.head_branch !== expectedBranch ||
    observed.head_sha !== expectedSha ||
    observed.base_ref !== expectedBase
  ) {
    return blocked('artifact_fence_changed', 'superseded PR fence no longer verifies against GitHub');
  }
  return { ok: true };
}

function absenceRetryMatchesFence(absenceRetry: JsonObject, fence: JsonObject): boolean {
  return [
    'kind',
    'prior_attempt_id',
    'prior_job_id',
    'repository',
    'head_branch',
    'lease_id',
    'lease_epoch',
    'effect_authorized_at',
    'lease_expires_at',
    'branch_exists',
    'pull_request_count',
    'observed_at',
    'evidence_digest'
  ].every((key) => absenceRetry[key] === fence[key]);
}

function jsonObject(value: JsonValue | undefined): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value;
}

function stringValue(value: JsonValue | undefined): string | null {
  return typeof value === 'string' && value.trim() === value && value ? value : null;
}

function blocked(
  reasonCode: 'artifact_fence_invalid' | 'artifact_fence_changed',
  error: string
): FactoryArtifactFenceValidation {
  return { ok: false, reasonCode, error };
}
