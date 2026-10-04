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
const testLease = { lease_id: '00000000-0000-4000-8000-000000000001', owner: 'test-executor', epoch: 1, fence_token: 'test-fence' };

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

    const result = await runHermesCreateDraftPrViaGrpc(greenJob(), 5000, testLease);
    assert.equal(result.prUrl, 'https://github.com/Driftward-LLC/capykit/pull/28');
    assert.deepEqual(result.validation, {
      local_commands: [{ command: 'npm test', exit_code: 0 }],
      secret_check: { command: 'npm run check:secrets', exit_code: 0 },
    });
    assert.equal(result.hermesSessionId, null);
    assert.equal(result.summary, 'draft_pr_created');
    assert.doesNotMatch(JSON.stringify(result), /FAKE_(?:SESSION|SUMMARY|OUTPUT)_VALUE/);

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

test('identity preflight rejects a gateway without durable invocation support', async () => {
  const gateway = mockGateway();
  gateway.checkIdentity = (_call: MockCall, callback: MockCallback) => callback(null, {
    githubLogin: 'factory-hermes[bot]', gitAuthorName: 'factory-hermes[bot]',
    gitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com', accessibleRepo: 'Driftward-LLC/capykit',
  });
  await withGateway(async (target) => {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
    await assert.rejects(checkHermesIdentityViaGrpc({ repo: 'Driftward-LLC/capykit', expectedGithubLogin: 'factory-hermes[bot]',
      expectedGitAuthorName: 'factory-hermes[bot]', expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
      forbiddenGithubLogins: [], forbiddenGitAuthorEmails: [], timeoutMs: 5000 }),
    (error: unknown) => error instanceof NonRetryableBridgeExecutionError && error.reasonCode === 'hermes_grpc_invocation_protocol_unsupported' && error.failureStage === 'preflight');
  }, gateway);
});

test('Hermes gRPC TCP targets fail closed until explicitly authorized', () => {
  const previous = snapshotEnv([
    'DRIFTWARD_HERMES_GRPC_TARGET',
    'DRIFTWARD_HERMES_GRPC_ALLOW_TCP',
    'DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP',
    'DRIFTWARD_HERMES_GRPC_AUTH_TOKEN',
    'DRIFTWARD_HERMES_GRPC_TLS',
    'DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH',
    'DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH',
  ]);
  try {
    for (const key of previous.keys()) delete process.env[key];
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = 'dns:///hermes.internal:50051';
    delete process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP;
    delete process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN;
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_disabled');

    process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP = '1';
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_tls_required');

    process.env.DRIFTWARD_HERMES_GRPC_TLS = '1';
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_auth_required');

    process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN = 'test-token';
    assert.equal(hermesGrpcTarget(), 'dns:///hermes.internal:50051');

    delete process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN;
    process.env.DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH = '/test/client.key';
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_auth_required');
    process.env.DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH = '/test/client.crt';
    assert.equal(hermesGrpcTarget(), 'dns:///hermes.internal:50051');

    delete process.env.DRIFTWARD_HERMES_GRPC_TLS;
    process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN = 'test-token';
    process.env.DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP = '1';
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_tls_required');
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = '127.0.0.1:50051';
    assert.equal(hermesGrpcTarget(), '127.0.0.1:50051');
    delete process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN;
    assertFailure(() => hermesGrpcTarget(), 'hermes_grpc_tcp_auth_required');
  } finally {
    restoreEnv(previous);
  }
});

test('Hermes gRPC connection failures are attributed to the transport boundary', async () => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-missing-'));
  const previous = snapshotEnv(['DRIFTWARD_HERMES_GRPC_TARGET']);
  try {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = `unix://${path.join(tmp, 'missing.sock')}`;
    await assert.rejects(
      () => checkHermesIdentityViaGrpc({
        repo: 'Driftward-LLC/capykit',
        expectedGithubLogin: 'factory-hermes[bot]',
        expectedGitAuthorName: 'factory-hermes[bot]',
        expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
        forbiddenGithubLogins: [],
        forbiddenGitAuthorEmails: [],
        timeoutMs: 100,
      }),
      (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.failureDomain, 'gateway_transport');
        assert.equal(error.failureStage, 'gateway_connect');
        assert.equal(error.retryable, true);
        assert.ok(error.transportStatus === 'grpc_unavailable' || error.transportStatus === 'grpc_deadline_exceeded');
        return true;
      },
    );
  } finally {
    restoreEnv(previous);
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('malformed Hermes gRPC results are attributed to protocol result parsing', async () => {
  const gateway = mockGateway();
  gateway.createDraftPullRequest = (_call: MockCall, callback: MockCallback) => {
    callback(null, {
      status: 'created',
      prUrl: 'https://github.com/Driftward-LLC/capykit/pull/28',
      prNumber: 28,
      branch: 'codex/hermes-grpc-gateway',
      validationJson: '{"token":"ghp_not-a-real-secret"'
    });
  };

  await withGateway(async (target) => {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
    await assert.rejects(
      () => runHermesCreateDraftPrViaGrpc(greenJob(), 5000, testLease),
      (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.reasonCode, 'hermes_grpc_response_malformed');
        assert.equal(error.failureDomain, 'hermes_protocol');
        assert.equal(error.failureStage, 'result_parse');
        assert.equal(error.retryable, false);
        assert.equal(error.transportStatus, 'grpc_ok');
        assert.doesNotMatch(error.message, /ghp_not-a-real-secret/);
        return true;
      },
    );
  }, gateway);
});

test('untrusted gateway failure metadata is discarded before Factory settlement', async () => {
  const gateway = mockGateway();
  gateway.checkIdentity = (_call: MockCall, callback: MockCallback) => {
    const metadata = new grpc.Metadata();
    metadata.set('reason-code', 'sk-proj-FAKE_REASON_VALUE');
    metadata.set('transport-status', 'sk-proj-FAKE_TRANSPORT_VALUE');
    metadata.set('process-signal', 'sk-proj-FAKE_SIGNAL_VALUE');
    metadata.set('process-exit-code', String(2 ** 40));
    callback(Object.assign(new Error('gateway sk-proj-FAKE_MESSAGE_VALUE'), {
      code: grpc.status.FAILED_PRECONDITION,
      details: 'gateway sk-proj-FAKE_MESSAGE_VALUE',
      metadata,
    }) as grpc.ServiceError, null);
  };

  await withGateway(async (target) => {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
    await assert.rejects(
      () => checkHermesIdentityViaGrpc({
        repo: 'Driftward-LLC/capykit',
        expectedGithubLogin: 'factory-hermes[bot]',
        expectedGitAuthorName: 'factory-hermes[bot]',
        expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
        forbiddenGithubLogins: [],
        forbiddenGitAuthorEmails: [],
        timeoutMs: 5000,
      }),
      (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.reasonCode, 'hermes_grpc_identity_check_failed');
        assert.equal(error.transportStatus, 'grpc_failed_precondition');
        assert.equal(error.processSignal, null);
        assert.equal(error.processExitCode, null);
        assert.doesNotMatch(error.message, /FAKE_(?:REASON|TRANSPORT|SIGNAL|MESSAGE)_VALUE/);
        return true;
      },
    );
  }, gateway);
});

test('synchronous Hermes gRPC client setup failures are classified and sanitized', async () => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-setup-'));
  const missingCaPath = path.join(tmp, 'private-ca.pem');
  const previous = snapshotEnv([
    'DRIFTWARD_HERMES_GRPC_TARGET',
    'DRIFTWARD_HERMES_GRPC_TLS',
    'DRIFTWARD_HERMES_GRPC_CA_PATH',
    'DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH',
    'DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH'
  ]);
  try {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = `unix://${path.join(tmp, 'hermes.sock')}`;
    process.env.DRIFTWARD_HERMES_GRPC_TLS = '1';
    process.env.DRIFTWARD_HERMES_GRPC_CA_PATH = missingCaPath;
    delete process.env.DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH;
    delete process.env.DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH;

    await assert.rejects(
      () => checkHermesIdentityViaGrpc({
        repo: 'Driftward-LLC/capykit',
        expectedGithubLogin: 'factory-hermes[bot]',
        expectedGitAuthorName: 'factory-hermes[bot]',
        expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
        forbiddenGithubLogins: [],
        forbiddenGitAuthorEmails: [],
        timeoutMs: 5000,
      }),
      (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.reasonCode, 'hermes_grpc_client_setup_failed');
        assert.equal(error.failureDomain, 'gateway_transport');
        assert.equal(error.failureStage, 'gateway_connect');
        assert.equal(error.retryable, false);
        assert.equal(error.transportStatus, 'client_setup_failed');
        assert.doesNotMatch(error.message, /private-ca\.pem|ENOENT/);
        return true;
      },
    );
  } finally {
    restoreEnv(previous);
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('synchronous Hermes gRPC request metadata failures do not expose auth material', async () => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-metadata-'));
  const secret = 'auth-material-that-must-not-leak';
  const previous = snapshotEnv([
    'DRIFTWARD_HERMES_GRPC_TARGET',
    'DRIFTWARD_HERMES_GRPC_AUTH_TOKEN',
    'DRIFTWARD_HERMES_GRPC_TLS'
  ]);
  try {
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = `unix://${path.join(tmp, 'hermes.sock')}`;
    process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN = `${secret}\ninvalid`;
    delete process.env.DRIFTWARD_HERMES_GRPC_TLS;

    await assert.rejects(
      () => checkHermesIdentityViaGrpc({
        repo: 'Driftward-LLC/capykit',
        expectedGithubLogin: 'factory-hermes[bot]',
        expectedGitAuthorName: 'factory-hermes[bot]',
        expectedGitAuthorEmail: '4491454+factory-hermes[bot]@users.noreply.github.com',
        forbiddenGithubLogins: [],
        forbiddenGitAuthorEmails: [],
        timeoutMs: 5000,
      }),
      (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.reasonCode, 'hermes_grpc_client_setup_failed');
        assert.equal(error.failureDomain, 'gateway_transport');
        assert.equal(error.failureStage, 'gateway_connect');
        assert.equal(error.transportStatus, 'client_setup_failed');
        assert.doesNotMatch(error.message, new RegExp(secret));
        return true;
      },
    );
  } finally {
    restoreEnv(previous);
    rmSync(tmp, { recursive: true, force: true });
  }
});

async function withGateway(
  fn: (target: string) => Promise<void>,
  gateway: grpc.UntypedServiceImplementation = mockGateway(),
): Promise<void> {
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-test-'));
  const socketPath = path.join(tmp, 'hermes.sock');
  const target = `unix://${socketPath}`;
  const previous = snapshotEnv(['DRIFTWARD_HERMES_GRPC_TARGET']);
  const server = new grpc.Server();
  server.addService(loadGatewayService().service, gateway);
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
        invocationProtocolVersion: 1,
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
        commitSha: 'abc1234',
        branch: 'codex/hermes-grpc-gateway',
        hermesSessionId: 'sk-proj-FAKE_SESSION_VALUE',
        summary: 'raw model summary sk-proj-FAKE_SUMMARY_VALUE',
        validationJson: JSON.stringify({
          local_commands: [{ command: 'npm test', exit_code: 0, stdout: 'sk-proj-FAKE_OUTPUT_VALUE' }],
          secret_check: { command: 'npm run check:secrets', exit_code: 0 },
          model_output: 'sk-proj-FAKE_OUTPUT_VALUE',
        })
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
    payload: {
      repo: 'Driftward-LLC/capykit',
      dry_run: false,
      verification_policy: {
        required_local_commands: ['npm test'],
        secret_check_command: 'npm run check:secrets',
      },
    },
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
