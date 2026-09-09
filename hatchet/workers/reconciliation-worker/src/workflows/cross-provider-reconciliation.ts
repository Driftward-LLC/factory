import { createHash } from 'node:crypto';
import type { BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import type { ObservationLookup } from '../ingress-observation-client';
import type { HatchetRunLookup } from '../hatchet-run-observer';

export type ReconciliationFinding = {
  findingKey: string;
  severity: 'warning' | 'critical';
  kind: string;
  jobId: string | null;
  subject: string;
  idempotencyKey?: string;
  state?: string;
  reason: string;
  branch?: string;
  prUrl?: string;
  expected: JsonObject;
  observed: JsonObject;
};

export const RECONCILIATION_KINDS = [
  'bridge_job_without_events',
  'bridge_job_missing_accepted_event',
  'bridge_job_event_history_incomplete',
  'stale_active_bridge_job',
  'running_create_draft_pr_missing_repo_or_branch',
  'effect_committed_without_pr_url',
  'verification_pending_without_pr_url',
  'completed_create_draft_pr_without_pr_url',
  'ready_for_human_review_without_pr_url',
  'failed_job_has_external_effect_refs',
  'duplicate_bridge_branch',
  'duplicate_bridge_pr_url',
  'reconciliation_scan_incomplete',
  'ingress_inventory_unavailable',
  'ingress_inventory_incomplete',
  'ingress_stored_not_published',
  'ingress_published_unconsumed',
  'hatchet_inventory_unavailable',
  'hatchet_inventory_incomplete',
  'hatchet_run_without_bridge',
  'hatchet_run_not_found',
  'hatchet_run_timeout',
  'hatchet_run_unavailable',
  'hatchet_run_malformed',
  'hatchet_run_auth',
  'hatchet_task_missing_from_run',
  'hatchet_terminal_run_with_active_bridge_job',
  'hatchet_provenance_missing',
  'hermes_execution_ref_missing',
  'hermes_receipt_contradicts_bridge_history',
  'github_pr_observation_missing',
  'github_observation_timeout',
  'github_observation_unavailable',
  'github_observation_malformed',
  'github_observation_auth',
  'github_observation_stale',
  'github_pr_url_mismatch',
  'github_pr_repo_mismatch',
  'github_pr_branch_mismatch',
  'github_pr_commit_mismatch',
  'github_pr_not_draft',
  'github_pr_closed_unmerged_after_effect',
  'github_pr_missing_linear_link',
  'linear_issue_observation_missing',
  'linear_observation_timeout',
  'linear_observation_unavailable',
  'linear_observation_malformed',
  'linear_observation_auth',
  'linear_observation_stale',
  'linear_terminal_issue_with_active_bridge_job',
  'linear_issue_blocked_with_active_bridge_job',
  'linear_issue_ineligible_with_active_bridge_job',
  'linear_route_mismatch',
] as const;

type Event = { event_type: string; details: Record<string, unknown> };
const ACTIVE = new Set(['accepted', 'running', 'effect_committed', 'verification_pending']);
const TERMINAL_LINEAR = new Set(['done', 'canceled', 'cancelled', 'duplicate', 'in review', 'needs operator', 'blocked']);

export function compareCrossProvider(
  job: BridgeJob,
  events: Event[],
  lookups: { github?: ObservationLookup; linear?: ObservationLookup; hatchet?: HatchetRunLookup },
  cutoff: number,
): ReconciliationFinding[] {
  const findings: ReconciliationFinding[] = [];
  const active = ACTIVE.has(job.state);
  const stale = Date.parse(job.updated_at) <= cutoff;
  const dryRun = job.payload.dry_run === true;
  const effectAction = job.requested_action === 'create_draft_pr' || job.requested_action === 'repair_draft_pr';
  const issue = stringPayload(job, 'linear_issue');
  const expected = expectedJob(job);

  if (job.hatchet_workflow_run_id && lookups.hatchet) {
    const hatchet = lookups.hatchet;
    if (hatchet.status !== 'matched') {
      findings.push(finding(job, hatchet.status === 'not_found' ? 'critical' : 'warning', `hatchet_run_${hatchet.status}`, 'Hatchet workflow correlation did not return a usable run', expected, { status: hatchet.status, workflowRunId: job.hatchet_workflow_run_id }));
    } else {
      if (job.hatchet_step_run_id && !hatchet.taskExternalIds.includes(job.hatchet_step_run_id)) findings.push(finding(job, 'critical', 'hatchet_task_missing_from_run', 'bridge task correlation is absent from the Hatchet workflow run', expected, { taskExternalIds: hatchet.taskExternalIds, runStatus: hatchet.runStatus }));
      const terminal = ['COMPLETED', 'FAILED', 'CANCELLED'].includes(hatchet.runStatus.toUpperCase());
      if (active && terminal) findings.push(finding(job, 'critical', 'hatchet_terminal_run_with_active_bridge_job', 'Hatchet run is terminal while bridge job remains active', expected, { runStatus: hatchet.runStatus }));
    }
  }

  if (job.idempotency_key.startsWith('hatchet:') && (!job.hatchet_workflow_run_id || !job.hatchet_step_run_id)) findings.push(finding(job, 'warning', 'hatchet_provenance_missing', 'Hatchet-originated bridge job lacks workflow/task correlation', expected, { workflowRunId: job.hatchet_workflow_run_id, stepRunId: job.hatchet_step_run_id }));
  if (!dryRun && effectAction && (job.state === 'effect_committed' || job.state === 'verification_pending' || job.state === 'ready_for_human_review' || job.state === 'completed') && !job.reason_code?.startsWith('reconciled_')) {
    if (!job.hermes_session_id) {
      findings.push(finding(job, 'critical', 'hermes_execution_ref_missing', 'real effect lacks a Hermes execution receipt', expected, { receiptPresent: false }));
    } else if (!events.some((event) => event.details.hermes_session_id === job.hermes_session_id)) {
      findings.push(finding(job, 'critical', 'hermes_receipt_contradicts_bridge_history', 'Hermes execution receipt is absent from append-only bridge history', expected, { receiptPresent: false, bridgeReceiptRef: job.hermes_session_id }));
    }
  }
  if (job.pr_url && !dryRun && lookups.github) {
    const github = lookups.github;
    if (github.status === 'not_found') {
      if (stale) findings.push(finding(job, 'warning', 'github_pr_observation_missing', 'GitHub PR observation is absent after grace period', expected, { status: github.status }));
    } else if (github.status !== 'matched') {
      findings.push(finding(job, 'warning', `github_observation_${github.status}`, 'GitHub observation source did not return a usable result', expected, { status: github.status }));
    } else {
      const observed = github.observation.github || {};
      if (observed.prUrl !== job.pr_url) findings.push(finding(job, 'critical', 'github_pr_url_mismatch', 'GitHub observation PR URL differs from bridge record', expected, observed));
      if (typeof expected.repo === 'string' && observed.repo !== expected.repo) findings.push(finding(job, 'critical', 'github_pr_repo_mismatch', 'GitHub observation repository differs from bridge route', expected, observed));
      if (job.branch && observed.headRefName !== job.branch) findings.push(finding(job, 'critical', 'github_pr_branch_mismatch', 'GitHub observation branch differs from deterministic bridge branch', expected, observed));
      if (job.commit_sha && observed.headSha !== job.commit_sha) findings.push(finding(job, 'critical', 'github_pr_commit_mismatch', 'GitHub observation head commit differs from bridge record', expected, observed));
      if (observed.isDraft !== true) findings.push(finding(job, 'critical', 'github_pr_not_draft', 'GitHub observation is not a draft PR', expected, observed));
      if (observed.state?.toLowerCase() === 'closed' && observed.merged !== true) findings.push(finding(job, 'critical', 'github_pr_closed_unmerged_after_effect', 'GitHub reports an unmerged closed PR after bridge effect', expected, observed));
      if (issue && !observed.linearIssues?.includes(issue)) findings.push(finding(job, 'warning', 'github_pr_missing_linear_link', 'GitHub observation lacks the originating Linear issue link', expected, observed));
    }
  }
  if (issue && lookups.linear) {
    const linear = lookups.linear;
    if (linear.status === 'not_found') {
      if (active && stale) findings.push(finding(job, 'warning', 'linear_issue_observation_missing', 'active bridge job has no Linear observation after grace period', expected, { status: linear.status }));
    } else if (linear.status !== 'matched') {
      findings.push(finding(job, 'warning', `linear_observation_${linear.status}`, 'Linear observation source did not return a usable result', expected, { status: linear.status }));
    } else {
      const observed = linear.observation.linear || {};
      const status = observed.status?.trim().toLowerCase();
      if (active && status && TERMINAL_LINEAR.has(status)) findings.push(finding(job, 'critical', 'linear_terminal_issue_with_active_bridge_job', 'Linear issue is terminal while bridge job remains active', expected, observed));
      if (active && (observed.blockedByCount || 0) > 0) findings.push(finding(job, 'critical', 'linear_issue_blocked_with_active_bridge_job', 'Linear issue has blockers while bridge job remains active', expected, observed));
      if (active && observed.dispatchEligible !== true) findings.push(finding(job, 'warning', 'linear_issue_ineligible_with_active_bridge_job', 'Linear issue is no longer eligible while bridge job remains active', expected, observed));
      const routing = objectPayload(job, 'routing');
      if (routing?.project_id && observed.projectId && routing.project_id !== observed.projectId) findings.push(finding(job, 'critical', 'linear_route_mismatch', 'Linear project route differs from bridge routing record', expected, observed));
    }
  }
  return findings;
}

export function finding(
  job: BridgeJob,
  severity: 'warning' | 'critical',
  kind: string,
  reason: string,
  expected: Record<string, unknown> = expectedJob(job),
  observed: Record<string, unknown> = {},
): ReconciliationFinding {
  return scopedFinding({ jobId: job.job_id, subject: `bridge:${job.job_id}`, idempotencyKey: job.idempotency_key, state: job.state, branch: job.branch || undefined, prUrl: job.pr_url || undefined }, severity, kind, reason, expected, observed);
}

export function scopedFinding(
  subject: Pick<ReconciliationFinding, 'jobId' | 'subject' | 'idempotencyKey' | 'state' | 'branch' | 'prUrl'>,
  severity: 'warning' | 'critical',
  kind: string,
  reason: string,
  expected: Record<string, unknown>,
  observed: Record<string, unknown>,
): ReconciliationFinding {
  const fingerprint = canonical({ schema: 'reconciliation.v1', subject: subject.subject, driftKind: kind, expected, observed });
  const digest = createHash('sha256').update(JSON.stringify(fingerprint)).digest('hex').slice(0, 24);
  return { findingKey: `reconciliation:v1:${kind}:${digest}`, severity, kind, reason, expected: canonical(expected), observed: canonical(observed), ...subject };
}

export function expectedJob(job: BridgeJob): JsonObject {
  return {
    state: job.state,
    repo: stringPayload(job, 'repo'),
    branch: job.branch,
    commitSha: job.commit_sha,
    prUrl: job.pr_url,
    linearIssue: stringPayload(job, 'linear_issue'),
    dryRun: job.payload.dry_run === true,
  };
}

function canonical(value: Record<string, unknown>): JsonObject {
  return Object.fromEntries(Object.entries(value).filter(([key]) => !['timestamp', 'createdAt', 'updatedAt', 'error', 'sessionId', 'rawPayload'].includes(key)).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonicalValue(item)])) as JsonObject;
}

function canonicalValue(value: unknown): JsonObject[keyof JsonObject] {
  if (Array.isArray(value)) return value.map(canonicalValue).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  if (value && typeof value === 'object') return canonical(value as Record<string, unknown>);
  return (value === undefined ? null : value) as JsonObject[keyof JsonObject];
}

function stringPayload(job: BridgeJob, key: string): string | null {
  const value = job.payload[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function objectPayload(job: BridgeJob, key: string): Record<string, string> | null {
  const value = job.payload[key];
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
}
