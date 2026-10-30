export type BridgeState = 'accepted' | 'running' | 'effect_committed' | 'verification_pending' | 'ready_for_human_review' | 'completed' | 'failed';

export type BridgeAction = 'create_draft_pr' | 'repair_draft_pr' | 'inspect_draft_pr_state' | 'read_only_review';

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = {
  [key: string]: JsonValue;
};

export type BridgeJob = JsonObject & {
  execution_runtime?: { lease_id: string; epoch: number; runtime: import('./execution-runtime').ExecutionRuntimeBinding } | null;
  job_id: string;
  idempotency_key: string;
  state: BridgeState;
  requested_action: BridgeAction;
  payload: JsonObject;
  payload_hash: string;
  branch: string | null;
  commit_sha: string | null;
  pr_url: string | null;
  hermes_session_id: string | null;
  hatchet_workflow_run_id: string | null;
  hatchet_step_run_id: string | null;
  verification_context: JsonObject | null;
  verification_observed_context: JsonObject | null;
  verification_receipt: JsonObject | null;
  verification_digest: string | null;
  reason_code: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  reconciliation_duplicate_refs?: { branch_job_id?: string; pr_job_id?: string };
};

export type BridgeSubmitRequest = {
  idempotency_key: string;
  requested_action: BridgeAction;
  hatchet_workflow_run_id?: string;
  hatchet_step_run_id?: string;
  payload: JsonObject;
};

export type BridgeSubmitResponse = {
  created: boolean;
  job: BridgeJob;
};

export type FactoryIntakeReserveRequest = {
  work_item_key: string;
  mode: 'real' | 'dry-run';
  request_id: string;
  reason: string;
  hatchet_workflow_run_id?: string;
  hatchet_step_run_id?: string;
  payload: JsonObject;
};

export type FactoryIntakeReserveResponse = {
  created: boolean;
  accepted_event_enqueued?: boolean;
  attempt: FactoryAttempt;
  job: BridgeJob;
};

export type BridgeTransitionRequest = {
  state: BridgeState;
  details?: JsonObject;
};

export type BridgeTransitionResponse = {
  job: BridgeJob;
};

export type BridgeCorrelationRequest = {
  source_event_key?: string;
  source_delivery_id?: string;
};

export type BridgeCorrelationResponse = {
  job: BridgeJob;
};

export type BridgeEventsResponse = {
  events: Array<{
    event_id: string;
    job_id: string;
    event_type: string;
    state: BridgeState;
    details: JsonObject;
    created_at: string;
  }>;
  total: number;
};

export type BridgeJobsResponse = {
  jobs: BridgeJob[];
  total: number;
};

export type ReconciliationScanPage = BridgeJobsResponse & {
  cohort: 'active' | 'history';
  token: string;
  complete: boolean;
  sweep_complete: boolean;
  replayed: boolean;
};

export type ReconciliationCorrelations = {
  source_event_keys: string[];
  legacy_work_items: string[];
  hatchet_run_ids: string[];
};

export type FactoryAttempt = JsonObject & {
  attempt_id: string;
  work_item_key: string;
  mode: 'real' | 'dry-run';
  generation: number;
  status: string;
  job_id: string | null;
  branch: string;
  reason: string | null;
  supersede_pr_url: string | null;
};

export type ReconciliationFindingRequest = {
  finding_key: string;
  job_id: string | null;
  subject: string;
  kind: string;
  severity: 'warning' | 'critical';
  details: JsonObject;
};

export type ReconciliationFindingResponse = {
  created_finding_keys: string[];
  existing_finding_keys: string[];
};
