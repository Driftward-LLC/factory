import { chmodSync, existsSync, readFileSync, unlinkSync } from 'node:fs';
import { timingSafeEqual } from 'node:crypto';
import path from 'node:path';
import { executionRuntimeBinding, sameExecutionRuntime, withExecutionSpan } from '@driftward/hatchet-worker-shared';
import { SpanKind } from '@opentelemetry/api';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { gatewayReasonCode, processSignal, transportStatus } from './bounded-evidence';
import {
  assertHermesIdentityViaCommand,
  inspectGitHubHeadViaCommand,
  inspectDraftPrStateViaCommand,
  inspectDraftPullRequestViaCommand,
  runHermesCreateDraftPrViaCommand,
  sanitizeText,
  type DraftPrStateInspection,
  type GitHubHeadInspection,
  type GithubPrView,
  type HermesDraftPrResult
} from './create-draft-pr';
import { NonRetryableBridgeExecutionError } from './errors';
import { InvocationJournal, invocationLease } from './invocation-journal';
import { MODEL_SELECTION_HEADER, type ModelSelectionObserver } from './model-selection';
import { IndependentReviewer } from './independent-review';
import {
  DEFAULT_HERMES_GRPC_TARGET,
  isHermesGrpcUnixTarget,
  resolveHermesGatewayProtoPath,
  validateHermesGrpcTransport,
  type HermesIdentityCheck
} from './hermes-gateway-client';

type CheckIdentityRequest = {
  repo?: string;
};

type CreateDraftPullRequestRequest = {
  bridgeJobId?: string;
  idempotencyKey?: string;
  requestedAction?: string;
  repo?: string;
  branch?: string;
  jobJson?: string;
  timeoutMs?: number;
  executionLeaseJson?: string;
  executionRuntimeJson?: string;
};

type InspectDraftPullRequestRequest = {
  repo?: string;
  prNumber?: number;
  timeoutMs?: number;
};

type InspectPullRequestStateRequest = {
  repository?: string;
  reference?: string;
};

type InspectGitHubHeadRequest = {
  repository?: string;
  headBranch?: string;
  timeoutMs?: number;
};

type GatewayService = grpc.ServiceClientConstructor & { service: grpc.ServiceDefinition };
type UnaryCall<T> = grpc.ServerUnaryCall<T, unknown>;
type SendUnary<T> = grpc.sendUnaryData<T>;

export function createServer(target: string, runDraftPr = runHermesCreateDraftPrViaCommand): grpc.Server {
  const reviewer = new IndependentReviewer(process.env.DRIFTWARD_FACTORY_REVIEW_DIRECTORY || '/var/lib/driftward-hermes-factory/reviews');
  // Open lazily so read-only identity/inspection remains available during a storage fault.
  let journal: InvocationJournal | undefined;
  const invocations = () => journal ||= new InvocationJournal(process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY ||
    (process.env.DRIFTWARD_HERMES_RUNTIME_MODE === 'local-executor'
      ? path.join(process.env.HERMES_HOME || '/opt/data', 'factory-invocations')
      : '/var/lib/driftward-hermes-factory/invocations'));
  const selections = new Map<string, ModelSelectionObserver>();
  const inspectInvocation = async (request: CreateDraftPullRequestRequest, cancel: boolean) => {
    const job = parseBridgeJob(request);
    const snapshot = invocations().status(job);
    if ((snapshot.runtime || request.executionRuntimeJson) && !sameExecutionRuntime(snapshot.runtime, JSON.parse(request.executionRuntimeJson || 'null'))) {
      throw new NonRetryableBridgeExecutionError('hermes_invocation_conflict', 'Invocation runtime differs from the control-plane binding');
    }
    const strict = process.env.DRIFTWARD_HERMES_EXPERIMENTAL_NATIVE_OWNER === '1';
    const status = cancel && (strict || request.executionLeaseJson)
      ? await invocations().cancel(job, invocationLease(JSON.parse(requiredString(request.executionLeaseJson, 'execution_lease_json'))))
      : invocations().status(job, cancel);
    return { ...status, receipt: status.receipt ? draftPrResponse(status.receipt) : undefined,
      executionRuntimeJson: status.runtime ? JSON.stringify(status.runtime) : '',
      executionLeaseJson: status.lease ? JSON.stringify(status.lease) : '', nativeStopJson: status.nativeStop ? JSON.stringify(status.nativeStop) : '' };
  };
  const server = new grpc.Server();
  server.addService(loadGatewayService().service, {
    readOnlyReview: unary(target, async (request: { jobJson?: string }) => {
      const raw = requiredString(request.jobJson, 'job_json');
      if (raw.length > 100000) throw new Error('Review job exceeds gateway limit');
      const job = JSON.parse(raw) as BridgeJob;
      return { receiptJson: JSON.stringify(await reviewer.run(job)) };
    }),
    checkIdentity: unary(target, async (request: CheckIdentityRequest) => {
      invocations();
      const identity = await assertHermesIdentityViaCommand(requiredString(request.repo, 'repo'));
      return identityResponse(identity);
    }),
    prepareInvocation: unary(target, async (request: CreateDraftPullRequestRequest) => {
      const job = parseBridgeJob(request);
      const lease = invocationLease(JSON.parse(requiredString(request.executionLeaseJson, 'execution_lease_json')));
      const runtime = await invocations().prepare(job, lease, (invocation) =>
        withExecutionSpan('factory.hermes.execute', () => runDraftPr(job, undefined, optionalPositiveInt(request.timeoutMs), undefined,
          (selection) => selections.get(job.job_id)?.(selection), invocation), { job, service: 'factory-executor' }));
      return { state: 'running', executionRuntimeJson: JSON.stringify(runtime), executionLeaseJson: JSON.stringify(lease) };
    }),
    createDraftPullRequest: unary(target, async (request: CreateDraftPullRequestRequest, call) => {
      const job = parseBridgeJob(request);
      const lease = invocationLease(JSON.parse(requiredString(request.executionLeaseJson, 'execution_lease_json')));
      const runtime = executionRuntimeBinding(JSON.parse(requiredString(request.executionRuntimeJson, 'execution_runtime_json')));
      // Preparation pauses before model selection. Install its observer before
      // releasing the start gate so metadata still arrives before execution ends.
      if (selections.has(job.job_id)) throw new NonRetryableBridgeExecutionError('hermes_invocation_conflict', 'Invocation already has an active caller');
      selections.set(job.job_id, (selection) => {
        const metadata = new grpc.Metadata();
        metadata.set(MODEL_SELECTION_HEADER, Buffer.from(JSON.stringify({ job_id: job.job_id, selection })));
        call.sendMetadata(metadata);
      });
      try {
        const result = await withExecutionSpan('factory.executor.invoke', () => invocations().start(job, lease, runtime),
          { job, service: 'factory-executor' });
        return draftPrResponse(result);
      } finally { selections.delete(job.job_id); }
    }),
    inspectInvocation: unary(target, async (request: CreateDraftPullRequestRequest) => inspectInvocation(request, false)),
    cancelInvocation: unary(target, async (request: CreateDraftPullRequestRequest) => inspectInvocation(request, true)),
    inspectDraftPullRequest: unary(target, async (request: InspectDraftPullRequestRequest) => {
      const repo = requiredString(request.repo, 'repo');
      const prNumber = requiredPositiveInt(request.prNumber, 'pr_number');
      const timeout = positiveInt(request.timeoutMs, 30_000);
      const observation = await inspectDraftPullRequestViaCommand(repo, prNumber, timeout);
      return draftPrObservationResponse(observation);
    }),
    inspectPullRequestState: unary(target, async (request: InspectPullRequestStateRequest) => {
      const repository = requiredString(request.repository, 'repository');
      const reference = requiredString(request.reference, 'reference');
      const parsed = parsePrReference(repository, reference);
      if (!parsed) {
        return prStateResponse({
          repository,
          number: 0,
          url: '',
          state: 'unknown',
          merged: false,
          draft: false,
          head_branch: null,
          head_sha: null,
          base_ref: null,
          reason: 'invalid_reference'
        });
      }
      const inspection = await inspectDraftPrStateViaCommand(repository, parsed);
      return prStateResponse(inspection);
    }),
    inspectGitHubHead: unary(target, async (request: InspectGitHubHeadRequest) => {
      const repository = requiredString(request.repository, 'repository');
      const headBranch = requiredString(request.headBranch, 'head_branch');
      const inspection = await inspectGitHubHeadViaCommand(repository, headBranch, positiveInt(request.timeoutMs, 15_000));
      return gitHubHeadInspectionResponse(inspection);
    })
  });
  return server;
}

function unary<TRequest, TResponse>(
  target: string,
  handler: (request: TRequest, call: UnaryCall<TRequest>) => Promise<TResponse>
): (call: UnaryCall<TRequest>, callback: SendUnary<TResponse>) => void {
  return (call, callback) => {
    void (async () => {
      try {
        authorize(call.metadata, target);
        callback(null, await withExecutionSpan('factory.gateway.handle', () => handler(call.request, call), {
          parent: call.metadata.get('traceparent')[0], service: 'factory-executor', kind: SpanKind.SERVER,
        }));
      } catch (error) {
        callback(toServiceError(error), null);
      }
    })();
  };
}

function loadGatewayService(): GatewayService {
  const packageDefinition = protoLoader.loadSync(resolveHermesGatewayProtoPath(), {
    keepCase: false,
    longs: Number,
    enums: String,
    defaults: true,
    oneofs: true
  });
  const loaded = grpc.loadPackageDefinition(packageDefinition) as Record<string, unknown>;
  const service = (((loaded.driftward as Record<string, unknown>).factory as Record<string, unknown>).hermes as Record<string, unknown>).v1 as Record<string, unknown>;
  return service.HermesFactoryGateway as GatewayService;
}

async function main(): Promise<void> {
  const target = parseTarget(process.argv.slice(2));
  validateServerTarget(target);
  const socketPath = unixSocketPathFromTarget(target);
  if (socketPath && existsSync(socketPath)) unlinkSync(socketPath);

  const server = createServer(target);
  const port = await bindAsync(server, target, serverCredentials(target));
  if (socketPath) chmodSync(socketPath, socketMode());
  console.log(`Hermes Factory gRPC gateway listening on ${isHermesGrpcUnixTarget(target) ? socketPath : `tcp:${port}`}`);

  const shutdown = () => {
    server.tryShutdown(() => {
      if (socketPath && existsSync(socketPath)) unlinkSync(socketPath);
      process.exit(0);
    });
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

function parseTarget(args: string[]): string {
  let target = process.env.DRIFTWARD_HERMES_GRPC_BIND_TARGET || process.env.DRIFTWARD_HERMES_GRPC_TARGET || DEFAULT_HERMES_GRPC_TARGET;
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--target') {
      target = requiredArg(args, ++i, '--target');
      continue;
    }
    if (arg === '--socket') {
      target = `unix://${requiredArg(args, ++i, '--socket')}`;
      continue;
    }
    if (arg === '-h' || arg === '--help') {
      console.log('usage: node dist/hermes/hermes-gateway-server.js [--target TARGET | --socket /path/to/hermes.sock]');
      process.exit(0);
    }
    throw new Error(`unsupported argument: ${arg}`);
  }
  return target;
}

function bindAsync(server: grpc.Server, target: string, credentials: grpc.ServerCredentials): Promise<number> {
  return new Promise((resolve, reject) => {
    server.bindAsync(target, credentials, (error, port) => {
      if (error) reject(error);
      else resolve(port);
    });
  });
}

function serverCredentials(target: string): grpc.ServerCredentials {
  if (process.env.DRIFTWARD_HERMES_GRPC_TLS === '1') {
    const privateKey = requiredFile(process.env.DRIFTWARD_HERMES_GRPC_SERVER_KEY_PATH, 'DRIFTWARD_HERMES_GRPC_SERVER_KEY_PATH');
    const certChain = requiredFile(process.env.DRIFTWARD_HERMES_GRPC_SERVER_CERT_PATH, 'DRIFTWARD_HERMES_GRPC_SERVER_CERT_PATH');
    const rootCert = optionalFile(process.env.DRIFTWARD_HERMES_GRPC_CA_PATH);
    return grpc.ServerCredentials.createSsl(rootCert, [{ private_key: privateKey, cert_chain: certChain }], Boolean(rootCert));
  }
  if (!isHermesGrpcUnixTarget(target) && process.env.DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP !== '1') {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_tls_required',
      'TCP Hermes gRPC gateway binds require TLS or DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP=1'
    );
  }
  return grpc.ServerCredentials.createInsecure();
}

function validateServerTarget(target: string): void {
  validateHermesGrpcTransport(target);
  if (isHermesGrpcUnixTarget(target)) return;
  const mutualTls = process.env.DRIFTWARD_HERMES_GRPC_TLS === '1'
    && process.env.DRIFTWARD_HERMES_GRPC_CA_PATH?.trim();
  if (!(process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim() && !mutualTls) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_tcp_auth_required',
      'TCP Hermes gRPC gateway binds require an auth token or a CA that enforces TLS client certificates'
    );
  }
}

function authorize(metadata: grpc.Metadata, target: string): void {
  const expected = (process.env.DRIFTWARD_HERMES_GRPC_AUTH_TOKEN || '').trim();
  if (!expected) {
    if (isHermesGrpcUnixTarget(target)) return;
    if (process.env.DRIFTWARD_HERMES_GRPC_TLS === '1' && process.env.DRIFTWARD_HERMES_GRPC_CA_PATH?.trim()) return;
    throw serviceError(grpc.status.UNAUTHENTICATED, 'hermes_grpc_auth_required', 'Hermes gRPC TCP gateway requires an auth token');
  }
  const observed = metadata.get('authorization');
  const expectedBytes = Buffer.from(`Bearer ${expected}`);
  const observedBytes = typeof observed[0] === 'string' ? Buffer.from(observed[0]) : Buffer.alloc(0);
  if (observed.length !== 1 || observedBytes.length !== expectedBytes.length || !timingSafeEqual(observedBytes, expectedBytes)) {
    throw serviceError(grpc.status.UNAUTHENTICATED, 'hermes_grpc_auth_failed', 'Hermes gRPC gateway authentication failed');
  }
}

function parseBridgeJob(request: CreateDraftPullRequestRequest): BridgeJob {
  const raw = requiredString(request.jobJson, 'job_json');
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new NonRetryableBridgeExecutionError(
      'hermes_grpc_request_malformed',
      `job_json is not valid JSON: ${sanitizeText(error instanceof Error ? error.message : String(error))}`
    );
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new NonRetryableBridgeExecutionError('hermes_grpc_request_malformed', 'job_json must encode a bridge job object');
  }
  const job = parsed as BridgeJob;
  if (job.job_id !== request.bridgeJobId || job.idempotency_key !== request.idempotencyKey || job.requested_action !== request.requestedAction || job.branch !== request.branch || job.payload?.repo !== request.repo) {
    throw new NonRetryableBridgeExecutionError('hermes_grpc_request_malformed', 'gRPC request fields did not match job_json');
  }
  return job;
}

function parsePrReference(repository: string, reference: string): { number: number; url: string } | null {
  if (/^[1-9][0-9]*$/.test(reference)) {
    const number = Number(reference);
    return Number.isSafeInteger(number) ? { number, url: `https://github.com/${repository}/pull/${number}` } : null;
  }
  const match = reference.match(/^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)$/);
  return match && match[1] === repository ? { number: Number(match[2]), url: reference } : null;
}

function identityResponse(identity: HermesIdentityCheck): Record<string, unknown> {
  return {
    invocationProtocolVersion: 2,
    githubLogin: identity.githubLogin,
    gitAuthorName: identity.gitAuthorName,
    gitAuthorEmail: identity.gitAuthorEmail,
    accessibleRepo: identity.accessibleRepo
  };
}

function draftPrResponse(result: HermesDraftPrResult): Record<string, unknown> {
  return {
    status: result.status,
    prUrl: result.prUrl,
    prNumber: result.prNumber,
    commitSha: result.commitSha || '',
    branch: result.branch,
    hermesSessionId: result.hermesSessionId || '',
    summary: result.summary,
    validationJson: result.validation === null ? '' : JSON.stringify(result.validation)
  };
}

function draftPrObservationResponse(observation: GithubPrView & { diffText: string }): Record<string, unknown> {
  return {
    headRefName: observation.headRefName || '',
    baseRefName: observation.baseRefName || '',
    isDraft: Boolean(observation.isDraft),
    url: observation.url || '',
    author: { login: observation.author?.login || '' },
    body: observation.body || '',
    files: (observation.files || []).map((file) => ({ path: file.path || '' })),
    statusCheckRollup: (observation.statusCheckRollup || []).map((check) => ({
      name: check.name || '',
      conclusion: check.conclusion || '',
      state: check.state || '',
      status: check.status || ''
    })),
    headRefOid: observation.headRefOid || '',
    diffText: observation.diffText
  };
}

function prStateResponse(inspection: DraftPrStateInspection): Record<string, unknown> {
  return {
    repository: inspection.repository,
    number: inspection.number,
    url: inspection.url,
    state: inspection.state,
    merged: inspection.merged,
    draft: inspection.draft,
    headBranch: inspection.head_branch || '',
    headSha: inspection.head_sha || '',
    baseRef: inspection.base_ref || '',
    reason: inspection.reason
  };
}

function gitHubHeadInspectionResponse(inspection: GitHubHeadInspection): Record<string, unknown> {
  return {
    kind: inspection.kind,
    repository: inspection.repository,
    headBranch: inspection.head_branch,
    branchExists: inspection.branch_exists,
    pullRequests: inspection.pull_requests.map((pullRequest) => ({
      number: pullRequest.number,
      url: pullRequest.url,
      state: pullRequest.state,
      isDraft: pullRequest.isDraft,
      headRefName: pullRequest.headRefName,
      headRefOid: pullRequest.headRefOid || '',
      baseRefName: pullRequest.baseRefName || '',
      mergedAt: pullRequest.mergedAt || '',
      closedAt: pullRequest.closedAt || '',
      title: pullRequest.title || ''
    })),
    observedAt: inspection.observed_at,
    reason: inspection.reason
  };
}

function positiveInt(value: number | undefined, fallback: number | undefined): number {
  if (typeof value === 'number' && Number.isInteger(value) && value > 0) return value;
  if (typeof fallback === 'number') return fallback;
  throw new NonRetryableBridgeExecutionError('hermes_grpc_request_malformed', 'positive integer value is required');
}

function requiredPositiveInt(value: number | undefined, field: string): number {
  if (typeof value === 'number' && Number.isInteger(value) && value > 0) return value;
  throw new NonRetryableBridgeExecutionError('hermes_grpc_request_malformed', `${field} must be a positive integer`);
}

function optionalPositiveInt(value: number | undefined): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : undefined;
}

function requiredString(value: string | undefined, field: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    throw new NonRetryableBridgeExecutionError('hermes_grpc_request_malformed', `${field} is required`);
  }
  return trimmed;
}

function requiredArg(args: string[], index: number, flag: string): string {
  const value = args[index];
  if (!value) throw new Error(`${flag} requires a value`);
  return value;
}

function unixSocketPathFromTarget(target: string): string | null {
  if (!isHermesGrpcUnixTarget(target)) return null;
  if (target.startsWith('unix:///')) return '/' + target.slice('unix:///'.length);
  if (target.startsWith('unix://')) return target.slice('unix://'.length);
  return target.slice('unix:'.length);
}

function socketMode(): number {
  const raw = process.env.DRIFTWARD_HERMES_GRPC_SOCKET_MODE || '660';
  return /^[0-7]{3,4}$/.test(raw) ? Number.parseInt(raw, 8) : 0o660;
}

function optionalFile(rawPath: string | undefined): Buffer | null {
  const filePath = rawPath?.trim();
  return filePath ? readFileSync(filePath) : null;
}

function requiredFile(rawPath: string | undefined, envName: string): Buffer {
  const filePath = rawPath?.trim();
  if (!filePath) throw new NonRetryableBridgeExecutionError('hermes_grpc_tls_config_invalid', `${envName} is required when DRIFTWARD_HERMES_GRPC_TLS=1`);
  return readFileSync(filePath);
}

function toServiceError(error: unknown): grpc.ServiceError {
  if (isServiceError(error)) return error;
  if (error instanceof NonRetryableBridgeExecutionError) {
    return serviceError(grpc.status.FAILED_PRECONDITION, error.reasonCode, error.message, {
      failureDomain: error.failureDomain,
      failureStage: error.failureStage,
      retryable: error.retryable,
      transportStatus: error.transportStatus,
      processExitCode: error.processExitCode,
      processSignal: error.processSignal,
      timedOut: error.timedOut,
      bufferExceeded: error.bufferExceeded,
    });
  }
  return serviceError(
    grpc.status.UNKNOWN,
    'hermes_grpc_gateway_failed',
    'Hermes gateway failed without structured bounded evidence',
    { failureDomain: 'gateway', failureStage: 'gateway_handler', retryable: null },
  );
}

function serviceError(
  code: grpc.status,
  reasonCode: string,
  message: string,
  evidence: {
    failureDomain?: string;
    failureStage?: string;
    retryable?: boolean | null;
    transportStatus?: string | null;
    processExitCode?: number | null;
    processSignal?: string | null;
    timedOut?: boolean | null;
    bufferExceeded?: boolean | null;
  } = {},
): grpc.ServiceError {
  const metadata = new grpc.Metadata();
  metadata.set('reason-code', gatewayReasonCode(reasonCode) || 'hermes_grpc_gateway_failed');
  if (evidence.failureDomain) metadata.set('failure-domain', evidence.failureDomain);
  if (evidence.failureStage) metadata.set('failure-stage', evidence.failureStage);
  if (evidence.retryable !== undefined && evidence.retryable !== null) metadata.set('retryable', String(evidence.retryable));
  const boundedTransportStatus = transportStatus(evidence.transportStatus);
  if (boundedTransportStatus) metadata.set('transport-status', boundedTransportStatus);
  if (Number.isInteger(evidence.processExitCode) && Number(evidence.processExitCode) >= -(2 ** 31) && Number(evidence.processExitCode) < 2 ** 31) {
    metadata.set('process-exit-code', String(evidence.processExitCode));
  }
  const boundedProcessSignal = processSignal(evidence.processSignal);
  if (boundedProcessSignal) metadata.set('process-signal', boundedProcessSignal);
  if (evidence.timedOut !== undefined && evidence.timedOut !== null) metadata.set('timed-out', String(evidence.timedOut));
  if (evidence.bufferExceeded !== undefined && evidence.bufferExceeded !== null) metadata.set('buffer-exceeded', String(evidence.bufferExceeded));
  return Object.assign(new Error(sanitizeText(message)), { code, metadata }) as grpc.ServiceError;
}

function isServiceError(error: unknown): error is grpc.ServiceError {
  return Boolean(error && typeof error === 'object' && 'code' in error && 'metadata' in error);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(sanitizeText(error instanceof Error ? error.message : String(error)));
    process.exit(1);
  });
}
