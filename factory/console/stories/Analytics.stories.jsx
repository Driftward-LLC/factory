import Analytics from '../src/Analytics.jsx';
import '../src/styles.css';

const coverage = (observed, eligible = 18) => ({ observed, eligible, rate: eligible ? observed / eligible : null, complete: observed === eligible });
const report = {
  schema: 'factory-console-analytics-v1', generated_at: '2026-09-30T12:00:00Z',
  window: { days: 30, started_at: '2026-08-31T12:00:00Z', ended_at: '2026-09-30T12:00:00Z' },
  coverage: { cohort_attempts: 27, cohort_work_items: 13, dry_run_attempts_excluded: 4, legacy_attempts_excluded: 0, bridge_available: true, ingress_available: true },
  funnel: { stages: Object.fromEntries(Object.entries({ attempted: 13, executor_started: 13, draft_pr_available: 13, verified_ready: 4 }).map(([key, count]) => [key, { count, rate_from_attempted: count / 13 }])) },
  github: {
    coverage: coverage(0), merged_prs: 0, closed_unmerged_prs: 0,
    check_suite_observations: { available: true, coverage: coverage(0) },
    reviews: { available: true, coverage: coverage(0), changes_requested_prs: 0, changes_requested_rate: null },
    first_pass_checks: { available: false, success_rate: null },
  },
  recovery: { by_attempt_kind: { initial: { attempts: 18, verified: 3 }, repair: { attempts: 7, verified: 3 }, retry: { attempts: 1, verified: 0 }, absence_retry: { attempts: 1, verified: 0 }, unknown: { attempts: 0, verified: 0 } }, repair_budgets_exhausted: 3 },
  failures: { currently_failed_attempts: 19, by_classification: [{ reason_code: 'verification_github_check_failed', count: 9 }] },
  latency_ms: { effect_committed_to_verified_ready: { p50: 180000, samples: 6, eligible: 25 } },
};

export default {
  title: 'Factory/Analytics', component: Analytics,
  args: { report, days: 30, onDaysChange: () => {} },
  parameters: { docs: { description: { component: 'Factory page composed from shared Metric and Notice components. Fictional data only. Zero GitHub coverage must leave outcome and review signals unavailable. Partial rates describe observed PRs; recorded verification does not establish current review approval.' } } },
};
export const MissingGitHubEvidence = {};
export const PartialReviews = { args: { report: {
  ...report, github: {
    ...report.github, coverage: coverage(6), merged_prs: 3, closed_unmerged_prs: 1,
    reviews: { available: true, coverage: coverage(5), changes_requested_prs: 2, changes_requested_rate: 0.4 },
  },
} } };
export const EmptyWindow = { args: { report: {
  ...report,
  coverage: { ...report.coverage, cohort_attempts: 0, cohort_work_items: 0 },
  funnel: { stages: Object.fromEntries(Object.keys(report.funnel.stages).map(key => [key, { count: 0, rate_from_attempted: null }])) },
  github: { ...report.github, coverage: coverage(0, 0), check_suite_observations: { available: true, coverage: coverage(0, 0) }, reviews: { available: true, coverage: coverage(0, 0), changes_requested_prs: 0, changes_requested_rate: null } },
  recovery: { by_attempt_kind: Object.fromEntries(Object.keys(report.recovery.by_attempt_kind).map(key => [key, { attempts: 0, verified: 0 }])), repair_budgets_exhausted: 0 },
  failures: { currently_failed_attempts: 0, by_classification: [] }, latency_ms: {},
} } };
export const Loading = { args: { report: undefined } };
export const Unavailable = { args: { report: undefined, error: 'Request failed.' } };
