import assert from 'node:assert/strict';
import test from 'node:test';
import type { BridgeJob, ReconciliationCorrelations } from '@driftward/hatchet-worker-shared';
import type { ObservationInventoryLookup, ProviderObservation } from '../ingress-observation-client';
import type { HatchetRunInventoryLookup } from '../hatchet-run-observer';
import { incompleteBridgeScan, inspectHatchetInventory, inspectIngressInventory, inventoryCorrelationQueries } from './inventory-reconciliation';

const CUTOFF = Date.parse('2026-08-13T11:30:00Z');
const NO_MATCHES: ReconciliationCorrelations = { source_event_keys: [], legacy_work_items: [], hatchet_run_ids: [] };

test('full-ledger ingress matches find consumed events and legacy jobs outside the scanned page', () => {
  const inventory: ObservationInventoryLookup = {
    status: 'matched', complete: false,
    observations: [observation('correlated', 'ENG-1'), observation('legacy', 'ENG-2'), observation('orphan', 'ENG-3')],
  };
  const matches = { ...NO_MATCHES, source_event_keys: ['correlated'], legacy_work_items: ['ENG-2'] };
  const findings = inspectIngressInventory(inventory, [], CUTOFF, matches);
  assert.deepEqual(findings.map((item) => item.kind), ['ingress_inventory_incomplete', 'ingress_published_unconsumed']);
  assert.equal(findings[1].subject, 'ingress:orphan');
  assert.equal(inspectIngressInventory(inventory, [], CUTOFF).length, 4);
});

test('authoritative ingress matches do not use the page sample as their source of truth', () => {
  const inventory: ObservationInventoryLookup = { status: 'matched', complete: true, observations: [observation('correlated', 'ENG-1')] };
  const sample = [job({ source_event_key: 'correlated', linear_issue: 'ENG-1' })];
  assert.deepEqual(inspectIngressInventory(inventory, sample, CUTOFF), []);
  assert.equal(inspectIngressInventory(inventory, sample, CUTOFF, NO_MATCHES)[0]?.kind, 'ingress_published_unconsumed');
});

test('full-ledger Hatchet correlation accepts either run or task IDs from another bridge page', () => {
  const inventory: HatchetRunInventoryLookup = {
    status: 'matched', complete: false,
    runs: ['workflow-match', 'task-match', 'orphan'].map((id) => ({ workflowRunId: `run-${id}`, taskExternalId: `task-${id}`, insertedAt: '2026-08-13T10:00:00Z' })),
  };
  const findings = inspectHatchetInventory(inventory, [], CUTOFF, { ...NO_MATCHES, hatchet_run_ids: ['run-workflow-match', 'task-task-match'] });
  assert.deepEqual(findings.map((item) => item.kind), ['hatchet_inventory_incomplete', 'hatchet_run_without_bridge']);
  assert.equal(findings[1].subject, 'hatchet:run-orphan:task-orphan');
  assert.equal(inspectHatchetInventory(inventory, [], CUTOFF).length, 4);
});

test('authoritative matches preserve publication drift and grace periods', () => {
  const unpublished = { ...observation('stored', 'ENG-1'), hatchetPublished: false };
  const fresh = { ...observation('fresh', 'ENG-2'), observedAt: '2026-08-13T11:59:00Z' };
  const findings = inspectIngressInventory({ status: 'matched', complete: true, observations: [unpublished, fresh] }, [], CUTOFF, NO_MATCHES);
  assert.deepEqual(findings.map((item) => item.kind), ['ingress_stored_not_published']);
  assert.deepEqual(inspectHatchetInventory({ status: 'matched', complete: true, runs: [{ workflowRunId: 'fresh', taskExternalId: 'task-fresh', insertedAt: fresh.observedAt }] }, [], CUTOFF, NO_MATCHES), []);
});

test('correlation queries are deduplicated without truncating 500 Hatchet identities', () => {
  const ingress: ObservationInventoryLookup = { status: 'matched', complete: true, observations: [observation('one', 'ENG-1'), observation('one', 'ENG-1'), observation('two', 'ENG-1'), { ...observation('github', 'repo#1'), source: 'github' }] };
  const runs = Array.from({ length: 250 }, (_, i) => ({ workflowRunId: `run-${i}`, taskExternalId: `task-${i}` }));
  const queries = inventoryCorrelationQueries(ingress, { status: 'matched', complete: false, runs: [...runs, runs[0]] });
  assert.deepEqual(queries.source_event_keys, ['one', 'two', 'github']);
  assert.deepEqual(queries.legacy_work_items, ['ENG-1']);
  assert.equal(queries.hatchet_run_ids.length, 500);
  assert.ok(queries.hatchet_run_ids.includes('task-249'));
  assert.deepEqual(inventoryCorrelationQueries({ status: 'unavailable' }, { status: 'auth' }), NO_MATCHES);
});

test('a tail page cannot report complete global coverage just because it fits the limit', () => {
  const finding = incompleteBridgeScan(3, 3, 250, { complete: false, scope: 'history' });
  assert.equal(finding?.kind, 'reconciliation_scan_incomplete');
  assert.equal(finding?.observed.scope, 'history');
  assert.equal(finding?.observed.complete, false);
  assert.equal(incompleteBridgeScan(0, 0, 250, { complete: false })?.kind, 'reconciliation_scan_incomplete');
  assert.equal(incompleteBridgeScan(3, 3, 250, { complete: true }), null);
  assert.equal(incompleteBridgeScan(3, 3, 250), null);
  assert.equal(incompleteBridgeScan(4, 3, 250, { complete: true })?.kind, 'reconciliation_scan_incomplete');
});

function observation(eventKey: string, subject: string): ProviderObservation {
  return { eventKey, source: 'linear', subject, observedAt: '2026-08-13T10:00:00Z', hatchetPublished: true, linear: { dispatchEligible: true } };
}

function job(payload: BridgeJob['payload']): BridgeJob {
  return {
    job_id: 'sample', idempotency_key: 'sample', state: 'completed', requested_action: 'create_draft_pr',
    payload, payload_hash: 'hash', branch: null, commit_sha: null, pr_url: null, hermes_session_id: null,
    hatchet_workflow_run_id: 'run-sample', hatchet_step_run_id: 'task-sample', verification_context: null,
    verification_observed_context: null, verification_receipt: null, verification_digest: null,
    reason_code: null, error: null, created_at: '2026-08-13T09:00:00Z', updated_at: '2026-08-13T10:00:00Z',
  };
}
