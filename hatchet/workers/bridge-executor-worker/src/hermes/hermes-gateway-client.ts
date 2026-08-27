import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import type { BridgeJob, JsonValue } from '@driftward/hatchet-worker-shared';
import { NonRetryableBridgeExecutionError } from './errors';

export const DEFAULT_HERMES_GRPC_TARGET = 'unix:///run/driftward-hermes-factory/hermes.sock';

export type HermesIdentityCheck = {
  githubLogin: string;
  gitAuthorName: string;
  gitAuthorEmail: string;
  accessibleRepo: string;
};

export type GatewayDraftPrResult = {
  status: 'created' | 'reused';
  prUrl: string;
  prNumber: number;
  commitSha: string | null;
  branch: string;
  hermesSessionId: string | null;
  summary: string;
  validation: JsonValue | null;
};

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

type UnaryCallback<T> = (error: grpc.ServiceError | null, response: T) => void;
type GatewayClient = grpc.Client & {
  checkIdentity(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<CheckIdentityResponse>): grpc.ClientUnaryCall;
  createDraftPullRequest(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<DraftPullRequestResult>): grpc.ClientUnaryCall;
  inspectDraftPullRequest(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<InspectDraftPullRequestResponse>): grpc.ClientUnaryCall;
  inspectPullRequestState(request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<InspectPullRequestStateResponse>): grpc.ClientUnaryCall;
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
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_response_malformed',
      'Hermes gRPC draft PR response status must be created or reused'
    );
  }
  const prUrl = requiredResponseString(response.prUrl, 'pr_url');
  const branch = requiredResponseString(response.branch, 'branch');
  const prNumber = response.prNumber || prNumberFromUrl(prUrl);
  if (!/^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/.test(prUrl) || !prNumber) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_response_malformed',
      'Hermes gRPC draft PR response must include a valid GitHub PR URL and number'
    );
  }
  if (branch !== job.branch) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_response_malformed',
      `Hermes gRPC draft PR response branch ${branch} did not match bridge branch ${job.branch}`
    );
  }
  return {
    status,
    prUrl,
    prNumber,
    commitSha: emptyToNull(response.commitSha),
    branch,
    hermesSessionId: emptyToNull(response.hermesSessionId),
    summary: response.summary || status,
    validation: parseJsonValue(response.validationJson || '', 'validation_json')
  };
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

function callGateway<T>(method: keyof GatewayClient, request: Record<string, unknown>, timeoutMs: number, fallbackReasonCode: string): Promise<T> {
  const client = getClient();
  const metadata = new grpc.Metadata();
  const token = (process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim();
  if (token) metadata.set('authorization', `Bearer ${token}`);
  const options: grpc.CallOptions = {};
  if (timeoutMs > 0) options.deadline = new Date(Date.now() + timeoutMs);

  return new Promise<T>((resolve, reject) => {
    const fn = client[method] as unknown as (request: Record<string, unknown>, metadata: grpc.Metadata, options: grpc.CallOptions, callback: UnaryCallback<T>) => grpc.ClientUnaryCall;
    fn.call(client, request, metadata, options, (error, response) => {
      if (error) {
        reject(toBridgeError(error, fallbackReasonCode));
        return;
      }
      resolve(response);
    });
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
  if (isHermesGrpcUnixTarget(target)) return;
  if (process.env.DRIFTWARD_HERMES_GRPC_ALLOW_TCP !== '1') {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_disabled',
      'TCP Hermes gRPC targets require DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1'
    );
  }
  if (process.env.DRIFTWARD_HERMES_GRPC_TLS !== '1' && !(process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim()) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_auth_required',
      'TCP Hermes gRPC targets require TLS or DRIFTWARD_HERMES_GRPC_AUTH_TOKEN'
    );
  }
}

function optionalFile(rawPath: string | undefined): Buffer | null {
  const filePath = rawPath?.trim();
  return filePath ? readFileSync(filePath) : null;
}

function toBridgeError(error: grpc.ServiceError, fallbackReasonCode: string): NonRetryableBridgeExecutionError {
  const metadataReason = firstMetadataValue(error.metadata, 'reason-code');
  let reasonCode = metadataReason || fallbackReasonCode;
  if (!metadataReason) {
    if (error.code === grpc.status.UNAVAILABLE) reasonCode = 'hermes_grpc_gateway_unavailable';
    if (error.code === grpc.status.DEADLINE_EXCEEDED) reasonCode = 'hermes_grpc_gateway_timeout';
    if (error.code === grpc.status.UNAUTHENTICATED) reasonCode = 'hermes_grpc_gateway_unauthenticated';
    if (error.code === grpc.status.PERMISSION_DENIED) reasonCode = 'hermes_grpc_gateway_forbidden';
  }
  return new NonRetryableBridgeExecutionError(reasonCode, sanitizeText(error.details || error.message || 'Hermes gRPC gateway request failed'));
}

function firstMetadataValue(metadata: grpc.Metadata | undefined, key: string): string | null {
  const value = metadata?.get(key)?.[0];
  if (Buffer.isBuffer(value)) return value.toString('utf8');
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function parseJsonValue(raw: string, field: string): JsonValue | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!isJsonValue(parsed)) throw new Error(`${field} is not a JSON value`);
    return parsed;
  } catch (error) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_response_malformed',
      `Hermes gRPC ${field} was not valid JSON: ${sanitizeText(error instanceof Error ? error.message : String(error))}`
    );
  }
}

function isJsonValue(value: unknown): value is JsonValue {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value) || Array.isArray(value) || (typeof value === 'object' && value !== null);
}

function requiredResponseString(value: string | undefined, field: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_response_malformed',
      `Hermes gRPC response field ${field} is required`
    );
  }
  return trimmed;
}

function emptyToNull(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed || null;
}

function emptyToUndefined(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed || undefined;
}

function prNumberFromUrl(prUrl: string): number {
  const match = prUrl.match(/\/pull\/(\d+)$/);
  return match ? Number.parseInt(match[1], 10) : 0;
}

function sanitizeText(value: string, limit: number = 320): string {
  return value.replace(/(?:ghp_|github_pat_|xox[baprs]-|Bearer\s+)[A-Za-z0-9_\-.]+/gi, '[redacted]').replace(/[\r\n\t]+/g, ' ').trim().slice(0, limit);
}
