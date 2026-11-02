import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import type { FactoryIntakeReserveRequest, FactoryIntakeReserveResponse } from '@factory/hatchet-worker-shared';
import {
  LinearProjectRoutingError,
  ROUTING_REGISTRY_PATH_ENV,
  resolveLinearProjectRoute
} from './linear-project-routing';
import {
  assertReadyLinearIssueDispatch,
  assertExistingJobRouting,
  assertRepoAllowed,
  buildBridgePayload,
  type LinearReadyIssuePayloadInput
} from './linear-ready-intake-payload';
import { reserveFactoryIntake } from './linear-ready-factory-intake';
import { DraftPrVerifierPolicyError, resolveDraftPrVerifierPolicy, verifierPolicyPayload } from './draft-pr-verifier-policy';

const CAPYKIT_PROJECT_ID = '11111111-1111-4111-8111-111111111111';
const HOST_PROJECT_ID = '22222222-2222-4222-8222-222222222222';
const CHECKED_IN_REGISTRY = path.resolve(__dirname, '../../../test-fixtures/routing-registry.json');
process.env.NODE_ENV = 'test';
process.env.FACTORY_ORGANIZATION_CONFIG = path.resolve(__dirname, '../../../test-fixtures/organization.json');
process.env.HATCHET_VERIFIER_POLICY_PATH = path.resolve(__dirname, '../../../test-fixtures/verifier-policy.json');

test('routes Capykit and standalone Factory to their reviewed repositories', () => {
  assert.deepEqual(resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY }), {
    projectId: CAPYKIT_PROJECT_ID,
    repo: 'example/widgets',
    executionEnabled: true,
    registryVersion: 2
  });
  assert.equal(resolveLinearProjectRoute(HOST_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY }).repo, 'example/control-plane');
  const policy = resolveDraftPrVerifierPolicy('example/control-plane');
  assert.equal(policy.id, 'factory-operator-refresh-v1');
  assert.equal(policy.expected_base, 'main');
  assert(policy.allowed_changed_paths.includes('hatchet/bridge/hatchet_hermes_bridge/operator.py'));
  assert(!policy.allowed_changed_paths.includes('hatchet'));
  assert(policy.forbidden_changed_paths.includes('.github/workflows'));
  assert(policy.forbidden_changed_paths.includes('hatchet/workers'));
});

test('fails closed for missing and unmapped projects', () => {
  assertRoutingError(() => resolveLinearProjectRoute(undefined, { registryPath: CHECKED_IN_REGISTRY }), 'project_id_missing');
  assertRoutingError(() => resolveLinearProjectRoute('not-a-project-id', { registryPath: CHECKED_IN_REGISTRY }), 'project_id_malformed');
  assertRoutingError(
    () => resolveLinearProjectRoute('f079c532-620d-458c-8abd-05e37f5ca4c1', { registryPath: CHECKED_IN_REGISTRY }),
    'project_id_unmapped'
  );
});

test('uses the non-secret registry path environment override', () => {
  const previous = process.env[ROUTING_REGISTRY_PATH_ENV];
  withRegistry(
    { version: 2, routes: [{ project_id: CAPYKIT_PROJECT_ID, repo: 'example/platform', execution_enabled: true }] },
    (registryPath) => {
      process.env[ROUTING_REGISTRY_PATH_ENV] = registryPath;
      try {
        assert.equal(resolveLinearProjectRoute(CAPYKIT_PROJECT_ID).repo, 'example/platform');
      } finally {
        restoreEnvironment(ROUTING_REGISTRY_PATH_ENV, previous);
      }
    }
  );
});

test('fails closed for duplicate, invalid, unreadable, and unparsable registries', () => {
  withRegistry(
    {
      version: 2,
      routes: [
        { project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets', execution_enabled: true },
        { project_id: CAPYKIT_PROJECT_ID, repo: 'example/platform', execution_enabled: true }
      ]
    },
    (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
      'routing_registry_project_id_duplicate'
    )
  );
  withRegistry(
    { version: 1, routes: [{ project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets', execution_enabled: true }] },
    (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
      'routing_registry_version_invalid'
    )
  );
  withRegistry(
    { version: 2, routes: [{ project_id: 'not-a-project-id', repo: 'example/widgets', execution_enabled: true }] },
    (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
      'routing_registry_project_id_malformed'
    )
  );
  withRegistry(
    { version: 2, routes: [{ project_id: CAPYKIT_PROJECT_ID, repo: 'not-a-repository', execution_enabled: true }] },
    (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
      'routing_registry_repo_malformed'
    )
  );
  assertRoutingError(
    () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: '/definitely/missing/routing-registry.json' }),
    'routing_registry_read_failed'
  );
  withRawRegistry('{', (registryPath) => assertRoutingError(
    () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
    'routing_registry_parse_failed'
  ));
  withRegistry(
    { version: 2, routes: [{ project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets' }] },
    (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }),
      'routing_registry_execution_policy_malformed'
    )
  );
});

test('rejects stale ready events for the disabled project before any bridge operation in dry-run and real mode', () => {
  const registry = JSON.parse(readFileSync(CHECKED_IN_REGISTRY, 'utf8'));
  registry.routes.find((route: { project_id: string }) => route.project_id === HOST_PROJECT_ID).execution_enabled = false;
  withRegistry(registry, (registryPath) => {
    const previousRegistry = process.env[ROUTING_REGISTRY_PATH_ENV];
    const previousMode = process.env.HATCHET_INTAKE_DRY_RUN;
    process.env[ROUTING_REGISTRY_PATH_ENV] = registryPath;
    try {
      for (const mode of ['true', 'false']) {
        process.env.HATCHET_INTAKE_DRY_RUN = mode;
        assertRoutingError(
          () => assertReadyLinearIssueDispatch({
            eventKey: `linear:issue:ENG-53:update:${mode}`,
            identifier: 'ENG-53',
            projectId: HOST_PROJECT_ID,
            status: 'Ready',
            teamKey: 'ENG',
            teamId: '22222222-2222-4222-8222-222222222222',
            workspaceId: '11111111-1111-4111-8111-111111111111',
            blockedBy: [],
            dispatchEligible: true,
            decision: 'ready_for_implementation',
            reasonCode: 'linear_issue_ready_for_implementation'
          }),
          'linear_issue_project_execution_disabled'
        );
      }
    } finally {
      restoreEnvironment('HATCHET_INTAKE_DRY_RUN', previousMode);
      restoreEnvironment(ROUTING_REGISTRY_PATH_ENV, previousRegistry);
    }
  });
});

test('rejects repositories outside the intake allowlist', () => {
  const previous = process.env.HATCHET_INTAKE_ALLOWED_REPOS;
  process.env.HATCHET_INTAKE_ALLOWED_REPOS = 'example/platform';
  try {
    assert.throws(
      () => assertRepoAllowed('example/widgets'),
      /not in HATCHET_INTAKE_ALLOWED_REPOS/
    );
  } finally {
    restoreEnvironment('HATCHET_INTAKE_ALLOWED_REPOS', previous);
  }
});

test('rejects reuse of a bridge job with conflicting routing evidence', () => {
  const route = resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY });
  assert.throws(
    () => assertExistingJobRouting({
      payload: {
        repo: 'example/platform',
        routing: {
          source: 'linear_project',
          project_id: HOST_PROJECT_ID,
          registry_version: 2
        }
      }
    }, route),
    /conflicts with resolved repo/
  );
});

test('includes immutable project routing evidence in bridge payloads', () => {
  const input: LinearReadyIssuePayloadInput = {
    issueId: '11111111-2222-4333-8444-555555555555',
    identifier: 'ENG-7',
    teamKey: 'ENG', teamId: '22222222-2222-4222-8222-222222222222', workspaceId: '11111111-1111-4111-8111-111111111111',
    eventKey: 'linear:issue:ENG-7:update:delivery-1',
    deliveryId: 'delivery-1',
    issueUrl: 'https://linear.app/example/issue/ENG-7/implement-capykit-discovery-cli-commands',
    title: 'Implement Capykit discovery CLI commands',
    projectId: CAPYKIT_PROJECT_ID,
    projectName: 'Capykit'
  };
  const route = resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY });
  const payload = buildBridgePayload(input, route, true);

  assert.equal(payload.repo, 'example/widgets');
  assert.deepEqual(payload.routing, {
    source: 'linear_project',
    project_id: CAPYKIT_PROJECT_ID,
    project_name: 'Capykit',
    registry_version: 2
  });
  assert.equal(payload.source_event_key, 'linear:issue:ENG-7:update:delivery-1');
  assert.equal(payload.source_delivery_id, 'delivery-1');
  assert.equal(payload.linear_issue_id, '11111111-2222-4333-8444-555555555555');
  assert.equal(payload.expected_base, 'main');
  assert.equal(payload.body, 'Refs Linear ENG-7');
  assert.equal('required_github_checks' in payload, false);
  assert.deepEqual(payload.allowed_changed_paths, ['src', 'tests', 'scripts', 'docs', 'README.md', 'package.json', 'package-lock.json', 'tsconfig.json', '.dockerignore', '.markdownlint.json', 'Dockerfile.connector', 'Dockerfile.hosted', 'compose.yaml', 'deploy/connector-config/.gitkeep', 'deploy/init-postgres.sh', '.gitleaksignore']);
  assert.equal(payload.secret_check_command, 'npm run check:secrets');
  assert.deepEqual(payload.verification_policy, verifierPolicyPayload(resolveDraftPrVerifierPolicy('example/widgets')));
});

test('ready intake reserves a factory attempt instead of submitting a plain bridge job', async () => {
  const route = resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY });
  const calls: unknown[] = [];
  const bridge = {
    async reserveFactoryIntake(request: FactoryIntakeReserveRequest): Promise<FactoryIntakeReserveResponse> {
      calls.push(request);
      return {
        created: true,
        accepted_event_enqueued: true,
        attempt: {
          attempt_id: 'attempt-1',
          work_item_key: 'ENG-9',
          mode: request.mode,
          generation: 1,
          status: 'accepted',
          job_id: 'job-1',
          branch: 'codex/attempt',
          reason: request.reason,
          supersede_pr_url: null
        },
        job: {
          job_id: 'job-1',
          idempotency_key: 'hatchet:work-item:linear:ENG-9:create_draft_pr:real:active',
          state: 'accepted',
          requested_action: 'create_draft_pr',
          payload: request.payload,
          payload_hash: 'hash',
          branch: 'codex/attempt',
          commit_sha: null,
          pr_url: null,
          hermes_session_id: null,
          hatchet_workflow_run_id: request.hatchet_workflow_run_id ?? null,
          hatchet_step_run_id: request.hatchet_step_run_id ?? null,
          verification_context: null,
          verification_observed_context: null,
          verification_receipt: null,
          verification_digest: null,
          reason_code: null,
          error: null,
          created_at: '2026-08-22T00:00:00Z',
          updated_at: '2026-08-22T00:00:00Z'
        }
      };
    }
  };

  const result = await reserveFactoryIntake(bridge, {
    input: {
      issueId: '11111111-2222-4333-8444-555555555555',
      identifier: 'ENG-9',
      teamKey: 'ENG', teamId: '22222222-2222-4222-8222-222222222222', workspaceId: '11111111-1111-4111-8111-111111111111',
      eventKey: 'linear:issue:ENG-9:update:delivery-1',
      deliveryId: 'delivery-1',
      issueUrl: 'https://linear.app/example/issue/ENG-9/implement-the-read-only-capykit-mcp-server',
      title: 'Implement the read-only Capykit MCP server',
      projectId: CAPYKIT_PROJECT_ID,
      projectName: 'Capykit'
    },
    route,
    mode: 'real',
    hatchetWorkflowRunId: 'workflow-1',
    hatchetStepRunId: 'step-1'
  });

  assert.equal(result.created, true);
  assert.equal(calls.length, 1);
  assert.equal((calls[0] as any).work_item_key, 'ENG-9');
  assert.equal((calls[0] as any).mode, 'real');
  assert.equal((calls[0] as any).request_id, 'linear:issue:ENG-9:update:delivery-1');
  assert.equal((calls[0] as any).payload.dry_run, false);
  assert.equal((calls[0] as any).payload.repo, 'example/widgets');
  assert.equal(result.attempt.attempt_id, 'attempt-1');
});

test('verifier policy fails closed when required enforcement is missing or empty', () => {
  withRawRegistry(JSON.stringify({ version: 1, policies: [] }), (policyPath) => {
    assert.throws(() => resolveDraftPrVerifierPolicy('example/widgets', { policyPath }), (error: unknown) => error instanceof DraftPrVerifierPolicyError && error.code === 'verification_policy_missing');
  });
  withRawRegistry(JSON.stringify({ version: 1, policies: [{ id: 'bad', repo: 'example/widgets', expected_base: 'main', allowed_changed_paths: [], forbidden_changed_paths: ['.env'], required_local_commands: ['npm test'], secret_check_command: 'npm run check:secrets' }] }), (policyPath) => {
    assert.throws(() => resolveDraftPrVerifierPolicy('example/widgets', { policyPath }), (error: unknown) => error instanceof DraftPrVerifierPolicyError && error.code === 'verification_policy_invalid');
  });
});

test('Docker verifier policy resolution rejects host policy paths', () => {
  const previousRuntime = process.env.HATCHET_FACTORY_RUNTIME;
  try {
    process.env.HATCHET_FACTORY_RUNTIME = 'docker';
    assert.throws(
      () => resolveDraftPrVerifierPolicy('example/widgets', { policyPath: '/etc/factory/hatchet-verifier-policy.json' }),
      (error: unknown) => error instanceof DraftPrVerifierPolicyError && error.code === 'verification_policy_unavailable'
        && error.message.includes('/etc/factory/config/verifier-policy.json')
    );
  } finally {
    restoreEnvironment('HATCHET_FACTORY_RUNTIME', previousRuntime);
  }
});

function withRegistry(value: unknown, fn: (registryPath: string) => void): void {
  withRawRegistry(JSON.stringify(value), fn);
}

function withRawRegistry(raw: string, fn: (registryPath: string) => void): void {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'hatchet-routing-test-'));
  const registryPath = path.join(directory, 'routing-registry.json');
  writeFileSync(registryPath, raw);
  try {
    fn(registryPath);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function assertRoutingError(action: () => void, code: string): void {
  assert.throws(action, (error: unknown) => (
    error instanceof LinearProjectRoutingError && error.code === code
  ));
}

function restoreEnvironment(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
    return;
  }
  process.env[name] = value;
}

test('shipped defaults deny routes and verifier enrollment', () => {
  const registryPath = path.resolve(__dirname, '../../../routing-registry.json');
  assertRoutingError(() => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }), 'project_id_unmapped');
  assert.throws(() => resolveDraftPrVerifierPolicy('ExampleOrg/widget', {
    policyPath: path.resolve(__dirname, '../../../verifier-policy.json')
  }), (error: unknown) => error instanceof DraftPrVerifierPolicyError && error.code === 'verification_policy_missing');
  const previous = process.env.HATCHET_INTAKE_ALLOWED_REPOS;
  try {
    delete process.env.HATCHET_INTAKE_ALLOWED_REPOS;
    assert.throws(() => assertRepoAllowed('example/widgets'), /not in HATCHET_INTAKE_ALLOWED_REPOS/);
  } finally { restoreEnvironment('HATCHET_INTAKE_ALLOWED_REPOS', previous); }
});

test('another organization routes with its configured team and verifier base branch', () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-acme-routing-'));
  const names = ['FACTORY_ORGANIZATION_CONFIG', ROUTING_REGISTRY_PATH_ENV, 'HATCHET_VERIFIER_POLICY_PATH'];
  const previous = names.map((key) => process.env[key]);
  try {
    const organization = JSON.parse(readFileSync(process.env.FACTORY_ORGANIZATION_CONFIG!, 'utf8'));
    organization.linear.team_key = 'ACME';
    writeFileSync(path.join(directory, 'organization.json'), JSON.stringify(organization));
    writeFileSync(path.join(directory, 'routing.json'), JSON.stringify({ version: 2, routes: [
      { project_id: CAPYKIT_PROJECT_ID, repo: 'ExampleOrg/widget', execution_enabled: true }
    ] }));
    const source = JSON.parse(readFileSync(process.env.HATCHET_VERIFIER_POLICY_PATH!, 'utf8')).policies[0];
    writeFileSync(path.join(directory, 'verifier.json'), JSON.stringify({ version: 1,
      policies: [{ ...source, repo: 'ExampleOrg/widget', expected_base: 'develop' }] }));
    process.env.FACTORY_ORGANIZATION_CONFIG = path.join(directory, 'organization.json');
    process.env[ROUTING_REGISTRY_PATH_ENV] = path.join(directory, 'routing.json');
    process.env.HATCHET_VERIFIER_POLICY_PATH = path.join(directory, 'verifier.json');
    const input = { eventKey: 'synthetic:acme:1', identifier: 'ACME-42', teamKey: 'ACME',
      teamId: organization.linear.team_id, workspaceId: organization.linear.workspace_id,
      status: 'Ready', dispatchEligible: true, decision: 'ready_for_implementation',
      reasonCode: 'linear_issue_ready_for_implementation', projectId: CAPYKIT_PROJECT_ID };
    const route = assertReadyLinearIssueDispatch(input);
    assert.equal(route.repo, 'ExampleOrg/widget');
    assert.equal(buildBridgePayload(input, route, false).expected_base, 'develop');
    for (const changes of [{ identifier: 'ENG-42' }, { teamKey: 'ENG' }, { teamId: 'other' }, { workspaceId: 'other' }]) {
      assert.throws(() => assertReadyLinearIssueDispatch({ ...input, ...changes }), /failed intake validation/);
    }
  } finally {
    names.forEach((key, i) => restoreEnvironment(key, previous[i]));
    rmSync(directory, { recursive: true });
  }
});

test('routing rejects duplicate repository identity but accepts an entirely disabled catalog', () => {
  withRegistry({ version: 2, routes: [
    { project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets', execution_enabled: false },
    { project_id: HOST_PROJECT_ID, repo: 'EXAMPLE/widgets', execution_enabled: false }
  ] }, (registryPath) => assertRoutingError(() => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }), 'routing_registry_repo_duplicate'));
  withRegistry({ version: 2, routes: [
    { project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets', execution_enabled: false },
    { project_id: HOST_PROJECT_ID, repo: 'example/control-plane', execution_enabled: false }
  ] }, (registryPath) => assertRoutingError(() => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }), 'linear_issue_project_execution_disabled'));
});

test('an organization switch between observation and admission cannot relabel the old issue', async () => {
  const previous = process.env.FACTORY_ORGANIZATION_CONFIG;
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-stale-intake-'));
  const input = { identifier: 'ENG-42', teamKey: 'ENG', teamId: '22222222-2222-4222-8222-222222222222',
    workspaceId: '11111111-1111-4111-8111-111111111111', projectId: CAPYKIT_PROJECT_ID };
  const route = resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath: CHECKED_IN_REGISTRY });
  assert.deepEqual(buildBridgePayload(input, route, false).organization_binding, {
    workspace_id: input.workspaceId, team_id: input.teamId, team_key: input.teamKey
  });
  try {
    const configuration = JSON.parse(readFileSync(previous!, 'utf8'));
    configuration.linear.workspace_id = '44444444-4444-4444-8444-444444444444';
    configuration.linear.team_id = '55555555-5555-4555-8555-555555555555';
    const filename = path.join(directory, 'organization.json');
    writeFileSync(filename, JSON.stringify(configuration));
    process.env.FACTORY_ORGANIZATION_CONFIG = filename;
    let admissions = 0;
    const bridge = { reserveFactoryIntake: async () => { admissions++; throw new Error('must not submit'); } };
    await assert.rejects(reserveFactoryIntake(bridge, { input, route, mode: 'real', hatchetWorkflowRunId: 'workflow', hatchetStepRunId: 'step' }), /identity conflicts/);
    assert.equal(admissions, 0);
  } finally {
    restoreEnvironment('FACTORY_ORGANIZATION_CONFIG', previous);
    rmSync(directory, { recursive: true });
  }
});

test('optional retired repositories are bounded, valid, unique and disjoint from routed repositories', () => {
  const routes = [{ project_id: CAPYKIT_PROJECT_ID, repo: 'example/widgets', execution_enabled: false }];
  for (const retired_repositories of ['example/retired', Array.from({ length: 33 }, (_, index) => `example/retired-${index}`),
    ['not-a-repository'], ['example/old', 'EXAMPLE/OLD'], ['EXAMPLE/widgets']]) {
    withRegistry({ version: 2, routes, retired_repositories }, (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }), 'routing_registry_retired_repositories_invalid'));
  }
  for (const retired_repositories of [[], ['example/retired']]) {
    withRegistry({ version: 2, routes, retired_repositories }, (registryPath) => assertRoutingError(
      () => resolveLinearProjectRoute(CAPYKIT_PROJECT_ID, { registryPath }), 'linear_issue_project_execution_disabled'));
  }
});
