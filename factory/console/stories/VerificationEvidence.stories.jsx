import VerificationEvidence from '../src/VerificationEvidence.jsx';
import '../src/styles.css';

const evidence = {
  schema: 'factory-console-verification-v1', attempt_id: 'fictional-attempt', mode: 'real', available: true,
  specification: { result: 'recorded', recorded_at: '2026-09-20T10:00:00Z' },
  implementation: { result: 'recorded', recorded_at: '2026-09-20T11:00:00Z', commit_sha: 'a'.repeat(40) },
  verification: { result: 'passed', recorded_at: '2026-09-20T11:10:00Z', commit_sha: 'a'.repeat(40), policy_digest: 'b'.repeat(64), receipt_digest: 'c'.repeat(64), checks_passed: 3, local_commands_passed: 2, local_evidence_source: 'executor_reported' },
  inspection: { status: 'recorded', observed_at: '2026-09-20T11:12:00Z', head_sha: 'a'.repeat(40), pr_state: 'open', merged: false, draft: true },
  head_comparison: 'same_head', independent_review: 'unavailable',
};

export default {
  title: 'Factory/VerificationEvidence', component: VerificationEvidence,
  args: { evidence, attemptId: 'fictional-attempt' },
  parameters: { docs: { description: { component: 'Read-only evidence for the newest returned attempt. Fictional data only. A matching stored PR head is not live verification or approval. Changed heads do not mutate historical receipts. Local checks remain executor-reported; independent review is unavailable.' } } },
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
