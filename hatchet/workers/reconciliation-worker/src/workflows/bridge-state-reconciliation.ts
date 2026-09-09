import { BridgeClient, HATCHET_EVENTS, type BridgeJob } from '@driftward/hatchet-worker-shared';
import { ConcurrencyLimitStrategy } from '@hatchet-dev/typescript-sdk/v1';
import { hatchet } from '../hatchet-client';
import { IngressObservationClient, type ObservationLookup } from '../ingress-observation-client';
import { observeHatchetRun, observeHatchetRuns, type HatchetRunLookup } from '../hatchet-run-observer';
import { compareCrossProvider, expectedJob, finding, type ReconciliationFinding } from './cross-provider-reconciliation';
import { bridgeEventHistoryIncomplete, incompleteBridgeScan, inspectHatchetInventory, inspectIngressInventory } from './inventory-reconciliation';
import { recordFindingBatches } from './finding-batches';
import { reconciliationInput, type BridgeStateReconciliationInput } from './reconciliation-input';
import { redispatchStaleVerification } from './verification-lease-recovery';

type BridgeEvent = { event_id: string; job_id: string; event_type: string; state: string; details: Record<string, unknown>; created_at: string };

export type BridgeStateReconciliationOutput = {
  'inspect-bridge-ledger': {
    checkedJobs: number;
    sourceCounts: { bridge: number; ingress: number; hatchet: number };
    complete: boolean;
    findingCount: number;
    findings: ReconciliationFinding[];
  };
};

const ACTIVE_STATES = new Set(['accepted', 'running', 'effect_committed', 'verification_pending']);
const CONCURRENCY = 4;

export const bridgeStateReconciliation = hatchet.workflow<BridgeStateReconciliationInput, BridgeStateReconciliationOutput>({
  name: 'reconciliation-bridge-state-check',
  on: { cron: '*/15 * * * *' },
  concurrency: {
    expression: '"reconciliation-bridge-state-check"',
    maxRuns: 1,
    limitStrategy: ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
  },
});

bridgeStateReconciliation.task({
  name: 'inspect-bridge-ledger',
  executionTimeout: '60s',
  fn: async (input, ctx) => {
    const options = reconciliationInput(input);
    const bridge = new BridgeClient();
    const observationClient = new IngressObservationClient();
    const maxJobs = boundedInteger(options.maxJobs, 1, 250, 250);
    const maxEventsPerJob = boundedInteger(options.maxEventsPerJob, 1, 250, 100);
    const staleAfterMinutes = boundedInteger(options.staleAfterMinutes, 1, 10080, 30);
    const cutoff = Date.now() - staleAfterMinutes * 60 * 1000;
    const [{ jobs, total }, ingressInventory, hatchetInventory] = await Promise.all([
      bridge.listJobs(maxJobs), observationClient.inventory(maxJobs), observeHatchetRuns(hatchet.runs, maxJobs),
    ]);
    const inspected = await mapBounded(jobs, CONCURRENCY, async (job) => {
      const { events, total: eventTotal } = await bridge.listEvents(job.job_id, maxEventsPerJob);
      return { job, eventTotal, eventCount: events.length, findings: [...inspectJob(job, events, cutoff), ...compareCrossProvider(job, events, await providerLookups(job, observationClient), cutoff)] };
    });
    const findings = inspected.flatMap((item) => {
      const eventFinding = bridgeEventHistoryIncomplete(item.job, item.eventTotal, item.eventCount, maxEventsPerJob);
      return eventFinding ? [...item.findings, eventFinding] : item.findings;
    });

    findings.push(...findDuplicateRefs(jobs));
    const complete = total <= jobs.length && inspected.every((item) => item.eventTotal <= item.eventCount) && ingressInventory.status === 'matched' && ingressInventory.complete && hatchetInventory.status === 'matched' && hatchetInventory.complete;
    const incompleteBridge = incompleteBridgeScan(total, jobs.length, maxJobs);
    if (incompleteBridge) findings.push(incompleteBridge);
    findings.push(...inspectIngressInventory(ingressInventory, jobs, cutoff));
    findings.push(...inspectHatchetInventory(hatchetInventory, jobs, cutoff));

    const recorded = findings.length > 0 ? await recordFindingBatches(bridge, findings.map((item) => ({
      finding_key: item.findingKey, job_id: item.jobId, subject: item.subject, kind: item.kind, severity: item.severity,
      details: { expected: item.expected, observed: item.observed },
    }))) : { created_finding_keys: [], existing_finding_keys: [], failedBatches: 0 };
    const created = new Set(recorded.created_finding_keys);
    const newFindings = findings.filter((item) => created.has(item.findingKey));
    const jobById = new Map(jobs.map((job) => [job.job_id, job]));
    const recoveryDispatchFailures = await redispatchStaleVerification(findings, jobById, bridge, (event, data, options) => hatchet.events.push(event, data, options));
    const sourceCounts = { bridge: jobs.length, ingress: ingressInventory.status === 'matched' ? ingressInventory.observations.length : 0, hatchet: hatchetInventory.status === 'matched' ? hatchetInventory.runs.length : 0 };

    ctx.logger.info('Bridge reconciliation check completed', { ...sourceCounts, bridgeTotal: total, complete, findingCount: findings.length, newFindingCount: newFindings.length, findingWriteFailures: recorded.failedBatches || 0, recoveryDispatchFailures, requestId: options.requestId });
    if (newFindings.length > 0 || options.emitWhenClean === true) {
      await hatchet.events.push(HATCHET_EVENTS.RECONCILIATION_DETECTED, {
        requestId: options.requestId || null, checkedJobs: jobs.length, sourceCounts, complete, findingCount: findings.length, findingWriteFailures: recorded.failedBatches || 0, findings: newFindings, clean: complete && findings.length === 0,
      }, { additionalMetadata: { source: 'reconciliation-worker', request_id: options.requestId || '' } });
    }
    return { checkedJobs: jobs.length, sourceCounts, complete, findingCount: findings.length, findings };
  },
});

/** Re-delivery reuses the original bridge job, branch, and PR; it never submits a new effect. */
function inspectJob(job: BridgeJob, events: BridgeEvent[], cutoff: number): ReconciliationFinding[] {
  const findings: ReconciliationFinding[] = [];
  const eventTypes = new Set(events.map((event) => event.event_type));
  if (events.length === 0) findings.push(finding(job, 'critical', 'bridge_job_without_events', 'bridge job has no append-only event history'));
  else if (!eventTypes.has('accepted')) findings.push(finding(job, 'warning', 'bridge_job_missing_accepted_event', 'bridge job event history is missing accepted'));
  if (ACTIVE_STATES.has(job.state) && Date.parse(job.updated_at) <= cutoff) findings.push(finding(job, 'warning', 'stale_active_bridge_job', `bridge job has been ${job.state} longer than the stale threshold`));
  if (job.requested_action === 'create_draft_pr' || job.requested_action === 'repair_draft_pr') {
    const repo = job.payload.repo;
    if (job.state === 'running' && (typeof repo !== 'string' || !repo || !job.branch)) findings.push(finding(job, 'critical', 'running_create_draft_pr_missing_repo_or_branch', 'running create_draft_pr job is missing repo or deterministic branch'));
    if (job.state === 'effect_committed' && !job.pr_url) findings.push(finding(job, 'critical', 'effect_committed_without_pr_url', 'effect_committed create_draft_pr job is missing pr_url'));
    if (job.state === 'verification_pending' && !job.pr_url) findings.push(finding(job, 'critical', 'verification_pending_without_pr_url', 'verification_pending create_draft_pr job is missing pr_url'));
    if (job.state === 'completed' && !job.pr_url) findings.push(finding(job, 'critical', 'completed_create_draft_pr_without_pr_url', 'completed create_draft_pr job is missing pr_url'));
    if (job.state === 'ready_for_human_review' && !job.pr_url) findings.push(finding(job, 'critical', 'ready_for_human_review_without_pr_url', 'verified ready create_draft_pr job is missing pr_url'));
  }
  if (job.state === 'failed' && (job.pr_url || job.commit_sha) && !eventTypes.has('transition:effect_committed')) findings.push(finding(job, 'critical', 'failed_job_has_external_effect_refs', 'failed bridge job has external references without a committed-effect event'));
  return findings;
}

export function findDuplicateRefs(jobs: BridgeJob[]): ReconciliationFinding[] {
  const findings: ReconciliationFinding[] = [];
  const branchOwners = new Map<string, BridgeJob>(); const prOwners = new Map<string, BridgeJob>();
  for (const job of jobs) {
    if (job.state === 'failed') continue;
    if (job.branch) { const owner = branchOwners.get(job.branch); if (owner && owner.job_id !== job.job_id) findings.push(finding(job, 'critical', 'duplicate_bridge_branch', `branch is also recorded by bridge job ${owner.job_id}`, expectedJob(job), { branch: job.branch, otherJobId: owner.job_id })); else branchOwners.set(job.branch, job); }
    if (job.pr_url) { const owner = prOwners.get(job.pr_url); if (owner && owner.job_id !== job.job_id) findings.push(finding(job, 'critical', 'duplicate_bridge_pr_url', `pr_url is also recorded by bridge job ${owner.job_id}`, expectedJob(job), { prUrl: job.pr_url, otherJobId: owner.job_id })); else prOwners.set(job.pr_url, job); }
  }
  return findings;
}

function boundedInteger(value: number | undefined, min: number, max: number, fallback: number): number { return typeof value !== 'number' || !Number.isFinite(value) ? fallback : Math.min(max, Math.max(min, Math.floor(value))); }

async function providerLookups(job: BridgeJob, client: IngressObservationClient): Promise<{ github?: ObservationLookup; linear?: ObservationLookup; hatchet?: HatchetRunLookup }> {
  const issue = typeof job.payload.linear_issue === 'string' && job.payload.linear_issue.trim() ? job.payload.linear_issue.trim() : null;
  const match = job.pr_url && /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)$/.exec(job.pr_url);
  const [hatchetLookup, linear, github] = await Promise.all([
    job.hatchet_workflow_run_id ? observeHatchetRun(hatchet.runs, job.hatchet_workflow_run_id) : Promise.resolve(undefined),
    issue ? client.latest('linear', issue) : Promise.resolve(undefined),
    match && job.payload.dry_run !== true ? client.latest('github', `${match[1]}#${match[2]}`) : Promise.resolve(undefined),
  ]);
  return { hatchet: hatchetLookup, linear, github };
}

async function mapBounded<T, R>(items: T[], concurrency: number, work: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = []; let index = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => { while (index < items.length) { const current = index++; results[current] = await work(items[current]); } });
  await Promise.all(workers); return results;
}
