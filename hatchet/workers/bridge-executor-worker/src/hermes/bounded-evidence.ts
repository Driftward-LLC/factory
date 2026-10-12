import type { BridgeJob, JsonObject, JsonValue } from '@driftward/hatchet-worker-shared';

export type BoundedHermesDraftPrReceipt = {
  status: 'created' | 'reused';
  prUrl: string;
  prNumber: number;
  commitSha: string | null;
  branch: string;
  hermesSessionId: null;
  summary: 'draft_pr_created' | 'draft_pr_reused';
  validation: JsonObject | null;
};

type UntrustedReceipt = {
  status?: unknown;
  prUrl?: unknown;
  prNumber?: unknown;
  commitSha?: unknown;
  branch?: unknown;
  validation?: unknown;
};

const GATEWAY_REASON_CODES = new Set([
  'hermes_invocation_conflict',
  'hermes_grpc_invocation_inspection_failed',
  'hermes_grpc_invocation_protocol_unsupported',
  'github_head_inspection_malformed',
  'hermes_async_receipts_disabled',
  'hermes_expected_git_author_email_not_bot',
  'hermes_expected_git_author_name_not_bot',
  'hermes_expected_github_identity_forbidden',
  'hermes_expected_github_identity_not_bot',
  'hermes_expected_identity_mismatch',
  'hermes_git_author_forbidden',
  'hermes_git_author_mismatch',
  'hermes_git_author_name_mismatch',
  'hermes_git_author_not_bot',
  'hermes_git_author_not_configured',
  'hermes_github_app_repo_access_mismatch',
  'hermes_github_identity_not_configured',
  'hermes_grpc_auth_failed',
  'hermes_grpc_auth_required',
  'hermes_grpc_client_setup_failed',
  'hermes_grpc_create_draft_pr_failed',
  'hermes_grpc_gateway_failed',
  'hermes_grpc_gateway_forbidden',
  'hermes_grpc_gateway_timeout',
  'hermes_grpc_gateway_unauthenticated',
  'hermes_grpc_gateway_unavailable',
  'hermes_grpc_gateway_unconfigured',
  'hermes_grpc_head_inspection_failed',
  'hermes_grpc_identity_check_failed',
  'hermes_grpc_pr_inspection_failed',
  'hermes_grpc_pr_state_inspection_failed',
  'hermes_grpc_proto_missing',
  'hermes_grpc_request_malformed',
  'hermes_grpc_response_malformed',
  'hermes_grpc_tcp_auth_required',
  'hermes_grpc_tcp_disabled',
  'hermes_grpc_tcp_tls_required',
  'hermes_grpc_tls_config_invalid',
  'hermes_pr_branch_mismatch',
  'hermes_pr_contains_tempfile_reference',
  'hermes_pr_not_draft',
  'hermes_pr_number_missing',
  'hermes_pr_url_repo_mismatch',
  'hermes_pr_view_url_mismatch',
  'hermes_process_exit_nonzero',
  'hermes_process_output_limit_exceeded',
  'hermes_process_spawn_failed',
  'hermes_process_timeout',
  'hermes_protocol_result_invalid',
  'hermes_receipt_runner_dispatch_failed',
  'hermes_receipt_runner_missing',
  'hermes_repair_pr_url_mismatch',
  'hermes_runtime_group_missing',
  'hermes_runtime_identity_check_failed',
  'hermes_runtime_json_parse_failed',
  'invalid_branch',
  'invalid_repo',
  'missing_branch',
  'missing_execution_context',
  'missing_expected_base',
  'missing_linear_issue',
  'missing_repo',
  'missing_secret_check_command',
  'repair_branch_mismatch',
  'repair_context_missing',
  'revision_context_invalid',
  'revision_head_changed',
  'repo_not_allowlisted',
  'verification_changed_file_forbidden',
  'verification_changed_file_scope_invalid',
  'verification_commit_mismatch',
  'verification_evidence_malformed',
  'verification_evidence_unavailable',
  'verification_github_check_failed',
  'verification_github_checks_pending',
  'verification_github_checks_timeout',
  'verification_local_command_evidence_missing',
  'verification_local_command_failed',
  'verification_local_command_policy_mismatch',
  'verification_policy_invalid',
  'verification_policy_missing',
  'verification_policy_unavailable',
  'verification_pr_author_mismatch',
  'verification_pr_base_mismatch',
  'verification_pr_linear_link_missing',
  'verification_secret_check_evidence_missing',
  'verification_secret_check_failed',
  'verification_secret_check_policy_mismatch',
]);

export const TRANSPORT_STATUSES = new Set([
  'client_setup_failed',
  'grpc_aborted',
  'grpc_already_exists',
  'grpc_cancelled',
  'grpc_data_loss',
  'grpc_deadline_exceeded',
  'grpc_failed_precondition',
  'grpc_internal',
  'grpc_invalid_argument',
  'grpc_not_found',
  'grpc_ok',
  'grpc_out_of_range',
  'grpc_permission_denied',
  'grpc_resource_exhausted',
  'grpc_unauthenticated',
  'grpc_unavailable',
  'grpc_unimplemented',
  'grpc_unknown',
]);

export const PROCESS_SIGNALS = new Set([
  'SIGABRT', 'SIGALRM', 'SIGBUS', 'SIGCHLD', 'SIGCONT', 'SIGFPE', 'SIGHUP',
  'SIGILL', 'SIGINT', 'SIGIO', 'SIGIOT', 'SIGKILL', 'SIGPIPE', 'SIGPOLL',
  'SIGPROF', 'SIGPWR', 'SIGQUIT', 'SIGSEGV', 'SIGSTKFLT', 'SIGSTOP', 'SIGSYS',
  'SIGTERM', 'SIGTRAP', 'SIGTSTP', 'SIGTTIN', 'SIGTTOU', 'SIGURG', 'SIGUSR1',
  'SIGUSR2', 'SIGVTALRM', 'SIGWINCH', 'SIGXCPU', 'SIGXFSZ',
]);

const SECRET_SHAPE = /(?:^|[^A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|hf_|gsk_|lin_api_|glpat-|npm_|pypi-|akia[a-z0-9]|asia[a-z0-9]|aiza[a-z0-9]|bearer(?:[._+\-]|$))|eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}|[A-Za-z0-9_.-]{128,}/i;

export function isSecretShaped(value: string): boolean {
  return SECRET_SHAPE.test(value);
}

export function gatewayReasonCode(value: string | null | undefined): string | null {
  return typeof value === 'string' && GATEWAY_REASON_CODES.has(value) ? value : null;
}

export function transportStatus(value: string | null | undefined): string | null {
  return typeof value === 'string' && TRANSPORT_STATUSES.has(value) ? value : null;
}

export function processSignal(value: string | null | undefined): string | null {
  return typeof value === 'string' && PROCESS_SIGNALS.has(value) ? value : null;
}

export function boundedHermesDraftPrReceipt(
  job: Pick<BridgeJob, 'branch' | 'payload' | 'requested_action'>,
  raw: UntrustedReceipt,
): BoundedHermesDraftPrReceipt | null {
  if (raw.status !== 'created' && raw.status !== 'reused') return null;
  const repo = typeof job.payload.repo === 'string' ? job.payload.repo : '';
  const branch = typeof job.branch === 'string' ? job.branch : null;
  const prUrl = typeof raw.prUrl === 'string' ? raw.prUrl : null;
  const match = prUrl
    ? prUrl.match(/^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]*)$/)
    : null;
  if (!prUrl || !match || match[1] !== repo || !branch || raw.branch !== branch) return null;
  const prNumber = Number(match[2]);
  if (!Number.isSafeInteger(prNumber) || (raw.prNumber !== undefined && raw.prNumber !== prNumber)) return null;
  const repair = job.payload.factory_repair;
  if (job.requested_action === 'repair_draft_pr') {
    if (!repair || typeof repair !== 'object' || Array.isArray(repair)
      || repair.pr_url !== prUrl || raw.status !== 'reused') return null;
  }
  const commitSha = typeof raw.commitSha === 'string' && /^[0-9a-f]{7,64}$/.test(raw.commitSha.trim())
    ? raw.commitSha.trim()
    : null;
  return {
    status: raw.status,
    prUrl,
    prNumber,
    commitSha,
    branch,
    hermesSessionId: null,
    summary: raw.status === 'created' ? 'draft_pr_created' : 'draft_pr_reused',
    validation: boundedVerificationContext(job, raw.validation),
  };
}

export function boundedVerificationContext(
  job: Pick<BridgeJob, 'payload'>,
  raw: unknown,
): JsonObject | null {
  const policy = job.payload.verification_policy;
  if (!policy || typeof policy !== 'object' || Array.isArray(policy)
    || !Array.isArray(policy.required_local_commands)
    || policy.required_local_commands.length === 0
    || policy.required_local_commands.length > 32
    || policy.required_local_commands.some((command) => typeof command !== 'string' || !command)
    || typeof policy.secret_check_command !== 'string' || !policy.secret_check_command) return null;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  if (!Array.isArray(value.local_commands)) return null;
  const localCommands: JsonObject[] = [];
  for (const expected of policy.required_local_commands as string[]) {
    const matches = value.local_commands.filter((item) => isBoundedCommand(item, expected));
    if (matches.length !== 1) return null;
    const match = matches[0] as { exit_code: number };
    localCommands.push({ command: expected, exit_code: match.exit_code });
  }
  if (!isBoundedCommand(value.secret_check, policy.secret_check_command)) return null;
  return {
    local_commands: localCommands,
    secret_check: {
      command: policy.secret_check_command,
      exit_code: (value.secret_check as { exit_code: number }).exit_code,
    },
  };
}

function isBoundedCommand(value: unknown, expected: string): value is { command: string; exit_code: number } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return item.command === expected && Number.isInteger(item.exit_code)
    && Number(item.exit_code) >= -(2 ** 31) && Number(item.exit_code) < 2 ** 31;
}

export function redactSecretShapes(value: string): string {
  return value
    .replace(/eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}/gi, '[redacted]')
    .replace(/[A-Za-z0-9_.-]{128,}/g, '[redacted]')
    .replace(/(^|[^A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|hf_|gsk_|lin_api_|glpat-|npm_|pypi-|akia|asia|aiza|bearer(?:[._+\-]|$))[A-Za-z0-9_.+\/-]*/gim, '$1[redacted]')
    .replace(/\bBearer\s+[^\s]+/gi, '[redacted]');
}
