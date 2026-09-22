# Factory operator interface

Factory is a workbench for the person supervising software delivery. Its visual
identity comes from readable execution records, explicit evidence, and a clear
path from a finding to the affected attempt. React is the frontend foundation.

The current visual reference is Linear's public [My issues screen](https://linear.app/docs/my-issues)
and [application redesign](https://linear.app/now/how-we-redesigned-the-linear-ui).
The user approved these references after Mobbin's app library required sign-in.
Use the actual product screens, rather than Linear's marketing page, as the
reference for navigation, list density, status icons, and surface hierarchy.
Factory retains its own identity, read-only controls, and evidence semantics.

Shared components, theme tokens, and icons live in the `@driftward/ui` workspace
at `factory/console/ui/`. Storybook renders the same implementations as the
console. See [the design system guide](docs/design-system.md) for the catalog,
agent access, and contribution checks. Edit Factory layout styles in
`factory/console/src/styles.css`; the served `styles.css` is generated.

## Operator tasks and information order

1. Find unresolved execution and work awaiting verification.
2. Inspect a recent failure, while seeing that a newer attempt may exist.
3. Follow active work to its issue, artifact, and recorded evidence.
4. Check system findings and status delivery failures.
5. Consult historical totals when needed.

Overview starts with a compact, three-part review summary, followed immediately
by active work. Recent activity and system status share the lower desktop view.
The sidebar provides direct links to the same review filters. Queue counts use the same returned
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

Analytics uses its own 7-, 30-, or 90-day attempt cohort, rather than the active
and recent list used by Overview and Work. Show delivery stages, recorded
verification, repair outcomes, and GitHub evidence coverage. Keep work-item,
attempt, and PR counts explicitly labeled. Review rates describe observed
reviews only; unobserved PRs must not become zero merge or review rates. Show
missing evidence and stale snapshots, and preserve the selected period in the
URL. Analytics loads only while its page is open.

The daily activity chart uses UTC event dates and includes work that began
before the selected window. Keep this separate from the creation-cohort totals.
Label partial boundary days and each series' grain; count a PR's first recorded
association without claiming it is GitHub's creation time. Missing dated
evidence stays unavailable, with gaps instead of interpolated or zero values.
Provide keyboard and touch day selection and a table of exact daily values.

## Visual language

Use the existing Factory name and F mark. The workspace uses dark neutral
surfaces: `#101113` navigation, `#151618` canvas, `#191a1d` grouped content,
`#2b2c32` separators, `#ececef` primary text, and `#a1a3ad` supporting text.
Selection is a quiet neutral fill. Lavender is reserved for the existing brand
mark, focus rings, links, and the next-step guidance. No remote assets or fonts
are required to use the console.

Use platform sans-serif for interface text and monospace for issue identifiers
and technical references. The hierarchy is compact: 24px page title, 14px
section titles, 13–14px data and control labels, and 11–12px secondary metadata.
Keep explicit labels and adequate contrast; metadata must remain readable at
200% text size. Counts use tabular numerals.

Use 8–12px control spacing, 12–16px row padding, and 24–32px between sections.
Keep tables and activity lists on the base surface with fine separators.
The review summary is one group with internal dividers, not independent raised
cards. Containers use 6–8px radii; no shadows or decorative artwork are needed.

Icons come from `@radix-ui/react-icons`, pinned and bundled locally. Their
15px design grid fits compact controls better than large illustrative icons.
Use icon plus text for navigation and status. Decorative icons are hidden from
assistive technology. Do not substitute emoji or remove labels to fit icons.

Status icons and color supplement text:

- Green check: verification recorded, not current PR acceptance.
- Amber partial circle: execution or pending work.
- Red crossed circle: failure or unresolved outcome.
- Blue or neutral circle: informational or unknown stage.

Use external-link icons for destinations outside the console. Quick Work
filters and sidebar queue shortcuts must use existing URL-backed filter state.
Lookup belongs in the top toolbar; preserve its accessible label and keyboard
submission. Introduce motion only for an interaction need and honor reduced
motion preferences.

## Copy and evidence

Write the task or result directly: “Needs inspection”, “Pending verification”,
“Status delivery failures”. Remove slogans that do not help the
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

Design for the content as width changes. Review summary columns become compact
rows on phones. Quick Work filters scroll within their navigation region;
secondary filters remain available through the existing filter toggle. The sidebar becomes top navigation
when it restricts reading width. Filters become two columns, then one. Phone
layouts keep search visible and collapse secondary filters behind a toggle
that reports the active-filter count. At narrow
widths, attempts become compact entries showing identity, stage, and a suggested
inspection step. Repository, mode, artifact, timestamps, and generation expand
under “Attempt details”. Issue history uses the same treatment.

Desktop and intermediate-width tables may scroll inside an explicitly labeled,
keyboard-focusable region. The page itself must not overflow horizontally.
Primary controls and mobile action links have at least 44px touch height.
Keep the mobile filter toggle and Clear action on one row to preserve space
for work records.
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
