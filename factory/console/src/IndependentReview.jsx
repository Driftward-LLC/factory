import { StatusBadge, Heading, MetadataList, MetadataListItem } from '@driftward/ui';
import { label, time } from './model.js';

const states = {
  not_requested: ['', 'Not requested'],
  pending: ['warning', 'Pending'],
  running: ['warning', 'Running'],
  changes_requested: ['warning', 'Changes requested'],
  passed: ['success', 'Passed (recorded)'],
  needs_operator: ['danger', 'Needs operator'],
  unavailable: ['', 'Unavailable'],
  stale: ['warning', 'Stale'],
  superseded: ['', 'Superseded by repair'],
};

function projection(review, attemptId) {
  if (review?.schema !== 'factory-console-independent-review-v1' || review.attempt_id !== attemptId
      || !Object.hasOwn(states, review.status)) return { status: 'unavailable' };
  if (review.status === 'passed' && (!/^[0-9a-f]{40}$/.test(review.reviewed_sha || '')
      || !Number.isInteger(review.pass_count) || review.pass_count < 1 || review.pass_count > 2
      || review.max_passes !== 2 || review.reviewer_model !== 'gpt-6-astra'
      || !Number.isFinite(Date.parse(review.recorded_at || '')))) return { ...review, status: 'unavailable' };
  if (review.status !== 'superseded' && /^[0-9a-f]{40}$/.test(review.current_sha || '') && /^[0-9a-f]{40}$/.test(review.reviewed_sha || '')
      && review.current_sha !== review.reviewed_sha) return { ...review, status: 'stale' };
  return review;
}

export function IndependentReviewBadge({ review, attemptId }) {
  const value = projection(review, attemptId);
  const [tone, text] = states[value.status];
  return <StatusBadge tone={tone}>Independent review: {text}</StatusBadge>;
}

export default function IndependentReview({ review, attemptId }) {
  const value = projection(review, attemptId);
  const findings = Array.isArray(value.findings) ? value.findings.slice(0, 20) : [];
  return <section className="independent-review" aria-labelledby="independent-review-title" data-review-status={value.status}>
    <Heading level={4} id="independent-review-title">Independent review</Heading>
    <IndependentReviewBadge review={review} attemptId={attemptId} />
    <p className="muted">Separate from deterministic checks. These are stored review records, not live GitHub state or permission to merge or deploy.</p>
    {value.status === 'not_requested' && <p className="muted">This attempt was not enrolled in independent review. Historical verification labels do not establish independent approval.</p>}
    {value.status === 'unavailable' && <p className="muted">Missing or inconclusive review evidence does not establish approval.</p>}
    {value.status === 'stale' && <p className="muted">A changed recorded head invalidates the earlier review. Its result does not cover the new revision.</p>}
    {value.status === 'superseded' && <p className="muted">The reserved repair supersedes this review in work queues. Its historical result and findings remain recorded for the earlier SHA.</p>}
    {value.inherited_from_attempt_id && <p className="muted">This repair inherits an operator stop from the earlier review. No additional review pass was recorded for the unchanged head.</p>}
    {value.reason_code && <p className="muted">Recorded reason: {value.reason_code}</p>}
    {value.status !== 'not_requested' && <MetadataList columns="multi" className="evidence-facts">
      <MetadataListItem label="Reviewed SHA">{value.reviewed_sha || 'Unavailable'}</MetadataListItem>
      <MetadataListItem label="Recorded current head">{value.current_sha || 'Unavailable'}</MetadataListItem>
      <MetadataListItem label="Review passes">{Number.isInteger(value.pass_count) && value.pass_count >= 0 ? `${value.pass_count} / 2 maximum` : 'Unavailable'}</MetadataListItem>
      <MetadataListItem label="Reviewer model">{value.reviewer_model || 'Unavailable'}</MetadataListItem>
      <MetadataListItem label="Reviewer session reference">{value.reviewer_session_id || 'Unavailable'}</MetadataListItem>
      <MetadataListItem label="Review record updated">{value.recorded_at ? time(value.recorded_at) : 'Unavailable'}</MetadataListItem>
      {value.historical_status && <MetadataListItem label="Historical review result">{label(value.historical_status)}</MetadataListItem>}
      {value.superseded_by_attempt_id && <MetadataListItem label="Reserved repair attempt">{value.superseded_by_attempt_id}</MetadataListItem>}
      {value.inherited_from_attempt_id && <MetadataListItem label="Earlier reviewed attempt">{value.inherited_from_attempt_id}</MetadataListItem>}
    </MetadataList>}
    {findings.length > 0 && <details className="review-findings">
      <summary>Recorded review findings ({findings.length})</summary>
      <ul>{findings.map((finding, index) => <li key={index}>
        <strong>{finding.title || 'Review finding'}</strong>
        {Number.isInteger(finding.priority) && <small>Priority {finding.priority}</small>}
        {finding.body && <p>{finding.body}</p>}
        {finding.file && <p className="muted">{finding.file}{finding.line ? `:${finding.line}` : ''}</p>}
      </li>)}</ul>
    </details>}
  </section>;
}
