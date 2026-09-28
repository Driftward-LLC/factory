import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import type { BridgeJob, JsonValue } from '@driftward/hatchet-worker-shared';
import {
  boundedHermesDraftPrReceipt,
  gatewayReasonCode,
  processSignal,
  redactSecretShapes,
  transportStatus,
  type BoundedHermesDraftPrReceipt,
} from './bounded-evidence';
import {
  NonRetryableBridgeExecutionError,
  type ExecutionFailureDomain,
  type ExecutionFailureStage,
} from './errors';

export const DEFAULT_HERMES_GRPC_TARGET = 'unix:///run/driftward-hermes-factory/hermes.sock';

export type HermesIdentityCheck = {
  githubLogin: string;
  gitAuthorName: string;
  gitAuthorEmail: string;
  accessibleRepo: string;
};

export type GatewayDraftPrResult = BoundedHermesDraftPrReceipt;

export type GatewayDraftPrObservation = {
  headRefName?: string;
  baseRefName?: string;
  isDraft?: boolean;
  url?: string;
  author?: { login?: string } | null;
  body?: string;
  files?: Array<{ path?: string }>;
  statusCheckRollup?: Array<{
    name?: string;
    conclusion?: string | null;
    state?: string | null;
    status?: string | null;
  }> | null;
  headRefOid?: string;
  diffText: string;
};

export type GatewayDraftPrStateInspection = {
  repository: string;
  number: number;
  url: string;
  state: 'open' | 'closed' | 'unknown';
  merged: boolean;
  draft: boolean;
  head_branch: string | null;
  head_sha: string | null;
  base_ref: string | null;
  reason: 'ok' | 'inspect_unavailable' | 'invalid_reference';
};

export type GatewayGitHubHeadPullRequest = {
  number: number;
  url: string;
  state: string;
  isDraft: boolean;
  headRefName: string;
  headRefOid: string | null;
  baseRefName: string | null;
  mergedAt: string | null;
  closedAt: string | null;
  title: string | null;
};

export type GatewayGitHubHeadInspection = {
  kind: 'github_branch_pr_absence_v1';
  repository: string;
  head_branch: string;
  branch_exists: boolean;
  pull_requests: GatewayGitHubHeadPullRequest[];
  observed_at: string;
  reason: 'ok' | 'inspect_unavailable' | 'invalid_reference';
};

type CheckIdentityResponse = {
  githubLogin?: string;
  gitAuthorName?: string;
  gitAuthorEmail?: string;
  accessibleRepo?: string;
};

type DraftPullRequestResult = {
  status?: string;
  prUrl?: string;
  prNumber?: number;
  commitSha?: string;
  branch?: string;
  hermesSessionId?: string;
  summary?: string;
  validationJson?: string;
};

type InspectDraftPullRequestResponse = {
  headRefName?: string;
  baseRefName?: string;
  isDraft?: boolean;
  url?: string;
  author?: { login?: string } | null;
  body?: string;
  files?: Array<{ path?: string }>;
  statusCheckRollup?: Array<{
    name?: string;
    conclusion?: string;
    state?: string;
    status?: string;
  }>;
  headRefOid?: string;
  diffText?: string;
};

type InspectPullRequestStateResponse = {
  repository?: string;
  number?: number;
  url?: string;
  state?: string;
  merged?: boolean;
  draft?: boolean;
  headBranch?: string;
  headSha?: string;
  baseRef?: string;
  reason?: string;
};

type GitHubHeadPullRequestResponse = {
  number?: number;
  url?: string;
  state?: string;
  isDraft?: boolean;
  headRefName?: string;
  headRefOid?: string;
  baseRefName?: string;
  mergedAt?: string;
  closedAt?: string;
  title?: string;
};

type InspectGitHubHeadResponse = {
  kind?: string;
  repository?: string;
  headBranch?: string;
  branchExists?: boolean;
  pullRequests?: GitHubHeadPullRequestResponse[];
  observedAt?: string;
  reason?: string;
};

type UnaryCallback<T> = (error: grpc.ServiceError | null, response: T) => void;
type GatewayClient = grpc.Client & {
  checkIdentity(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<CheckIdentityResponse>): grpc.ClientUnaryCall;
  createDraftPullRequest(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<DraftPullRequestResult>): grpc.ClientUnaryCall;
  inspectDraftPullRequest(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<InspectDraftPullRequestResponse>): grpc.ClientUnaryCall;
  inspectPullRequestState(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<InspectPullRequestStateResponse>): grpc.ClientUnaryCall;
  inspectGitHubHead(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<InspectGitHubHeadResponse>): grpc.ClientUnaryCall;
};

let cachedClient: { target: string; client: GatewayClient } | null = null;

export function hermesGrpcGatewayEnabled(): boolean {
  return Boolean((process.env.DRIFTWARD_HERMES_GRPC_TARGET || '').trim());
}

export function hermesGrpcTarget(): string {
  const target = (process.env.DRIFTWARD_HERMES_GRPC_TARGET || '').trim();
  if (!target) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_gateway_unconfigured',
      'DRIFTWARD_HERMES_GRPC_TARGET must be set for Hermes gRPC execution'
    );
  }
  validateTargetPolicy(target);
  return target;
}

export function isHermesGrpcUnixTarget(target: string): boolean {
  return target.startsWith('unix:');
}

export async function checkHermesIdentityViaGrpc(request: {
  repo: string;
  expectedGithubLogin: string;
  expectedGitAuthorName: string;
  expectedGitAuthorEmail: string;
  forbiddenGithubLogins: string[];
  forbiddenGitAuthorEmails: string[];
  timeoutMs: number;
}): Promise<HermesIdentityCheck> {
  const response = await callGateway<CheckIdentityResponse>('checkIdentity', {
    requestId: `identity:${request.repo}`,
    repo: request.repo,
    expectedGithubLogin: request.expectedGithubLogin,
    expectedGitAuthorName: request.expectedGitAuthorName,
    expectedGitAuthorEmail: request.expectedGitAuthorEmail,
    forbiddenGithubLogins: request.forbiddenGithubLogins,
    forbiddenGitAuthorEmails: request.forbiddenGitAuthorEmails
  }, request.timeoutMs, 'hermes_grpc_identity_check_failed');

  return {
    githubLogin: requiredResponseString(response.githubLogin, 'github_login'),
    gitAuthorName: requiredResponseString(response.gitAuthorName, 'git_author_name'),
    gitAuthorEmail: requiredResponseString(response.gitAuthorEmail, 'git_author_email'),
    accessibleRepo: requiredResponseString(response.accessibleRepo, 'accessible_repo')
  };
}

export async function runHermesCreateDraftPrViaGrpc(job: BridgeJob, timeoutMs: number): Promise<GatewayDraftPrResult> {
  const response = await callGateway<DraftPullRequestResult>('createDraftPullRequest', {
    requestId: `create-draft-pr:${job.job_id}`,
    bridgeJobId: job.job_id,
    idempotencyKey: job.idempotency_key,
    requestedAction: job.requested_action,
    repo: String(job.payload.repo || ''),
    branch: job.branch || '',
    jobJson: JSON.stringify(job),
    timeoutMs
  }, timeoutMs, 'hermes_grpc_create_draft_pr_failed');

  const status = response.status;
  if (status !== 'created' && status !== 'reused') {
    throw malformedGatewayResponse('Hermes gRPC draft PR response status must be created or reused');
  }
  const prUrl = requiredResponseString(response.prUrl, 'pr_url');
  const branch = requiredResponseString(response.branch, 'branch');
  const result = boundedHermesDraftPrReceipt(job, {
    status,
    prUrl,
    prNumber: Number.isSafeInteger(response.prNumber) && Number(response.prNumber) > 0
      ? response.prNumber
      : undefined,
    commitSha: response.commitSha,
    branch,
    validation: parseJsonValue(response.validationJson || '', 'validation_json')
  });
  if (!result) {
    throw malformedGatewayResponse('Hermes gRPC draft PR response did not match the bounded Factory receipt schema');
  }
  return result;
}

export async function inspectDraftPullRequestViaGrpc(repo: string, prNumber: number, timeoutMs: number): Promise<GatewayDraftPrObservation> {
  const response = await callGateway<InspectDraftPullRequestResponse>('inspectDraftPullRequest', {
    requestId: `inspect-draft-pr:${repo}:${prNumber}`,
    repo,
    prNumber,
    timeoutMs
  }, timeoutMs, 'hermes_grpc_pr_inspection_failed');

  return {
    headRefName: emptyToUndefined(response.headRefName),
    baseRefName: emptyToUndefined(response.baseRefName),
    isDraft: response.isDraft,
    url: emptyToUndefined(response.url),
    author: response.author?.login ? { login: response.author.login } : null,
    body: emptyToUndefined(response.body),
    files: Array.isArray(response.files) ? response.files.map((file) => ({ path: emptyToUndefined(file.path) })) : [],
    statusCheckRollup: Array.isArray(response.statusCheckRollup)
      ? response.statusCheckRollup.map((check) => ({
        name: emptyToUndefined(check.name),
        conclusion: emptyToNull(check.conclusion),
        state: emptyToNull(check.state),
        status: emptyToNull(check.status)
      }))
      : [],
    headRefOid: emptyToUndefined(response.headRefOid),
    diffText: response.diffText || ''
  };
}

export async function inspectDraftPrStateViaGrpc(repository: string, reference: string | number, timeoutMs: number): Promise<GatewayDraftPrStateInspection> {
  const response = await callGateway<InspectPullRequestStateResponse>('inspectPullRequestState', {
    requestId: `inspect-pr-state:${repository}:${reference}`,
    repository,
    reference: String(reference),
    timeoutMs
  }, timeoutMs, 'hermes_grpc_pr_state_inspection_failed');

  return {
    repository: response.repository || repository,
    number: response.number || 0,
    url: response.url || '',
    state: response.state === 'open' || response.state === 'closed' ? response.state : 'unknown',
    merged: Boolean(response.merged),
    draft: Boolean(response.draft),
    head_branch: emptyToNull(response.headBranch),
    head_sha: emptyToNull(response.headSha),
    base_ref: emptyToNull(response.baseRef),
    reason: response.reason === 'ok' || response.reason === 'invalid_reference' ? response.reason : 'inspect_unavailable'
  };
}

export async function inspectGitHubHeadViaGrpc(repository: string, headBranch: string, timeoutMs: number): Promise<GatewayGitHubHeadInspection> {
  const response = await callGateway<InspectGitHubHeadResponse>('inspectGitHubHead', {
    requestId: `inspect-github-head:${repository}:${headBranch}`,
    repository,
    headBranch,
    timeoutMs
  }, timeoutMs, 'hermes_grpc_head_inspection_failed');
  const reason = response.reason === 'ok' || response.reason === 'invalid_reference' ? response.reason : 'inspect_unavailable';
  const wellFormed = response.kind === 'github_branch_pr_absence_v1' &&
    Boolean(response.repository) &&
    Boolean(response.headBranch) &&
    Boolean(response.observedAt);

  return {
    kind: response.kind === 'github_branch_pr_absence_v1' ? response.kind : 'github_branch_pr_absence_v1',
    repository: response.repository || repository,
    head_branch: response.headBranch || headBranch,
    branch_exists: Boolean(response.branchExists),
    pull_requests: Array.isArray(response.pullRequests) ? response.pullRequests.map((item) => ({
      number: Number.isSafeInteger(item.number) ? Number(item.number) : 0,
      url: item.url || '',
      state: item.state || 'unknown',
      isDraft: Boolean(item.isDraft),
      headRefName: item.headRefName || '',
      headRefOid: emptyToNull(item.headRefOid),
      baseRefName: emptyToNull(item.baseRefName),
      mergedAt: emptyToNull(item.mergedAt),
      closedAt: emptyToNull(item.closedAt),
      title: emptyToNull(item.title)
    })) : [],
    observed_at: response.observedAt || '',
    reason: wellFormed ? reason : 'inspect_unavailable'
  };
}

function callGateway<T>(method: keyof GatewayClient, request: Record<string, unknown>, timeoutMs: number, fallbackReasonCode: string): Promise<T> {
  let client: GatewayClient;
  let metadata: grpc.Metadata;
  let options: grpc.CallOptions;
  try {
    client = getClient();
    metadata = new grpc.Metadata();
    const token = (process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim();
    if (token) metadata.set('authorization', `Bearer ${token}`);
    options = {};
    if (timeoutMs > 0) options.deadline = new Date(Date.now() + timeoutMs);
  } catch (error) {
    return Promise.reject(gatewayClientSetupError(error));
  }

  return new Promise<T>((resolve, reject) => {
    const fn = client[method] as unknown as (request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<T>) => grpc.ClientUnaryCall;
    try {
      fn.call(client, request, metadata, options, (error, response) => {
        if (error) {
          reject(toBridgeError(error, fallbackReasonCode));
          return;
        }
        resolve(response);
      });
    } catch (error) {
      reject(gatewayClientSetupError(error));
    }
  });
}

function getClient(): GatewayClient {
  const target = hermesGrpcTarget();
  if (cachedClient && cachedClient.target === target) return cachedClient.client;
  if (cachedClient) cachedClient.client.close();

  const packageDefinition = protoLoader.loadSync(resolveHermesGatewayProtoPath(), {
    keepCase: false,
    longs: Number,
    enums: String,
    defaults: true,
    oneofs: true
  });
  const loaded = grpc.loadPackageDefinition(packageDefinition) as Record<string, unknown>;
  const service = (((loaded.driftward as Record<string, unknown>).factory as Record<string, unknown>).hermes as Record<string, unknown>).v1 as Record<string, unknown>;
  const ClientCtor = service.HermesFactoryGateway as new (target: string, credentials: grpc.ChannelCredentials, options?: grpc.ClientOptions) => GatewayClient;
  const client = new ClientCtor(target, channelCredentials(target), {
    'grpc.primary_user_agent': 'driftward-factory-bridge-executor'
  });
  cachedClient = { target, client };
  return client;
}

export function resolveHermesGatewayProtoPath(): string {
  const candidates = [
    path.resolve(__dirname, 'hermes-gateway.proto'),
    path.resolve(__dirname, '../../src/hermes/hermes-gateway.proto'),
    path.resolve(process.cwd(), 'src/hermes/hermes-gateway.proto')
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_proto_missing',
      `Hermes gRPC proto was not found under ${candidates.join(', ')}`
    );
  }
  return found;
}

function channelCredentials(target: string): grpc.ChannelCredentials {
  if (process.env.DRIFTWARD_HERMES_GRPC_TLS === '1') {
    const rootCert = optionalFile(process.env.DRIFTWARD_HERMES_GRPC_CA_PATH);
    const privateKey = optionalFile(process.env.DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH);
    const certChain = optionalFile(process.env.DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH);
    if ((privateKey && !certChain) || (!privateKey && certChain)) {
      throw new NonRetryableBridgeExecutionError(
        'hermes_grpc_tls_config_invalid',
        'DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH and DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH must be set together'
      );
    }
    return grpc.credentials.createSsl(rootCert || undefined, privateKey || undefined, certChain || undefined);
  }
  if (!isHermesGrpcUnixTarget(target) && process.env.DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP !== '1') {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_tls_required',
      'TCP Hermes gRPC targets require TLS or DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP=1'
    );
  }
  return grpc.credentials.createInsecure();
}

function validateTargetPolicy(target: string): void {
  validateHermesGrpcTransport(target);
  if (isHermesGrpcUnixTarget(target)) return;
  const clientCertificate = process.env.DRIFTWARD_HERMES_GRPC_TLS === '1'
    && process.env.DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH?.trim()
    && process.env.DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH?.trim();
  if (!(process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim() && !clientCertificate) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_auth_required',
      'TCP Hermes gRPC targets require an auth token or a TLS client certificate and key'
    );
  }
}

export function validateHermesGrpcTransport(target: string): void {
  if (isHermesGrpcUnixTarget(target)) return;
  if (process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP !== '1') {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_disabled',
      'TCP Hermes gRPC targets require DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1'
    );
  }
  const insecureLoopback = process.env.DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP === '1'
    && /^(127\.0\.0\.1|\[::1\]):\d+$/.test(target);
  if (process.env.DRIFTWARD_HERMES_GRPC_TLS !== '1' && !insecureLoopback) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_tls_required',
      'TCP Hermes gRPC requires TLS; insecure development transport is limited to explicit loopback IP targets'
    );
  }
}

function optionalFile(rawPath: string | undefined): Buffer | null {
  const filePath = rawPath?.trim();
  return filePath ? readFileSync(filePath) : null;
}

function toBridgeError(error: grpc.ServiceError, fallbackReasonCode: string): NonRetryableBridgeExecutionError {
  const metadataReason = gatewayReasonCode(firstMetadataValue(error.metadata, 'reason-code'));
  let reasonCode = metadataReason || fallbackReasonCode;
  if (!metadataReason) {
    if (error.code === grpc.status.UNAVAILABLE) reasonCode = 'hermes_grpc_gateway_unavailable';
    if (error.code === grpc.status.DEADLINE_EXCEEDED) reasonCode = 'hermes_grpc_gateway_timeout';
    if (error.code === grpc.status.UNAUTHENTICATED) reasonCode = 'hermes_grpc_gateway_unauthenticated';
    if (error.code === grpc.status.PERMISSION_DENIED) reasonCode = 'hermes_grpc_gateway_forbidden';
  }
  const transportFailure = error.code === grpc.status.UNAVAILABLE || error.code === grpc.status.DEADLINE_EXCEEDED;
  return new NonRetryableBridgeExecutionError(
    reasonCode,
    sanitizeText(error.details || error.message || 'Hermes gRPC gateway request failed'),
    {
      failureDomain: executionFailureDomain(firstMetadataValue(error.metadata, 'failure-domain'))
        || (transportFailure ? 'gateway_transport' : 'gateway'),
      failureStage: executionFailureStage(firstMetadataValue(error.metadata, 'failure-stage'))
        || (transportFailure ? 'gateway_connect' : 'gateway_handler'),
      retryable: metadataBoolean(error.metadata, 'retryable') ?? (transportFailure ? true : false),
      transportStatus: transportStatus(firstMetadataValue(error.metadata, 'transport-status')) || grpcStatusName(error.code),
      processExitCode: metadataInteger(error.metadata, 'process-exit-code'),
      processSignal: processSignal(firstMetadataValue(error.metadata, 'process-signal')),
      timedOut: metadataBoolean(error.metadata, 'timed-out'),
      bufferExceeded: metadataBoolean(error.metadata, 'buffer-exceeded'),
    },
  );
}

function firstMetadataValue(metadata: grpc.Metadata | undefined, key: string): string | null {
  const value = metadata?.get(key)?.[0];
  if (Buffer.isBuffer(value)) return value.toString('utf8');
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function metadataBoolean(metadata: grpc.Metadata | undefined, key: string): boolean | null {
  const value = firstMetadataValue(metadata, key);
  return value === 'true' ? true : value === 'false' ? false : null;
}

function metadataInteger(metadata: grpc.Metadata | undefined, key: string): number | null {
  const value = firstMetadataValue(metadata, key);
  if (!value || !/^-?\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= -(2 ** 31) && parsed < 2 ** 31 ? parsed : null;
}

function executionFailureDomain(value: string | null): ExecutionFailureDomain | undefined {
  const allowed: ExecutionFailureDomain[] = [
    'unknown', 'factory', 'executor', 'executor_backend', 'gateway_transport', 'gateway',
    'hermes_process', 'hermes_protocol', 'auth', 'provider', 'tool', 'github', 'verification',
  ];
  return allowed.find((candidate) => candidate === value);
}

function executionFailureStage(value: string | null): ExecutionFailureStage | undefined {
  const allowed: ExecutionFailureStage[] = [
    'unknown', 'preflight', 'artifact_fence', 'effect_authorization', 'receipt_dispatch',
    'backend_invocation_fence', 'backend_execution', 'gateway_connect', 'gateway_handler',
    'hermes_spawn', 'hermes_exit', 'hermes_timeout', 'result_parse', 'auth_resolve',
    'auth_refresh', 'model_request', 'model_stream', 'tool_execution', 'effect_commit',
    'verification_checks', 'verification_recovery',
  ];
  return allowed.find((candidate) => candidate === value);
}

function grpcStatusName(code: grpc.status): string {
  const names: Partial<Record<grpc.status, string>> = {
    [grpc.status.CANCELLED]: 'grpc_cancelled',
    [grpc.status.UNKNOWN]: 'grpc_unknown',
    [grpc.status.INVALID_ARGUMENT]: 'grpc_invalid_argument',
    [grpc.status.DEADLINE_EXCEEDED]: 'grpc_deadline_exceeded',
    [grpc.status.NOT_FOUND]: 'grpc_not_found',
    [grpc.status.ALREADY_EXISTS]: 'grpc_already_exists',
    [grpc.status.PERMISSION_DENIED]: 'grpc_permission_denied',
    [grpc.status.RESOURCE_EXHAUSTED]: 'grpc_resource_exhausted',
    [grpc.status.FAILED_PRECONDITION]: 'grpc_failed_precondition',
    [grpc.status.ABORTED]: 'grpc_aborted',
    [grpc.status.OUT_OF_RANGE]: 'grpc_out_of_range',
    [grpc.status.UNIMPLEMENTED]: 'grpc_unimplemented',
    [grpc.status.INTERNAL]: 'grpc_internal',
    [grpc.status.UNAVAILABLE]: 'grpc_unavailable',
    [grpc.status.DATA_LOSS]: 'grpc_data_loss',
    [grpc.status.UNAUTHENTICATED]: 'grpc_unauthenticated',
  };
  return names[code] || 'grpc_ok';
}

function parseJsonValue(raw: string, field: string): JsonValue | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!isJsonValue(parsed)) throw new Error(`${field} is not a JSON value`);
    return parsed;
  } catch {
    throw malformedGatewayResponse(`Hermes gRPC ${field} was not valid JSON`);
  }
}

function isJsonValue(value: unknown): value is JsonValue {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value) || Array.isArray(value) || (typeof value === 'object' && value !== null);
}

function requiredResponseString(value: string | undefined, field: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    throw malformedGatewayResponse(`Hermes gRPC response field ${field} is required`);
  }
  return trimmed;
}

function malformedGatewayResponse(message: string): NonRetryableBridgeExecutionError {
  return new NonRetryableBridgeExecutionError(
    'hermes_grpc_response_malformed',
    message,
    {
      failureDomain: 'hermes_protocol',
      failureStage: 'result_parse',
      retryable: false,
      transportStatus: 'grpc_ok',
    },
  );
}

function gatewayClientSetupError(error: unknown): NonRetryableBridgeExecutionError {
  const known = error instanceof NonRetryableBridgeExecutionError ? error : null;
  return new NonRetryableBridgeExecutionError(
    known?.reasonCode || 'hermes_grpc_client_setup_failed',
    known ? sanitizeText(known.message) : 'Hermes gRPC client setup failed before request dispatch',
    {
      failureDomain: 'gateway_transport',
      failureStage: 'gateway_connect',
      retryable: false,
      transportStatus: 'client_setup_failed',
    },
  );
}

function emptyToNull(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed || null;
}

function emptyToUndefined(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed || undefined;
}

function sanitizeText(value: string, limit: number = 320): string {
  return redactSecretShapes(value).replace(/[\r\n\t]+/g, ' ').trim().slice(0, limit);
}
