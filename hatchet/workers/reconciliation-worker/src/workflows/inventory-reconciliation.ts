import type { BridgeJob, ReconciliationCorrelations } from '@driftward/hatchet-worker-shared';
import type { ObservationInventoryLookup } from '../ingress-observation-client';
import type { HatchetRunInventoryLookup } from '../hatchet-run-observer';
import { expectedJob, finding, scopedFinding, type ReconciliationFinding } from './cross-provider-reconciliation';

export function inventoryCorrelationQueries(ingress: ObservationInventoryLookup, hatchet: HatchetRunInventoryLookup): ReconciliationCorrelations {
  const observations = ingress.status === 'matched' ? ingress.observations : [];
  const runs = hatchet.status === 'matched' ? hatchet.runs : [];
  return {
    source_event_keys: [...new Set(observations.map((observation) => observation.eventKey))],
    legacy_work_items: [...new Set(observations.filter((observation) => observation.source === 'linear').map((observation) => observation.subject))],
    hatchet_run_ids: [...new Set(runs.flatMap((run) => [run.workflowRunId, run.taskExternalId]))],
  };
}

export function inspectIngressInventory(inventory: ObservationInventoryLookup, jobs: BridgeJob[], cutoff: number, correlations?: ReconciliationCorrelations): ReconciliationFinding[] {
  if (inventory.status !== 'matched') return [systemFinding('ingress_inventory_unavailable', 'sanitized ingress inventory is unavailable', { source: 'ingress' }, { status: inventory.status })];
  const findings: ReconciliationFinding[] = [];
  if (!inventory.complete) findings.push(systemFinding('ingress_inventory_incomplete', 'ingress inventory reached its safe limit', { source: 'ingress' }, { scanned: inventory.observations.length }));
  const consumedEventKeys = new Set(correlations?.source_event_keys ?? jobs.flatMap((job) => sourceEventKeys(job)));
  const legacyIssues = new Set(correlations?.legacy_work_items ?? jobs.filter((job) => sourceEventKeys(job).length === 0).map((job) => typeof job.payload.linear_issue === 'string' ? job.payload.linear_issue : '').filter(Boolean));
  for (const observation of inventory.observations) {
    const subject = { jobId: null, subject: `ingress:${observation.eventKey}` };
    const expected = { source: observation.source, subject: observation.subject, published: true };
    if (!observation.hatchetPublished) findings.push(scopedFinding(subject, 'critical', 'ingress_stored_not_published', 'ingress event was stored but was not published to Hatchet', expected, { published: false, decision: observation.decision, reasonCode: observation.reasonCode }));
    const oldEnough = Date.parse(observation.providerUpdatedAt || observation.observedAt) <= cutoff;
    const consumed = consumedEventKeys.has(observation.eventKey)
      || legacyIssues.has(observation.subject);
    if (observation.source === 'linear' && observation.hatchetPublished && observation.linear?.dispatchEligible && oldEnough && !consumed) {
      findings.push(scopedFinding(subject, 'warning', 'ingress_published_unconsumed', 'eligible Linear ingress event has no correlated bridge job after grace period', { ...expected, bridgeJob: true }, { published: true, bridgeJob: false, dispatchEligible: true }));
    }
  }
  return findings;
}

function sourceEventKeys(job: BridgeJob): string[] {
  const values = job.payload.source_event_keys;
  if (Array.isArray(values)) return values.filter((value): value is string => typeof value === 'string');
  return typeof job.payload.source_event_key === 'string' ? [job.payload.source_event_key] : [];
}

export function inspectHatchetInventory(inventory: HatchetRunInventoryLookup, jobs: BridgeJob[], cutoff: number, correlations?: ReconciliationCorrelations): ReconciliationFinding[] {
  if (inventory.status !== 'matched') return [systemFinding('hatchet_inventory_unavailable', 'Hatchet run inventory is unavailable', { source: 'hatchet' }, { status: inventory.status })];
  const findings: ReconciliationFinding[] = [];
  if (!inventory.complete) findings.push(systemFinding('hatchet_inventory_incomplete', 'Hatchet run inventory reached its safe limit', { source: 'hatchet' }, { scanned: inventory.runs.length }));
  const correlated = new Set(correlations?.hatchet_run_ids ?? jobs.flatMap((job) => [job.hatchet_workflow_run_id, job.hatchet_step_run_id]).filter((id): id is string => Boolean(id)));
  for (const run of inventory.runs) {
    if (correlated.has(run.workflowRunId) || correlated.has(run.taskExternalId) || (run.insertedAt && Date.parse(run.insertedAt) > cutoff)) continue;
    findings.push(scopedFinding({ jobId: null, subject: `hatchet:${run.workflowRunId}:${run.taskExternalId}` }, 'warning', 'hatchet_run_without_bridge', 'Hatchet run has no correlated bridge job after grace period', { bridgeJob: true }, { workflowRunId: run.workflowRunId, taskExternalId: run.taskExternalId, status: run.status, workflowName: run.workflowName }));
  }
  return findings;
}

export function incompleteBridgeScan(total: number, scanned: number, limit: number, coverage?: { complete: boolean; scope?: string }): ReconciliationFinding | null {
  return total > scanned || coverage?.complete === false
    ? systemFinding('reconciliation_scan_incomplete', 'bridge inventory is not completely covered by this bounded scan', { total }, { scanned, limit, ...coverage })
    : null;
}

export function bridgeEventHistoryIncomplete(job: BridgeJob, total: number, scanned: number, limit: number): ReconciliationFinding | null {
  return total > scanned
    ? finding(job, 'warning', 'bridge_job_event_history_incomplete', 'bridge job event history exceeded the bounded read limit', expectedJob(job), { eventTotal: total, eventCount: scanned, limit })
    : null;
}

function systemFinding(kind: string, reason: string, expected: Record<string, unknown>, observed: Record<string, unknown>): ReconciliationFinding {
  return scopedFinding({ jobId: null, subject: 'reconciliation:system' }, 'warning', kind, reason, expected, observed);
}
