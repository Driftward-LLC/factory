const elements = {
  active: document.querySelector('#active-attempts'),
  connection: document.querySelector('#connection-state'),
  githubLink: document.querySelector('#github-link'),
  hatchetLink: document.querySelector('#hatchet-link'),
  issueAttempts: document.querySelector('#issue-attempts'),
  issueDetail: document.querySelector('#issue-detail'),
  issueForm: document.querySelector('#issue-form'),
  issueInput: document.querySelector('#issue-input'),
  issueSummary: document.querySelector('#issue-summary'),
  issueTimeline: document.querySelector('#issue-timeline'),
  issueTitle: document.querySelector('#issue-title'),
  linearLink: document.querySelector('#linear-link'),
  metricActive: document.querySelector('#metric-active'),
  metricMode: document.querySelector('#metric-mode'),
  metricReady: document.querySelector('#metric-ready'),
  metricTotal: document.querySelector('#metric-total'),
  notice: document.querySelector('#notice'),
  recent: document.querySelector('#recent-attempts'),
  refresh: document.querySelector('#refresh-button'),
  updated: document.querySelector('#updated-at'),
};

const issuePattern = /^ENG-[1-9][0-9]*$/;
let selectedIssue = new URLSearchParams(window.location.search).get('issue')?.toUpperCase() || '';
let refreshing = false;

function formatLabel(value) {
  return String(value || 'unknown').replaceAll('_', ' ');
}

function formatTime(value, withDate = false) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    day: withDate ? 'numeric' : undefined,
    hour: '2-digit',
    minute: '2-digit',
    month: withDate ? 'short' : undefined,
  }).format(date);
}

function statusTone(status) {
  const value = String(status || '').toLowerCase();
  if (value.includes('verified') || value.includes('ready') || value === 'completed' || value === 'released') return 'success';
  if (value.includes('fail') || value.includes('cancel') || value.includes('unknown') || value.includes('blocked')) return 'danger';
  if (value.includes('pending') || value.includes('authoriz') || value.includes('running') || value.includes('active')) return 'warning';
  if (value.includes('accepted') || value.includes('reserved')) return 'info';
  return '';
}

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function pill(status) {
  return make('span', `pill ${statusTone(status)}`, formatLabel(status));
}

function issueButton(issue) {
  const button = make('button', 'issue-button', issue);
  button.type = 'button';
  button.addEventListener('click', () => openIssue(issue, true));
  return button;
}

function externalLink(label, url) {
  if (!url) return make('span', '', '-');
  const link = make('a', '', label);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  return link;
}

function tableCell(content) {
  const cell = document.createElement('td');
  if (content instanceof Node) cell.append(content);
  else cell.textContent = content ?? '-';
  return cell;
}

function shortId(value) {
  if (!value) return '-';
  const text = String(value);
  return text.length > 12 ? `${text.slice(0, 8)}...` : text;
}

function prUrl(record) {
  return record.job?.pr_url || record.attempt?.prior_pr_url || null;
}

function attemptRow(record, detailed = false) {
  const attempt = record.attempt || {};
  const job = record.job || {};
  const lease = record.execution_lease || {};
  const row = document.createElement('tr');

  if (!detailed) row.append(tableCell(issueButton(attempt.work_item_key || '-')));
  row.append(tableCell(String(attempt.generation ?? '-')));

  const attemptCell = document.createElement('td');
  attemptCell.append(pill(attempt.status));
  if (!detailed) {
    const code = make('code', '', shortId(attempt.attempt_id));
    code.title = attempt.attempt_id || '';
    attemptCell.append(code);
  }
  row.append(attemptCell);

  const jobCell = document.createElement('td');
  jobCell.append(pill(job.state || 'not assigned'));
  if (job.requested_action) jobCell.append(make('code', '', formatLabel(job.requested_action)));
  row.append(jobCell);

  if (detailed) {
    const branch = make('code', '', job.branch || attempt.branch || '-');
    branch.title = job.branch || attempt.branch || '';
    row.append(tableCell(branch));
    row.append(tableCell(prUrl(record) ? externalLink(`PR #${prUrl(record).split('/').pop()}`, prUrl(record)) : '-'));
  } else {
    row.append(tableCell(pill(lease.status || 'not acquired')));
  }

  row.append(tableCell(formatTime(attempt.updated_at, true)));
  return row;
}

function renderTable(target, records, columns, detailed = false) {
  target.replaceChildren();
  if (!records.length) {
    const row = document.createElement('tr');
    const cell = make('td', 'empty', 'No attempts found');
    cell.colSpan = columns;
    row.append(cell);
    target.append(row);
    return;
  }
  records.forEach((record) => target.append(attemptRow(record, detailed)));
}

function renderRecent(records) {
  elements.recent.replaceChildren();
  if (!records.length) {
    elements.recent.append(make('li', 'empty', 'No attempts recorded'));
    return;
  }
  records.slice(0, 12).forEach((record) => {
    const attempt = record.attempt || {};
    const item = make('li', 'recent-item');
    item.append(issueButton(attempt.work_item_key || '-'));
    const status = make('span', 'recent-status');
    status.append(pill(attempt.status));
    item.append(status);
    item.append(make('span', 'recent-meta', `Generation ${attempt.generation ?? '-'} / ${formatTime(attempt.updated_at, true)}`));
    elements.recent.append(item);
  });
}

function showNotice(message) {
  elements.notice.textContent = message;
  elements.notice.hidden = !message;
}

function setConnection(online) {
  elements.connection.classList.toggle('online', online);
  elements.connection.classList.toggle('offline', !online);
  elements.connection.lastChild.textContent = online ? 'Online' : 'Unavailable';
}

function sumCounts(counts) {
  return Object.values(counts || {}).reduce((total, count) => total + Number(count || 0), 0);
}

async function request(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  const value = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

async function loadOverview() {
  const value = await request('/api/overview');
  const counts = value.status_counts || {};
  elements.metricMode.textContent = formatLabel(value.mode);
  elements.metricActive.textContent = String(value.active_attempts?.length || 0);
  elements.metricReady.textContent = String(counts.verified_ready || 0);
  elements.metricTotal.textContent = String(sumCounts(counts));
  elements.updated.textContent = `Updated ${formatTime(value.generated_at)}`;
  const configuredHatchet = value.links?.hatchet || '';
  elements.hatchetLink.href = configuredHatchet.includes('127.0.0.1')
    ? `${window.location.protocol}//${window.location.hostname}:8888/`
    : configuredHatchet;
  renderTable(elements.active, value.active_attempts || [], 6);
  renderRecent(value.recent_attempts || []);
  setConnection(true);
}

function summaryItem(label, content) {
  const item = make('div', 'summary-item');
  item.append(make('span', '', label));
  if (content instanceof Node) item.append(content);
  else item.append(make('strong', '', content));
  return item;
}

function renderIssueSummary(value) {
  const latest = value.attempts?.[0] || {};
  const attempt = latest.attempt || {};
  const job = latest.job || {};
  elements.issueSummary.replaceChildren(
    summaryItem('Current state', pill(attempt.status || 'not started')),
    summaryItem('Latest generation', String(attempt.generation ?? '-')),
    summaryItem('Job action', formatLabel(job.requested_action || 'not assigned')),
    summaryItem('Last updated', formatTime(attempt.updated_at, true)),
  );
}

function eventDetail(event) {
  const details = event.details || {};
  const candidates = [details.summary, details.error, details.reason_code, details.reason, details.action];
  const direct = candidates.find((value) => typeof value === 'string' && value.trim());
  if (direct) return direct;
  if (details.generation) return `Generation ${details.generation}`;
  if (details.job_id) return `Job ${shortId(details.job_id)}`;
  return '';
}

function renderTimeline(events) {
  elements.issueTimeline.replaceChildren();
  if (!events.length) {
    elements.issueTimeline.append(make('li', 'empty', 'No lifecycle events recorded'));
    return;
  }
  [...events].reverse().slice(0, 80).forEach((event) => {
    const item = make('li', `timeline-item ${statusTone(event.state || event.event_type)}`);
    item.append(make('strong', '', formatLabel(event.event_type)));
    item.append(make('span', 'timeline-meta', `${formatTime(event.created_at, true)} / generation ${event.generation ?? '-'}`));
    const detail = eventDetail(event);
    if (detail) item.append(make('p', 'timeline-detail', detail));
    elements.issueTimeline.append(item);
  });
}

async function openIssue(issue, scroll = false) {
  const normalized = String(issue || '').trim().toUpperCase();
  if (!issuePattern.test(normalized)) {
    showNotice('Enter an Engineering issue such as ENG-119.');
    return;
  }
  showNotice('');
  elements.issueInput.value = normalized;
  elements.issueDetail.hidden = false;
  elements.issueTitle.textContent = normalized;
  elements.issueSummary.replaceChildren(summaryItem('Loading', 'Reading lifecycle...'));
  elements.issueAttempts.replaceChildren();
  elements.issueTimeline.replaceChildren();
  try {
    const value = await request(`/api/lifecycle/${encodeURIComponent(normalized)}`);
    selectedIssue = normalized;
    const url = new URL(window.location.href);
    url.searchParams.set('issue', normalized);
    window.history.replaceState({}, '', url);
    elements.linearLink.href = `https://linear.app/driftward/issue/${encodeURIComponent(normalized)}`;
    const latestPr = (value.attempts || []).map(prUrl).find(Boolean);
    elements.githubLink.hidden = !latestPr;
    if (latestPr) elements.githubLink.href = latestPr;
    renderIssueSummary(value);
    renderTable(elements.issueAttempts, value.attempts || [], 6, true);
    renderTimeline(value.timeline || []);
    if (scroll) elements.issueDetail.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    showNotice(`Could not load ${normalized}: ${error.message}`);
    elements.issueDetail.hidden = true;
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  elements.refresh.disabled = true;
  try {
    await loadOverview();
    if (selectedIssue) await openIssue(selectedIssue);
  } catch (error) {
    setConnection(false);
    showNotice(`Factory data is unavailable: ${error.message}`);
  } finally {
    refreshing = false;
    elements.refresh.disabled = false;
  }
}

elements.issueForm.addEventListener('submit', (event) => {
  event.preventDefault();
  openIssue(elements.issueInput.value, true);
});
elements.refresh.addEventListener('click', refresh);

refresh();
window.setInterval(refresh, 30000);
