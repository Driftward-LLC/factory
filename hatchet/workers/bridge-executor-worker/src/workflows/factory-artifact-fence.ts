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
  if (rawFence === undefined || rawFence === null) return { ok: true };

  const fence = jsonObject(rawFence);
  if (!fence) return blocked('artifact_fence_invalid', 'factory_artifact_fence must be a JSON object');

  const kind = stringValue(fence.kind);
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
