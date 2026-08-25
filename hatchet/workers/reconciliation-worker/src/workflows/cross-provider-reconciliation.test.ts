import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { createInterface } from 'node:readline';
import path from 'node:path';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { HATCHET_CONTRACT } from '@driftward/hatchet-worker-shared';
import { IngressObservationClient, isValidObservationTimestamp } from '../ingress-observation-client';
import { compareCrossProvider, RECONCILIATION_KINDS } from './cross-provider-reconciliation';
import { bridgeEventHistoryIncomplete, inspectHatchetInventory, inspectIngressInventory } from './inventory-reconciliation';

const NOW = Date.parse('2026-08-13T12:00:00Z');
const CUTOFF = NOW - 30 * 60 * 1000;

test('reports a GitHub branch mismatch from a sanitized external observation', () => {
  const job = completedRealJob();
  const findings = compareCrossProvider(job, hermesEvents(job), {
    github: {
      status: 'matched',
      observation: githubObservation({ headRefName: 'codex/unexpected' }),
    },
    linear: { status: 'matched', observation: linearObservation({ status: 'In Progress' }) },
  }, CUTOFF);

  assert.deepEqual(findings.map((finding) => finding.kind), ['github_pr_branch_mismatch']);
  assert.match(findings[0].findingKey, /^reconciliation:v1:github_pr_branch_mismatch:/);
});

test('reports terminal Linear state while a bridge job remains active', () => {
  const job = activeRealJob();
  const findings = compareCrossProvider(job, hermesEvents(job), {
    linear: { status: 'matched', observation: linearObservation({ status: 'Blocked', blockedByCount: 1 }) },
  }, CUTOFF);

  assert.deepEqual(
    findings.map((finding) => finding.kind),
    ['linear_terminal_issue_with_active_bridge_job', 'linear_issue_blocked_with_active_bridge_job'],
  );
});

test('reports an observation lookup failure without throwing or transitioning bridge state', () => {
  const job = completedRealJob();
  const findings = compareCrossProvider(job, hermesEvents(job), {
    github: { status: 'unavailable' },
  }, CUTOFF);

  assert.equal(findings[0].kind, 'github_observation_unavailable');
  assert.equal(findings[0].severity, 'warning');
});

test('waits for the grace period before reporting a missing GitHub observation', () => {
  const fresh = completedRealJob({ updated_at: '2026-08-13T11:55:00Z' });
  const stale = completedRealJob({ updated_at: '2026-08-13T10:00:00Z' });

  assert.equal(compareCrossProvider(fresh, hermesEvents(fresh), { github: { status: 'not_found' } }, CUTOFF).length, 0);
  assert.equal(
    compareCrossProvider(stale, hermesEvents(stale), { github: { status: 'not_found' } }, CUTOFF)[0].kind,
    'github_pr_observation_missing',
  );
});

test('requires a Hermes execution reference and its append-only bridge event', () => {
  const missingReference = completedRealJob({ hermes_session_id: null });
  const absentEvent = completedRealJob();
  const missingReferenceFindings = compareCrossProvider(missingReference, [], {}, CUTOFF);
  const absentEventFindings = compareCrossProvider(absentEvent, [], {}, CUTOFF);

  assert.equal(missingReferenceFindings[0].kind, 'hermes_execution_ref_missing');
  assert.equal(absentEventFindings[0].kind, 'hermes_receipt_contradicts_bridge_history');
});

test('checks Hatchet provenance for jobs made by current workers', () => {
  const job = activeRealJob({ hatchet_workflow_run_id: null, hatchet_step_run_id: null });
  const findings = compareCrossProvider(job, hermesEvents(job), {}, CUTOFF);

  assert.equal(findings.find((finding) => finding.kind === 'hatchet_provenance_missing')?.severity, 'warning');
});

test('rejects non-loopback observation endpoints and safely reports unavailable ingress', async () => {
  assert.throws(() => new IngressObservationClient({ baseUrl: 'https://api.linear.app' }), /loopback/);
  const client = new IngressObservationClient({ baseUrl: 'http://127.0.0.1:1', timeoutMs: 100 });
  assert.deepEqual(await client.latest('linear', 'ENG-39'), { status: 'unavailable' });
});

test('classifies a delayed observation response as timeout', async (t) => {
  const server = createServer((request, response) => {
    const subject = new URL(request.url || '/', 'http://127.0.0.1').searchParams.get('subject') || '';
    const delay = subject === 'slow' ? 350 : 25;
    setTimeout(() => {
      if (!response.destroyed) {
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({
          status: 'matched',
          observation: {
            eventKey: `linear:issue:${subject}:update:test`, source: 'linear', subject,
            observedAt: '2026-08-13T12:00:00Z', providerUpdatedAt: '2026-08-13T12:00:00Z',
          },
        }));
      }
    }, delay);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  const client = new IngressObservationClient({ baseUrl: `http://127.0.0.1:${address.port}`, timeoutMs: 100 });
  const timeout = await client.latest('linear', 'slow');
  assert.deepEqual(timeout, { status: 'timeout' });
  const timeoutFindings = compareCrossProvider(completedRealJob(), hermesEvents(completedRealJob()), { linear: timeout }, CUTOFF);
  assert.equal(timeoutFindings[0].kind, 'linear_observation_timeout');
});

test('serializes overlapping reconciliation ingress reads through the real Python HTTPServer', async (t) => {
  const server = await startSerialIngressServer();
  t.after(() => server.close());

  const clients = [
    new IngressObservationClient({ baseUrl: server.baseUrl, timeoutMs: 250 }),
    new IngressObservationClient({ baseUrl: server.baseUrl, timeoutMs: 250 }),
  ];
  const startedAt = Date.now();
  const results = await Promise.all(
    clients.flatMap((client, index) => [
      client.latest('linear', `ENG-39-${index}-one`),
      client.latest('linear', `ENG-39-${index}-two`),
    ]),
  );
  const elapsedMs = Date.now() - startedAt;
  const stats = await (await fetch(server.baseUrl + '/test/stats')).json() as { maxActive: number; requestCount: number };

  assert.ok(results.every((result) => result.status === 'not_found'));
  assert.ok(elapsedMs >= 500, 'expected serialized reads to take at least 500ms, got ' + elapsedMs + 'ms');
  assert.deepEqual(stats, { maxActive: 1, requestCount: 4 });
});

test('rejects malformed provider timestamps before they can be treated as fresh', () => {
  assert.equal(isValidObservationTimestamp('not-a-timestamp'), false);
  assert.equal(isValidObservationTimestamp('2026-08-13T12:00:00Z'), true);
});

test('finding keys are stable for the same drift and change with observed branch or status', () => {
  const job = completedRealJob();
  const first = compareCrossProvider(job, hermesEvents(job), { github: { status: 'matched', observation: githubObservation({ headRefName: 'codex/one' }) } }, CUTOFF)[0];
  const retry = compareCrossProvider(job, hermesEvents(job), { github: { status: 'matched', observation: githubObservation({ headRefName: 'codex/one' }) } }, CUTOFF)[0];
  const changed = compareCrossProvider(job, hermesEvents(job), { github: { status: 'matched', observation: githubObservation({ headRefName: 'codex/two' }) } }, CUTOFF)[0];
  assert.equal(first.findingKey, retry.findingKey);
  assert.notEqual(first.findingKey, changed.findingKey);
});

test('the canonical contract taxonomy covers every emitted reconciliation kind', () => {
  const contract = HATCHET_CONTRACT as unknown as { reconciliation_kinds: Record<string, unknown> };
  assert.deepEqual([...RECONCILIATION_KINDS].sort(), Object.keys(contract.reconciliation_kinds).sort());
});

test('compares GitHub repository, commit, draft and Linear route eligibility', () => {
  const job = completedRealJob({
    state: 'running',
    payload: { repo: 'Driftward-LLC/driftward-host', linear_issue: 'ENG-39', dry_run: false, routing: { project_id: 'project-a' } },
  });
  const findings = compareCrossProvider(job, hermesEvents(job), {
    github: { status: 'matched', observation: githubObservation({ repo: 'Driftward-LLC/capykit', headSha: 'different', isDraft: false }) },
    linear: { status: 'matched', observation: linearObservation({ dispatchEligible: false, projectId: 'project-b' }) },
  }, CUTOFF);
  assert.deepEqual(findings.map((item) => item.kind), ['github_pr_repo_mismatch', 'github_pr_commit_mismatch', 'github_pr_not_draft', 'linear_issue_ineligible_with_active_bridge_job', 'linear_route_mismatch']);
});

test('detects ingress publication and consumption drift from the sanitized inventory', () => {
  const results = inspectIngressInventory({
    status: 'matched', complete: true,
    observations: [
      { eventKey: 'linear:issue:ENG-40:update:stored', source: 'linear', subject: 'ENG-40', observedAt: '2026-08-13T09:00:00Z', hatchetPublished: false },
      { eventKey: 'linear:issue:ENG-41:update:published', source: 'linear', subject: 'ENG-41', observedAt: '2026-08-13T09:00:00Z', hatchetPublished: true, linear: { dispatchEligible: true } },
    ],
  }, [], CUTOFF);
  assert.deepEqual(results.map((item) => item.kind), ['ingress_stored_not_published', 'ingress_published_unconsumed']);
  assert.equal(results[0].jobId, null);
});

test('reconciles ingress consumption by event key while preserving legacy issue fallback', () => {
  const first = { eventKey: 'linear:issue:ENG-40:update:first', source: 'linear' as const, subject: 'ENG-40', observedAt: '2026-08-13T09:00:00Z', hatchetPublished: true, linear: { dispatchEligible: true } };
  const second = { eventKey: 'linear:issue:ENG-40:update:second', source: 'linear' as const, subject: 'ENG-40', observedAt: '2026-08-13T08:00:00Z', hatchetPublished: true, linear: { dispatchEligible: true } };
  const correlated = completedRealJob({ payload: { repo: 'Driftward-LLC/driftward-host', linear_issue: 'ENG-40', dry_run: true, source_event_key: first.eventKey, source_event_keys: [first.eventKey] } });
  const findings = inspectIngressInventory({ status: 'matched', complete: true, observations: [first, second] }, [correlated], CUTOFF);
  assert.deepEqual(findings.map((item) => item.kind), ['ingress_published_unconsumed']);
  assert.equal(findings[0].subject, `ingress:${second.eventKey}`);
  const legacy = completedRealJob({ payload: { repo: 'Driftward-LLC/driftward-host', linear_issue: 'ENG-40', dry_run: true } });
  assert.equal(inspectIngressInventory({ status: 'matched', complete: true, observations: [first, second] }, [legacy], CUTOFF).length, 0);
});

test('detects orphan Hatchet runs and treats a full bounded page as incomplete', () => {
  const orphan = inspectHatchetInventory({
    status: 'matched', complete: false,
    runs: [{ workflowRunId: 'run-orphan', taskExternalId: 'task-orphan', workflowName: 'execution-dispatch-create-draft-pr', insertedAt: '2026-08-13T09:00:00Z' }],
  }, [], CUTOFF);
  assert.deepEqual(orphan.map((item) => item.kind), ['hatchet_inventory_incomplete', 'hatchet_run_without_bridge']);
  const unavailable = inspectHatchetInventory({ status: 'auth' }, [], CUTOFF);
  assert.equal(unavailable[0].kind, 'hatchet_inventory_unavailable');
});

test('marks truncated per-job event history incomplete', () => {
  assert.equal(bridgeEventHistoryIncomplete(completedRealJob(), 101, 100, 100)?.kind, 'bridge_job_event_history_incomplete');
  assert.equal(bridgeEventHistoryIncomplete(completedRealJob(), 100, 100, 100), null);
});

function completedRealJob(overrides: Partial<BridgeJob> = {}): BridgeJob {
  return {
    ...baseJob(),
    state: 'completed',
    pr_url: 'https://github.com/Driftward-LLC/driftward-host/pull/123',
    hermes_session_id: 'hermes-session-123',
    reason_code: 'hermes_created_completed',
    updated_at: '2026-08-13T10:00:00Z',
    ...overrides,
  };
}

function activeRealJob(overrides: Partial<BridgeJob> = {}): BridgeJob {
  return {
    ...baseJob(),
    state: 'running',
    updated_at: '2026-08-13T10:00:00Z',
    ...overrides,
  };
}

function baseJob(): BridgeJob {
  return {
    job_id: 'job-123',
    idempotency_key: 'hatchet:work-item:linear:ENG-39:create_draft_pr:real:active',
    state: 'accepted',
    requested_action: 'create_draft_pr',
    payload: {
      repo: 'Driftward-LLC/driftward-host',
      linear_issue: 'ENG-39',
      dry_run: false,
    },
    payload_hash: 'hash',
    branch: 'codex/hatchet-hermes-example',
    commit_sha: 'abc123',
    pr_url: null,
    hermes_session_id: null,
    hatchet_workflow_run_id: 'workflow-123',
    hatchet_step_run_id: 'step-123',
    verification_context: null,
    verification_observed_context: null,
    verification_receipt: null,
    verification_digest: null,
    reason_code: null,
    error: null,
    created_at: '2026-08-13T09:00:00Z',
    updated_at: '2026-08-13T09:00:00Z',
  };
}

function hermesEvents(job: BridgeJob) {
  return [{
    event_id: 'event-1',
    job_id: job.job_id,
    event_type: 'transition:effect_committed',
    state: 'effect_committed',
    details: { hermes_session_id: job.hermes_session_id || 'hermes-session-123' },
    created_at: '2026-08-13T09:30:00Z',
  }];
}

function githubObservation(overrides: { repo?: string; headRefName?: string; headSha?: string; isDraft?: boolean } = {}) {
  return {
    eventKey: 'github:pull_request:Driftward-LLC/driftward-host:123:created:delivery-1',
    source: 'github' as const,
    subject: 'Driftward-LLC/driftward-host#123',
    action: 'opened',
    observedAt: '2026-08-13T09:30:00Z',
    github: {
      repo: overrides.repo || 'Driftward-LLC/driftward-host',
      prUrl: 'https://github.com/Driftward-LLC/driftward-host/pull/123',
      state: 'open',
      merged: false,
      headRefName: overrides.headRefName || 'codex/hatchet-hermes-example',
      headSha: overrides.headSha || 'abc123',
      isDraft: overrides.isDraft ?? true,
      linearIssues: ['ENG-39'],
    },
  };
}

function linearObservation(overrides: { status?: string; blockedByCount?: number; dispatchEligible?: boolean; projectId?: string } = {}) {
  return {
    eventKey: 'linear:issue:ENG-39:update:delivery-1',
    source: 'linear' as const,
    subject: 'ENG-39',
    action: 'update',
    observedAt: '2026-08-13T09:30:00Z',
    linear: {
      identifier: 'ENG-39',
      projectId: overrides.projectId,
      status: overrides.status || 'Ready',
      blockedByCount: overrides.blockedByCount || 0,
      dispatchEligible: overrides.dispatchEligible ?? true,
    },
  };
}

const SERIAL_INGRESS_SERVER = String.raw`
import time
from http.server import HTTPServer

from hatchet_event_ingress.server import IngressHandler
from hatchet_event_ingress.store import connect

class DelayedIngressHandler(IngressHandler):
    active = 0
    max_active = 0
    request_count = 0

    def do_GET(self):
        if self.path == "/test/stats":
            self._write_json(200, {"maxActive": type(self).max_active, "requestCount": type(self).request_count})
            return
        if self.path.startswith("/observations/latest"):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            type(self).request_count += 1
            try:
                time.sleep(0.15)
                super().do_GET()
            finally:
                type(self).active -= 1
            return
        super().do_GET()

    def log_message(self, format, *args):
        pass

httpd = HTTPServer(("127.0.0.1", 0), DelayedIngressHandler)
httpd.conn = connect(":memory:")
print(httpd.server_port, flush=True)
httpd.serve_forever()
`;

async function startSerialIngressServer(): Promise<{ baseUrl: string; close(): Promise<void> }> {
  const ingressPath = path.resolve(__dirname, '../../../../event-ingress');
  const child = spawn('python3', ['-c', SERIAL_INGRESS_SERVER], {
    env: { ...process.env, PYTHONPATH: [ingressPath, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const port = await readServerPort(child);
  return {
    baseUrl: 'http://127.0.0.1:' + port,
    async close(): Promise<void> {
      if (!child.killed) {
        child.kill('SIGTERM');
        await once(child, 'exit');
      }
    },
  };
}

function readServerPort(child: ChildProcess): Promise<number> {
  return new Promise((resolve, reject) => {
    if (!child.stdout) {
      reject(new Error('serial ingress server has no stdout'));
      return;
    }
    const output = createInterface({ input: child.stdout });
    const onExit = (code: number | null) => reject(new Error('serial ingress server exited before ready (code ' + code + ')'));
    child.once('error', reject);
    child.once('exit', onExit);
    output.once('line', (line) => {
      const port = Number(line);
      output.close();
      child.removeListener('exit', onExit);
      if (!Number.isInteger(port) || port < 1) {
        reject(new Error('invalid serial ingress server port: ' + line));
        return;
      }
      resolve(port);
    });
  });
}
