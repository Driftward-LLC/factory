import { StatusBadge, SummaryItem, Card, Heading, MetadataList, MetadataListItem } from '@driftward/ui';
import { CheckCircledIcon } from '@driftward/ui/icons';
import { time } from './model.js';
import IndependentReview from './IndependentReview.jsx';

const results = {
  recorded: ['', 'Recorded'], passed: ['success', 'Deterministic checks passed (recorded)'],
  failed: ['danger', 'Verification failed'], unavailable: ['', 'Unavailable'],
  invalid: ['warning', 'Evidence invalid'], not_applicable: ['', 'Not applicable'],
};
const inspections = {
  recorded: 'Latest stored PR inspection', stale: 'Stored PR inspection is stale',
  pending: 'PR inspection pending', expired: 'PR inspection expired',
  unavailable: 'PR inspection unavailable', invalid: 'PR inspection evidence is invalid',
};
const comparisons = {
  same_head: 'The latest stored inspection matches the verified SHA. This is an observation, not live verification or review approval.',
  head_changed: 'The inspected head differs from the verified SHA. Earlier verification does not cover that revision.',
  inspection_predates_verification: 'The PR inspection predates verification. A newer observation is needed to compare heads.',
  unavailable: 'There is not enough evidence to compare the verified SHA with an inspected PR head.',
};

function Checkpoint({ title, checkpoint }) {
  const [tone, text] = Object.hasOwn(results, checkpoint?.result) ? results[checkpoint.result] : results.unavailable;
  return <SummaryItem title={title}>
    <StatusBadge tone={tone}>{text}</StatusBadge>
    {checkpoint?.recorded_at && <small>Recorded {time(checkpoint.recorded_at)}</small>}
  </SummaryItem>;
}

export default function VerificationEvidence({ evidence, attemptId }) {
  const supported = evidence?.schema === 'factory-console-verification-v1' && evidence.attempt_id === attemptId;
  const verification = supported ? evidence.verification || {} : {};
  const inspection = supported ? evidence.inspection || {} : {};
  const comparison = supported && Object.hasOwn(comparisons, evidence.head_comparison) ? evidence.head_comparison : 'unavailable';
  const simulated = supported && evidence.mode === 'dry-run';
  return <section className="verification-evidence" aria-labelledby="verification-evidence-title">
    <Heading level={3} id="verification-evidence-title"><CheckCircledIcon aria-hidden="true" /> Verification evidence</Heading>
    {!supported ? <p className="muted">Verification evidence is unavailable for this attempt.</p> : <>
      <p className="muted">Newest returned attempt only. These records do not grant permission to merge or deploy.</p>
      {!evidence.available && <p className="muted">No immutable checkpoints were recorded for this attempt. Historical stage labels are not checkpoint evidence.</p>}
      <Card padding={0} className="evidence-checkpoints">
        <Checkpoint title="Specification snapshot" checkpoint={evidence.specification} />
        <Checkpoint title="Implementation receipt" checkpoint={evidence.implementation} />
        <Checkpoint title="Deterministic verification receipt" checkpoint={verification} />
      </Card>
      {simulated ? <p>Dry-run evidence does not establish a real artifact or verification.</p> : <>
        <div className="evidence-comparison" data-comparison={comparison}>
          <StatusBadge tone={comparison === 'head_changed' || inspection.status === 'invalid' ? 'warning' : ''}>
            {Object.hasOwn(inspections, inspection.status) ? inspections[inspection.status] : inspections.unavailable}
          </StatusBadge>
          <p>{comparisons[comparison]}</p>
          {inspection.status === 'stale' && <p className="muted">This inspection is outside its freshness window. Refreshing this page only rereads stored evidence; confirm the current PR on GitHub.</p>}
        </div>
        <MetadataList columns="multi" className="evidence-facts">
          <MetadataListItem label="Verified SHA">{verification.result === 'passed' ? verification.commit_sha || 'Unavailable' : 'Unavailable'}</MetadataListItem>
          <MetadataListItem label="Inspected PR head">{inspection.head_sha || 'Unavailable'}</MetadataListItem>
          <MetadataListItem label="Verification recorded">{verification.recorded_at ? time(verification.recorded_at) : 'Unavailable'}</MetadataListItem>
          <MetadataListItem label="PR observed">{inspection.observed_at ? time(inspection.observed_at) : 'Unavailable'}</MetadataListItem>
          <MetadataListItem label="Observed PR state">{inspection.pr_state === 'open' ? 'Open' : inspection.pr_state === 'closed' ? inspection.merged === true ? 'Merged' : 'Closed' : 'Unavailable'}</MetadataListItem>
        </MetadataList>
        {verification.result === 'passed' && <details>
          <summary>Recorded check evidence</summary>
          <p className="muted">{verification.checks_passed ?? 'Unavailable'} GitHub checks passed at verification. Local command results are executor-reported, not independent test execution.</p>
          <dl>
            <dt>Reported local commands passed</dt><dd>{verification.local_commands_passed ?? 'Unavailable'}</dd>
            <dt>Verifier policy digest</dt><dd>{verification.policy_digest || 'Unavailable'}</dd>
            <dt>Verification receipt digest</dt><dd>{verification.receipt_digest || 'Unavailable'}</dd>
          </dl>
        </details>}
        <IndependentReview review={evidence.independent_review} attemptId={attemptId} />
      </>}
    </>}
  </section>;
}
