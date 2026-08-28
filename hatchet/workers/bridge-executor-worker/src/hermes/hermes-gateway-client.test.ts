import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { NonRetryableBridgeExecutionError } from './errors';
import {
  checkHermesIdentityViaGrpc,
  hermesGrpcTarget,
  inspectGitHubHeadViaGrpc,
  inspectDraftPrStateViaGrpc,
  inspectDraftPullRequestViaGrpc,
  resolveHermesGatewayProtoPath,
  runHermesCreateDraftPrViaGrpc
} from './hermes-gateway-client';

type MockCall = grpc.ServerUnaryCall<Record<string, unknown>, Record<string, unknown>>;
type MockCallback = grpc.sendUnaryData<Record<string, unknown>>;

test('Hermes gRPC client speaks over a Unix domain socket', async () => {
  await withGateway(async (target) => {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;

    const identity = await checkHermesIdentityViaGrpc({
      repo: 'Driftward-LLC/capykit',
      expectedGithubLogin: 'factory-hermes[bot]',
      expectedGitAuthorName: 'factory-hermes[bot]',
      expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
      forbiddenGithubLogins: ['derek-nguyen'],
      forbiddenGitAuthorEmails: ['derek@driftward.io'],
      timeoutMs: 5000
    });
    assert.equal(identity.accessibleRepo, 'Driftward-LLC/capykit');

    const result = await runHermesCreateDraftPrViaGrpc(greenJob(), 5000);
    assert.equal(result.prUrl, 'https://github.com/Driftward-LLC/capykit/pull/28');
    assert.equal(result.validation && typeof result.validation === 'object' && !Array.isArray(result.validation), true);

    const observation = await inspectDraftPullRequestViaGrpc('Driftward-LLC/capykit', 28, 5000);
    assert.equal(observation.headRefName, 'codex/hermes-grpc-gateway');
    assert.equal(observation.statusCheckRollup?.[0]?.conclusion, 'SUCCESS');
    assert.equal(observation.diffText, 'diff --git a/README.md b/README.md');

    const state = await inspectDraftPrStateViaGrpc('Driftward-LLC/capykit', 'https://github.com/Driftward-LLC/capykit/pull/28', 5000);
    assert.equal(state.state, 'open');
    assert.equal(state.head_sha, 'abc123');

    const head = await inspectGitHubHeadViaGrpc('Driftward-LLC/capykit', 'codex/hermes-grpc-gateway', 5000);
    assert.equal(head.kind, 'github_branch_pr_absence_v1');
    assert.equal(head.branch_exists, false);
    assert.equal(head.pull_requests.length, 0);
  });
});

test('Hermes gRPC TCP targets fail closed until explicitly authorized', () => {
  const previous = snapshotEnv([
    'DRIFTWARD_HERMES_GRPC_TARGET',
    'DRIFTWARD_HERMES_GRPC_ALLOW_TCP',
    'DRIFTWARD_HERMES_GRPC_AUTH_TOKEN'
  ]);
  try {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = 'dns:///hermes.internal:50051';
    delete process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP;
    delete process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN;
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_disabled');

    process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP = '1';
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_auth_required');

    process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN = 'test-token';
    assert.equal(hermesGrpcTarget(), 'dns:///hermes.internal:50051');
  } finally {
    restoreEnv(previous);
  }
});

async function withGateway(fn: (target: string) => Promise<void>): Promise<void> {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-test-'));
  const socketPath = path.join(tmp, 'hermes.sock');
  const target = `unix://${socketPath}`;
  const previous = snapshotEnv(['DRIFTWARD_HERMES_GRPC_TARGET']);
  const server = new grpc.Server();
  server.addService(loadGatewayService().service, mockGateway());
  await bind(server, target);
  try {
    await fn(target);
  } finally {
    server.forceShutdown();
    restoreEnv(previous);
    rmSync(tmp, { recursive: true, force: true });
  }
}

function loadGatewayService(): grpc.ServiceClientConstructor & { service: grpc.ServiceDefinition } {
  const packageDefinition = protoLoader.loadSync(resolveHermesGatewayProtoPath(), {
    keepCase: false,
    longs: Number,
    enums: String,
    defaults: true,
    oneofs: true
  });
  const loaded = grpc.loadPackageDefinition(packageDefinition) as Record<string, unknown>;
  const service = (((loaded.driftward as Record<string, unknown>).factory as Record<string, unknown>).hermes as Record<string, unknown>).v1 as Record<string, unknown>;
  return service.HermesFactoryGateway as grpc.ServiceClientConstructor & { service: grpc.ServiceDefinition };
}

function mockGateway(): grpc.UntypedServiceImplementation {
  return {
    checkIdentity: (call: MockCall, callback: MockCallback) => {
      assert.equal(call.request.repo, 'Driftward-LLC/capykit');
      callback(null, {
        githubLogin: 'factory-hermes[bot]',
        gitAuthorName: 'factory-hermes[bot]',
        gitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
        accessibleRepo: 'Driftward-LLC/capykit'
      });
    },
    createDraftPullRequest: (call: MockCall, callback: MockCallback) => {
      assert.equal(call.request.bridgeJobId, 'job-28');
      assert.equal(call.request.idempotencyKey, 'hatchet:work-item:linear:ENG-28:create_draft_pr:real:active');
      assert.equal(call.request.branch, 'codex/hermes-grpc-gateway');
      callback(null, {
        status: 'created',
        prUrl: 'https://github.com/Driftward-LLC/capykit/pull/28',
        prNumber: 28,
        commitSha: 'abc123',
        branch: 'codex/hermes-grpc-gateway',
        hermesSessionId: 'session-28',
        summary: 'created draft PR through mock Hermes gateway',
        validationJson: JSON.stringify({ local_commands: [{ command: 'npm test', exit_code: 0 }], secret_check: { command: 'npm run check:secrets', exit_code: 0 } })
      });
    },
    inspectDraftPullRequest: (_call: MockCall, callback: MockCallback) => {
      callback(null, {
        headRefName: 'codex/hermes-grpc-gateway',
        baseRefName: 'main',
        isDraft: true,
        url: 'https://github.com/Driftward-LLC/capykit/pull/28',
        author: { login: 'factory-hermes[bot]' },
        body: 'Refs Linear ENG-28',
        files: [{ path: 'README.md' }],
        statusCheckRollup: [{ name: 'quality (ubuntu-latest)', conclusion: 'SUCCESS', state: '', status: '' }],
        headRefOid: 'abc123',
        diffText: 'diff --git a/README.md b/README.md'
      });
    },
    inspectPullRequestState: (_call: MockCall, callback: MockCallback) => {
      callback(null, {
        repository: 'Driftward-LLC/capykit',
        number: 28,
        url: 'https://github.com/Driftward-LLC/capykit/pull/28',
        state: 'open',
        merged: false,
        draft: true,
        headBranch: 'codex/hermes-grpc-gateway',
        headSha: 'abc123',
        baseRef: 'main',
        reason: 'ok'
      });
    },
    inspectGitHubHead: (_call: MockCall, callback: MockCallback) => {
      callback(null, {
        kind: 'github_branch_pr_absence_v1',
        repository: 'Driftward-LLC/capykit',
        headBranch: 'codex/hermes-grpc-gateway',
        branchExists: false,
        pullRequests: [],
        observedAt: '2026-08-28T00:00:00Z',
        reason: 'ok'
      });
    }
  };
}

function bind(server: grpc.Server, target: string): Promise<number> {
  return new Promise((resolve, reject) => {
    server.bindAsync(target, grpc.ServerCredentials.createInsecure(), (error, port) => {
      if (error) reject(error);
      else resolve(port);
    });
  });
}

function greenJob(): BridgeJob {
  return {
    job_id: 'job-28',
    idempotency_key: 'hatchet:work-item:linear:ENG-28:create_draft_pr:real:active',
    state: 'running',
    requested_action: 'create_draft_pr',
    payload: { repo: 'Driftward-LLC/capykit', dry_run: false },
    payload_hash: 'hash',
    branch: 'codex/hermes-grpc-gateway',
    commit_sha: null,
    pr_url: null,
    hermes_session_id: null,
    hatchet_workflow_run_id: 'run-28',
    hatchet_step_run_id: 'step-28',
    verification_context: null,
    verification_observed_context: null,
    verification_receipt: null,
    verification_digest: null,
    reason_code: null,
    error: null,
    created_at: '2026-08-27T00:00:00Z',
    updated_at: '2026-08-27T00:00:00Z'
  };
}

function assertFailure(fn: () => unknown, reasonCode: string): void {
  assert.throws(fn, (error: unknown) => error instanceof NonRetryableBridgeExecutionError && error.reasonCode === reasonCode);
}

function snapshotEnv(keys: string[]): Map<string, string | undefined> {
  return new Map(keys.map((key) => [key, process.env[key]]));
}

function restoreEnv(snapshot: Map<string, string | undefined>): void {
  for (const [key, value] of snapshot) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
}
