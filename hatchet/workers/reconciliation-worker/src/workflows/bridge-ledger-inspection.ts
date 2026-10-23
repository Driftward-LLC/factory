import { type BridgeClient, HATCHET_EVENTS, type BridgeJob, type ReconciliationCorrelations } from '@driftward/hatchet-worker-shared';
import { IngressObservationClient, type ObservationLookup } from '../ingress-observation-client';
import { observeHatchetRun, observeHatchetRuns, type HatchetRunLookup } from '../hatchet-run-observer';
import { compareCrossProvider, expectedJob, finding, type ReconciliationFinding } from './cross-provider-reconciliation';
import { bridgeEventHistoryIncomplete, incompleteBridgeScan, inspectHatchetInventory, inspectIngressInventory, inventoryCorrelationQueries } from './inventory-reconciliation';
import { recordFindingBatches } from './finding-batches';
import type { BridgeStateReconciliationInput } from './reconciliation-input';
import { enqueueStaleVerification, enqueueStaleExecutionReceipts } from './verification-recovery';

type BridgeEvent = { event_id: string; job_id: string; event_type: string; state: string; details: Record<string, unknown>; created_at: string };

type Dependencies = {
  bridge: Pick<BridgeClient, 'scanReconciliation' | 'acknowledgeReconciliationScan' | 'reconciliationCorrelations' | 'listEvents' | 'recordReconciliationFindings' | 'enqueueVerificationResume' | 'enqueueExecutionReceiptRecovery'>;
  observations: Pick<IngressObservationClient, 'latest' | 'inventory'>;
  runs: Parameters<typeof observeHatchetRuns>[0];
  pushEvent: (event: string, data: Record<string, unknown>, options: Record<string, unknown>) => Promise<unknown>;
  log: (message: string, details: Record<string, unknown>) => void;
};

const ACTIVE_STATES = new Set(['accepted', 'running', 'effect_committed', 'verification_pending']);
const CONCURRENCY = 4;

/** Clients are supplied by the workflow wrapper, so the full scan can be tested without a live worker. */
export async function inspectBridgeLedger(options: BridgeStateReconciliationInput, { bridge, observations, runs, pushEvent, log }: Dependencies) {
  const maxJobs = boundedInteger(options.maxJobs, 1, 250, 250);
  const maxEventsPerJob = boundedInteger(options.maxEventsPerJob, 1, 250, 100);
  const staleAfterMinutes = boundedInteger(options.staleAfterMinutes, 1, 10080, 30);
  const cutoff = Date.now() - staleAfterMinutes * 60 * 1000;
  const historyLimit = maxJobs > 1 ? Math.max(1, Math.floor(maxJobs / 5)) : 0;
  const [active, history, ingressInventory, hatchetInventory] = await Promise.all([
    bridge.scanReconciliation('active', maxJobs - historyLimit),
    historyLimit ? bridge.scanReconciliation('history', historyLimit) : Promise.resolve(null),
    observations.inventory(maxJobs), observeHatchetRuns(runs, maxJobs),
  ]);
  const pages = history ? [active, history] : [active];
  const jobs = [...new Map(pages.flatMap((page) => page.jobs).map((job) => [job.job_id, job])).values()];
  const total = pages.reduce((sum, page) => sum + page.total, 0);
  const coverage = {
    active: { scanned: active.jobs.length, total: active.total, complete: active.complete, sweepComplete: active.sweep_complete },
    history: { scanned: history?.jobs.length || 0, total: history?.total ?? null, complete: history?.complete || false, sweepComplete: history?.sweep_complete || false },
  };
  // A failed authoritative lookup aborts, retaining the pending pages. It never proves absence.
  const query = inventoryCorrelationQueries(ingressInventory, hatchetInventory);
  const correlations: ReconciliationCorrelations = { source_event_keys: [], legacy_work_items: [], hatchet_run_ids: [] };
  const querySize = Math.max(...Object.values(query).map((values) => values.length));
  for (let offset = 0; offset < querySize; offset += 250) {
    const matched = await bridge.reconciliationCorrelations({ source_event_keys: query.source_event_keys.slice(offset, offset + 250), legacy_work_items: query.legacy_work_items.slice(offset, offset + 250), hatchet_run_ids: query.hatchet_run_ids.slice(offset, offset + 250) });
    for (const key of ['source_event_keys', 'legacy_work_items', 'hatchet_run_ids'] as const) correlations[key].push(...matched[key]);
  }
  const inspected = await mapBounded(jobs, CONCURRENCY, async (job) => {
    const { events, total: eventTotal } = await bridge.listEvents(job.job_id, maxEventsPerJob);
    const eventsComplete = eventTotal <= events.length;
    return { job, eventTotal, eventCount: events.length, findings: [...inspectJob(job, events, cutoff, eventsComplete), ...compareCrossProvider(job, events, await providerLookups(job, observations, runs), cutoff, eventsComplete)] };
  });
  const findings = inspected.flatMap((item) => {
    const eventFinding = bridgeEventHistoryIncomplete(item.job, item.eventTotal, item.eventCount, maxEventsPerJob);
    return eventFinding ? [...item.findings, eventFinding] : item.findings;
  });

  findings.push(...findDuplicateRefs(jobs));
  const bridgeComplete = coverage.active.complete && coverage.history.complete;
  const complete = bridgeComplete && inspected.every((item) => item.eventTotal <= item.eventCount) && ingressInventory.status === 'matched' && ingressInventory.complete && hatchetInventory.status === 'matched' && hatchetInventory.complete;
  const incompleteBridge = incompleteBridgeScan(total, jobs.length, maxJobs, { complete: bridgeComplete });
  if (incompleteBridge) findings.push(incompleteBridge);
  findings.push(...inspectIngressInventory(ingressInventory, jobs, cutoff, correlations));
  findings.push(...inspectHatchetInventory(hatchetInventory, jobs, cutoff, correlations));

  const recorded = findings.length > 0 ? await recordFindingBatches(bridge, findings.map((item) => ({
    finding_key: item.findingKey, job_id: item.jobId, subject: item.subject, kind: item.kind, severity: item.severity,
    details: { expected: item.expected, observed: item.observed },
  }))) : { created_finding_keys: [], existing_finding_keys: [], failedBatches: 0 };
  const created = new Set(recorded.created_finding_keys);
  const newFindings = findings.filter((item) => created.has(item.findingKey));
  // A previous attempt may have persisted findings but failed before publishing.
  // Pending-page replay is at-least-once; stable finding keys let consumers dedupe.
  const eventFindings = pages.some((page) => page.replayed) ? findings : newFindings;
  const jobById = new Map(jobs.map((job) => [job.job_id, job]));
  const recovery = await enqueueStaleVerification(findings, jobById, bridge, staleAfterMinutes);
  const executionRecovery = await enqueueStaleExecutionReceipts(findings, jobById, bridge, staleAfterMinutes);
  const sourceCounts = { bridge: jobs.length, ingress: ingressInventory.status === 'matched' ? ingressInventory.observations.length : 0, hatchet: hatchetInventory.status === 'matched' ? hatchetInventory.runs.length : 0 };

  log('Bridge reconciliation check completed', { ...sourceCounts, bridgeTotal: total, coverage, complete, findingCount: findings.length, newFindingCount: newFindings.length, findingWriteFailures: recorded.failedBatches || 0, recovery, requestId: options.requestId });
  if (recorded.failedBatches || recovery.failed || executionRecovery.failed) throw new Error('Reconciliation writes failed; scan progress retained for retry');
  if (eventFindings.length > 0 || options.emitWhenClean === true) {
    await pushEvent(HATCHET_EVENTS.RECONCILIATION_DETECTED, {
      requestId: options.requestId || null, checkedJobs: jobs.length, sourceCounts, coverage, complete, findingCount: findings.length, newFindingCount: newFindings.length, findingWriteFailures: recorded.failedBatches || 0, findings: eventFindings, clean: complete && findings.length === 0,
    }, { additionalMetadata: { source: 'reconciliation-worker', request_id: options.requestId || '' } });
  }
  // Never advance on inspection, persistence, recovery or event-delivery failure.
  for (const page of pages) await bridge.acknowledgeReconciliationScan(page.cohort, page.token);
  return { checkedJobs: jobs.length, sourceCounts, coverage, complete, recovery, executionRecovery, findingCount: findings.length, findings };
}

/** Re-delivery reuses the original bridge job, branch, and PR; it never submits a new effect. */
function inspectJob(job: BridgeJob, events: BridgeEvent[], cutoff: number, eventsComplete: boolean): ReconciliationFinding[] {
  const findings: ReconciliationFinding[] = [];
  const eventTypes = new Set(events.map((event) => event.event_type));
  if (eventsComplete && events.length === 0) findings.push(finding(job, 'critical', 'bridge_job_without_events', 'bridge job has no append-only event history'));
  else if (eventsComplete && !eventTypes.has('accepted')) findings.push(finding(job, 'warning', 'bridge_job_missing_accepted_event', 'bridge job event history is missing accepted'));
  if (ACTIVE_STATES.has(job.state) && Date.parse(job.updated_at) <= cutoff) findings.push(finding(job, 'warning', 'stale_active_bridge_job', `bridge job has been ${job.state} longer than the stale threshold`));
  if (job.requested_action === 'create_draft_pr' || job.requested_action === 'repair_draft_pr') {
    const repo = job.payload.repo;
    if (job.state === 'running' && (typeof repo !== 'string' || !repo || !job.branch)) findings.push(finding(job, 'critical', 'running_create_draft_pr_missing_repo_or_branch', 'running create_draft_pr job is missing repo or deterministic branch'));
    if (job.state === 'effect_committed' && !job.pr_url) findings.push(finding(job, 'critical', 'effect_committed_without_pr_url', 'effect_committed create_draft_pr job is missing pr_url'));
    if (job.state === 'verification_pending' && !job.pr_url) findings.push(finding(job, 'critical', 'verification_pending_without_pr_url', 'verification_pending create_draft_pr job is missing pr_url'));
    if (job.state === 'completed' && !job.pr_url) findings.push(finding(job, 'critical', 'completed_create_draft_pr_without_pr_url', 'completed create_draft_pr job is missing pr_url'));
    if (job.state === 'ready_for_human_review' && !job.pr_url) findings.push(finding(job, 'critical', 'ready_for_human_review_without_pr_url', 'verified ready create_draft_pr job is missing pr_url'));
  }
  if (eventsComplete && job.state === 'failed' && (job.pr_url || job.commit_sha) && !eventTypes.has('transition:effect_committed')) findings.push(finding(job, 'critical', 'failed_job_has_external_effect_refs', 'failed bridge job has external references without a committed-effect event'));
  return findings;
}

export function findDuplicateRefs(jobs: BridgeJob[]): ReconciliationFinding[] {
  const findings: ReconciliationFinding[] = [];
  const branchOwners = new Map<string, BridgeJob>(); const prOwners = new Map<string, BridgeJob>();
  for (const job of jobs) {
    if (job.state === 'failed') continue;
    const branchOwner = job.reconciliation_duplicate_refs?.branch_job_id || (job.branch ? branchOwners.get(job.branch)?.job_id : undefined);
    const prOwner = job.reconciliation_duplicate_refs?.pr_job_id || (job.pr_url ? prOwners.get(job.pr_url)?.job_id : undefined);
    if (job.branch && branchOwner && branchOwner !== job.job_id) findings.push(finding(job, 'critical', 'duplicate_bridge_branch', `branch is also recorded by bridge job ${branchOwner}`, expectedJob(job), { branch: job.branch, otherJobId: branchOwner }));
    if (job.pr_url && prOwner && prOwner !== job.job_id) findings.push(finding(job, 'critical', 'duplicate_bridge_pr_url', `pr_url is also recorded by bridge job ${prOwner}`, expectedJob(job), { prUrl: job.pr_url, otherJobId: prOwner }));
    if (job.branch && !branchOwners.has(job.branch)) branchOwners.set(job.branch, job);
    if (job.pr_url && !prOwners.has(job.pr_url)) prOwners.set(job.pr_url, job);
  }
  return findings;
}

function boundedInteger(value: number | undefined, min: number, max: number, fallback: number): number { return typeof value !== 'number' || !Number.isFinite(value) ? fallback : Math.min(max, Math.max(min, Math.floor(value))); }

async function providerLookups(job: BridgeJob, client: Dependencies['observations'], runs: Dependencies['runs']): Promise<{ github?: ObservationLookup; linear?: ObservationLookup; hatchet?: HatchetRunLookup }> {
  const issue = typeof job.payload.linear_issue === 'string' && job.payload.linear_issue.trim() ? job.payload.linear_issue.trim() : null;
  const match = job.pr_url && /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)$/.exec(job.pr_url);
  const [hatchetLookup, linear, github] = await Promise.all([
    job.hatchet_workflow_run_id ? observeHatchetRun(runs, job.hatchet_workflow_run_id) : Promise.resolve(undefined),
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
