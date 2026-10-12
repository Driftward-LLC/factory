import { Metric, Notice } from '@driftward/ui';
import { count, duration, label, time } from './model.js';
import AnalyticsTrend from './AnalyticsTrend.jsx';

const percent = (value) => Number.isFinite(value)
  ? new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 }).format(value)
  : 'Unavailable';
const coverageText = (coverage) => coverage
  ? `${count(coverage.observed)} of ${count(coverage.eligible)} PRs observed`
  : 'Coverage unavailable';

export default function Analytics({ report, days, onDaysChange, error }) {
  const stages = report?.funnel.stages;
  const github = report?.github;
  const recovery = report?.recovery;
  const review = github?.reviews;
  const outcomesKnown = github?.coverage?.observed > 0 || github?.coverage?.eligible === 0;
  const reviewObserved = review?.available && review.coverage?.observed > 0;
  const evidence = [
    ['PR outcomes', github?.coverage],
    ['CI check suites', github?.check_suite_observations?.coverage],
    ['Reviews', review?.coverage],
  ];
  const missingEvidence = report && (
    !report.coverage.ingress_available || evidence.some(([, coverage]) => !coverage?.complete)
  );
  const quality = [
    ['first-pass', 'First-pass CI',
      github?.first_pass_checks?.available ? percent(github.first_pass_checks.success_rate) : 'Unavailable',
      'Requires the full required-check result for the original PR commit.'],
    ['review-rate', 'Reviews requesting changes',
      reviewObserved ? percent(review.changes_requested_rate) : 'Unavailable',
      reviewObserved
        ? `${count(review.changes_requested_prs)} of ${count(review.coverage.observed)} reviewed PRs. Coverage: ${coverageText(review.coverage)}.`
        : 'No usable review observations for this cohort.'],
    ['merged', 'Observed merged PRs', outcomesKnown ? count(github.merged_prs) : 'Unavailable',
      'Recorded outcomes only. Unobserved PRs may also have merged.'],
    ['closed', 'Observed closed without merge', outcomesKnown ? count(github.closed_unmerged_prs) : 'Unavailable',
      'Recorded outcomes only. Open and unobserved PRs remain unsettled.'],
  ];
  return (
    <section className="analytics" aria-label="Delivery and PR quality analytics">
      <div className="analytics-toolbar">
        <p className="muted">Real attempts created in this window. Dry runs and legacy imports are excluded.</p>
        <label htmlFor="analytics-days">Time window
          <select id="analytics-days" value={days} onChange={(event) => onDaysChange(Number(event.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </label>
      </div>
      {!report && <p className="empty" role="status">{error ? 'Analytics unavailable. Try refreshing.' : 'Loading analytics…'}</p>}
      {report && <div id="analytics-content">
        <p className="muted analytics-window">{time(report.window.started_at)} – {time(report.window.ended_at)}</p>
        <div className="analytics-metrics">
          <Metric name="Work items" id="analytics-work-items" value={count(report.coverage.cohort_work_items)}>
            Across {count(report.coverage.cohort_attempts)} attempts
          </Metric>
          <Metric name="Tracked PRs" value={count(github.coverage?.eligible)}>
            Unique PRs associated with these attempts
          </Metric>
          <Metric name="Verified (recorded)" id="analytics-verified" value={count(stages.verified_ready?.count)}>
            Work items · current acceptance may differ
          </Metric>
          <Metric name="Repair attempts" value={count(recovery.by_attempt_kind?.repair?.attempts)}>
            {count(recovery.by_attempt_kind?.repair?.verified)} reached recorded verification
          </Metric>
        </div>
        <Notice id="analytics-empty">{report.coverage.cohort_attempts === 0
          ? 'No real attempts were created in this window. Choose a longer time window to inspect earlier work.' : ''}</Notice>
        <Notice id="analytics-coverage-warning">{missingEvidence
          ? 'GitHub evidence is incomplete. Missing checks, reviews, or outcomes cannot establish PR quality. Coverage is shown below.' : ''}</Notice>
        <AnalyticsTrend series={report.time_series} />
        <div className="analytics-columns">
          <section className="panel" aria-labelledby="analytics-delivery-title">
            <h2 id="analytics-delivery-title">Delivery funnel</h2>
            <p className="muted">Unique work items · share of all attempted work</p>
            <ol className="analytics-funnel">
              {[
                ['attempted', 'Attempted'], ['executor_started', 'Execution started'],
                ['draft_pr_available', 'Draft PR available'], ['verified_ready', 'Verified (recorded)'],
              ].map(([key, title]) => <li key={key}>
                <div><span>{title}</span><strong>{count(stages[key]?.count)} <small>{percent(stages[key]?.rate_from_attempted)}</small></strong></div>
                {Number.isFinite(stages[key]?.count) && <progress aria-label={title} value={stages[key].count} max={stages.attempted?.count || 1} />}
              </li>)}
            </ol>
            <p className="muted">Unfinished work is still in progress. Recorded verification is a workflow milestone, not a review approval.</p>
          </section>
          <section className="panel" aria-labelledby="analytics-quality-title">
            <h2 id="analytics-quality-title">PR quality signals</h2>
            <p className="muted">Review rates apply only to PRs with recorded reviews.</p>
            <dl className="analytics-signals">
              {quality.map(([id, title, value, note]) => <div key={id}>
                <dt>{title}<small>{note}</small></dt>
                <dd id={'analytics-' + id}>{value}</dd>
              </div>)}
            </dl>
          </section>
          <section className="panel" aria-labelledby="analytics-recovery-title">
            <h2 id="analytics-recovery-title">Attempt outcomes</h2>
            <p className="muted">One work item can have several attempts.</p>
            <div className="table-wrap" role="region" aria-label="Attempt outcomes by kind" tabIndex={0}>
              <table><thead><tr><th scope="col">Attempt kind</th><th scope="col">Attempts</th><th scope="col">Verified (recorded)</th></tr></thead>
                <tbody>{[
                  ['initial', 'Initial'], ['repair', 'Repair'], ['revision', 'Revision'], ['retry', 'Retry'], ['absence_retry', 'Absence retry'], ['unknown', 'Unknown'],
                ].map(([key, title]) => <tr key={key}><td>{title}</td><td>{count(recovery.by_attempt_kind?.[key]?.attempts)}</td><td>{count(recovery.by_attempt_kind?.[key]?.verified)}</td></tr>)}</tbody>
              </table>
            </div>
            <p className="muted">Revision attempts follow operator feedback on an existing PR; repair attempts respond to verification failure. Repair budgets exhausted: {count(recovery.repair_budgets_exhausted)}. Human rework effort is not measured yet.</p>
          </section>
          <section className="panel" aria-labelledby="analytics-evidence-title">
            <h2 id="analytics-evidence-title">Evidence coverage</h2>
            <p className="muted">Authenticated GitHub events matched to tracked PRs.</p>
            <dl className="analytics-signals">
              {evidence.map(([title, coverage]) => <div key={title}>
                <dt>{title}</dt><dd>{coverageText(coverage)}</dd>
              </div>)}
            </dl>
            <p className="muted">Factory also checks CI during verification. Those checks are separate from the event coverage shown here.</p>
            <p className="muted">Human rework, independent review findings, and post-merge defects are not measured in this report.</p>
          </section>
        </div>
        <details className="analytics-diagnostics">
          <summary>Failure reasons and verification timing</summary>
          <p className="muted">{count(report.failures.currently_failed_attempts)} attempts are currently failed. Reasons below include historical failures, even if later recovered.</p>
          <ul className="analytics-failures">{report.failures.by_classification.map((failure, index) =>
            <li key={index}><span>{label(failure.reason_code)}</span><strong>{count(failure.count)}</strong></li>)}</ul>
          {!report.failures.by_classification.length && <p className="muted">No failure evidence recorded for this cohort.</p>}
          <p className="muted">Committed effect to recorded verification: median {duration(report.latency_ms?.effect_committed_to_verified_ready?.p50)} · {count(report.latency_ms?.effect_committed_to_verified_ready?.samples)} samples of {count(report.latency_ms?.effect_committed_to_verified_ready?.eligible)} eligible attempts.</p>
          <p className="muted">Excluded: {count(report.coverage.dry_run_attempts_excluded)} dry-run attempts and {count(report.coverage.legacy_attempts_excluded)} legacy imports.</p>
        </details>
      </div>}
    </section>
  );
}
