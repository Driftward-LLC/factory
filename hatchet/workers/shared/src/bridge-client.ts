import type {
  BridgeEventsResponse,
  BridgeJob,
  BridgeJobsResponse,
  ReconciliationScanPage,
  ReconciliationCorrelations,
  BridgeSubmitRequest,
  BridgeSubmitResponse,
  BridgeTransitionRequest,
  BridgeTransitionResponse
  , ReconciliationFindingRequest, ReconciliationFindingResponse, BridgeCorrelationRequest, BridgeCorrelationResponse, JsonObject, FactoryAttempt, FactoryIntakeReserveRequest, FactoryIntakeReserveResponse
} from './types';

export class BridgeClientError extends Error {
  status: number;
  body: string;

  constructor(message: string, status: number, body: string) {
    super(message);
    this.name = 'BridgeClientError';
    this.status = status;
    this.body = body;
  }
}

export type BridgeClientOptions = {
  baseUrl?: string;
  token?: string;
  timeoutMs?: number;
  verifierToken?: string;
  inspectionToken?: string;
  recoveryToken?: string;
};

export type VerificationLeaseResponse = { acquired?: boolean; claimed?: boolean; lease?: Record<string, unknown> };
export type FactoryFailureClassification = {
  failure_domain: string;
  failure_stage: string;
  retryable: boolean | null;
  failure_disposition: 'retry' | 'terminal' | 'recovery_required';
  operator_intervention_required: boolean | null;
};

export class BridgeClient {
  private baseUrl: string;
  private token?: string;
  private timeoutMs: number;
  private verifierToken?: string;
  private inspectionToken?: string;
  private recoveryToken?: string;

  constructor(options: BridgeClientOptions = {}) {
    this.baseUrl = (options.baseUrl || process.env.HATCHET_HERMES_BRIDGE_URL || 'http://127.0.0.1:19877').replace(/\/+$/, '');
    this.token = options.token || process.env.HATCHET_HERMES_BRIDGE_TOKEN;
    this.verifierToken = options.verifierToken || process.env.HATCHET_HERMES_VERIFIER_TOKEN;
    this.inspectionToken = options.inspectionToken || process.env.HATCHET_HERMES_INSPECTION_TOKEN;
    this.recoveryToken = options.recoveryToken || process.env.HATCHET_HERMES_RECOVERY_TOKEN;
    const configured = options.timeoutMs || Number(process.env.HATCHET_HERMES_BRIDGE_TIMEOUT_MS);
    this.timeoutMs = Math.min(5000, Math.max(100, Number.isFinite(configured) ? Math.floor(configured) : 1500));
  }

  async submitJob(request: BridgeSubmitRequest): Promise<BridgeSubmitResponse> {
    return this.requestJson<BridgeSubmitResponse>('/jobs', {
      method: 'POST',
      body: JSON.stringify(request)
    });
  }

  async reserveFactoryIntake(request: FactoryIntakeReserveRequest): Promise<FactoryIntakeReserveResponse> {
    return this.requestJson<FactoryIntakeReserveResponse>('/factory/intake/reserve', {
      method: 'POST',
      body: JSON.stringify(request)
    });
  }

  async getJob(ref: string): Promise<BridgeJob> {
    return this.requestJson<BridgeJob>(`/jobs/${encodeURIComponent(ref)}`, {
      method: 'GET'
    });
  }

  async listJobs(limit?: number): Promise<BridgeJobsResponse> {
    const path = typeof limit === 'number' && Number.isFinite(limit)
      ? `/jobs?limit=${encodeURIComponent(String(Math.max(1, Math.floor(limit))))}`
      : '/jobs';
    return this.requestJson<BridgeJobsResponse>(path, {
      method: 'GET'
    });
  }

  async transitionJob(ref: string, request: BridgeTransitionRequest): Promise<BridgeTransitionResponse> {
    return this.requestJson<BridgeTransitionResponse>(`/jobs/${encodeURIComponent(ref)}/transitions`, {
      method: 'POST',
      body: JSON.stringify(request)
    });
  }

  async scanReconciliation(cohort: ReconciliationScanPage['cohort'], limit: number): Promise<ReconciliationScanPage> {
    return this.requestJson('/reconciliation/scan', { method: 'POST', body: JSON.stringify({ cohort, limit }) });
  }

  async acknowledgeReconciliationScan(cohort: ReconciliationScanPage['cohort'], token: string): Promise<{ ok: boolean }> {
    return this.requestJson('/reconciliation/scan/ack', { method: 'POST', body: JSON.stringify({ cohort, token }) });
  }

  async reconciliationCorrelations(query: ReconciliationCorrelations): Promise<ReconciliationCorrelations> {
    return this.requestJson('/reconciliation/correlations', { method: 'POST', body: JSON.stringify(query) });
  }

  async completeVerifiedReady(ref: string, attemptId: string, receipt: JsonObject): Promise<BridgeTransitionResponse> {
    return this.completeVerification(ref, { state: 'ready_for_human_review', details: { reason_code: 'draft_pr_verified', verification_attempt_id: attemptId, verification_receipt: receipt } });
  }

  async completeVerificationFailure(ref: string, attemptId: string, reasonCode: string, error: string, classification: FactoryFailureClassification = {
    failure_domain: 'verification',
    failure_stage: 'verification_checks',
    retryable: null,
    failure_disposition: 'terminal',
    operator_intervention_required: null,
  }): Promise<BridgeTransitionResponse> {
    return this.completeVerification(ref, {
      state: 'failed',
      details: {
        reason_code: reasonCode,
        verification_attempt_id: attemptId,
        error,
        ...classification,
      }
    });
  }

  async recordJobCorrelation(ref: string, request: BridgeCorrelationRequest): Promise<BridgeCorrelationResponse> {
    return this.requestJson<BridgeCorrelationResponse>(`/jobs/${encodeURIComponent(ref)}/correlations`, {
      method: 'POST', body: JSON.stringify(request)
    });
  }

  async acquireVerificationLease(ref: string, attemptId: string, ttlSeconds: number = 300): Promise<VerificationLeaseResponse> {
    return this.requestJson<VerificationLeaseResponse>(`/jobs/${encodeURIComponent(ref)}/verification/lease`, { method: 'POST', body: JSON.stringify({ attempt_id: attemptId, ttl_seconds: ttlSeconds }) });
  }

  async claimVerificationLease(ref: string, attemptId: string): Promise<VerificationLeaseResponse> {
    const headers: Record<string, string> = {};
    if (this.verifierToken) headers['X-Hatchet-Verifier-Token'] = this.verifierToken;
    return this.requestJson<VerificationLeaseResponse>(`/jobs/${encodeURIComponent(ref)}/verification/lease/claim`, { method: 'POST', body: JSON.stringify({ attempt_id: attemptId }), headers });
  }

  async recordVerificationObservation(ref: string, attemptId: string, receipt: JsonObject): Promise<BridgeTransitionResponse> {
    const headers: Record<string, string> = {};
    if (this.verifierToken) headers['X-Hatchet-Verifier-Token'] = this.verifierToken;
    return this.requestJson<BridgeTransitionResponse>(`/jobs/${encodeURIComponent(ref)}/verification/observation`, { method: 'POST', body: JSON.stringify({ verification_attempt_id: attemptId, verification_receipt: receipt }), headers });
  }

  private async completeVerification(ref: string, request: BridgeTransitionRequest): Promise<BridgeTransitionResponse> {
    const headers: Record<string, string> = {};
    if (this.verifierToken) headers['X-Hatchet-Verifier-Token'] = this.verifierToken;
    return this.requestJson<BridgeTransitionResponse>(`/jobs/${encodeURIComponent(ref)}/verification/completion`, { method: 'POST', body: JSON.stringify(request), headers });
  }

  async listEvents(ref: string, limit?: number): Promise<BridgeEventsResponse> {
    const suffix = typeof limit === 'number' && Number.isFinite(limit)
      ? `?limit=${encodeURIComponent(String(Math.max(1, Math.floor(limit))))}`
      : '';
    return this.requestJson<BridgeEventsResponse>(`/jobs/${encodeURIComponent(ref)}/events${suffix}`, {
      method: 'GET'
    });
  }

  async getFactoryAttemptForJob(jobId: string): Promise<FactoryAttempt | null> {
    const result = await this.requestJson<{ attempt: FactoryAttempt | null }>(`/attempts/job/${encodeURIComponent(jobId)}`, { method: 'GET' });
    return result.attempt;
  }

  async claimFactoryExecution(jobId: string, owner: string): Promise<{ claimed: boolean; reason?: string; lease?: { lease_id: string; owner: string; epoch: number; fence_token: string } }> {
    return this.requestJson(`/jobs/${encodeURIComponent(jobId)}/factory-execution/claim`, { method: 'POST', body: JSON.stringify({ owner, ttl_seconds: 900 }) });
  }
  async factoryExecutionMayEffect(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }): Promise<boolean> {
    const result = await this.requestJson<{ allowed: boolean }>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/may-effect`, { method: 'POST', body: JSON.stringify(lease) });
    return result.allowed === true;
  }
  async renewFactoryExecution(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }): Promise<boolean> {
    const result = await this.requestJson<{ renewed: boolean }>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/heartbeat`, { method: 'POST', body: JSON.stringify(lease) });
    return result.renewed === true;
  }
  async authorizeFactoryEffect(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }): Promise<boolean> {
    const result = await this.requestJson<{ authorized: boolean }>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/authorize`, { method: 'POST', body: JSON.stringify(lease) });
    return result.authorized === true;
  }
  async recordFactoryExecutionRoute(jobId: string, details: JsonObject): Promise<boolean> {
    const result = await this.requestJson<{ recorded: boolean }>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/route`, {
      method: 'POST',
      body: JSON.stringify({ details }),
    });
    return result.recorded === true;
  }
  async recordFactoryExecutionAnalytics(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }, eventType: 'execution_backend_invoked' | 'execution_backend_completed', details: JsonObject): Promise<boolean> {
    const result = await this.requestJson<{ recorded: boolean }>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/analytics`, {
      method: 'POST',
      body: JSON.stringify({
        lease_id: lease.lease_id,
        owner: lease.owner,
        epoch: lease.epoch,
        fence_token: lease.fence_token,
        event_type: eventType,
        details,
      })
    });
    return result.recorded === true;
  }
  async settleFactoryExecution(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }, effectObserved: boolean, failureDetails?: JsonObject, failureEventType: 'execution_backend_failed' | 'execution_dispatch_failed' | 'execution_effect_commit_failed' = 'execution_backend_failed'): Promise<void> {
    await this.requestJson(`/jobs/${encodeURIComponent(jobId)}/factory-execution/settle`, { method: 'POST', body: JSON.stringify({ ...lease, effect_observed: effectObserved, ...(failureDetails ? { failure_details: failureDetails, failure_event_type: failureEventType } : {}) }) });
  }
  async commitFactoryExecution(jobId: string, lease: { lease_id: string; owner: string; epoch: number; fence_token: string }, details: JsonObject): Promise<BridgeTransitionResponse> {
    return this.requestJson<BridgeTransitionResponse>(`/jobs/${encodeURIComponent(jobId)}/factory-execution/commit`, { method: 'POST', body: JSON.stringify({ ...lease, details }) });
  }
  async recordPrInspection(jobId: string, requestKey: string, capability: string, result: JsonObject): Promise<void> {
    const headers: Record<string, string> = {};
    if (this.inspectionToken) headers['X-Hatchet-Inspection-Token'] = this.inspectionToken;
    await this.requestJson(`/jobs/${encodeURIComponent(jobId)}/inspection-result`, { method: 'POST', body: JSON.stringify({ inspection_request_key: requestKey, inspection_capability: capability, result }), headers });
  }
  async recoverFactoryExecution(jobId: string, requestKey: string, evidence: JsonObject): Promise<void> {
    const headers: Record<string, string> = {};
    if (this.recoveryToken) headers['X-Hatchet-Recovery-Token'] = this.recoveryToken;
    await this.requestJson(`/jobs/${encodeURIComponent(jobId)}/factory-execution/recover`, { method: 'POST', body: JSON.stringify({ request_key: requestKey, evidence }), headers });
  }

  async recordReconciliationFindings(findings: ReconciliationFindingRequest[]): Promise<ReconciliationFindingResponse> {
    return this.requestJson<ReconciliationFindingResponse>('/reconciliation/findings', {
      method: 'POST',
      body: JSON.stringify({ findings })
    });
  }

  private async requestJson<T>(path: string, init: RequestInit): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Content-Type', 'application/json');
    if (this.token) {
      headers.set('Authorization', `Bearer ${this.token}`);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}${path}`, { ...init, headers, signal: controller.signal });
      const body = await res.text();
      if (!res.ok) throw new BridgeClientError(formatBridgeError(res.status, body), res.status, body);
      return JSON.parse(body) as T;
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') throw new BridgeClientError('Bridge request timed out', 504, 'timeout');
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
}

function formatBridgeError(status: number, body: string): string {
  let reason: unknown;
  try {
    const parsed = JSON.parse(body) as { error?: unknown };
    reason = parsed.error;
  } catch {
    reason = undefined;
  }
  if (typeof reason === 'string' && reason.trim()) {
    return `Bridge request failed with HTTP ${status}: ${reason.trim()}`;
  }
  return `Bridge request failed with HTTP ${status}`;
}

export function buildIdempotencyKey(input: {
  requestId?: string;
  workItemId: string;
  action: string;
  expectedHeadSha?: string;
}): string {
  const runOrRequest = input.requestId || input.workItemId;
  const head = input.expectedHeadSha || 'unknown-head';
  return `hatchet:${runOrRequest}:${input.action}:${input.workItemId}:${head}`;
}
