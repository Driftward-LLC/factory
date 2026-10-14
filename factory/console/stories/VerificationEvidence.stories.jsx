import VerificationEvidence from '../src/VerificationEvidence.jsx';
import '../src/styles.css';

const evidence = {
  schema: 'factory-console-verification-v1', attempt_id: 'fictional-attempt', mode: 'real', available: true,
  specification: { result: 'recorded', recorded_at: '2026-09-20T10:00:00Z' },
  implementation: { result: 'recorded', recorded_at: '2026-09-20T11:00:00Z', commit_sha: 'a'.repeat(40) },
  verification: { result: 'passed', recorded_at: '2026-09-20T11:10:00Z', commit_sha: 'a'.repeat(40), policy_digest: 'b'.repeat(64), receipt_digest: 'c'.repeat(64), checks_passed: 3, local_commands_passed: 2, local_evidence_source: 'executor_reported' },
  inspection: { status: 'recorded', observed_at: '2026-09-20T11:12:00Z', head_sha: 'a'.repeat(40), pr_state: 'open', merged: false, draft: true },
  head_comparison: 'same_head', independent_review: { schema: 'factory-console-independent-review-v1', attempt_id: 'fictional-attempt', status: 'not_requested', pass_count: 0, max_passes: 2 },
};

export default {
  title: 'Factory/VerificationEvidence', component: VerificationEvidence,
  args: { evidence, attemptId: 'fictional-attempt' },
  parameters: { docs: { description: { component: 'Read-only evidence for the newest returned attempt. Fictional data only. Deterministic checks and exact-head independent review are separate records. A matching stored PR head is not live verification. Changed heads do not mutate historical receipts. No record grants merge or deploy permission.' } } },
};
export const MatchingInspection = {};
export const HeadChanged = { args: { evidence: { ...evidence, head_comparison: 'head_changed', inspection: { ...evidence.inspection, head_sha: 'd'.repeat(40) } } } };
export const StaleInspection = { args: { evidence: { ...evidence, inspection: { ...evidence.inspection, status: 'stale' } } } };
export const InspectionPredatesVerification = { args: { evidence: { ...evidence, head_comparison: 'inspection_predates_verification', inspection: { ...evidence.inspection, observed_at: '2026-09-20T11:05:00Z' } } } };
export const VerificationFailed = { args: { evidence: { ...evidence, head_comparison: 'unavailable', verification: { result: 'failed', recorded_at: '2026-09-20T11:10:00Z' } } } };
export const LegacyWithoutCheckpoints = { args: { evidence: { ...evidence, available: false, head_comparison: 'unavailable', specification: { result: 'unavailable' }, implementation: { result: 'unavailable' }, verification: { result: 'unavailable' } } } };
export const DryRun = { args: { evidence: { ...evidence, mode: 'dry-run', implementation: { result: 'not_applicable' }, verification: { result: 'not_applicable' } } } };
export const InvalidInspection = { args: { evidence: { ...evidence, head_comparison: 'unavailable', inspection: { status: 'invalid' } } } };
export const Unavailable = { args: { evidence: undefined } };

const review = { schema: 'factory-console-independent-review-v1', attempt_id: 'fictional-attempt', status: 'passed',
  reviewed_sha: 'a'.repeat(40), current_sha: 'a'.repeat(40), pass_count: 1, max_passes: 2,
  reviewer_model: 'gpt-6-astra', reviewer_session_id: 'fictional-review-session', recorded_at: '2026-09-20T11:15:00Z', reason_code: 'ok', findings: [] };
const findings = [{ priority: 1, title: 'Fictional missing error handling', file: 'src/example.ts', line: 23, body: 'This fictional finding illustrates a bounded review record.' }];
export const IndependentReviewPending = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'pending', reviewer_model: null, reviewer_session_id: null } } } };
export const IndependentReviewRunning = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'running' } } } };
export const IndependentReviewChangesRequested = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'changes_requested', findings } } } };
export const IndependentReviewPassed = { args: { evidence: { ...evidence, independent_review: review } } };
export const IndependentReviewNeedsOperator = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'needs_operator', pass_count: 2, reason_code: 'review_budget_exhausted', findings } } } };
export const IndependentReviewUnavailable = { args: { evidence: { ...evidence, independent_review: undefined } } };
export const IndependentReviewUnknown = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'unknown' } } } };
export const IndependentReviewStale = { args: { evidence: { ...evidence, independent_review: { ...review, status: 'stale', current_sha: 'b'.repeat(40) } } } };
export const IndependentReviewInvalidPass = { args: { evidence: { ...evidence, independent_review: { ...review, reviewed_sha: null } } } };
