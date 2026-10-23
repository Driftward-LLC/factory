import assert from 'node:assert/strict';
import test from 'node:test';
import { HATCHET_EVENTS, type BridgeJob, type ReconciliationCorrelations, type ReconciliationScanPage } from '@driftward/hatchet-worker-shared';
import { inspectBridgeLedger } from './bridge-ledger-inspection';

type Dependencies = Parameters<typeof inspectBridgeLedger>[1];

test('default scan budgets prioritize 200 active jobs and reserve 50 for history', async () => {
  const fixture = harness();
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(fixture.scans, [['active', 200], ['history', 50]]);
  assert.equal(result.complete, true);
  assert.equal(result.findingCount, 0);
  assert.deepEqual(fixture.calls.filter((call) => call.startsWith('ack:')), ['ack:active', 'ack:history']);
});

test('a one-job budget reports unknown historical coverage instead of a clean scan', async () => {
  const fixture = harness();
  const result = await inspectBridgeLedger({ maxJobs: 1 }, fixture.deps);
  assert.deepEqual(fixture.scans, [['active', 1]]);
  assert.equal(result.coverage.history.total, null);
  assert.equal(result.complete, false);
  assert.deepEqual(result.findings.map((item) => item.kind), ['reconciliation_scan_incomplete']);
  assert.equal(fixture.events[0]?.data.clean, false);
});

test('finishing a tail page does not imply complete coverage of the whole ledger', async () => {
  const fixture = harness([job()]);
  fixture.pages.active.complete = false;
  fixture.pages.active.sweep_complete = true;
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.equal(result.checkedJobs, 1);
  assert.equal(result.coverage.active.sweepComplete, true);
  assert.equal(result.complete, false);
  assert.ok(result.findings.some((item) => item.kind === 'reconciliation_scan_incomplete'));
});

test('500 run and task identities are checked against the full ledger in 250-ID batches', async () => {
  const fixture = harness();
  fixture.deps.runs.list = async () => Array.from({ length: 250 }, (_, i) => ({ workflowRunExternalId: `run-${i}`, taskExternalId: `task-${i}`, taskInsertedAt: '2026-01-01T00:00:00Z' }));
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(fixture.queries.map((query) => query.hatchet_run_ids.length), [250, 250]);
  assert.equal(new Set(fixture.queries.flatMap((query) => query.hatchet_run_ids)).size, 500);
  assert.equal(result.findings.some((item) => item.kind === 'hatchet_run_without_bridge'), false);
  assert.equal(result.findings.some((item) => item.kind === 'hatchet_inventory_incomplete'), true);
});

test('ingress events correlated outside the current bridge pages are not falsely unconsumed', async () => {
  const fixture = harness();
  fixture.deps.observations.inventory = async () => ({ status: 'matched', complete: true, observations: [{ eventKey: 'outside-page', source: 'linear', subject: 'ENG-1', observedAt: '2026-01-01T00:00:00Z', hatchetPublished: true, linear: { dispatchEligible: true } }] });
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(fixture.queries[0].source_event_keys, ['outside-page']);
  assert.equal(result.findingCount, 0);
});

test('incomplete history cannot establish absent accepted or committed-effect events', async () => {
  const fixture = harness([], [job({ state: 'failed', pr_url: 'https://github.com/Driftward-LLC/factory/pull/1', commit_sha: 'abc123' })]);
  fixture.deps.bridge.listEvents = async () => ({ total: 101, events: [] });
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(result.findings.map((item) => item.kind), ['bridge_job_event_history_incomplete']);
  assert.equal(result.complete, false);
});

test('full-ledger duplicate-reference evidence finds an owner outside the current page', async () => {
  const fixture = harness([], [job({ branch: 'codex/duplicate', pr_url: 'https://github.com/Driftward-LLC/factory/pull/1', reconciliation_duplicate_refs: { branch_job_id: 'outside-branch-owner', pr_job_id: 'outside-pr-owner' } })]);
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(result.findings.map((item) => item.kind), ['duplicate_bridge_branch', 'duplicate_bridge_pr_url']);
  assert.equal(result.findings[0].observed.otherJobId, 'outside-branch-owner');
  assert.equal(result.findings[1].observed.otherJobId, 'outside-pr-owner');
});

for (const failure of ['listEvents', 'correlations', 'findingWrites', 'recovery', 'eventPush'] as const) {
  test(`${failure} failure retains scan pages without acknowledging progress`, async () => {
    const fixture = harness([job({ state: 'verification_pending', requested_action: 'create_draft_pr', payload: { dry_run: false }, pr_url: 'https://github.com/Driftward-LLC/factory/pull/1', hermes_session_id: 'session' })]);
    const fail = async (): Promise<never> => { throw new Error('forced failure'); };
    if (failure === 'listEvents') fixture.deps.bridge.listEvents = fail;
    if (failure === 'correlations') {
      fixture.deps.runs.list = async () => [{ workflowRunExternalId: 'run-1', taskExternalId: 'task-1' }];
      fixture.deps.bridge.reconciliationCorrelations = fail;
    }
    if (failure === 'findingWrites') fixture.deps.bridge.recordReconciliationFindings = fail;
    if (failure === 'recovery') fixture.deps.bridge.enqueueVerificationResume = fail;
    if (failure === 'eventPush') fixture.deps.pushEvent = fail;
    await assert.rejects(inspectBridgeLedger({}, fixture.deps));
    assert.deepEqual(fixture.calls.filter((call) => call.startsWith('ack:')), []);
  });
}

test('both cohorts advance only after findings persist and the event publishes', async () => {
  const fixture = harness([job({ state: 'accepted' })]);
  await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(fixture.calls, ['persist', `event:${HATCHET_EVENTS.RECONCILIATION_DETECTED}`, 'ack:active', 'ack:history']);
});

test('a retained page republishes already-persisted findings after an event delivery failure', async () => {
  const fixture = harness([job({ state: 'accepted' })]);
  const stored = new Set<string>();
  fixture.deps.bridge.recordReconciliationFindings = async (findings) => {
    fixture.calls.push('persist');
    const keys = findings.map((finding) => finding.finding_key);
    const created_finding_keys = keys.filter((key) => !stored.has(key));
    const existing_finding_keys = keys.filter((key) => stored.has(key));
    keys.forEach((key) => stored.add(key));
    return { created_finding_keys, existing_finding_keys };
  };
  const push = fixture.deps.pushEvent;
  fixture.deps.pushEvent = async () => { throw new Error('lost delivery'); };
  await assert.rejects(inspectBridgeLedger({}, fixture.deps), /lost delivery/);
  assert.equal(stored.size, 1);
  assert.deepEqual(fixture.calls, ['persist']);

  fixture.pages.active.replayed = true;
  fixture.pages.history.replayed = true;
  fixture.deps.pushEvent = push;
  await inspectBridgeLedger({}, fixture.deps);
  assert.deepEqual(fixture.calls, ['persist', 'persist', `event:${HATCHET_EVENTS.RECONCILIATION_DETECTED}`, 'ack:active', 'ack:history']);
  const payload = fixture.events[0].data;
  assert.equal(payload.newFindingCount, 0);
  assert.ok(Array.isArray(payload.findings));
  assert.equal(payload.findings.length, 1);
  assert.equal(payload.findings[0].findingKey, [...stored][0]);
});

test('recovery is queued before progress advances without publishing a direct accepted event', async () => {
  const fixture = harness([job({ state: 'verification_pending', requested_action: 'create_draft_pr', payload: { dry_run: false }, pr_url: 'https://github.com/Driftward-LLC/factory/pull/1', hermes_session_id: 'session' })]);
  fixture.deps.bridge.enqueueVerificationResume = async (ref, staleAfterMinutes) => {
    assert.equal(ref, 'sample');
    assert.equal(staleAfterMinutes, 45);
    fixture.calls.push('enqueue');
    return { enqueued: true, reason: 'enqueued' };
  };
  const result = await inspectBridgeLedger({ staleAfterMinutes: 45 }, fixture.deps);
  assert.equal(result.recovery.enqueued, 1);
  assert.deepEqual(fixture.calls, ['persist', 'enqueue', `event:${HATCHET_EVENTS.RECONCILIATION_DETECTED}`, 'ack:active', 'ack:history']);
  assert.equal(fixture.events.some((item) => item.event === HATCHET_EVENTS.HERMES_JOB_ACCEPTED), false);
});

test('execution receipt recovery dispatch failure retains scan progress, then retries the same job', async () => {
  const fixture = harness([job({ state: 'running', requested_action: 'create_draft_pr', payload: { dry_run: false, repo: 'example/project' }, branch: 'codex/test' })]);
  fixture.deps.bridge.enqueueExecutionReceiptRecovery = async () => { throw new Error('outbox unavailable'); };
  await assert.rejects(inspectBridgeLedger({}, fixture.deps), /writes failed/);
  assert.equal(fixture.calls.some((call) => call.startsWith('ack:')), false);
  fixture.deps.bridge.enqueueExecutionReceiptRecovery = async (id) => {
    assert.equal(id, 'sample');
    return { enqueued: true, reason: 'enqueued' };
  };
  const result = await inspectBridgeLedger({}, fixture.deps);
  assert.equal(result.executionRecovery.enqueued, 1);
  assert.deepEqual(fixture.calls.slice(-2), ['ack:active', 'ack:history']);
});

function harness(active: BridgeJob[] = [], history: BridgeJob[] = []) {
  const calls: string[] = [];
  const scans: Array<[string, number]> = [];
  const queries: ReconciliationCorrelations[] = [];
  const events: Array<{ event: string; data: Record<string, unknown> }> = [];
  const pages: Record<'active' | 'history', ReconciliationScanPage> = {
    active: { cohort: 'active', token: 'active-token', jobs: active, total: active.length, complete: true, sweep_complete: true, replayed: false },
    history: { cohort: 'history', token: 'history-token', jobs: history, total: history.length, complete: true, sweep_complete: true, replayed: false },
  };
  const deps: Dependencies = {
    bridge: {
      scanReconciliation: async (cohort, limit) => { scans.push([cohort, limit]); return pages[cohort]; },
      acknowledgeReconciliationScan: async (cohort) => { calls.push(`ack:${cohort}`); return { ok: true }; },
      reconciliationCorrelations: async (query) => { queries.push(query); return query; },
      listEvents: async (ref) => ({ total: 1, events: [{ event_id: 'accepted', job_id: ref, event_type: 'accepted', state: 'accepted', details: { hermes_session_id: 'session' }, created_at: '2026-01-01T00:00:00Z' }] }),
      recordReconciliationFindings: async (findings) => { calls.push('persist'); return { created_finding_keys: findings.map((item) => item.finding_key), existing_finding_keys: [] }; },
      enqueueVerificationResume: async () => ({ enqueued: false, reason: 'lease_active' }),
      enqueueExecutionReceiptRecovery: async () => ({ enqueued: false, reason: 'not_eligible' }),
    },
    observations: { inventory: async () => ({ status: 'matched', complete: true, observations: [] }), latest: async () => ({ status: 'unavailable' }) },
    runs: { list: async () => [], getDetails: async () => ({ status: 'RUNNING', taskRuns: {} }) },
    pushEvent: async (event, data) => { calls.push(`event:${event}`); events.push({ event, data }); },
    log: () => undefined,
  };
  return { deps, pages, calls, scans, queries, events };
}

function job(overrides: Partial<BridgeJob> = {}): BridgeJob {
  return {
    job_id: 'sample', idempotency_key: 'sample', state: 'completed', requested_action: 'inspect_draft_pr_state',
    payload: { dry_run: true }, payload_hash: 'hash', branch: null, commit_sha: null, pr_url: null, hermes_session_id: null,
    hatchet_workflow_run_id: null, hatchet_step_run_id: null, verification_context: null,
    verification_observed_context: null, verification_receipt: null, verification_digest: null,
    reason_code: null, error: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...overrides,
  };
}
