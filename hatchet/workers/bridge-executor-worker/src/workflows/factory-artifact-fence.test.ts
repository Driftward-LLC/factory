import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import {
  type DraftPrStateInspection,
  type GitHubHeadInspection
} from '../hermes/create-draft-pr';
import { validateFactoryArtifactFence } from './factory-artifact-fence';

test('absence retry fence passes when the prior branch and PR remain absent', async () => {
  const result = await validateFactoryArtifactFence(absenceRetryJob(), {
    inspectGitHubHead: async (repository, headBranch) => absentHeadInspection(repository, headBranch)
  });

  assert.deepEqual(result, { ok: true });
});

test('absence retry fence blocks when the prior branch reappears', async () => {
  const result = await validateFactoryArtifactFence(absenceRetryJob(), {
    inspectGitHubHead: async (repository, headBranch) => ({
      ...absentHeadInspection(repository, headBranch),
      branch_exists: true
    })
  });

  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reasonCode, 'artifact_fence_changed');
});

test('absence retry fence requires matching factory_absence_retry lineage', async () => {
  const result = await validateFactoryArtifactFence(baseJob({
    factory_artifact_fence: absenceFence()
  }), {
    inspectGitHubHead: async (repository, headBranch) => absentHeadInspection(repository, headBranch)
  });

  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reasonCode, 'artifact_fence_invalid');
});

test('superseded PR fence still passes for the same closed unmerged PR', async () => {
  const result = await validateFactoryArtifactFence(baseJob({
    factory_artifact_fence: {
      prior_pr_url: 'https://github.com/Driftward-LLC/capykit/pull/28',
      repository: 'Driftward-LLC/capykit',
      head_branch: 'codex/old-branch',
      head_sha: 'abc123',
      base_ref: 'main'
    }
  }), {
    inspectDraftPrState: async () => ({
      repository: 'Driftward-LLC/capykit',
      number: 28,
      url: 'https://github.com/Driftward-LLC/capykit/pull/28',
      state: 'closed',
      merged: false,
      draft: true,
      head_branch: 'codex/old-branch',
      head_sha: 'abc123',
      base_ref: 'main',
      reason: 'ok'
    } satisfies DraftPrStateInspection)
  });

  assert.deepEqual(result, { ok: true });
});

test('revision fence requires the approved existing open draft and full head', async () => {
  const source = { prior_attempt_id: 'attempt-1', prior_job_id: 'job-1', prior_pr_url: 'https://github.com/Driftward-LLC/capykit/pull/80',
    repository: 'Driftward-LLC/capykit', head_branch: 'codex/existing', head_sha: 'a'.repeat(40), base_ref: 'main' };
  const job = baseJob({ expected_head_sha: source.head_sha, factory_artifact_fence: { kind: 'github_open_draft_continuation_v1', ...source },
    factory_repair: { trigger: 'review_feedback', source_attempt_id: 'attempt-1', source_job_id: 'job-1', pr_url: source.prior_pr_url, branch: source.head_branch, commit_sha: source.head_sha },
    factory_revision: { source_attempt_id: 'attempt-1', source_job_id: 'job-1', reviewed_head_sha: source.head_sha } });
  job.branch = source.head_branch;
  job.requested_action = 'repair_draft_pr';
  const observed: DraftPrStateInspection = { repository: source.repository, number: 80, url: source.prior_pr_url, state: 'open', merged: false,
    draft: true, head_branch: source.head_branch, head_sha: source.head_sha, base_ref: 'main', reason: 'ok' };
  assert.deepEqual(await validateFactoryArtifactFence(job, { inspectDraftPrState: async () => observed }), { ok: true });
  const repair = { ...job, payload: { ...job.payload, factory_revision: null,
    factory_repair: { source_attempt_id: 'attempt-1', source_job_id: 'job-1', pr_url: source.prior_pr_url, branch: source.head_branch, commit_sha: source.head_sha, reason_code: 'verification_github_checks_failed' } } };
  assert.deepEqual(await validateFactoryArtifactFence(repair, { inspectDraftPrState: async () => observed }), { ok: true });
  for (const change of [{ head_sha: 'b'.repeat(40) }, { state: 'closed' as const }, { merged: true }, { draft: false }, { head_branch: 'other' }, { reason: 'inspect_unavailable' as const }]) {
    const result = await validateFactoryArtifactFence(job, { inspectDraftPrState: async () => ({ ...observed, ...change }) });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.reasonCode, 'artifact_fence_changed');
  }
});

function absenceRetryJob(): BridgeJob {
  const fence = absenceFence();
  return baseJob({
    factory_artifact_fence: fence,
    factory_absence_retry: { ...fence }
  });
}

function absenceFence(): JsonObject {
  return {
    kind: 'github_branch_pr_absence_v1',
    prior_attempt_id: 'attempt-1',
    prior_job_id: 'job-1',
    repository: 'Driftward-LLC/capykit',
    head_branch: 'codex/hatchet-hermes-old',
    lease_id: 'lease-1',
    lease_epoch: 1,
    effect_authorized_at: '2026-08-28T00:00:00Z',
    lease_expires_at: '2026-08-28T00:10:00Z',
    branch_exists: false,
    pull_request_count: 0,
    observed_at: '2026-08-28T00:11:00Z',
    evidence_digest: 'digest'
  };
}

function absentHeadInspection(repository: string, headBranch: string): GitHubHeadInspection {
  return {
    kind: 'github_branch_pr_absence_v1',
    repository,
    head_branch: headBranch,
    branch_exists: false,
    pull_requests: [],
    observed_at: '2026-08-28T00:12:00Z',
    reason: 'ok'
  };
}

function baseJob(payload: JsonObject): BridgeJob {
  return {
    job_id: 'job-2',
    idempotency_key: 'hatchet:work-item:linear:ENG-15:create_draft_pr:real:attempt:2',
    state: 'running',
    requested_action: 'create_draft_pr',
    payload: {
      repo: 'Driftward-LLC/capykit',
      dry_run: false,
      ...payload
    },
    payload_hash: 'hash',
    branch: 'codex/hatchet-hermes-new',
    commit_sha: null,
    pr_url: null,
    hermes_session_id: null,
    hatchet_workflow_run_id: 'run-2',
    hatchet_step_run_id: 'step-2',
    verification_context: null,
    verification_observed_context: null,
    verification_receipt: null,
    verification_digest: null,
    reason_code: null,
    error: null,
    created_at: '2026-08-28T00:00:00Z',
    updated_at: '2026-08-28T00:00:00Z'
  };
}
