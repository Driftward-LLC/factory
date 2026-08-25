import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { compareCrossProvider } from '../workflows/cross-provider-reconciliation';

const jobId = process.argv[2];
if (!jobId) throw new Error('usage: negative-mismatch-smoke <existing-bridge-job-id>');

const job: BridgeJob = {
  job_id: jobId,
  idempotency_key: 'hatchet:negative-mismatch-smoke',
  state: 'completed', requested_action: 'create_draft_pr',
  payload: { repo: 'Driftward-LLC/driftward-host', linear_issue: 'ENG-39', dry_run: false },
  payload_hash: 'negative-smoke', branch: 'codex/expected-branch', commit_sha: 'expected-sha',
  pr_url: 'https://github.com/Driftward-LLC/driftward-host/pull/999999', hermes_session_id: 'receipt',
  hatchet_workflow_run_id: null, hatchet_step_run_id: null, reason_code: 'reconciled_smoke', error: null,
  verification_context: null, verification_observed_context: null, verification_receipt: null, verification_digest: null,
  created_at: '2026-08-13T00:00:00Z', updated_at: '2026-08-13T00:00:00Z',
};
const findings = compareCrossProvider(job, [], {
  github: { status: 'matched', observation: {
    eventKey: 'github:negative-mismatch', source: 'github', subject: 'Driftward-LLC/driftward-host#999999', observedAt: '2026-08-13T00:00:00Z',
    github: { prUrl: 'https://github.com/Driftward-LLC/driftward-host/pull/999999', headRefName: 'codex/observed-mismatch', headSha: 'observed-sha', isDraft: true, state: 'open', merged: false, linearIssues: ['ENG-39'] },
  } },
}, Date.now() - 60_000);
const mismatch = findings.find((item) => item.kind === 'github_pr_branch_mismatch');
if (!mismatch) throw new Error('negative mismatch was not detected');
console.log(JSON.stringify(mismatch));
