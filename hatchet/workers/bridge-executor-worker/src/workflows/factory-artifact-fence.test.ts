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

test('operator review repair rechecks the exact source draft and fails on drift', async () => {
  const job = reviewRepairJob();
  const observe = (): DraftPrStateInspection => ({
    repository: 'Driftward-LLC/capykit', number: 77, url: 'https://github.com/Driftward-LLC/capykit/pull/77',
    state: 'open', merged: false, draft: true, head_branch: job.branch, head_sha: 'a'.repeat(40), base_ref: 'main', reason: 'ok',
  });
  let calls = 0;
  assert.deepEqual(await validateFactoryArtifactFence(job, { inspectDraftPrState: async (repo, reference) => {
    calls += 1;
    assert.equal(repo, 'Driftward-LLC/capykit');
    assert.equal(reference, 'https://github.com/Driftward-LLC/capykit/pull/77');
    return observe();
  } }), { ok: true });
  assert.equal(calls, 1);
  const review = job.payload.factory_review_repair as JsonObject;
  review.feedback = 'Bug. '.repeat(399) + 'Fix.' + '😀';
  assert.equal([...String(review.feedback)].length, 2000);
  assert.deepEqual(await validateFactoryArtifactFence(job, { inspectDraftPrState: async () => observe() }), { ok: true });
  review.feedback += '😀';
  assert.equal((await validateFactoryArtifactFence(job)).ok, false);
  review.feedback = 'Fix stale observations';
  for (const change of [{ head_sha: 'b'.repeat(40) }, { head_branch: 'codex/other' }, { base_ref: 'other' },
    { state: 'closed' as const }, { draft: false }, { merged: true }, { repository: 'other/repo' },
    { reason: 'inspect_unavailable' as const }]) {
    const result = await validateFactoryArtifactFence(job, { inspectDraftPrState: async () => ({ ...observe(), ...change }) });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.reasonCode, 'artifact_fence_changed');
  }
  const invalidPayloads: JsonObject[] = [{ expected_head_sha: 'b'.repeat(40) }, { factory_artifact_fence: null },
    { factory_repair: { ...job.payload.factory_repair as JsonObject, source_attempt_id: 'other' } }];
  for (const payload of invalidPayloads) {
    const result = await validateFactoryArtifactFence({ ...job, payload: { ...job.payload, ...payload } }, {
      inspectDraftPrState: async () => { throw new Error('invalid authority must not inspect'); },
    });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.reasonCode, 'artifact_fence_invalid');
  }
});

function reviewRepairJob(): BridgeJob {
  const job = baseJob({});
  const prUrl = 'https://github.com/Driftward-LLC/capykit/pull/77';
  job.requested_action = 'repair_draft_pr';
  job.branch = 'codex/existing';
  job.payload = {
    ...job.payload, expected_head_sha: 'a'.repeat(40), expected_base: 'main',
    factory_artifact_fence: { kind: 'operator_review_repair_v1', repository: 'Driftward-LLC/capykit',
      prior_attempt_id: 'source-attempt', prior_job_id: 'source-job', prior_pr_url: prUrl,
      head_branch: job.branch, head_sha: 'a'.repeat(40), base_ref: 'main' },
    factory_repair: { source_attempt_id: 'source-attempt', source_job_id: 'source-job', source_generation: 1,
      reason_code: 'operator_review_feedback', error: 'review', verification_attempt_id: null,
      pr_url: prUrl, branch: job.branch, commit_sha: 'a'.repeat(40) },
    factory_review_repair: { source_attempt_id: 'source-attempt', repository: 'Driftward-LLC/capykit',
      pr_url: prUrl, branch: job.branch, reviewed_head_sha: 'a'.repeat(40), feedback: 'Fix stale observations', automatic_repairs_allowed: false },
  };
  return job;
}

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
