# Factory capability roadmap and operator dashboard

Status: roadmap proposed; first console implementation slice in development.
Date: 2026-09-14.
Source baseline: Factory `41950320dee90c2afffd8ed00aeef127cfbe984f`.

## Direction

Build Factory into a reliable software delivery system with an operator UI.
Keep Hermes as the execution backend and Linear as the work source of truth.
Use Paperclip as a reference for task visibility and operator interaction.
Use Factory.ai as a closer software-delivery reference for lifecycle coverage,
repository readiness, application QA and milestone validation. Prioritize
accepted software and operator effort over matching either product's feature list.

The operator has previously experienced poor Paperclip/Hermes integration.
The specific failure modes are not yet established. Treat session continuity,
completion reporting, cancellation, and authentication attribution as explicit
qualification cases, without assuming which one caused that experience.

## Additional reference: Factory.ai

Reviewed 2026-09-14. Factory.ai is a separate vendor; use "Driftward Factory"
for this repository's product in comparisons and operator-facing descriptions.
This naming distinction does not propose a repository or CLI rename.

Factory.ai's [Software Factory documentation](https://docs.factory.ai/software-factory/overview)
describes lifecycle-stage health and repository/integration coverage. The
combined surface is marked Private Preview, with illustrative dashboard values.
Its [Release surface](https://docs.factory.ai/software-factory/release) is also
Private Preview. These are design references, not independently measured
delivery results or proof that all features are generally available.

Separate documented capabilities include
[automated PR review](https://docs.factory.ai/software-factory/code-review-ci),
[application QA](https://docs.factory.ai/software-factory/automated-qa), and
[Missions](https://docs.factory.ai/missions/overview). The
[planning guide](https://docs.factory.ai/missions/planning) describes validation
at milestone boundaries and reproducible application startup as prerequisites
for useful QA. Its [headless execution interface](https://docs.factory.ai/droid-exec/overview)
supports structured results and session continuation. No Hermes compatibility
or drop-in replacement for Driftward's typed gateway was established here.

### Changes to this plan

1. Add a lifecycle coverage strip to the dashboard after a minimal read
   projection exists: Intake, Implementation, Verification, Review, Release,
   and Feedback. Each stage distinguishes configuration from observed success.
   Use "not configured", "enabled", "needs attention", and "evidence missing"
   with timestamps and repository scope. A running service cannot make a stage
   green by itself. Enablement is not a throughput metric.
2. Add a repository readiness check before expanding automated verification or
   planning. Derive it from reviewed repository configuration: install, start,
   test and cleanup commands; required service fixtures; permitted test target;
   validation policy; and successful evidence from a specific revision. Report
   the missing prerequisite directly rather than inventing an overall score.
3. Extend stage 3 with application-level acceptance flows where appropriate.
   UI work requires relevant browser checks; API and CLI changes require their
   corresponding behavior checks. A blocked environment is a blocked check,
   not a pass. Keep evidence in the existing approved artifact location, with
   only bounded references/digests in Factory. Credential and screenshot
   handling follows the existing execution/artifact boundary.
4. Extend stage 5 with milestone grouping over Linear work, a versioned plan
   and validation checkpoints. Start with sequential dependent tasks. Add
   concurrent execution only when resource limits and artifact isolation have
   been qualified. Reuse Hatchet orchestration and Hermes profiles/skills;
   avoid a second planner-owned task database.
5. After stage 6, assess post-merge observation: deployed revision, rollback,
   incident and regression references, and suggested process improvements.
   Learning produces reviewable test/skill/policy changes; it cannot silently
   relax acceptance rules. Deployment and incident sources need explicit
   producers before their dashboard metrics appear.

The first console implementation slice remains bounded to available reads.
Readiness and coverage are the next read-model additions, not decorative
placeholders implying autonomous release already exists. Before expanding to
the full lifecycle, include Factory.ai in a build-versus-buy evaluation using
actual access, integration fit, costs and matched delivery outcomes; this
reference review does not authorize installing or replacing the runtime.

## What already exists

| Area | Implemented baseline | Gap to address |
| --- | --- | --- |
| Work intake | Signed Linear ingress, readiness/dependency gates, immutable project routing | Explain eligibility and waiting reasons in the UI; later add assisted specification |
| Execution | Hatchet workflows, bridge ledger, fenced Hermes execution, deterministic branches | Bounded progress and backend failure evidence; supported-version contract qualification |
| Verification | PR identity/base/path checks, GitHub check observations, policy-bound reported validation and commit receipts | Independent test evidence and review, current-head approval invalidation |
| Repair | Bounded automatic repair of existing draft PRs and operator-controlled fresh retries | Extend the current lineage to independent-review feedback; avoid another repair engine |
| Console | HTML/CSS/JavaScript, FastAPI, authenticated overview and issue lifecycle | Attention queue, usable work navigation, evidence detail, projects and delivery metrics |
| Analytics | Read-only cohort report over existing ledgers, explicit missing-data semantics | Browser-safe projection and complete prospective GitHub/backend evidence |
| Controls | Root-only retry, cancellation, receipt refresh and dead-letter archive | Operator guidance first; narrowly authorized browser actions later |

Dedicated triage, specification, verification, review and repair workers in
`hatchet/workers/catalog.json` include planned entries. This does not mean that
the existing executor has no verification or automatic repair. Implement new
workflow responsibilities in existing worker packages when their authority
fits; a separate worker process needs an actual isolation or capacity reason.

## Delivery order

Dashboard work and evidence work can proceed independently. Independent review
depends on sound artifact evidence. Broader autonomous planning and execution
should follow review and resource controls.

| Stage | Deliverable | Acceptance gate |
| --- | --- | --- |
| 1. Operator dashboard foundation | Overview, active/recent work, issue detail, attention summaries, explicit stale/unknown states | An operator can identify an active issue, inspect its PR and latest lifecycle evidence, and distinguish unavailable data from an empty queue |
| 2. Execution and delivery evidence | Versioned Hermes progress/failure envelope; authenticated GitHub outcomes; required-check aggregation | New attempts have route/invocation correlation; incomplete evidence stays unknown; duplicate events cannot duplicate work |
| 3. Verification and independent review | Separate verification/review executions bound to PR, head SHA and policy; bounded revision loop | Failed checks block acceptance; implementer cannot self-approve; a changed head invalidates earlier approval; exhausted repair escalates once |
| 4. Project and resource controls | Project views, concurrency/runtime/attempt limits, pause-new-work semantics; usage/cost accounting where supported | Admission limits hold under concurrent requests; unknown usage is explicit; pause does not imply an in-flight external effect was undone |
| 5. Assisted planning and delegation | Versioned specs, acceptance criteria, scoped subtasks and dependencies in Linear; specialist Hermes profiles/skills | Approved spec version follows each attempt; blocked subtasks do not dispatch; one owner and one active effect lineage per work item |
| 6. Operator actions and later delivery automation | Narrow browser preview/confirm flows for selected existing controls; subsequently assess merge/release workflows | Identity, authorization, fresh state, idempotency and audit checks pass; no generic root-operator proxy; merge/release requires separate reviewed policy |

Minimal concurrency, timeout and repair limits are prerequisites for expanding
autonomy, even if the full project/cost UI ships later. Stages 3 and 4 may ship
in small interleaved changes rather than waiting for a complete subsystem.

## Dashboard product brief

Primary operator questions:

1. What needs my attention, and what is the next supported action?
2. What is running, what is it waiting for, and when did we last hear from it?
3. What code was produced, what evidence supports it, and who must review it?
4. Which projects are delivering accepted changes, at what cost and human effort?

Use a persistent navigation rail and a work table with an issue detail pane.
Retain Factory's existing branding. Prioritize readable status, issue identity,
repository, elapsed time, evidence freshness and PR links. Put job UUIDs, lease
epochs and raw event codes in expandable technical detail.

Target navigation, introduced only as each view becomes functional:

| View | Main content | Availability |
| --- | --- | --- |
| Overview | Attention summaries, active work, recent outcomes, source freshness | First release |
| Work | Filterable active/recent attempts; later complete paginated work-item list | First release with explicit scope |
| Issue detail | Current attempt, PR/branch, history, latest known evidence, next step | First release; enrich evidence subsequently |
| Reviews | Current-head verification, reviewer assignment and decisions | After stage 3; verified-ready is not itself a review decision |
| Projects | Route enablement, work, capacity, policy and budget | After project projection and resource controls |
| Delivery | Funnel, latency, repair outcomes and evidence coverage | After sanitized analytics projection |
| System | Actionable reconciliation and delivery health; historical debt separately | Summary first, detailed findings subsequently |

The first screen should group operational attention above the work table.
Historical failures must not automatically become outstanding incidents.
Show separate categories for active work requiring attention, system findings,
and historical debt. A verified receipt means "verified at this SHA/time";
without fresh PR evidence it must not imply the PR is still open or awaiting
review today.

Use the existing 30-second refresh as the initial mechanism. Pause polling in
hidden tabs, prevent overlapping requests, retain the last successful view on
failure, and visibly mark its age. Never animate fabricated model progress.
Use explicit state mappings rather than interpreting substrings of status names.

## First implementation slice

### A. Console navigation and work inspection

Touch `factory/console/index.html`, `styles.css`, and `app.js`. Keep the current
static asset delivery and FastAPI service; no new frontend framework, database,
container, browser credential, or hosting platform is needed for this slice.

- Add Overview and Work navigation with shareable URL query state.
- Reuse `/api/overview` for active work, the most recent 30 attempts, mode,
  all-time attempt-state counts, reconciliation totals and lifecycle availability.
- Show issue, repository when present, readable stage, generation, PR,
  last update and elapsed time where a valid start timestamp exists.
- Provide filters over the returned records, labeled "active and recent".
  These are not a complete Linear backlog or a complete historical search.
- Reuse `/api/lifecycle/{issue}` for issue inspection and attempt history.
  Preserve the existing `?issue=ENG-N` deep link and add back/forward support.
- Explain stage, artifact and freshness in the primary detail pane. Keep raw
  implementation identifiers secondary. Link to GitHub and Linear.
- Use global summary cards only with their actual denominator/time scope.
  The current `status_counts` are attempt counts across the ledger, not a
  30-day conversion funnel or a count of unique work items.
- Label timeline truncation. Current lifecycle assembly has per-source limits,
  a combined limit and a UI cap; it cannot promise a complete history.
- Render server strings as text and validate external-link protocols/hosts.
- Include keyboard navigation, visible focus, readable mobile layout, and
  loading, no-data, unavailable, partial-data and stale-data states.

Acceptance: an operator can navigate from overview to an issue's PR and latest
returned lifecycle evidence without a shell; refresh failure does not erase
context or misrepresent old data as current. No browser mutation endpoint is
introduced. Historical verified attempts are labeled as historical evidence.

### B. Complete work and attention projections

Add bounded, versioned read endpoints in the existing console service only
when the first slice needs data the current endpoints cannot supply:

| Proposed endpoint | Purpose | Required contract |
| --- | --- | --- |
| `GET /api/work-items` | Group attempts by work item; server-side filters and pagination | Stable cursor/order, explicit mode/repository/status filters, snapshot timestamp, limit and truncation metadata |
| `GET /api/attention` | Actionable finding rows and supported next steps | Stable finding identity, scope, severity, first/last seen, evidence link, explicit suggested action without claiming current authorization |
| `GET /api/delivery?days=30` | Browser-safe analytics subset | Version, cohort definition, counts/denominators, coverage, unavailable reasons and freshness; bounded days |

Reuse existing store and analytics functions. Keep response construction as an
allowlist, not an unrestricted serialization of the root operator report.
Start with small bounded queries and one shared in-process cache only if needed
for expensive summaries; no ETL job or parallel analytics database. The current
operator container has a 128 MiB memory limit and 0.10 CPU allocation, so
measure query latency and resource use before polling full analytics.

Authenticated console reads currently trust Tailscale Serve identity on the
private loopback-published service. Check the trusted proxy boundary and
authorized audience before expanding visible fields. Reuse the existing
operator process's read access to the ledgers; never put its bearer token into
the browser or proxy arbitrary `/v1/*` operator calls.

### C. Evidence and review views

Show backend selection, invocation state, verified SHA, policy version, checks,
review decisions and bounded failure stage only when their producers exist.
New optional fields must include unavailable/unsupported semantics for older
attempts. Do not derive provider/authentication success from configuration.
Keep transcripts and provider credentials in Hermes. If a safe session link is
available, navigate to its separately authenticated surface rather than copying
session content into Factory.

## Hermes reliability work

Extend the typed gateway contract rather than rebuilding Hermes orchestration.
Factory owns dispatch/effect state; Hermes owns profiles, skills, sessions,
model/tool execution and credentials. Host gateway/runtime changes belong with
their actual owning repository; keep Factory-side contracts and consumers here.

Define a versioned, sanitized execution envelope with attempt and invocation
IDs, session reference when available, backend version, start/progress/end
timestamps, phase enum, terminal outcome and bounded failure classification.
Phase emission must come from real execution boundaries. A live process or
renewed lease is not evidence that the model made useful progress.

Qualification cases: accepted request with lost response; process restart;
resume the same attempt/session; stale completion from an old lease; timeout
after PR publication; malformed receipt; cancellation requested versus process
actually stopped; authentication failure versus provider failure; unavailable
usage. Reuse the current deterministic branch/effect fence across recovery.
Do not blindly retry an execution whose external outcome is unknown.

Reuse Hermes profiles and skills for implementer and reviewer roles. Independent
review uses a fresh session and an explicit artifact/spec snapshot, with no
implementer conversation memory injected as evidence. Memory may guide process,
but cannot replace verification or authorize work.

## Cost and acceptance contracts

Record usage only from a structured backend producer, with availability and
provenance. Distinguish reported usage, estimated cost and billed cost;
subscription-backed runs must not be described as free or as exact marginal
charges. Project/attempt/time limits can ship before reliable dollar accounting.
Hard dollar admission requires reservation and reconciliation under concurrency;
post-run cost reporting alone cannot guarantee a spending ceiling.

For acceptance, bind specification, repository, PR, head SHA, verifier policy
and reviewer decision. Fetch current artifact state before accepting a decision;
changed code requires fresh relevant evidence. Keep GitHub branch protections
as the merge authority until a separately reviewed merge workflow exists.

Measure eligible-work-to-verified conversion, mature merge outcomes, elapsed
time, retries per accepted change, required operator interventions and actual
human rework when instrumented. Always show denominator, coverage and cohort.
Do not weaken acceptance rules to make the dashboard's success rate look better.

## Later browser actions

Retry, cancellation and receipt refresh need a separate narrow authorization
design before becoming browser actions: authenticated actor/role, CSRF/origin
defense, preview, fresh state or digest, idempotency key and durable audit.
Revalidate after confirmation; disable stale previews. Cancellation must
distinguish fencing future effects from stopping an already-running Hermes job.

Dead-letter archive remains a root-only CLI operation under the current
repository instructions. Live Hatchet pause/remove/apply remains an explicit
reviewed operator operation. Displaying a control is never permission to bypass
those boundaries. Early UI releases should link to the runbook and describe the
supported operation rather than offering a nonfunctional button.

## Validation and rollout

For slice A, use disposable fixture ledgers with the actual console API and
existing console unit tests. Exercise multiple generations, unavailable
lifecycle data, a stale refresh, an invalid issue, and a late response after
switching issues. Verify browser behavior at desktop/mobile sizes and with a
keyboard. No production job is needed to validate navigation.

For added read projections, test authorization, sanitization, pagination,
explicit null/coverage semantics and read-only database access. Run the focused
bridge tests and relevant repository checks. Contract/worker changes also need
contract validation and worker build/tests. Measure the expanded console under
its existing container limits before changing resource allocation.

Implement as small draft PRs: (1) console navigation/work inspection,
(2) work/attention read projections, (3) delivery projection, with Hermes
evidence work proceeding on its own contract path. Publish through the existing
Factory release process after review; retain CLI access for rollback/recovery.

The first console slice is being implemented on
`codex/factory-operator-dashboard`. It uses the existing read endpoints.
Later roadmap stages remain proposed; live workflows, execution mode,
provider subscriptions, credentials, database schema and deployment are unchanged.
