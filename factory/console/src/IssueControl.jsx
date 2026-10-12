import { useEffect, useRef, useState } from 'react';
import { Button, ExternalLink, Notice, StatusBadge, SummaryItem } from '@driftward/ui';
import { prUrl, referenceUrl, time } from './model.js';

const states = {
  factory: ['Factory-controlled', 'info'],
  handoff_pending: ['Handoff pending', 'warning'],
  manual: ['Manual control', ''],
};
const actionKey = (value) => value.action + ':' + (value.intent || '');
const expiry = (value) => typeof value === 'number' ? value * 1000 : Date.parse(value);
const blockers = {
  trusted_retry_eligibility_failed: 'The current Linear issue must be Ready and unblocked before handback.',
  prior_draft_pr_blocks_retry: 'An open PR requires a continuation path, which is not available in this control.',
  prior_draft_pr_inspection_pending: 'A fresh PR inspection is required before handback.',
  prior_draft_pr_merged_create_new_issue: 'The previous PR is merged. Create a new issue for further work.',
  revision_draft_pr_inspection_pending: 'A fresh PR inspection is required. Request a PR observation, refresh control after it arrives, then preview revision again.',
  revision_requires_open_unmerged_draft_pr: 'Revision requires an open, unmerged draft PR. Inspect the current PR before deciding on further work.',
  revision_draft_pr_head_changed: 'The PR branch or commit changed. Earlier verification does not cover this head; reconcile the artifact before requesting revision.',
  revision_repository_changed: 'The current issue route differs from the recorded PR repository. Reconcile the issue route before revision.',
};

async function request(path, body, signal) {
  const response = await fetch(path, {
    method: body ? 'POST' : 'GET', signal, credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json', 'X-Factory-Action': 'issue-control' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const code = detail?.error || detail?.detail;
    const error = new Error(response.status === 401 || response.status === 403
      ? 'Your console identity is not authorized for this action.'
      : response.status === 409 ? 'The issue changed. Refresh and preview the action again.'
        : Object.hasOwn(blockers, code) ? blockers[code] : `Control request failed (HTTP ${response.status}).`);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  return response.json();
}

function validControl(value, issue) {
  if (value?.schema !== 'factory-console-issue-control-v1' || value.work_item_key !== issue || !Object.hasOwn(states, value.status) ||
      !Number.isInteger(value.version) || !Array.isArray(value.history) ||
      typeof value.actions_enabled !== 'boolean' || typeof value.safe_to_edit !== 'boolean' ||
      !Array.isArray(value.available_actions)) throw new Error('The issue control response is incomplete or mismatched.');
  return value;
}

// A remount fences every response, form and approval to its original issue.
export default function IssueControl({ issue, api = request, onChanged }) {
  return <IssueControlPanel key={issue} issue={issue} api={api} onChanged={onChanged} />;
}

function IssueControlPanel({ issue, api, onChanged }) {
  const path = '/api/issues/' + encodeURIComponent(issue) + '/control';
  const storageKey = 'factory-issue-control:' + issue;
  const [control, setControl] = useState(null);
  const [checkedAt, setCheckedAt] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [needsObservation, setNeedsObservation] = useState(false);
  const [message, setMessage] = useState('');
  const [choice, setChoice] = useState('');
  const [reason, setReason] = useState('');
  const [revision, setRevision] = useState({ feedback: '', review_url: '', design_url: '', reviewer: '', review_model: '' });
  const [preview, setPreview] = useState(null);
  const [unresolved, setUnresolved] = useState(() => {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); }
    catch { return { invalid: true }; }
  });
  const active = useRef(null);
  const mounted = useRef(false);
  const previewHeading = useRef(null);
  const statusHeading = useRef(null);

  async function call(suffix, body, basePath = path) {
    const controller = new AbortController();
    active.current = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try { return await api(basePath + suffix, body, controller.signal); }
    finally { clearTimeout(timeout); if (active.current === controller) active.current = null; }
  }

  async function refresh() {
    if (active.current) return;
    setBusy('refresh');
    try {
      const value = validControl(await call(''), issue);
      if (!mounted.current) return;
      setControl(value);
      setCheckedAt(new Date().toISOString());
      setPreview((previous) => previous && (!value.actions_enabled || previous.control?.version !== value.version ||
        !value.available_actions.some((item) => actionKey(item) === actionKey(previous.body))) ? null : previous);
      setError('');
      setNeedsObservation(false);
    } catch (failure) {
      if (mounted.current) { setError('Control state is unavailable or stale. ' + failure.message); setPreview(null); }
    } finally { if (mounted.current) setBusy(''); }
  }

  useEffect(() => {
    mounted.current = true;
    refresh();
    const whenVisible = () => { if (!document.hidden) refresh(); };
    const timer = setInterval(whenVisible, 30000);
    document.addEventListener('visibilitychange', whenVisible);
    return () => { mounted.current = false; active.current?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', whenVisible); };
  }, []);
  useEffect(() => {
    if (!preview) return;
    previewHeading.current?.focus();
    const timer = setTimeout(() => {
      setPreview(null);
      setMessage('The preview expired. Preview the action again before confirming.');
    }, Math.max(0, expiry(preview.expires_at) - Date.now()));
    return () => clearTimeout(timer);
  }, [preview]);

  const actions = (control?.available_actions || []).filter((item) =>
    ['takeover', 'return', 'revision'].includes(item.action) && typeof item.label === 'string');
  const selected = actions.find((item) => actionKey(item) === choice) || actions[0];
  const isRevision = selected?.action === 'revision';
  const revisionAvailable = actions.some((item) => item.action === 'revision');
  const hasInput = isRevision ? Boolean(revision.feedback.trim()) : Boolean(reason.trim());

  function editRevision(field, value) {
    setRevision((previous) => ({ ...previous, [field]: value })); setPreview(null);
  }

  async function inspectPr() {
    if (active.current || unresolved || !revisionAvailable || (error && !needsObservation)) return;
    setBusy('inspect'); setPreview(null); setMessage('');
    try {
      const value = await call('/revision/inspect', {}, '/api/issues/' + encodeURIComponent(issue));
      if (!mounted.current) return;
      if (value.reason !== 'inspection_queued' || typeof value.attempt_id !== 'string') throw new Error('The observation request response is incomplete.');
      setError(''); setNeedsObservation(false);
      setMessage('PR observation requested. Refresh control after the observation arrives, then preview revision. This does not start implementation.');
    } catch (failure) {
      if (mounted.current) setError('PR observation request could not be confirmed. ' + failure.message);
    } finally { if (mounted.current) setBusy(''); }
  }

  async function prepare(event) {
    event.preventDefault();
    if (active.current || unresolved || !selected || !hasInput) return;
    setBusy('preview'); setPreview(null); setError(''); setMessage('');
    const body = { action: selected.action, ...(selected.intent ? { intent: selected.intent } : {}), reason: isRevision ? revision.feedback.trim().replace(/\s+/g, ' ').slice(0, 320) : reason.trim(),
      ...(isRevision ? { revision: Object.fromEntries(Object.entries(revision).map(([key, value]) => [key, value.trim() || null])) } : {}) };
    try {
      const value = await call('/preview', body);
      if (!mounted.current) return;
      validControl(value.control, issue);
      if (value.work_item_key !== issue || value.action !== body.action || (value.intent || '') !== (body.intent || '') ||
          !Number.isFinite(expiry(value.expires_at)) || expiry(value.expires_at) <= Date.now() ||
          typeof value.snapshot_digest !== 'string' || typeof value.preview_token !== 'string') throw new Error('The action preview is incomplete or expired.');
      if (body.action === 'revision' && value.eligible === true && (
        typeof value.revision?.feedback !== 'string' || !value.revision.feedback.trim() || typeof value.revision.source_attempt_id !== 'string' ||
        !/^[0-9a-f]{40}$/.test(value.revision.reviewed_head_sha) || value.revision.reviewed_head_sha !== value.artifact?.head_sha ||
        !value.artifact?.branch || !prUrl({ job: { pr_url: value.artifact.pr_url, payload: { repo: value.artifact.repository } } })
      )) throw new Error('The revision preview is incomplete or mismatched.');
      setControl(value.control);
      setCheckedAt(new Date().toISOString());
      setPreview({ ...value, body, action_label: selected.label });
    } catch (failure) {
      if (mounted.current) {
        setNeedsObservation(failure.code === 'revision_draft_pr_inspection_pending');
        setError(failure.message + ' Refresh and preview again; no approval is retained.');
      }
    } finally { if (mounted.current) setBusy(''); }
  }

  async function confirm(previous) {
    if (active.current || (!previous && (!preview?.eligible || expiry(preview.expires_at) <= Date.now()))) return;
    const body = previous || { ...preview.body, snapshot_digest: preview.snapshot_digest, preview_token: preview.preview_token, request_id: crypto.randomUUID() };
    setBusy('confirm'); setError(''); setMessage('');
    // Persist before sending: a timeout, reload or navigation must reuse this request ID.
    try { sessionStorage.setItem(storageKey, JSON.stringify(body)); }
    catch { setError('Browser storage is unavailable. No action was sent; safe retry tracking is required.'); setBusy(''); return; }
    setUnresolved(body); setPreview(null);
    try {
      const result = await call('/confirm', body);
      if (result.request_id !== body.request_id) throw new Error('The confirmation response did not match this request.');
      const value = validControl(result.control, issue);
      sessionStorage.removeItem(storageKey);
      if (!mounted.current) return;
      setControl(value); setUnresolved(null); setReason(''); setChoice('');
      setRevision({ feedback: '', review_url: '', design_url: '', reviewer: '', review_model: '' });
      setCheckedAt(new Date().toISOString());
      setMessage(result.message || (body.action === 'revision' ? 'Revision attempt recorded. The issue, PR and branch are retained; earlier attempt history is preserved.' : 'Control change recorded. Current execution ownership is shown below.'));
      statusHeading.current?.focus();
      onChanged?.();
    } catch (failure) {
      if (!mounted.current) return;
      if ([400, 409, 410, 422].includes(failure.status)) {
        sessionStorage.removeItem(storageKey); setUnresolved(null);
        setError(failure.message + ' Refresh and preview again; no approval is retained.');
      } else setError('The action result is unknown. Check the previous request before starting another action.');
    } finally { if (mounted.current) setBusy(''); }
  }

  const [stateLabel, tone] = control ? states[control.status] : ['Control unavailable', ''];
  const artifact = preview?.artifact;
  const artifactUrl = artifact && prUrl({ job: { pr_url: artifact.pr_url, payload: { repo: artifact.repository } } });
  return <section className="issue-control" aria-labelledby="issue-control-title" aria-busy={Boolean(busy)}>
    <div className="section-heading">
      <div><h3 id="issue-control-title" ref={statusHeading} tabIndex={-1}>Execution control</h3><p>Ownership is separate from the Linear issue status.</p></div>
      <Button disabled={Boolean(busy)} onClick={refresh}>Refresh control</Button>
    </div>
    <div className="control-state" role="status">
      <StatusBadge tone={error ? 'warning' : tone}>{stateLabel}{error && control ? ' (stale)' : ''}</StatusBadge>
      {control?.status === 'manual' && control.safe_to_edit === true && !error && !unresolved && !busy && <p>Manual control—safe to edit. Confirmed by the execution authority.</p>}
      {control?.status === 'handoff_pending' && <p>Do not edit yet. Cancellation is requested, but execution or an external effect is still unresolved.</p>}
      {control?.status === 'factory' && <p>Factory may execute authorized work. Take over before making manual changes.</p>}
      {control?.status === 'factory' && control.automatic_intake_blocked && <p>Only explicit future attempts are allowed for this issue. Ordinary intake and old queued events remain fenced after handback.</p>}
      {control?.status === 'manual' && control.safe_to_edit !== true && <p>The issue is on hold. Safe handoff has not been confirmed.</p>}
      {control?.pending_attempts?.length > 0 && <ul className="control-pending">{control.pending_attempts.map((attempt, index) => <li key={attempt.attempt_id || attempt.job_id || index}>
        {attempt.attempt_id || attempt.job_id || 'Unresolved execution'}: {attempt.kind}
        <p>{recoveryGuidance(attempt.recovery?.state)}</p>
        {attempt.recovery?.observed_at && <small>Last recovery observation: {time(attempt.recovery.observed_at)}. This is recorded evidence, not a live execution check.</small>}
      </li>)}</ul>}
    </div>
    <Notice>{error}</Notice>
    {checkedAt && <p className="muted">Control checked {time(checkedAt)}. Refresh before editing; other operators can change ownership.</p>}
    <p className="muted" role="status">{message}</p>
    {!control && !error && <p className="muted">Loading issue control…</p>}
    {unresolved && <div className="control-unresolved">
      <p>A previous control request has an unresolved result. New actions are blocked.</p>
      {!unresolved.invalid && <Button disabled={Boolean(busy)} onClick={() => confirm(unresolved)}>Check previous request</Button>}
      {unresolved.invalid && <p>Stored request data could not be read. Ask an operator to reconcile the issue before submitting another action.</p>}
    </div>}
    {control && <>
      <div className="control-metadata">
        <SummaryItem title="Last changed by">{control.actor || 'No ownership change recorded'}</SummaryItem>
        <SummaryItem title="Last changed">{time(control.updated_at)}</SummaryItem>
        <SummaryItem title="Reason">{control.reason || 'No reason recorded'}</SummaryItem>
      </div>
      {!control.actions_enabled && <p className="muted">Read-only access. Issue control actions require an authorized operator and enabled runtime support.</p>}
      {control.actions_enabled && !actions.length && <p className="muted">No control action is currently available. Refresh after execution settles; unresolved effects require operator inspection.</p>}
      {control.actions_enabled && actions.length > 0 && !unresolved && <form className="control-form" onSubmit={prepare}>
        <label>Control action<select value={selected ? actionKey(selected) : ''} disabled={Boolean(busy)} onChange={(event) => { setChoice(event.target.value); setPreview(null); }}>
          {actions.map((item) => <option key={actionKey(item)} value={actionKey(item)}>{item.label}</option>)}
        </select></label>
        {isRevision ? <>
          <label className="revision-feedback">Revision feedback<textarea required maxLength={6000} rows={5} value={revision.feedback} disabled={Boolean(busy)} placeholder="Describe the review or design changes to make on this draft PR." onChange={(event) => editRevision('feedback', event.target.value)} /></label>
          <label>Review reference (optional)<input type="url" pattern="https://.*" maxLength={1000} value={revision.review_url} disabled={Boolean(busy)} placeholder="https://…" onChange={(event) => editRevision('review_url', event.target.value)} /></label>
          <label>Approved design reference (optional)<input type="url" pattern="https://.*" maxLength={1000} value={revision.design_url} disabled={Boolean(busy)} placeholder="https://…" onChange={(event) => editRevision('design_url', event.target.value)} /></label>
          <details className="revision-review-details"><summary>Optional review attribution</summary>
            <label>Reviewer (optional)<input maxLength={160} value={revision.reviewer} disabled={Boolean(busy)} onChange={(event) => editRevision('reviewer', event.target.value)} /></label>
            <label>Review model (optional)<input maxLength={160} value={revision.review_model} disabled={Boolean(busy)} onChange={(event) => editRevision('review_model', event.target.value)} /></label>
          </details>
          <p className="muted control-handback-note">Confirming a revision authorizes one new attempt on the existing draft PR. References and attribution are operator attestations; they do not prove independent review or approval.</p>
        </> : <label>Reason for this handoff<input required maxLength={320} value={reason} disabled={Boolean(busy)} placeholder="What are you taking over or handing back?" onChange={(event) => { setReason(event.target.value); setPreview(null); }} /></label>}
        {selected?.action === 'return' && <p className="muted control-handback-note">Handback authorizes only the selected action. Old queued events do not regain permission; ordinary intake remains fenced for this issue.</p>}
        <Button type="submit" disabled={Boolean(busy) || Boolean(error) || !hasInput}>{busy === 'preview' ? 'Preparing preview…' : isRevision ? 'Preview revision' : 'Preview control change'}</Button>
        {isRevision && <Button disabled={Boolean(busy) || Boolean(error && !needsObservation)} onClick={inspectPr}>Refresh PR observation</Button>}
      </form>}
      {preview && <div className="control-preview">
        <h4 ref={previewHeading} tabIndex={-1}>{preview.action === 'revision' ? 'Review revision request' : 'Review control change'}</h4>
        <p>{typeof preview.effect_summary === 'string' ? preview.effect_summary : 'Review the current issue and artifact before confirming.'}</p>
        {preview.intent === 'fresh_attempt' && <p>Requires a current Ready, unblocked Linear issue and a fresh inspection of any prior PR. This starts fresh work; it does not continue or verify manual edits.</p>}
        <dl>
          <dt>Issue / action</dt><dd>{issue} / {preview.action_label}</dd>
          {preview.action !== 'revision' && <><dt>Reason</dt><dd>{preview.body.reason}</dd></>}
          <dt>Pull request</dt><dd>{artifactUrl ? <ExternalLink href={artifactUrl}>{artifactUrl}</ExternalLink> : 'No supported pull request returned'}</dd>
          <dt>Target branch / commit</dt><dd>{artifact?.branch || 'Unavailable'} / {artifact?.head_sha || 'Unavailable'}</dd>
          {preview.action === 'revision' && <>
            <dt>Source attempt</dt><dd>{preview.revision?.source_attempt_id || 'Unavailable'}</dd>
            <dt>Revision feedback</dt><dd className="revision-copy">{preview.revision?.feedback || preview.body.revision.feedback}</dd>
            <dt>Review reference (operator attestation)</dt><dd>{referenceLink((preview.revision || preview.body.revision).review_url)}</dd>
            <dt>Approved design reference (operator attestation)</dt><dd>{referenceLink((preview.revision || preview.body.revision).design_url)}</dd>
            <dt>Reviewer / model (operator attestation)</dt><dd>{(preview.revision || preview.body.revision).reviewer || 'Not supplied'} / {(preview.revision || preview.body.revision).review_model || 'Not supplied'}</dd>
          </>}
          {preview.action === 'return' && <><dt>Linear observation used for handback</dt><dd>{time(preview.linear_observed_at)}</dd></>}
          <dt>Preview expires</dt><dd>{time(expiry(preview.expires_at))}</dd>
        </dl>
        {Array.isArray(preview.blockers) && preview.blockers.length > 0 && <ul>{preview.blockers.map((item, index) => <li key={index}>{typeof item === 'string' ? item : item.message || item.reason || 'Operator inspection required'}</li>)}</ul>}
        {preview.action === 'revision' && <p>Confirmation preserves earlier attempt history and pins this PR, branch and full commit. Factory must verify the new attempt separately. Independent review is not verified by these references.</p>}
        {preview.eligible === true ? <div className="control-buttons"><Button disabled={Boolean(busy)} onClick={() => confirm()}>Confirm {preview.action === 'takeover' ? 'manual takeover' : preview.action === 'revision' ? 'revision attempt' : 'return to Factory'}</Button><Button disabled={Boolean(busy)} onClick={() => setPreview(null)}>Dismiss preview</Button></div>
          : <p>This action is blocked. No control change has been made.</p>}
      </div>}
      <details className="control-audit"><summary>Ownership audit history ({control.history.length})</summary>
        <p className="muted">Latest returned ownership changes. Attempt execution history is shown separately.</p>
        <ol className="timeline">{control.history.length ? control.history.map((event, index) => <li className="timeline-item" key={event.request_id || index}>
          <strong>{['takeover', 'take_control'].includes(event.action) ? 'Manual takeover' : ['return', 'return_control'].includes(event.action) ? 'Return to Factory' : event.action || 'Ownership change'}</strong>
          <small>{event.actor || 'Actor unavailable'} · {time(event.created_at || event.updated_at)}</small><p>{event.reason || 'No reason recorded'}</p>
        </li>) : <li className="empty">No ownership changes recorded.</li>}</ol>
      </details>
    </>}
  </section>;
}

function referenceLink(value) {
  const url = referenceUrl(value);
  return url ? <ExternalLink href={url}>{url}</ExternalLink> : value ? 'Unsupported reference' : 'Not supplied';
}

function recoveryGuidance(state) {
  switch (state) {
    case 'running': return 'The executor reported that work was still running. Handoff waits for its result; cancellation does not undo changes already made.';
    case 'completed': return 'An execution receipt was found. Factory must accept the original receipt before handoff is safe.';
    case 'not_found': return 'No durable executor receipt was found. Older attempts require operator inspection of execution and PR evidence; starting another attempt is blocked.';
    case 'outcome_unknown': return 'The executor reserved this work but has no settled result. Operator recovery is required; lease expiry does not prove it stopped.';
    case 'unavailable': return 'The executor could not be reached. Factory will retry receipt lookup through reconciliation. Handoff remains blocked.';
    default: return 'Awaiting execution evidence. Factory can recover a stored receipt without starting another implementation.';
  }
}
