import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  DashboardIcon,
  RowsIcon,
  ExclamationTriangleIcon,
  Half2Icon,
  CrossCircledIcon,
  ArrowTopRightIcon,
  ArrowRightIcon,
  ChevronRightIcon,
  ReloadIcon,
  MagnifyingGlassIcon,
  MixerHorizontalIcon,
  CubeIcon,
  LockClosedIcon,
  ActivityLogIcon,
} from '@driftward/ui/icons';
import { Button, Notice, StatusBadge, ExternalLink, Metric, SummaryItem, BrandMark } from '@driftward/ui';
import Analytics from './Analytics.jsx';
import VerificationEvidence from './VerificationEvidence.jsx';
import IssueControl from './IssueControl.jsx';
import {
  activeStates,
  terminalStates,
  stages,
  steps,
  issuePattern,
  label,
  count,
  time,
  duration,
  readRoute,
  routeUrl,
  repository,
  prUrl,
  elapsed,
  needsInspection,
  verificationStates,
  nextAction,
} from './model.js';

function useRoute() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const onPop = () => setRoute(readRoute());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  function navigate(changes, replace = false) {
    const next = { ...route, ...changes };
    history[replace ? 'replaceState' : 'pushState']({}, '', routeUrl(next));
    setRoute(next);
  }
  return { route, navigate };
}

function validateSnapshot(value, path) {
  if (path === '/api/overview') {
    if (
      !Array.isArray(value.active_attempts) ||
      !Array.isArray(value.recent_attempts) ||
      !Number.isFinite(Date.parse(value.generated_at))
    ) {
      throw new Error('The overview response is incomplete.');
    }
  } else if (path.startsWith('/api/analytics?')) {
    if (
      value.schema !== 'factory-console-analytics-v1' ||
      value.window?.days !== Number(new URLSearchParams(path.split('?')[1]).get('days')) ||
      !Number.isFinite(Date.parse(value.generated_at)) ||
      !value.coverage || !value.funnel?.stages || !value.github || !value.recovery ||
      !Array.isArray(value.failures?.by_classification)
    ) throw new Error('The analytics response is incomplete or mismatched.');
    if (value.time_series && (
      value.time_series.timezone !== 'UTC' ||
      !Array.isArray(value.time_series.points) ||
      value.time_series.points.some((point) =>
        !/^\d{4}-\d{2}-\d{2}$/.test(point.date) ||
        !Number.isFinite(Date.parse(point.date)) ||
        ['prs_recorded', 'verified_attempts', 'repair_attempts', 'failed_attempts'].some((key) =>
          point[key] !== null && (!Number.isInteger(point[key]) || point[key] < 0)))
    )) throw new Error('The daily activity response is incomplete.');
  } else if (
    value.work_item_key !== path.split('/').pop() ||
    !Array.isArray(value.attempts) ||
    !Array.isArray(value.timeline)
  ) {
    throw new Error('The issue response is incomplete or mismatched.');
  }
}

// Keep a stale snapshot only for the same resource. Cleanup fences late responses.
function useSnapshot(path) {
  const [snapshot, setSnapshot] = useState({});
  const active = useRef(null);
  const refresh = useCallback(async () => {
    if (!path || active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setSnapshot((previous) => ({
      ...(previous.path === path ? previous : {}),
      path,
      loading: true,
    }));
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (!response.ok)
        throw new Error(
          response.status === 401
            ? 'Authentication required. Reopen the console through its private Tailscale URL.'
            : 'Request failed (HTTP ' + response.status + ').',
        );
      const value = await response.json();
      validateSnapshot(value, path);
      if (active.current === controller)
        setSnapshot({ path, value, loadedAt: new Date().toISOString(), loading: false, error: '' });
    } catch (error) {
      if (active.current === controller)
        setSnapshot((previous) => ({
          ...previous,
          loading: false,
          error: error.name === 'AbortError' ? 'Request timed out.' : error.message,
        }));
    } finally {
      clearTimeout(timeout);
      if (active.current === controller) active.current = null;
    }
  }, [path]);
  useEffect(() => {
    refresh();
    return () => {
      active.current?.abort();
      active.current = null;
    };
  }, [refresh]);
  return { ...(snapshot.path === path ? snapshot : {}), refresh };
}

function Pill({ status }) {
  const [text, tone] = Object.hasOwn(stages, status) ? stages[status] : [label(status), ''];
  return <StatusBadge tone={tone}>{text}</StatusBadge>;
}
function RouteLink({ route, navigate, changes, children, ...props }) {
  return (
    <a
      {...props}
      href={routeUrl({ ...route, ...changes })}
      onClick={(event) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
          return;
        event.preventDefault();
        navigate(changes);
      }}
    >
      {children}
    </a>
  );
}
function IssueLink({ issue, ...navigation }) {
  return issuePattern.test(issue) ? (
    <RouteLink {...navigation} changes={{ issue }} className="issue-link" data-issue={issue}>
      {issue}
    </RouteLink>
  ) : (
    <span>{issue || 'Unknown issue'}</span>
  );
}
function Artifact({ record }) {
  const url = prUrl(record);
  return url ? (
    <ExternalLink className="artifact-link" href={url}>
      PR #{url.split('/').filter(Boolean).pop()} <ArrowTopRightIcon aria-hidden="true" />
    </ExternalLink>
  ) : (
    <span className="muted">Not available</span>
  );
}
function AttemptTable({ id, records = [], detail = false, empty, ...navigation }) {
  return (
    <>
      <div
        className="table-wrap attempt-table"
        role="region"
        aria-label={detail ? 'Scrollable attempt history' : 'Scrollable attempts'}
        tabIndex={0}
      >
        <table>
          <caption className="sr-only">
            {detail ? 'Issue attempt history' : 'Factory attempts'}
          </caption>
          <thead>
            <tr>
              <th>{detail ? 'Generation' : 'Issue / repository'}</th>
              <th>Stage</th>
              <th>Mode</th>
              {!detail && <th>Elapsed</th>}
              <th>Pull request</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody id={id}>
            {!records.length ? (
              <tr>
                <td colSpan={detail ? 5 : 6} className="empty">
                  {empty}
                </td>
              </tr>
            ) : (
              records.map((record) => {
                const attempt = record.attempt || {};
                return (
                  <tr key={attempt.attempt_id}>
                    <td>
                      {detail ? (
                        (attempt.generation ?? 'Unknown')
                      ) : (
                        <>
                          <IssueLink issue={attempt.work_item_key} {...navigation} />
                          <small>{repository(record) || 'Repository unavailable'}</small>
                        </>
                      )}
                    </td>
                    <td>
                      <Pill status={attempt.status} />
                      {!detail && <small>Generation {attempt.generation ?? 'unknown'}</small>}
                    </td>
                    <td>
                      {attempt.mode === 'dry-run'
                        ? 'Dry run'
                        : attempt.mode === 'real'
                          ? 'Real'
                          : 'Unknown'}
                    </td>
                    {!detail && (
                      <td title="From attempt creation to now if active, otherwise to its last recorded update. Not model runtime.">
                        {elapsed(record)}
                        <small>
                          {activeStates.has(attempt.status) ? 'attempt age' : 'recorded span'}
                        </small>
                      </td>
                    )}
                    <td>
                      <Artifact record={record} />
                    </td>
                    <td>{time(attempt.updated_at)}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <ul
        id={id + '-mobile'}
        className="attempt-list"
        aria-label={detail ? 'Issue attempt history' : 'Factory attempts'}
      >
        {!records.length ? (
          <li className="empty">{empty}</li>
        ) : (
          records.map((record) => {
            const attempt = record.attempt || {};
            return (
              <li key={attempt.attempt_id}>
                <div className="attempt-heading">
                  {detail ? (
                    <strong>Generation {attempt.generation ?? 'Unknown'}</strong>
                  ) : (
                    <IssueLink issue={attempt.work_item_key} {...navigation} />
                  )}
                  <Pill status={attempt.status} />
                </div>
                <p className="attempt-action">{nextAction(record)}</p>
                {!detail && issuePattern.test(attempt.work_item_key) && (
                  <RouteLink
                    {...navigation}
                    changes={{ issue: attempt.work_item_key }}
                    className="inspect-link"
                  >
                    Inspect issue
                  </RouteLink>
                )}
                <details>
                  <summary>Attempt details</summary>
                  <dl>
                    <dt>Repository</dt>
                    <dd>{repository(record) || 'Unavailable'}</dd>
                    <dt>Generation / mode</dt>
                    <dd>
                      {attempt.generation ?? 'Unknown'} / {label(attempt.mode)}
                    </dd>
                    <dt>Pull request</dt>
                    <dd>
                      <Artifact record={record} />
                    </dd>
                    <dt>{activeStates.has(attempt.status) ? 'Attempt age' : 'Recorded span'}</dt>
                    <dd>{elapsed(record)}</dd>
                    <dt>Updated</dt>
                    <dd>{time(attempt.updated_at)}</dd>
                  </dl>
                </details>
              </li>
            );
          })
        )}
      </ul>
    </>
  );
}
function allRecords(overview) {
  const records = new Map();
  // Active entries win if an attempt is also present in the recent list.
  for (const record of [...overview.recent_attempts, ...overview.active_attempts]) {
    if (record.attempt?.attempt_id) records.set(record.attempt.attempt_id, record);
  }
  return [...records.values()].sort(
    (a, b) =>
      (Date.parse(b.attempt.updated_at) || 0) - (Date.parse(a.attempt.updated_at) || 0) ||
      String(a.attempt.attempt_id).localeCompare(String(b.attempt.attempt_id)),
  );
}
function hatchetUrl(value) {
  try {
    const url = new URL(value);
    if (['127.0.0.1', 'localhost'].includes(url.hostname)) {
      url.protocol = location.protocol;
      url.hostname = location.hostname;
    }
    if (
      url.protocol === location.protocol &&
      url.hostname === location.hostname &&
      url.port === '8888' &&
      !url.username &&
      !url.password
    )
      return url.origin + '/';
  } catch {
    /* Missing or untrusted destinations stay unavailable. */
  }
  return undefined;
}
function Overview({ overview, error, ...navigation }) {
  const counts = overview?.status_counts;
  const reconciliation = overview?.reconciliation;
  const lifecycle = overview?.lifecycle_outbox;
  const recent = overview?.recent_attempts.slice(0, 8) || [];
  const records = overview ? allRecords(overview) : [];
  const queues = [
    [
      'attention',
      'Needs inspection',
      'Unknown outcomes, cancellation requests, or expired leases.',
      records.filter(needsInspection).length,
    ],
    [
      'verifying',
      'Pending verification',
      'Recorded artifacts awaiting verification.',
      records.filter((record) => verificationStates.has(record.attempt?.status)).length,
    ],
    [
      'failed',
      'Recent failures',
      'Recorded failures. A newer attempt may be active.',
      records.filter((record) => record.attempt?.status === 'failed').length,
    ],
  ];
  return (
    <>
      <section className="review-section" aria-labelledby="review-title">
        <div className="section-heading">
          <div>
            <h2 id="review-title">Needs review</h2>
            <p className="muted">All modes · active + most recent 30 attempts</p>
          </div>
        </div>
        <ul className="review-queues">
          {queues.map(([status, title, description, total]) => (
            <li key={status}>
              <RouteLink
                {...navigation}
                changes={{ view: 'work', status, q: '', repo: '', mode: 'all', issue: '' }}
                data-queue={status}
              >
                <span className="queue-label">
                  {status === 'attention' ? (
                    <ExclamationTriangleIcon aria-hidden="true" />
                  ) : status === 'verifying' ? (
                    <Half2Icon aria-hidden="true" />
                  ) : (
                    <CrossCircledIcon aria-hidden="true" />
                  )}
                  <strong>{title}</strong>
                  <ArrowRightIcon className="queue-arrow" aria-hidden="true" />
                </span>
                <strong className="queue-count" id={'queue-' + status}>
                  {overview ? count(total) : 'Unavailable'}
                </strong>
                <small>{description}</small>
              </RouteLink>
            </li>
          ))}
        </ul>
      </section>
      <section className="panel" aria-labelledby="active-title">
        <div className="section-heading">
          <div>
            <h2 id="active-title">
              Active work{' '}
              <span className="heading-count">
                {overview ? count(overview.active_attempts.length) : '—'}
              </span>
            </h2>
            <p className="muted">Execution and verification, ordered by attention needed.</p>
          </div>
          <RouteLink
            {...navigation}
            changes={{ view: 'work', status: 'active', q: '', repo: '', mode: 'all', issue: '' }}
            data-work-link
          >
            View all <ArrowRightIcon aria-hidden="true" />
          </RouteLink>
        </div>
        <AttemptTable
          id="active-attempts"
          records={
            overview
              ? [...overview.active_attempts].sort((a, b) => {
                  const priority = (record) =>
                    needsInspection(record)
                      ? 0
                      : verificationStates.has(record.attempt?.status)
                        ? 1
                        : 2;
                  return priority(a) - priority(b);
                })
              : []
          }
          empty={
            overview
              ? 'No active attempts in this snapshot.'
              : error
                ? 'Active work unavailable.'
                : 'Loading active work…'
          }
          {...navigation}
        />
      </section>
      <div className="overview-secondary">
        <section className="panel" aria-labelledby="recent-title">
          <div className="section-heading">
            <div>
              <h2 id="recent-title">Recent activity</h2>
              <p className="muted">
                Latest 8 attempts · outcomes are recorded, not live PR status.
              </p>
            </div>
          </div>
          <ol id="recent-attempts" className="recent-list">
            {recent.length ? (
              recent.map((record) => (
                <li className="recent-item" key={record.attempt.attempt_id}>
                  <div>
                    <IssueLink issue={record.attempt.work_item_key} {...navigation} />
                    <small>
                      Generation {record.attempt.generation ?? 'unknown'} ·{' '}
                      {label(record.attempt.mode)} · {time(record.attempt.updated_at)}
                    </small>
                  </div>
                  <Pill status={record.attempt.status} />
                </li>
              ))
            ) : (
              <li className="empty">
                {overview
                  ? 'No recent attempts recorded.'
                  : error
                    ? 'Recent activity unavailable.'
                    : 'Loading recent attempts…'}
              </li>
            )}
          </ol>
        </section>
        <aside className="system-summary" aria-label="System and ledger">
          <section className="attention-section" aria-labelledby="attention-title">
            <div className="section-heading">
              <h2 id="attention-title">
                <ActivityLogIcon aria-hidden="true" /> System status
              </h2>
            </div>
            <div className="signals">
              <article className="signal" data-attention={reconciliation?.actionable > 0}>
                <strong id="signal-actionable">
                  {overview ? count(reconciliation?.actionable) : '—'}
                </strong>
                <h3>Reconciliation findings</h3>
                <p id="signal-actionable-detail">
                  {reconciliation?.actionable_severity
                    ? `${count(reconciliation.actionable_severity.critical ?? 0)} critical · ${count(reconciliation.actionable_severity.warning ?? 0)} warning. Inspect Factory status and issue history.`
                    : 'Severity evidence unavailable.'}
                </p>
              </article>
              <article
                className="signal"
                data-attention={Boolean(lifecycle?.available && lifecycle.dead_letters > 0)}
              >
                <strong id="signal-dead-letters">
                  {!overview
                    ? '—'
                    : lifecycle?.available
                      ? count(lifecycle.dead_letters)
                      : 'Unavailable'}
                </strong>
                <h3>Status delivery failures</h3>
                <p id="signal-dead-letter-detail">
                  {!lifecycle?.available
                    ? 'Source unavailable. Delivery status is unknown.'
                    : lifecycle.dead_letters > 0
                      ? 'Inspect with factory lifecycle dead-letters list. Archive remains root-only.'
                      : 'No permanent delivery failures in this snapshot.'}
                </p>
              </article>
            </div>
          </section>
          <details className="ledger-summary">
            <summary>
              Runtime &amp; ledger <ChevronRightIcon aria-hidden="true" />
            </summary>
            <section aria-label="Factory summary" className="metrics">
              <Metric
                name="Intake mode"
                id="metric-mode"
                value={
                  !overview
                    ? '—'
                    : overview.mode === 'real'
                      ? 'Real'
                      : overview.mode === 'dry-run'
                        ? 'Dry run'
                        : 'Unknown'
                }
              >
                Configured runtime mode
              </Metric>
              <Metric
                name="Active attempts"
                id="metric-active"
                value={overview ? count(overview.active_attempts.length) : '—'}
              >
                All modes · current snapshot
              </Metric>
              <Metric
                name="Verified attempts"
                id="metric-ready"
                value={
                  !overview
                    ? '—'
                    : counts && typeof counts === 'object'
                      ? count(counts.verified_ready ?? 0)
                      : 'Unavailable'
                }
              >
                All modes · all-time ledger state
              </Metric>
              <Metric
                name="Recorded attempts"
                id="metric-total"
                value={
                  !overview
                    ? '—'
                    : counts && Object.values(counts).every((n) => Number.isInteger(n) && n >= 0)
                      ? count(Object.values(counts).reduce((sum, n) => sum + n, 0))
                      : 'Unavailable'
                }
              >
                All modes · all-time ledger
              </Metric>
            </section>
            <p className="historical-note">
              Finished-job findings:{' '}
              <strong id="signal-terminal">
                {overview ? count(reconciliation?.scope?.terminal_work) : 'Unavailable'}
              </strong>
              . Historical findings do not indicate active work.
            </p>
          </details>
        </aside>
      </div>
    </>
  );
}
function Work({ records, available, error, route, navigate }) {
  const [filtersExpanded, setFiltersExpanded] = useState(false);
  const filterCount =
    Number(route.status !== 'all') + Number(route.mode !== 'all') + Number(Boolean(route.repo));
  const repos = new Set(records.map(repository).filter(Boolean));
  if (route.repo) repos.add(route.repo);
  const filtered = records.filter((record) => {
    const attempt = record.attempt || {};
    const groups = {
      all: true,
      active: activeStates.has(attempt.status),
      attention: needsInspection(record),
      verifying: verificationStates.has(attempt.status),
      verified: attempt.status === 'verified_ready',
      failed: attempt.status === 'failed',
      finished: terminalStates.has(attempt.status),
    };
    const search = [attempt.work_item_key, repository(record), record.job?.branch, attempt.branch]
      .join(' ')
      .toLowerCase();
    return (
      groups[route.status] &&
      (route.mode === 'all' || attempt.mode === route.mode) &&
      (!route.repo || repository(record) === route.repo) &&
      search.includes(route.q.trim().toLowerCase())
    );
  });
  return (
    <section className="panel" aria-labelledby="work-title">
      <div className="section-heading">
        <div>
          <h2 id="work-title">Active &amp; recent attempts</h2>
          <p className="muted">
            All active attempts plus the most recent 30. Filters apply to this returned set, not the
            full Linear backlog.
          </p>
        </div>
      </div>
      <nav className="work-scopes" aria-label="Work queues">
        {[
          ['all', 'All attempts'],
          ['active', 'Active'],
          ['attention', 'Needs inspection'],
          ['verifying', 'Verifying'],
          ['failed', 'Failed'],
        ].map(([status, title]) => (
          <RouteLink
            key={status}
            route={route}
            navigate={navigate}
            changes={{ status }}
            aria-current={route.status === status ? 'page' : undefined}
            data-work-scope={status}
          >
            {title}
          </RouteLink>
        ))}
      </nav>
      <form
        id="filters"
        className="filters"
        role="search"
        aria-label="Filter returned attempts"
        onSubmit={(event) => event.preventDefault()}
      >
        <label className="search-filter" htmlFor="work-query">
          Search
          <input
            id="work-query"
            type="search"
            placeholder="Issue, repository, branch"
            autoComplete="off"
            value={route.q}
            onChange={(event) => navigate({ q: event.target.value.slice(0, 200) }, true)}
          />
        </label>
        <Button
          className="filter-toggle"
          type="button"
          aria-controls="secondary-filters"
          aria-expanded={filtersExpanded}
          onClick={() => setFiltersExpanded((expanded) => !expanded)}
        >
          <MixerHorizontalIcon aria-hidden="true" /> Filters
          {filterCount ? ` (${filterCount} active)` : ''}
        </Button>
        <div id="secondary-filters" className="filter-secondary" data-expanded={filtersExpanded}>
          <label htmlFor="status-filter">
            Stage
            <select
              id="status-filter"
              value={route.status}
              onChange={(event) => navigate({ status: event.target.value })}
            >
              <option value="all">All stages</option>
              <option value="active">Active</option>
              <option value="attention">Active: needs inspection</option>
              <option value="verifying">Pending verification</option>
              <option value="verified">Verified (recorded)</option>
              <option value="failed">Failed (recorded)</option>
              <option value="finished">Finished</option>
            </select>
          </label>
          <label htmlFor="mode-filter">
            Mode
            <select
              id="mode-filter"
              value={route.mode}
              onChange={(event) => navigate({ mode: event.target.value })}
            >
              <option value="all">All modes</option>
              <option value="real">Real</option>
              <option value="dry-run">Dry run</option>
            </select>
          </label>
          <label htmlFor="repo-filter">
            Repository
            <select
              id="repo-filter"
              value={route.repo}
              onChange={(event) => navigate({ repo: event.target.value })}
            >
              <option value="">All repositories</option>
              {[...repos].sort().map((repo) => (
                <option key={repo} value={repo}>
                  {repo}
                </option>
              ))}
            </select>
          </label>
        </div>
        <Button
          id="clear-filters"
          type="button"
          onClick={() => navigate({ q: '', status: 'all', mode: 'all', repo: '' })}
        >
          Clear
        </Button>
      </form>
      <p id="work-count" className="muted" role="status" aria-live="polite">
        {available
          ? `${filtered.length} of ${records.length} returned attempts · active + recent, deduplicated by attempt`
          : error
            ? 'No work snapshot available.'
            : 'Waiting for data'}
      </p>
      <AttemptTable
        id="work-attempts"
        records={filtered}
        route={route}
        navigate={navigate}
        empty={
          !available
            ? error
              ? 'Work unavailable.'
              : 'Loading work…'
            : records.length
              ? 'No attempts match these filters. Try clearing them.'
              : 'No active or recent attempts recorded.'
        }
      />
    </section>
  );
}
function IssueDetail({ issue, snapshot, onClose, onControlChange }) {
  const heading = useRef(null);
  useEffect(() => {
    if (issue) heading.current?.focus();
  }, [issue]);
  const { value, error, loading, loadedAt } = snapshot;
  const record = value?.attempts[0] || {};
  const attempt = record.attempt || {};
  const job = record.job || {};
  const valid = issuePattern.test(issue);
  const url = prUrl(record);
  const timeline = [...(value?.timeline || [])]
    .sort(
      (a, b) =>
        (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0) ||
        String(b.event_id).localeCompare(String(a.event_id)),
    )
    .slice(0, 80);
  const details = [
    ['Attempt ID', attempt.attempt_id],
    ['Job ID', job.job_id],
    ['Branch', job.branch || attempt.branch],
    ['Recorded commit SHA (not live verification)', job.commit_sha],
    ['Job state', job.state],
    ['Lease state', record.execution_lease?.status],
    ['Lease expiry', time(record.execution_lease?.expires_at)],
    ['Last lease heartbeat', time(record.execution_lease?.last_heartbeat_at)],
    ['Hermes session reference', job.hermes_session_id],
    ['Hatchet run reference', job.hatchet_workflow_run_id],
    ['Recorded reason', job.reason_code],
    ['Recorded error', job.error],
  ];
  return (
    <section
      id="issue-detail"
      className="issue-detail"
      aria-labelledby="issue-title"
      hidden={!issue}
    >
      <div className="section-heading">
        <div>
          <h2 id="issue-title" ref={heading} tabIndex={-1}>
            {issue || 'Issue'}
          </h2>
        </div>
        <div className="issue-links">
          <ExternalLink
            id="linear-link"
            hidden={!valid}
            href={
              valid ? 'https://linear.app/driftward/issue/' + encodeURIComponent(issue) : undefined
            }
          >
            Linear <ArrowTopRightIcon aria-hidden="true" />
          </ExternalLink>
          <ExternalLink id="github-link" hidden={!url} href={url || undefined}>
            Pull request <ArrowTopRightIcon aria-hidden="true" />
          </ExternalLink>
          <Button id="close-issue" type="button" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
      <p id="issue-freshness" className="muted">
        {!valid
          ? ''
          : error
            ? value
              ? `Stale issue snapshot. Last retrieved ${time(loadedAt)}.`
              : 'No issue snapshot available.'
            : loading || !value
              ? value
                ? 'Refreshing issue evidence…'
                : 'Loading issue evidence…'
              : `Evidence retrieved ${time(loadedAt)}. Event timestamps below show when changes were recorded.`}
      </p>
      <Notice id="issue-notice">
        {!valid
          ? 'Enter an Engineering issue such as ENG-119.'
          : error
            ? `Could not refresh ${issue}. ${error}`
            : ''}
      </Notice>
      {valid && <IssueControl issue={issue} onChanged={onControlChange} />}
      <div id="issue-content" hidden={!value}>
        <div id="issue-summary" className="issue-summary">
          <SummaryItem title="Recorded stage">
            <Pill status={attempt.status} />
          </SummaryItem>
          <SummaryItem title="Repository">{repository(record)}</SummaryItem>
          <SummaryItem title="Generation / mode">
            {String(attempt.generation ?? '—') + ' / ' + label(attempt.mode)}
          </SummaryItem>
          <SummaryItem title="Last recorded change">{time(attempt.updated_at)}</SummaryItem>
        </div>
        <div className="next-step">
          <span className="eyebrow">Next step</span>
          <p id="next-step">
            {attempt.mode === 'dry-run'
              ? 'This is a dry-run attempt. Its simulated result does not establish that a real PR was created or verified.'
              : Object.hasOwn(steps, attempt.status)
                ? steps[attempt.status]
                : 'No supported next step can be inferred. Inspect the latest lifecycle evidence and current issue in Linear.'}
          </p>
        </div>
        <p id="artifact-note" className="muted">
          PR links and commit references are recorded evidence. Stored inspections below are not
          live GitHub state or review approval.
        </p>
        <VerificationEvidence evidence={value?.verification_evidence} attemptId={attempt.attempt_id} />
        <div className="detail-grid">
          <section>
            <h3>Attempt history</h3>
            <AttemptTable
              id="issue-attempts"
              records={value?.attempts}
              detail
              empty="No attempts returned for this issue."
            />
            <details key={issue}>
              <summary>Technical details</summary>
              <dl id="technical-details">
                {details.map(([name, detail]) => (
                  <Fragment key={name}>
                    <dt>{name}</dt>
                    <dd>{detail || 'Unavailable'}</dd>
                  </Fragment>
                ))}
              </dl>
            </details>
          </section>
          <section>
            <h3>Latest returned evidence</h3>
            <p className="muted">
              Up to 80 returned events, newest first. Source and API limits apply; this is not a
              complete audit history.
            </p>
            <ol id="issue-timeline" className="timeline">
              {timeline.length ? (
                timeline.map((event) => {
                  const detail = [
                    event.details?.summary,
                    event.details?.reason_code,
                    event.details?.error,
                    event.details?.reason,
                  ].find((text) => typeof text === 'string' && text.trim());
                  // Current attempt state on old events is not a historical transition.
                  return (
                    <li key={event.event_id} className="timeline-item">
                      <strong>{label(event.event_type)}</strong>
                      <small>
                        {time(event.created_at)} · generation {event.generation ?? 'unknown'}
                      </small>
                      {detail && <p>{detail}</p>}
                    </li>
                  );
                })
              ) : (
                <li className="empty">No events returned.</li>
              )}
            </ol>
          </section>
        </div>
      </div>
    </section>
  );
}
function App() {
  const { route, navigate } = useRoute();
  const analytics = route.view === 'analytics' && !route.issue;
  const overviewSnapshot = useSnapshot('/api/overview');
  const analyticsSnapshot = useSnapshot(analytics ? '/api/analytics?days=' + route.days : null);
  const issueSnapshot = useSnapshot(
    issuePattern.test(route.issue) ? '/api/lifecycle/' + route.issue : null,
  );
  const { value: overview, error: overviewError, refresh: refreshOverview } = overviewSnapshot;
  const { refresh: refreshIssue } = issueSnapshot;
  const { refresh: refreshAnalytics } = analyticsSnapshot;
  const [now, setNow] = useState(Date.now);
  const [issueDraft, setIssueDraft] = useState(route.issue);
  const issueInput = useRef(null);
  useEffect(() => setIssueDraft(route.issue), [route.issue]);
  const refresh = useCallback(() => {
    setNow(Date.now());
    refreshOverview();
    refreshIssue();
    refreshAnalytics();
  }, [refreshOverview, refreshIssue, refreshAnalytics]);
  useEffect(() => {
    const whenVisible = () => {
      if (!document.hidden) refresh();
    };
    const timer = setInterval(whenVisible, 30000);
    document.addEventListener('visibilitychange', whenVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', whenVisible);
    };
  }, [refresh]);
  const records = overview ? allRecords(overview) : [];
  const currentSnapshot = analytics ? analyticsSnapshot : overviewSnapshot;
  const { value: currentValue, error: currentError } = currentSnapshot;
  const age = currentValue ? Math.max(now, Date.now()) - Date.parse(currentValue.generated_at) : NaN;
  const stale = Boolean(currentValue && (currentError || age > 90000 || age < -5000));
  const hatchet = hatchetUrl(overview?.links?.hatchet);
  const navigation = { route, navigate };
  const work = route.view === 'work';
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to work
      </a>
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Driftward Factory home">
          <BrandMark />
          <span>
            <strong>Factory</strong>
            <small>Driftward Engineering</small>
          </span>
        </a>
        <p className="nav-label">Workspace</p>
        <nav aria-label="Main navigation">
          {['overview', 'work', 'analytics'].map((view) => (
            <RouteLink
              key={view}
              {...navigation}
              changes={{ view, issue: '' }}
              data-nav={view}
              aria-current={route.view === view ? 'page' : undefined}
            >
              {view === 'work' ? (
                <RowsIcon aria-hidden="true" />
              ) : view === 'analytics' ? (
                <ActivityLogIcon aria-hidden="true" />
              ) : (
                <DashboardIcon aria-hidden="true" />
              )}
              <span className="nav-text">{view === 'work' ? 'Work' : view === 'analytics' ? 'Analytics' : 'Overview'}</span>
              {view === 'work' ? (
                <span id="nav-work-count">{overview ? records.length : '—'}</span>
              ) : null}
            </RouteLink>
          ))}
        </nav>
        <div className="sidebar-queues">
          <p className="nav-label">Review queues</p>
          <nav aria-label="Review queues">
            {[
              [
                'attention',
                'Needs inspection',
                ExclamationTriangleIcon,
                records.filter(needsInspection).length,
              ],
              [
                'verifying',
                'Verification',
                Half2Icon,
                records.filter((r) => verificationStates.has(r.attempt?.status)).length,
              ],
              [
                'failed',
                'Recent failures',
                CrossCircledIcon,
                records.filter((r) => r.attempt?.status === 'failed').length,
              ],
            ].map(([status, title, QueueIcon, total]) => (
              <RouteLink
                key={status}
                {...navigation}
                changes={{ view: 'work', status, q: '', repo: '', mode: 'all', issue: '' }}
                data-sidebar-queue={status}
                aria-current={work && route.status === status ? 'page' : undefined}
              >
                <QueueIcon aria-hidden="true" />
                <span className="nav-text">{title}</span>
                <span className="nav-count">{overview ? count(total) : '—'}</span>
              </RouteLink>
            ))}
          </nav>
          <p className="nav-scope">Active + recent 30 · all modes</p>
        </div>
        <div className="sidebar-note">
          <ExternalLink id="hatchet-link" hidden={!hatchet} href={hatchet}>
            <CubeIcon aria-hidden="true" /> Hatchet <ArrowTopRightIcon aria-hidden="true" />
          </ExternalLink>
          <div className="operator-label">
            <LockClosedIcon aria-hidden="true" />
            <strong>Operator workspace</strong>
          </div>
          <p>Issue handoffs require an authorized operator. Recovery operations remain in the Factory CLI.</p>
        </div>
      </aside>
      <div className="shell">
        <header className="topbar">
          <span className="breadcrumb">
            <span>Workspace</span>
            <ChevronRightIcon aria-hidden="true" />
            <strong id="breadcrumb">{work ? 'Work' : route.view === 'analytics' ? 'Analytics' : 'Overview'}</strong>
            {route.issue && (
              <>
                <ChevronRightIcon aria-hidden="true" />
                <span>{route.issue}</span>
              </>
            )}
          </span>
          <div className="top-actions">
            <form
              id="issue-form"
              className="issue-search"
              onSubmit={(event) => {
                event.preventDefault();
                const issue = issueDraft.trim().toUpperCase();
                navigate({ issue });
                if (issue === route.issue) refreshIssue();
              }}
            >
              <label className="sr-only" htmlFor="issue-input">
                Go to issue
              </label>
              <div className="input-action">
                <MagnifyingGlassIcon aria-hidden="true" />
                <input
                  id="issue-input"
                  ref={issueInput}
                  name="issue"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Go to issue…"
                  pattern="[Ee][Nn][Gg]-[1-9][0-9]*"
                  required
                  value={issueDraft}
                  onChange={(event) => setIssueDraft(event.target.value)}
                />
                <Button type="submit">Open</Button>
              </div>
            </form>
            <span
              id="connection-state"
              role="status"
              aria-live="polite"
              data-state={!currentValue ? 'offline' : stale ? 'stale' : 'online'}
            >
              {!currentValue
                ? currentError
                  ? 'Unavailable'
                  : 'Connecting'
                : stale
                  ? 'Stale snapshot'
                  : 'Snapshot current'}
            </span>
            <Button
              id="refresh-button"
              type="button"
              disabled={Boolean(overviewSnapshot.loading || issueSnapshot.loading || analyticsSnapshot.loading)}
              onClick={refresh}
            >
              <ReloadIcon aria-hidden="true" />
              <span>Refresh</span>
            </Button>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <h1 id="page-title">
                {route.issue ? 'Issue inspection' : work ? 'Work' : analytics ? 'Analytics' : 'Overview'}
              </h1>
              <p id="page-description" className="muted">
                {route.issue
                  ? 'Review the recorded result and choose the next step.'
                  : work
                    ? 'Search active and recent attempts.'
                    : analytics
                      ? 'Delivery, PR quality signals, and the evidence behind them.'
                      : 'Execution, verification, and delivery.'}
              </p>
            </div>
          </div>
          <Notice id="notice">
            {currentError
              ? (currentValue
                  ? analytics ? 'Showing the last successful analytics snapshot. ' : 'Showing the last successful overview. '
                  : analytics ? 'Analytics data is unavailable. ' : 'Factory data is unavailable. ') + currentError
              : ''}
          </Notice>
          <p id="updated-at" className="freshness">
            {currentValue
              ? `${stale ? 'Last successful snapshot: ' : 'Snapshot: '}${time(currentValue.generated_at)} · age ${duration(age)}. Refreshes every 30s while visible.`
              : 'No snapshot available yet.'}
          </p>
          <div data-view="overview" hidden={route.view !== 'overview' || Boolean(route.issue)}>
            <Overview overview={overview} error={overviewError} {...navigation} />
          </div>
          <div data-view="work" hidden={!work || Boolean(route.issue)}>
            <Work
              records={records}
              available={Boolean(overview)}
              error={overviewError}
              {...navigation}
            />
          </div>
          <div data-view="analytics" hidden={!analytics}>
            <Analytics report={analyticsSnapshot.value} days={route.days}
              onDaysChange={(days) => navigate({ days })} error={analyticsSnapshot.error} />
          </div>
          <IssueDetail
            issue={route.issue}
            snapshot={issueSnapshot}
            onControlChange={refresh}
            onClose={() => {
              navigate({ issue: '' });
              issueInput.current?.focus();
            }}
          />
        </main>
      </div>
    </>
  );
}

createRoot(document.getElementById('root')).render(<App />);
