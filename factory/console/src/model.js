export const issuePattern = /^ENG-[1-9][0-9]*$/;
export const activeStates = new Set([
  'reserved',
  'accepted',
  'running',
  'effect_authorized',
  'effect_committed',
  'verification_pending',
  'cancellation_requested',
  'effect_outcome_unknown',
]);
export const terminalStates = new Set([
  'completed',
  'failed',
  'verified_ready',
  'canceled',
  'canceled_after_effect',
  'superseded',
]);
export const verificationStates = new Set(['effect_committed', 'verification_pending']);
export const stages = {
  reserved: ['Queued', 'info'],
  accepted: ['Accepted', 'info'],
  running: ['Executing', 'warning'],
  effect_authorized: ['Execution authorized', 'warning'],
  effect_committed: ['Artifact recorded', 'info'],
  verification_pending: ['Verifying', 'warning'],
  verified_ready: ['Verified (recorded)', 'success'],
  ready_for_human_review: ['Verified (recorded)', 'success'],
  failed: ['Failed (recorded)', 'danger'],
  completed: ['Completed (recorded)', ''],
  cancellation_requested: ['Cancellation requested', 'warning'],
  effect_outcome_unknown: ['Outcome unknown', 'danger'],
  canceled: ['Canceled', ''],
  canceled_after_effect: ['Canceled after effect', 'warning'],
  superseded: ['Superseded', ''],
};
export const steps = {
  reserved: 'Waiting for dispatch. Inspect the latest events if this stays queued.',
  accepted: 'Waiting for an executor. Inspect Hatchet if this does not advance.',
  running:
    'Hermes is executing. A running attempt is not proof of model progress; inspect the last recorded event.',
  effect_authorized:
    'Execution has been authorized. Inspect the artifact and latest events before considering recovery.',
  effect_committed: 'An artifact was recorded. Wait for its verification evidence.',
  verification_pending:
    'Verification is pending. Inspect the PR checks and latest returned events.',
  verified_ready:
    'Verification was recorded. Open the PR to confirm its current head, checks and review status before acting.',
  failed:
    'Inspect the failure evidence and current PR state. If another attempt is needed, use the root-only Factory retry preview.',
  cancellation_requested:
    'Cancellation was requested. This does not prove the Hermes process stopped or an external effect was undone.',
  effect_outcome_unknown:
    'Inspect the existing branch and PR before recovery. Do not start a fresh attempt while the external outcome is unknown.',
  canceled_after_effect:
    'An external effect may remain after cancellation. Inspect the recorded PR before taking further action.',
  canceled:
    'This attempt was canceled. Inspect its history before deciding whether the issue needs more work.',
  superseded: 'Inspect the latest generation; this attempt was superseded.',
  completed:
    'Inspect the recorded artifact and evidence. Completion alone is not verified acceptance.',
};

export function label(value) {
  return String(value || 'Unknown').replaceAll('_', ' ');
}
export function count(value) {
  return Number.isInteger(value) && value >= 0 ? String(value) : 'Unavailable';
}
export function time(value) {
  const date = new Date(value || '');
  return Number.isFinite(date.getTime())
    ? date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : 'Unavailable';
}
export function duration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'Unavailable';
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return '<1m';
  if (minutes < 60) return minutes + 'm';
  if (minutes < 1440) return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm';
  return Math.floor(minutes / 1440) + 'd ' + Math.floor((minutes % 1440) / 60) + 'h';
}

export function readRoute() {
  const query = new URLSearchParams(location.search);
  const status = query.get('status') || 'all';
  const mode = query.get('mode') || 'all';
  return {
    view: ['work', 'analytics'].includes(query.get('view')) ? query.get('view') : 'overview',
    days: [7, 30, 90].includes(Number(query.get('days'))) ? Number(query.get('days')) : 30,
    issue: (query.get('issue') || '').toUpperCase(),
    q: (query.get('q') || '').slice(0, 200),
    status: ['all', 'active', 'attention', 'verifying', 'verified', 'failed', 'finished'].includes(
      status,
    )
      ? status
      : 'all',
    mode: ['all', 'real', 'dry-run'].includes(mode) ? mode : 'all',
    repo: (query.get('repo') || '').slice(0, 200),
  };
}
export function routeUrl(next) {
  const url = new URL(location.href);
  url.search = '';
  url.hash = '';
  for (const [key, value] of Object.entries(next)) {
    if (key === 'days' && (value === 30 || next.view !== 'analytics')) continue;
    if (value && value !== 'all') url.searchParams.set(key, value);
  }
  return url.pathname + url.search;
}

export function repository(record) {
  return record.job?.payload?.repo || record.attempt?.repository || '';
}
export function prUrl(record) {
  const value = record.job?.pr_url || record.attempt?.prior_pr_url;
  try {
    const url = new URL(value);
    const match = url.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/([1-9][0-9]*)\/?$/);
    const repo = repository(record);
    if (
      url.protocol !== 'https:' ||
      url.hostname !== 'github.com' ||
      url.port ||
      url.username ||
      url.password ||
      !match
    )
      return null;
    if (repo && (match[1] + '/' + match[2]).toLowerCase() !== String(repo).toLowerCase())
      return null;
    return url.origin + url.pathname;
  } catch {
    return null;
  }
}

export function elapsed(record) {
  const attempt = record.attempt || {};
  const start = Date.parse(attempt.created_at || '');
  const end = activeStates.has(attempt.status) ? Date.now() : Date.parse(attempt.updated_at || '');
  return duration(end - start);
}

export function needsInspection(record) {
  return needsExecutionInspection(record)
    || ['changes_requested', 'needs_operator', 'stale'].includes(independentReviewStatus(record));
}

function needsExecutionInspection(record) {
  const status = record.attempt?.status;
  return (
    activeStates.has(status) &&
    (status === 'effect_outcome_unknown' ||
      status === 'cancellation_requested' ||
      record.execution_lease?.status === 'expired')
  );
}

export function nextAction(record) {
  if (record.attempt?.mode === 'dry-run') return 'Inspect simulated result';
  if (needsExecutionInspection(record)) return 'Inspect unresolved execution';
  if (verificationStates.has(record.attempt?.status)) return 'Inspect verification evidence';
  if (record.attempt?.status === 'failed') return 'Review failure evidence';
  const reviewAction = independentReviewAction(record);
  if (reviewAction) return reviewAction;
  if (record.attempt?.status === 'verified_ready') return 'Confirm current PR checks';
  return 'Inspect latest evidence';
}

export function independentReviewAction(record) {
  const actions = { pending: 'Wait for independent review', running: 'Wait for independent review',
    changes_requested: 'Inspect independent review findings', needs_operator: 'Operator action required: inspect review stop reason',
    stale: 'Inspect the changed review head' };
  const status = independentReviewStatus(record);
  return Object.hasOwn(actions, status) ? actions[status] : '';
}

function independentReviewStatus(record) {
  const review = record.independent_review;
  return review?.schema === 'factory-console-independent-review-v1' && review.attempt_id === record.attempt?.attempt_id
    ? review.status : '';
}

export function isActiveWork(record) {
  return activeStates.has(record.attempt?.status)
    || ['pending', 'running', 'changes_requested', 'needs_operator', 'stale'].includes(independentReviewStatus(record));
}
