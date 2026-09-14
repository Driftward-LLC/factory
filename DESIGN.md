# Factory operator interface

Factory is a workbench for the person supervising software delivery. Its visual
identity comes from readable execution records, explicit evidence, and a clear
path from a finding to the affected attempt. React is the frontend foundation.

This direction implements the operator priorities approved in this conversation.
It adapts the purpose, content, and usability checks in
[anti-slop](https://github.com/miqdadbadjuber/anti-slop) to Factory. It does not
install that project's plugin or inherit its setup wizard, approval process,
punctuation bans, or arbitrary limits on visual techniques.

## Operator tasks and information order

1. Find unresolved execution and work awaiting verification.
2. Inspect a recent failure, while seeing that a newer attempt may exist.
3. Check system findings and status delivery failures.
4. Follow active work to its issue, artifact, and recorded evidence.
5. Consult historical totals when needed.

Overview starts with links to review queues. Queue counts use the same returned
records and filters as Work. Unresolved execution means an active unknown
outcome, cancellation request, or expired lease; it does not mean the complete
Linear blocked backlog. Pending verification covers `effect_committed` and
`verification_pending`. Recent failures are historical attempts within the
returned set, not a claim that those issues are currently blocked.

Runtime mode, all-time attempt counts, and terminal reconciliation findings
belong in an expandable ledger summary. They must not compete with the review
queues. System findings are a separate projection and need not match the queue
counts. Do not imply an aggregate finding can be inspected in detail when the
API only provides a count.

Opening an issue shows its inspection view directly. Closing it restores the
previous page and filters. Preserve URL sharing, browser history, keyboard
focus, refresh behavior, and the existing read-only authorization boundary.

## Visual language

Use the existing Factory name and F mark. Avoid invented team avatars or new
brand artwork. The palette is a warm paper background, solid white reading
surfaces, dark green-charcoal navigation, and a restrained gold selection color.
Gold identifies the brand and selected navigation; status colors carry their
own operational meanings.

Use system sans-serif for instructions and reading, and monospace for issue
identifiers and technical references. This keeps the interface locally served,
legible with platform fonts, and free of remote font requests. Use tabular
numerals for comparable counts. Body text is approximately 14–16px; supporting
metadata may be 12–13px with adequate contrast. Headings establish hierarchy
without occupying a marketing-sized hero.

Use a 4px spacing rhythm: 8–12px within a control group, 14–18px in records,
24–28px between work sections. Inputs and reading containers have small,
consistent radii. Prefer separators for ledger rows and real groups of data.
Avoid adding a card around every label, decorative left stripes, and repeating
uppercase captions. Keep a visible, distinct keyboard focus ring.

Status colors supplement text:

- Green: verification recorded; it does not establish current PR acceptance.
- Amber: execution, pending verification, or a cancellation-related state.
- Red: failure or unresolved outcome requiring inspection.
- Blue: links and informational stages.
- Neutral: completed/canceled history, unknown labels, and ordinary metadata.

Use arrows on external links to indicate leaving the console. Navigation within
the console uses its text label and selected state. Introduce motion only for a
specific interaction need and honor reduced-motion preferences.

## Copy and evidence

Write the task or result directly: “Review pending verification”, “Inspect
issue”, “Status delivery failures”. Remove slogans that do not help the
operator decide what to do. Keep qualifiers that materially change meaning:
recorded verification, all modes, source limits, dry runs, and stale snapshots.

Do not infer live checks, merge readiness, model progress, cost, or an absence
of failures from missing evidence. Show unavailable data explicitly. Never
clear a stale warning merely because another refresh has started. Keep dry-run
results distinct from real effects. Test screenshots use disposable fixtures;
label them as test data when sharing them.

Recommendations in the UI must correspond to available evidence and existing
controls. Root-only operations remain in the CLI. Do not add a disabled action,
generic shell proxy, invented metric, or destination that has not been built.

## Responsive behavior

Design for the content as width changes. The sidebar becomes top navigation
when it restricts reading width. Filters become two columns, then one. Phone
layouts keep search visible and collapse secondary filters behind a toggle
that reports the active-filter count. At narrow
widths, attempts become compact entries showing identity, stage, and a suggested
inspection step. Repository, mode, artifact, timestamps, and generation expand
under “Attempt details”. Issue history uses the same treatment.

Desktop and intermediate-width tables may scroll inside an explicitly labeled,
keyboard-focusable region. The page itself must not overflow horizontally.
Primary controls and mobile action links have at least 44px touch height.
Do not reduce text below a useful reading size to fit desktop columns on a phone.

## Verification before handoff

Build the real React bundle and exercise its served assets with the browser
regression check. Verify the review links, filters, issue navigation, history,
expandable details, safe artifact links, and stale/error/empty states. Inspect
screenshots of Overview, Work, and an issue on desktop and mobile. Check
intermediate widths and 200% text scaling, keyboard focus and activation, and
text/background contrast. Screenshot inspection alone does not establish full
accessibility compliance.

The checked-in browser test uses disposable data and never dispatches real
work. Production rollout follows the existing Factory release process.
