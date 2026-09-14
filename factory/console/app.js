'use strict';

const byId = (id) => document.getElementById(id);
const issuePattern = /^ENG-[1-9][0-9]*$/;
const activeStates = new Set(['reserved', 'accepted', 'running', 'effect_authorized', 'effect_committed', 'verification_pending', 'cancellation_requested', 'effect_outcome_unknown']);
const terminalStates = new Set(['completed', 'failed', 'verified_ready', 'canceled', 'canceled_after_effect', 'superseded']);
const stages = {
  reserved: ['Queued', 'info'], accepted: ['Accepted', 'info'],
  running: ['Executing', 'warning'], effect_authorized: ['Execution authorized', 'warning'],
  effect_committed: ['Artifact recorded', 'info'], verification_pending: ['Verifying', 'warning'],
  verified_ready: ['Verified (recorded)', 'success'], ready_for_human_review: ['Verified (recorded)', 'success'],
  failed: ['Failed (recorded)', 'danger'], completed: ['Completed (recorded)', ''],
  cancellation_requested: ['Cancellation requested', 'warning'],
  effect_outcome_unknown: ['Outcome unknown', 'danger'],
  canceled: ['Canceled', ''], canceled_after_effect: ['Canceled after effect', 'warning'],
  superseded: ['Superseded', ''],
};
const steps = {
  reserved: 'Waiting for dispatch. Inspect the latest events if this stays queued.',
  accepted: 'Waiting for an executor. Inspect Hatchet if this does not advance.',
  running: 'Hermes is executing. A running attempt is not proof of model progress; inspect the last recorded event.',
  effect_authorized: 'Execution has been authorized. Inspect the artifact and latest events before considering recovery.',
  effect_committed: 'An artifact was recorded. Wait for its verification evidence.',
  verification_pending: 'Verification is pending. Inspect the PR checks and latest returned events.',
  verified_ready: 'Verification was recorded. Open the PR to confirm its current head, checks and review status before acting.',
  failed: 'Inspect the failure evidence and current PR state. If another attempt is needed, use the root-only Factory retry preview.',
  cancellation_requested: 'Cancellation was requested. This does not prove the Hermes process stopped or an external effect was undone.',
  effect_outcome_unknown: 'Inspect the existing branch and PR before recovery. Do not start a fresh attempt while the external outcome is unknown.',
  canceled_after_effect: 'An external effect may remain after cancellation. Inspect the recorded PR before taking further action.',
  canceled: 'This attempt was canceled. Inspect its history before deciding whether the issue needs more work.',
  superseded: 'Inspect the latest generation; this attempt was superseded.',
  completed: 'Inspect the recorded artifact and evidence. Completion alone is not verified acceptance.',
};
let route = readRoute();
let overview = null;
let refreshing = false;
let overviewError = '';
let issueController = null;
let issueSequence = 0;
let issueSnapshot = null;
let issueLoadedAt = null;

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function label(value) { return String(value || 'Unknown').replaceAll('_', ' '); }
function count(value) { return Number.isInteger(value) && value >= 0 ? String(value) : 'Unavailable'; }
function time(value) {
  const date = new Date(value || '');
  return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Unavailable';
}
function duration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'Unavailable';
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return '<1m';
  if (minutes < 60) return minutes + 'm';
  if (minutes < 1440) return Math.floor(minutes / 60) + 'h ' + minutes % 60 + 'm';
  return Math.floor(minutes / 1440) + 'd ' + Math.floor(minutes % 1440 / 60) + 'h';
}
function pill(status) {
  const [text, tone] = Object.hasOwn(stages, status) ? stages[status] : [label(status), ''];
  return make('span', 'pill ' + tone, text);
}
function message(id, text) {
  byId(id).textContent = text;
  byId(id).hidden = !text;
}
function readRoute() {
  const query = new URLSearchParams(location.search);
  const status = query.get('status') || 'all';
  const mode = query.get('mode') || 'all';
  return {
    view: query.get('view') === 'work' ? 'work' : 'overview',
    issue: (query.get('issue') || '').toUpperCase(),
    q: (query.get('q') || '').slice(0, 200),
    status: ['all', 'active', 'attention', 'verified', 'failed', 'finished'].includes(status) ? status : 'all',
    mode: ['all', 'real', 'dry-run'].includes(mode) ? mode : 'all',
    repo: (query.get('repo') || '').slice(0, 200),
  };
}
function routeUrl(next) {
  const url = new URL(location.href);
  url.search = '';
  url.hash = '';
  for (const [key, value] of Object.entries(next)) {
    if (value && value !== 'all') url.searchParams.set(key, value);
  }
  return url.pathname + url.search;
}
function navigate(changes, replace = false) {
  const previousIssue = route.issue;
  route = { ...route, ...changes };
  history[replace ? 'replaceState' : 'pushState']({}, '', routeUrl(route));
  renderView();
  if (route.issue !== previousIssue || Object.hasOwn(changes, 'issue')) {
    loadIssue(route.issue, true);
  }
}
function issueLink(issue) {
  if (!issuePattern.test(issue)) return make('span', '', issue || 'Unknown issue');
  const link = make('a', 'issue-link', issue);
  link.href = routeUrl({ ...route, issue });
  link.dataset.issue = issue;
  return link;
}
function repository(record) { return record.job?.payload?.repo || record.attempt?.repository || ''; }
function prUrl(record) {
  const value = record.job?.pr_url || record.attempt?.prior_pr_url;
  try {
    const url = new URL(value);
    const match = url.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/([1-9][0-9]*)\/?$/);
    const repo = repository(record);
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || !match) return null;
    if (repo && (match[1] + '/' + match[2]).toLowerCase() !== String(repo).toLowerCase()) return null;
    return url.origin + url.pathname;
  } catch { return null; }
}
function externalLink(text, url) {
  const link = make('a', '', text);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}
function artifact(record) {
  const url = prUrl(record);
  return url ? externalLink('PR #' + url.split('/').filter(Boolean).pop() + ' ↗', url) : make('span', 'muted', 'Not available');
}
function cell(value) {
  const td = document.createElement('td');
  td.append(value instanceof Node ? value : document.createTextNode(String(value)));
  return td;
}
function elapsed(record) {
  const attempt = record.attempt || {};
  const start = Date.parse(attempt.created_at || '');
  const end = activeStates.has(attempt.status) ? Date.now() : Date.parse(attempt.updated_at || '');
  return duration(end - start);
}
function attemptRow(record, detail = false) {
  const attempt = record.attempt || {};
  const row = document.createElement('tr');
  if (detail) {
    row.append(cell(attempt.generation ?? 'Unknown'));
  } else {
    const identity = cell(issueLink(attempt.work_item_key));
    identity.append(make('small', '', repository(record) || 'Repository unavailable'));
    row.append(identity);
  }
  const stage = cell(pill(attempt.status));
  if (!detail) stage.append(make('small', '', 'Generation ' + (attempt.generation ?? 'unknown')));
  row.append(stage, cell(attempt.mode === 'dry-run' ? 'Dry run' : attempt.mode === 'real' ? 'Real' : 'Unknown'));
  if (!detail) {
    const span = cell(elapsed(record));
    span.append(make('small', '', activeStates.has(attempt.status) ? 'attempt age' : 'recorded span'));
    span.title = 'From attempt creation to now if active, otherwise to its last recorded update. Not model runtime.';
    row.append(span);
  }
  row.append(cell(artifact(record)), cell(time(attempt.updated_at)));
  return row;
}
function renderTable(id, records, detail = false, empty = 'No attempts found.') {
  const target = byId(id);
  target.replaceChildren();
  if (!records.length) {
    const row = document.createElement('tr');
    const td = make('td', 'empty', empty);
    td.colSpan = detail ? 5 : 6;
    row.append(td);
    target.append(row);
  } else {
    target.append(...records.map((record) => attemptRow(record, detail)));
  }
}
function allRecords() {
  if (!overview) return [];
  const records = new Map();
  // Active entries win if an attempt is also present in the recent list.
  for (const record of [...overview.recent_attempts, ...overview.active_attempts]) {
    if (record.attempt?.attempt_id) records.set(record.attempt.attempt_id, record);
  }
  return [...records.values()].sort((a, b) =>
    (Date.parse(b.attempt.updated_at) || 0) - (Date.parse(a.attempt.updated_at) || 0) ||
    String(a.attempt.attempt_id).localeCompare(String(b.attempt.attempt_id)));
}
function needsInspection(record) {
  const status = record.attempt?.status;
  return activeStates.has(status) && (status === 'effect_outcome_unknown' ||
    status === 'cancellation_requested' || record.execution_lease?.status === 'expired');
}
function matches(record) {
  const attempt = record.attempt || {};
  const status = attempt.status;
  const groups = {
    all: true, active: activeStates.has(status), attention: needsInspection(record),
    verified: status === 'verified_ready', failed: status === 'failed', finished: terminalStates.has(status),
  };
  const search = [attempt.work_item_key, repository(record), record.job?.branch, attempt.branch].join(' ').toLowerCase();
  return groups[route.status] && (route.mode === 'all' || attempt.mode === route.mode) &&
    (!route.repo || repository(record) === route.repo) && search.includes(route.q.trim().toLowerCase());
}
function renderWork() {
  if (!overview) return;
  const records = allRecords();
  const repos = new Set(records.map(repository).filter(Boolean));
  if (route.repo) repos.add(route.repo);
  const select = byId('repo-filter');
  select.replaceChildren(new Option('All repositories', ''));
  [...repos].sort().forEach((repo) => select.add(new Option(repo, repo)));
  select.value = route.repo;
  const filtered = records.filter(matches);
  renderTable('work-attempts', filtered, false, records.length ? 'No attempts match these filters. Try clearing them.' : 'No active or recent attempts recorded.');
  byId('work-count').textContent = filtered.length + ' of ' + records.length + ' returned attempts · active + recent, deduplicated by attempt';
  byId('nav-work-count').textContent = String(records.length);
}
function renderView() {
  const work = route.view === 'work';
  document.querySelectorAll('[data-view]').forEach((element) => { element.hidden = element.dataset.view !== route.view; });
  document.querySelectorAll('[data-nav]').forEach((link) => {
    if (link.dataset.nav === route.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
    link.href = routeUrl({ ...route, view: link.dataset.nav, issue: '' });
  });
  byId('breadcrumb').textContent = work ? 'Work' : 'Overview';
  byId('page-title').textContent = work ? 'Factory work' : 'Factory overview';
  byId('page-description').textContent = work ? 'Find an attempt. Inspect its artifact and evidence.' : 'Work in motion. Evidence you can inspect.';
  byId('work-query').value = route.q;
  byId('status-filter').value = route.status;
  byId('mode-filter').value = route.mode;
  byId('issue-input').value = route.issue;
  renderWork();
}
function updateFreshness() {
  const age = overview ? Date.now() - Date.parse(overview.generated_at) : NaN;
  const stale = Boolean(overview && (overviewError || age > 90000 || age < -5000));
  const status = byId('connection-state');
  status.dataset.state = !overview ? 'offline' : stale ? 'stale' : 'online';
  status.textContent = !overview ? (overviewError ? 'Unavailable' : 'Connecting') : stale ? 'Stale snapshot' : 'Snapshot current';
  byId('updated-at').textContent = overview
    ? (stale ? 'Last successful snapshot: ' : 'Snapshot: ') + time(overview.generated_at) + ' · age ' + duration(age) + '. Refreshes every 30s while visible.'
    : 'No snapshot available yet.';
}
function renderOverview() {
  const counts = overview.status_counts;
  byId('metric-mode').textContent = overview.mode === 'real' ? 'Real' : overview.mode === 'dry-run' ? 'Dry run' : 'Unknown';
  byId('metric-active').textContent = count(overview.active_attempts.length);
  byId('metric-ready').textContent = counts && typeof counts === 'object' ? count(counts.verified_ready ?? 0) : 'Unavailable';
  byId('metric-total').textContent = counts && Object.values(counts).every((n) => Number.isInteger(n) && n >= 0)
    ? count(Object.values(counts).reduce((sum, n) => sum + n, 0)) : 'Unavailable';
  const reconciliation = overview.reconciliation;
  byId('signal-actionable').textContent = count(reconciliation?.actionable);
  byId('signal-actionable').closest('article').dataset.attention = String(reconciliation?.actionable > 0);
  byId('signal-actionable-detail').textContent = reconciliation?.actionable_severity
    ? count(reconciliation.actionable_severity.critical ?? 0) + ' critical · ' + count(reconciliation.actionable_severity.warning ?? 0) + ' warning. Inspect Factory status and the affected issue history.'
    : 'Severity evidence unavailable.';
  byId('signal-terminal').textContent = count(reconciliation?.scope?.terminal_work);
  const lifecycle = overview.lifecycle_outbox;
  byId('signal-dead-letters').textContent = lifecycle?.available ? count(lifecycle.dead_letters) : 'Unavailable';
  byId('signal-dead-letters').closest('article').dataset.attention = String(lifecycle?.available && lifecycle.dead_letters > 0);
  byId('signal-dead-letter-detail').textContent = !lifecycle?.available ? 'Lifecycle source unavailable; this is not a zero count.'
    : lifecycle.dead_letters > 0 ? 'Inspect with factory lifecycle dead-letters list. Archive remains root-only.' : 'No permanent delivery failures in this snapshot.';
  renderTable('active-attempts', overview.active_attempts, false, 'No active attempts in this snapshot.');
  byId('recent-attempts').replaceChildren();
  const recent = overview.recent_attempts.slice(0, 8);
  if (!recent.length) byId('recent-attempts').append(make('li', 'empty', 'No recent attempts recorded.'));
  recent.forEach((record) => {
    const item = make('li', 'recent-item');
    const identity = make('div');
    identity.append(issueLink(record.attempt?.work_item_key), make('small', '', 'Generation ' + (record.attempt?.generation ?? 'unknown') + ' · ' + label(record.attempt?.mode) + ' · ' + time(record.attempt?.updated_at)));
    item.append(identity, pill(record.attempt?.status));
    byId('recent-attempts').append(item);
  });
  const hatchet = byId('hatchet-link');
  hatchet.hidden = true;
  hatchet.removeAttribute('href');
  try {
    const url = new URL(overview.links?.hatchet);
    if (['127.0.0.1', 'localhost'].includes(url.hostname)) {
      url.protocol = location.protocol;
      url.hostname = location.hostname;
    }
    if (url.protocol === location.protocol && url.hostname === location.hostname && url.port === '8888' && !url.username && !url.password) {
      hatchet.href = url.origin + '/';
      hatchet.hidden = false;
    }
  } catch { /* Missing or untrusted destination remains unavailable. */ }
  renderWork();
  updateFreshness();
}
async function request(path, controller = new AbortController()) {
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { headers: { Accept: 'application/json' }, signal: controller.signal });
    if (!response.ok) throw new Error(response.status === 401 ? 'Authentication required. Reopen the console through its private Tailscale URL.' : 'Request failed (HTTP ' + response.status + ').');
    return await response.json();
  } finally { clearTimeout(timeout); }
}
async function loadOverview() {
  const value = await request('/api/overview');
  if (!Array.isArray(value.active_attempts) || !Array.isArray(value.recent_attempts) || !Number.isFinite(Date.parse(value.generated_at))) {
    throw new Error('The overview response is incomplete.');
  }
  overview = value;
  overviewError = '';
  message('notice', '');
  renderOverview();
}
function summaryItem(title, value) {
  const item = make('div', 'summary-item');
  item.append(make('span', '', title), value instanceof Node ? value : make('strong', '', value || 'Unavailable'));
  return item;
}
function renderIssue(value) {
  const record = value.attempts[0] || {};
  const attempt = record.attempt || {};
  const job = record.job || {};
  byId('issue-summary').replaceChildren(
    summaryItem('Recorded stage', pill(attempt.status)),
    summaryItem('Repository', repository(record)),
    summaryItem('Generation / mode', String(attempt.generation ?? '—') + ' / ' + label(attempt.mode)),
    summaryItem('Last recorded change', time(attempt.updated_at)),
  );
  byId('next-step').textContent = attempt.mode === 'dry-run'
    ? 'This is a dry-run attempt. Its simulated result does not establish that a real PR was created or verified.'
    : Object.hasOwn(steps, attempt.status) ? steps[attempt.status] : 'No supported next step can be inferred. Inspect the latest lifecycle evidence and current issue in Linear.';
  const url = prUrl(record);
  const github = byId('github-link');
  github.hidden = !url;
  if (url) github.href = url;
  else github.removeAttribute('href');
  renderTable('issue-attempts', value.attempts, true, 'No attempts returned for this issue.');
  byId('technical-details').replaceChildren();
  for (const [name, detail] of [
    ['Attempt ID', attempt.attempt_id], ['Job ID', job.job_id], ['Branch', job.branch || attempt.branch],
    ['Recorded commit SHA (not live verification)', job.commit_sha], ['Job state', job.state],
    ['Lease state', record.execution_lease?.status], ['Lease expiry', time(record.execution_lease?.expires_at)],
    ['Last lease heartbeat', time(record.execution_lease?.last_heartbeat_at)],
    ['Hermes session reference', job.hermes_session_id], ['Hatchet run reference', job.hatchet_workflow_run_id],
    ['Recorded reason', job.reason_code], ['Recorded error', job.error],
  ]) {
    byId('technical-details').append(make('dt', '', name), make('dd', '', detail || 'Unavailable'));
  }
  const timeline = [...value.timeline].sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0) ||
    String(b.event_id).localeCompare(String(a.event_id))).slice(0, 80);
  byId('issue-timeline').replaceChildren();
  if (!timeline.length) byId('issue-timeline').append(make('li', 'empty', 'No events returned.'));
  timeline.forEach((event) => {
    const item = make('li', 'timeline-item');
    // The API may attach the attempt's current state to old events. Do not color them as historical state transitions.
    item.append(make('strong', '', label(event.event_type)), make('small', '', time(event.created_at) + ' · generation ' + (event.generation ?? 'unknown')));
    const detail = [event.details?.summary, event.details?.reason_code, event.details?.error, event.details?.reason]
      .find((text) => typeof text === 'string' && text.trim());
    if (detail) item.append(make('p', '', detail));
    byId('issue-timeline').append(item);
  });
  byId('issue-content').hidden = false;
}
async function loadIssue(issue, focus = false) {
  const sequence = ++issueSequence;
  issueController?.abort();
  issueController = null;
  if (!issue) {
    byId('issue-detail').hidden = true;
    issueSnapshot = null;
    return;
  }
  byId('issue-detail').hidden = false;
  byId('issue-title').textContent = issue;
  const sameIssue = issueSnapshot?.work_item_key === issue;
  if (!sameIssue) {
    issueSnapshot = null;
    issueLoadedAt = null;
    byId('issue-content').hidden = true;
    byId('github-link').hidden = true;
    byId('github-link').removeAttribute('href');
  }
  message('issue-notice', '');
  byId('linear-link').hidden = !issuePattern.test(issue);
  if (issuePattern.test(issue)) byId('linear-link').href = 'https://linear.app/driftward/issue/' + encodeURIComponent(issue);
  else byId('linear-link').removeAttribute('href');
  byId('issue-freshness').textContent = sameIssue ? 'Refreshing issue evidence…' : 'Loading issue evidence…';
  if (focus) byId('issue-title').focus();
  if (!issuePattern.test(issue)) {
    byId('issue-freshness').textContent = '';
    message('issue-notice', 'Enter an Engineering issue such as ENG-119.');
    return;
  }
  const controller = new AbortController();
  issueController = controller;
  try {
    const value = await request('/api/lifecycle/' + encodeURIComponent(issue), controller);
    if (sequence !== issueSequence || route.issue !== issue) return;
    if (value.work_item_key !== issue || !Array.isArray(value.attempts) || !Array.isArray(value.timeline)) throw new Error('The issue response is incomplete or mismatched.');
    issueSnapshot = value;
    issueLoadedAt = new Date().toISOString();
    renderIssue(value);
    byId('issue-freshness').textContent = 'Evidence retrieved ' + time(issueLoadedAt) + '. Event timestamps below show when changes were recorded.';
  } catch (error) {
    if (sequence !== issueSequence) return;
    message('issue-notice', 'Could not refresh ' + issue + '. ' + (error.name === 'AbortError' ? 'Request timed out.' : error.message));
    byId('issue-freshness').textContent = sameIssue ? 'Stale issue snapshot. Last retrieved ' + time(issueLoadedAt) + '.' : 'No issue snapshot available.';
  } finally {
    if (sequence === issueSequence) issueController = null;
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  byId('refresh-button').disabled = true;
  try { await loadOverview(); }
  catch (error) {
    overviewError = error.name === 'AbortError' ? 'Request timed out.' : error.message;
    message('notice', (overview ? 'Showing the last successful overview. ' : 'Factory data is unavailable. ') + overviewError);
    if (!overview) {
      renderTable('active-attempts', [], false, 'Active work unavailable.');
      renderTable('work-attempts', [], false, 'Work unavailable.');
      byId('recent-attempts').replaceChildren(make('li', 'empty', 'Recent activity unavailable.'));
      byId('work-count').textContent = 'No work snapshot available.';
    }
    updateFreshness();
  } finally {
    refreshing = false;
    byId('refresh-button').disabled = false;
  }
  if (route.issue && !issueController) await loadIssue(route.issue);
}
document.addEventListener('click', (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a');
  if (!link) return;
  if (link.dataset.nav) { event.preventDefault(); navigate({ view: link.dataset.nav, issue: '' }); }
  else if (link.dataset.issue) { event.preventDefault(); navigate({ issue: link.dataset.issue }); }
  else if (link.hasAttribute('data-work-link')) { event.preventDefault(); navigate({ view: 'work', status: 'active', q: '', repo: '', mode: 'all', issue: '' }); }
});
byId('issue-form').addEventListener('submit', (event) => {
  event.preventDefault();
  navigate({ issue: byId('issue-input').value.trim().toUpperCase() });
});
byId('filters').addEventListener('submit', (event) => event.preventDefault());
byId('work-query').addEventListener('input', () => navigate({ q: byId('work-query').value.slice(0, 200) }, true));
for (const [id, key] of [['status-filter', 'status'], ['mode-filter', 'mode'], ['repo-filter', 'repo']]) {
  byId(id).addEventListener('change', () => navigate({ [key]: byId(id).value }));
}
byId('clear-filters').addEventListener('click', () => navigate({ q: '', status: 'all', mode: 'all', repo: '' }));
byId('close-issue').addEventListener('click', () => { navigate({ issue: '' }); byId('issue-input').focus(); });
byId('refresh-button').addEventListener('click', refresh);
window.addEventListener('popstate', () => { route = readRoute(); renderView(); loadIssue(route.issue); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.setInterval(() => { if (!document.hidden) { updateFreshness(); refresh(); } }, 30000);
renderView();
if (route.issue) loadIssue(route.issue);
refresh();
