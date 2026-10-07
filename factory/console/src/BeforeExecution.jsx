import { Button, ExternalLink, Notice, StatusBadge } from '@driftward/ui';
import { ArrowTopRightIcon } from '@driftward/ui/icons';
import { time } from './model.js';

export default function BeforeExecution({ intake, onOpen, issue = false, stale = false }) {
  return <section className="panel before-execution" aria-label="Before execution">
    <div className="section-heading"><div>
      <h2>Before execution <span className="heading-count">{intake?.available ? `${intake.items.length} observed` : 'Unavailable'}</span></h2>
      <p className="muted">Linear status is separate from Factory execution. No attempt recorded{issue ? ' for this issue' : ' for these issues'}.</p>
    </div></div>
    <details className="intake-scope"><summary>Source and refresh limits</summary><p className="muted">Recorded webhook observations, not a live Linear backlog or a confirmed execution queue.
      {!issue && ` Up to ${intake?.limit ?? 100} issues from the latest ${intake?.scan_limit ?? 500} observed issues. Attempt filters do not apply here.`}
      {' '}Refresh rereads records; it does not start work. Issues without a received event cannot appear.</p></details>
    <Notice>{stale ? 'Stale dashboard snapshot. These observations may no longer reflect current Linear status.' : ''}</Notice>
    <Notice>{intake?.truncated ? 'Partial list: the observation limit was reached. Use Go to issue to inspect a specific issue.' : ''}</Notice>
    {!intake?.available ? <p className="empty">Linear observations unavailable. This does not mean nothing is waiting.</p>
      : !intake.items.length ? <p className="empty">{issue ? 'No Linear observation received for this issue.' : 'No pre-execution issues found within the observed scope.'} Check Linear for issues Factory has not observed.</p>
      : <ul className="intake-list">{intake.items.map(item => <li key={item.issue}>
        <div className="intake-identity">
          {onOpen ? <Button onClick={() => onOpen(item.issue)}>{item.issue}</Button> : <strong>{item.issue}</strong>}
          <StatusBadge tone={item.linear_status === 'Blocked' ? 'warning' : 'info'}>Linear: {item.linear_status} (observed)</StatusBadge>
        </div>
        <p>{item.removed ? 'Issue removal observed. No execution implied.'
          : item.blocked_by_count > 0 ? `${item.blocked_by_count} blocking relation(s) observed. Resolve prerequisites in Linear before requesting intake.`
          : item.linear_status === 'Blocked' ? 'Blocked in Linear. Blocker details may be missing from the event; open Linear to review prerequisites.'
          : item.dispatch_eligible ? 'Awaiting intake: eligibility was recorded, but no attempt is recorded. This is not confirmation that work is queued or running.'
          : item.dispatch_eligible == null ? 'Intake eligibility was not recorded. Check Linear and Factory controls; no execution can be inferred.'
          : 'Not eligible at the last observation. Review the issue status, prerequisites and Factory controls before requesting intake.'}</p>
        {item.blocked_by?.length > 0 && <p>Observed blockers: {item.blocked_by.map(key => <ExternalLink key={key} href={'https://linear.app/driftward/issue/' + encodeURIComponent(key)}>{key} <ArrowTopRightIcon aria-hidden="true" /></ExternalLink>)}</p>}
        <p className="muted">Last observed {time(item.observed_at)} · Linear updated {time(item.provider_updated_at)}. Not revalidated with Linear.</p>
        <ExternalLink href={'https://linear.app/driftward/issue/' + encodeURIComponent(item.issue)}>Open in Linear <ArrowTopRightIcon aria-hidden="true" /></ExternalLink>
      </li>)}</ul>}
  </section>;
}
