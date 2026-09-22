# Factory analytics v1

Factory analytics answers operational questions from the durable evidence the
Factory already owns. It is a read-only projection over the bridge, ingress,
and lifecycle SQLite ledgers. The operator CLI exposes the full report; the
console's Analytics tab exposes a smaller aggregate view of the same evidence.

The report schema is `factory-analytics-v1`. Missing evidence is returned as
`unknown`, `null`, incomplete coverage, or an entry in `unavailable_metrics`.
The reporter must not infer a provider, authentication path, or failure domain
from a reason-code prefix, a Hermes session ID, or the presence of a PR.

## Operator command and API

Use the root-only Factory front door:

```bash
factory analytics
factory analytics --days 7
factory analytics --days 30 --json
```

`--days` defaults to `30` and accepts an integer from `1` through `365`.
Formatted output requires `jq`; `--json` returns the report unchanged.
The formatted view prints canonical metrics as `unavailable` when their JSON
value or required provenance is unavailable; it never renders a missing value
as a measured zero.

The CLI calls `GET /v1/analytics?days=N` through the root-owned Factory
operator Unix socket. The endpoint requires the operator bearer token, accepts
only one optional `days` query parameter, and returns the
`factory-analytics-v1` JSON object. The full report is not exposed by the public
ingress, the worker-facing bridge API, or the read-only web console.

The operator opens the existing database files read-only. The bridge ledger is
required; an unavailable bridge ledger fails the request. Ingress and lifecycle
are optional sources: an unavailable or incompatible optional ledger produces
`available: false` and `null` dependent metrics without suppressing bridge
results. An ingress event table may still support publication counts while a
legacy receipt table lacks signature provenance or while receipts cannot be
joined by exact `event_key`. In either case authenticated Ready
counts/correlation and canonical GitHub outcome/check/review metrics are
`null` or `available: false`; unknown-authenticity observations remain
explicitly observation-only.

### Console Analytics tab

The authenticated console provides `GET /api/analytics?days=N`, with schema
`factory-console-analytics-v1`. It accepts one optional integer `days` from
`1` through `365`, defaulting to `30`; duplicate or unknown parameters return
`400`. It uses the existing Tailscale identity boundary and no-store security
headers. A bridge/report failure returns the generic `503` response
`{"error":"analytics unavailable"}` without source paths or exception details.

This endpoint projects only window bounds, cohort/exclusion counts, delivery
funnel counts and rates, GitHub observation coverage and outcomes, check-suite
and review aggregates, repair counts, bounded failure classifications, and
stage latency distributions. Coverage retains its `observed`, `eligible`,
`rate`, and `complete` fields; missing evidence stays `null` or
`available: false`. Check-suite observations do not become first-pass CI.
Changes-requested rates use PRs with review observations as their denominator.
Readiness still means recorded verification, not reviewer approval.

The projection explicitly allows each field and stage name. Failure domains
and stages use the bridge's fixed enums; reasons use the console's fixed
allowlist, with other values grouped as `unknown`. It excludes source paths,
raw events, issue/repository/PR identifiers, backend/configuration labels,
provider/authentication settings, session material, and operator actions.
Both console entry points receive the optional ingress ledger path from
`HATCHET_FACTORY_OPERATOR_INGRESS_DB`; the existing lifecycle path is reused.
All three sources are opened read-only through the existing reporter. There
is no new database, background ingestion, migration, or operator API proxy.

The top-level JSON sections are:

| Key | Contents |
| --- | --- |
| `schema`, `generated_at`, `window` | Report version and cohort bounds |
| `coverage` | Source availability, exclusions, and attribution completeness |
| `intake` | Linear eligibility, exact event-to-attempt correlation, rejection, publication, and webhook-receipt counts |
| `funnel` | Unique-work-item delivery stages |
| `outcomes` | Attempt status and exact-match GitHub outcomes |
| `github` | PR state freshness/maturity plus safe check-suite and review observations |
| `latency_ms` | Attempt-stage duration distributions |
| `failures` | Attempt failure evidence and backend-invocation classifications |
| `recovery`, `safety`, `human` | Retry/repair performance, effect safety, and review readiness |
| `backends`, `backend_summary` | Observed invocation outcomes by backend/transport/repository/attempt kind |
| `routes` | Preflight intent-to-treat cohorts and their downstream outcomes |
| `activity` | Events observed in the window plus current active-backlog age |
| `delivery` | Ingress, attempt, inspection, and lifecycle outbox health |
| `reconciliation` | Current/historical findings by kind, severity, and scope |
| `sources` | Schema contract, rows, timestamps, lag, and invalid/future-time evidence |
| `lifecycle` | Lifecycle outbox delivery and dead-letter state |
| `unavailable_metrics` | Questions the current evidence cannot support |

Source ownership and query paths are:

| Source | Docker runtime path | Tables read | Query surface |
| --- | --- | --- | --- |
| Bridge ledger | `/var/lib/driftward-hatchet-bridge/bridge.db` | `factory_attempts`, `factory_attempt_events`, `bridge_jobs`, `bridge_events`, `reconciliation_findings` | Required source for every report |
| Event-ingress ledger | `/var/lib/driftward-hatchet-event-ingress/ingress.db` | `ingress_events`, `ingress_receipts` | Optional intake, delivery, redelivery, check/review, and GitHub outcome projection |
| Lifecycle ledger | `/var/lib/driftward-factory-lifecycle-writer/outbox.db` | `lifecycle_outbox` | Optional delivery/dead-letter projection |

There is no analytics table, write path, periodic ETL job, or historical
backfill. Query the full report through `factory analytics` or its aggregate
view through the console; use the underlying ledgers directly only for
focused forensic debugging.

## Window, cohort, and grains

All report timestamps are UTC. The selected interval is inclusive:
`window.started_at <= factory_attempts.created_at <= window.ended_at`.

The primary cohort is every attempt created in that interval where:

- `mode = real`; and
- `actor != legacy-import`.

Dry runs and legacy-imported attempts are excluded and reported separately in
`coverage`. The same work item may have more than one attempt in the cohort.
Attempt and bridge outcomes linked to the cohort are observed through report
generation time, even if their events occurred after the attempt-creation
window. This is a right-censored cohort: a recent attempt may still be active,
so absence of a terminal outcome is not a failure.

Bridge queries run inside one read transaction. Mutable attempt/job state is
current at that bridge snapshot, as declared by `window.mutable_state`; event
queries are bounded by `window.outcomes_observed_through`. The optional ingress
and lifecycle files each use their own read transaction, so the three ledgers
are consistent individually but are not a distributed atomic snapshot.

The report deliberately uses different grains:

| Section | Grain | Exact rule |
| --- | --- | --- |
| `intake` | ingress event; `eligible_work_items` is unique `subject` | Linear ingress rows created inside the report window |
| `intake.correlation` | signature-verified eligible ingress `event_key`; work-item latency starts at the first authenticated eligible event | Any authenticated eligible key for that subject may exactly match an attempt `request_id` or the linked job's `source_event_key(s)`; linked lineage and authenticated GitHub outcomes include reused attempts created before the primary cohort window; real-created, active-reused, dry-run-only, legacy-only, subject-mismatched, unlinked, and ambiguous events remain distinct |
| `intake.repeated_eligible_event_rate` | signature-verified eligible Linear ingress event | Authenticated events beyond the first authenticated eligible event for each `subject`, divided by all authenticated eligible Linear events; the raw all-eligible repeat count remains observation-only, and neither measure is webhook redelivery |
| `intake.correlation.active_attempt_reuse_rate` | signature-verified eligible Ready request | Exactly correlated `active_attempt_reused` outcomes divided by authenticated eligible Ready events |
| `intake.receipts` | webhook receipt | Append-only receipt rows received inside the window; duplicate and payload-conflict receipts are redeliveries |
| `funnel` | unique `work_item_key` | A work item reaches a stage if any cohort attempt has the required durable marker |
| `outcomes.by_status` | attempt | Current status of each cohort attempt at the bridge read snapshot |
| `latency_ms` | attempt with both endpoint events | One nonnegative duration sample per qualifying attempt |
| `failures.attempts_with_failure_evidence` | attempt | Preferred `transition:failed` classification when present; otherwise the latest `execution_backend_failed`, `execution_dispatch_failed`, or `execution_effect_commit_failed` boundary; current failed status remains the compatibility fallback |
| `failures.backend_invocations` | unique `(attempt_id, invocation_id)` | Explicit `execution_backend_failed` evidence only; invocation IDs are attempt-scoped |
| `backends` | backend/transport/version/auth group | Attempts and unique attempt-scoped invocations carrying explicit backend telemetry |
| `routes` | immutable route assignment | Every cohort attempt with `execution_route_selected`, grouped by experiment, variant, configured route, release/policy, repository, and attempt kind |
| `activity` | event observed in the selected interval | Includes events for attempts created before the primary cohort; current backlog is a report-time gauge |
| `delivery` | durable outbox or ingress event | Cohort delivery latency/retries plus global current backlog/age |
| `reconciliation` | finding row | A row is current when seen within the documented freshness interval; occurrences remain separate from finding count |
| `human.work_items_ready_for_review` | unique work item | A cohort work item has reached the durable verified-ready stage; this is readiness, not proof that a human received or acted on a handoff |
| `human.exception_intervention_attempts` | attempt | Distinct cohort attempts with an explicit intervention-required or recovery marker |
| `lifecycle.cohort_records` | lifecycle outbox row | Rows linked to cohort job IDs |
| `lifecycle.current_dead_letters` | lifecycle outbox row | All rows currently in `dead_letter`, independent of the cohort window |
| `lifecycle.dead_letter_event_reasons` | lifecycle outbox row | Source-event semantic reason for each current dead letter |
| `lifecycle.dead_letter_delivery_errors` | lifecycle outbox row with `last_error_code` | Delivery failure cause, kept separate from the source-event reason |

`outcomes.active_attempts`, `outcomes.terminal_attempts`, and
`outcomes.right_censored` make unfinished cohort work explicit in both the JSON
and formatted report.

The funnel denominator is `attempted`, the number of unique cohort work items.
Every `rate_from_attempted` uses that same denominator; it is not conversion
from the immediately preceding stage.

Funnel stage rules are:

| Stage | Durable evidence |
| --- | --- |
| `attempted` | At least one cohort attempt exists for the work item |
| `executor_started` | At least one `attempt_started` event |
| `effect_committed` | At least one `attempt_effect_committed` event, with the linked `transition:effect_committed` bridge event as a compatibility fallback |
| `draft_pr_available` | At least one cohort attempt has a valid linked-job PR URL or a repair's exact `prior_pr_url`; a prior URL establishes availability but does not create a new draft timestamp |
| `verified_ready` | At least one `attempt_verified_ready` event |

GitHub merge and closed-unmerged outcomes use the canonical latest GitHub
ingress observation by `provider_updated_at_us` and `event_id` whose `prUrl`
exactly matches a current or prior PR URL in the cohort and whose event key has
signature-verified receipt evidence. The observation must be no earlier than
the cohort's first durable association with that exact PR: repair targets bind
at reservation, produced PRs bind at effect commit, and historical rows may
use the linked job's update time as a conservative upper bound. A later
association never retroactively claims an earlier merge. They are not guessed
from bridge state.
`outcomes.github_observation_coverage` reports how many unique cohort PR URLs
have such an observation; `outcomes.github_grain` is `unique_pr_url`, separate
from the attempt-grain status counts. If receipt authenticity provenance is
unavailable, canonical outcome counts and coverage are `null`; unverified
matching observations never become zero-valued canonical outcomes.

Provider time and Factory receipt time have different authority. A valid,
explicit provider timestamp is required for merge occurrence time, Ready-to-
merge latency, merge-within-horizon, and repair-efficiency attribution.
Receipt-fallback observations may still describe the latest received PR state,
but a delayed delivery cannot establish when the merge happened; therefore
`github.merge_timestamp_coverage` and every temporal metric keep that outcome
unattributed/censored.

## Canonical evidence

Analytics v1 reuses append-only events and state already required for safe
execution. The names below are the canonical analytics markers. Hatchet wire
events remain defined in `hatchet/contracts/events.json`; declaring a wire
event is not evidence until a durable producer records it in one of these
ledgers.

### Event taxonomy and ownership

Counters, durations, and gauges are projections; producers emit audit records,
not pre-aggregated metrics. A counter counts canonical records at the stated
grain, a duration joins two timestamps for the same identity, and a gauge reads
current state at report time. This keeps a failed delivery or report rerun from
incrementing a second mutable counter.

The v1 event registry is below. `produced` means a durable producer exists now.
`reserved` defines the name, owner, and minimum payload for a future producer;
analytics must continue to report the dependent metric as unavailable until
that producer is connected.

| Canonical evidence/event | Owner | Kind | Minimum identity or fields | State |
| --- | --- | --- | --- | --- |
| `ingress_events[source=linear]` | event ingress | audit; intake counters | `event_id`, `subject`, `decision`, `reason_code`, `hatchet_published`, `created_at`, `provider_updated_at_us` | produced |
| `intake_reserved` | bridge intake reservation | audit; created-attempt counter and latency start | attempt identity, `generation`, `mode`, event timestamp | produced |
| `intake_active_job_reused` | bridge intake reservation | payload-free audit; reuse counter | attempt identity plus deterministic event ID bound to the source request key | produced |
| `intake_request_deduplicated` | bridge intake reservation | payload-free audit; exact replay counter | attempt identity plus deterministic event ID bound to the request key | produced |
| `spec_created` | future spec worker | audit; counter and revision duration start | `spec_id`, `work_item_key`, `revision`, `producer_version` | reserved |
| `spec_revised` | future spec worker | audit; revision counter | `spec_id`, `work_item_key`, `revision`, `reason_code` | reserved |
| `spec_approved` / `spec_rejected` | future spec worker or explicit human-approval service | audit; outcome counter and duration end | `spec_id`, `work_item_key`, `revision`, `actor_kind`, `reason_code` | reserved |
| `spec_used_for_implementation` | intake worker | audit; spec-use counter | `spec_id`, `revision`, `attempt_id`, `job_id`, immutable spec digest | reserved |
| `attempt_started` | bridge executor transition | audit; execution counter and duration start | attempt/job identity, event timestamp | produced |
| `execution_route_selected` | bridge executor before preflight | audit; intent-to-treat cohort | assignment/experiment/variant, intended route, configured labels, release and policy identity, `fallback_allowed=false` | produced |
| `execution_backend_invoked` / `execution_backend_completed` / `execution_backend_failed` | fenced bridge executor | audit; invocation counters and durations | execution identity plus the fields defined below | produced |
| `execution_dispatch_failed` / `execution_effect_commit_failed` | fenced bridge settlement | audit; boundary-failure counters | execution identity, failure classification, effect outcome | produced |
| `attempt_effect_committed` | fenced bridge commit | audit; effect counter and duration end | attempt/job identity, event timestamp | produced |
| `ingress_events[source=github]` | event ingress | audit; PR/check/review observations | `event_id`, exact `prUrl` when bindable, timestamp plus provenance, normalized state/check/review fields | produced |
| `ingress_receipts` | event ingress | payload-free audit; redelivery/conflict and authenticity counters | receipt/event identity, source/provider kind, payload digest, signature-verified flag, disposition, receive time | produced |
| `github_checks_completed` observation | event ingress and PR event worker | audit; suite outcome evidence | exact PR when GitHub supplies one, head SHA, suite ID, status/conclusion, count | produced; not an aggregate first-pass-CI result |
| `pr_checks_observed` | future required-check aggregator | audit; first-pass CI counter | `attempt_id`, `job_id`, exact `pr_url`, initial `head_sha`, required check-set digest, aggregate result | reserved |
| `github_review_submitted` observation | event ingress and PR event worker | audit; review outcome counter | exact `pr_url`, head/review commit SHA, provider event ID/time, review state, actor kind | produced |
| `pr_review_requested` / `human_rework_required` | future review/handoff producer | audit; requested-review and human-rework counters | exact PR/attempt identity, provider event ID/time, actor kind and reason code | reserved |
| `pr_merged` / `pr_closed_unmerged` | event ingress or PR event worker | audit; PR outcome counters and duration end | exact `pr_url`, `head_sha`, provider event ID, provider timestamp | reserved as typed events; latest exact ingress observation is the v1 compatibility source |
| `repair_reserved` / terminal attempt event | bridge repair reservation and verifier | audit; repair counters and outcome | attempt lineage, source attempt/job, reason code | produced |
| `retry_deduplicated` | retry operator through bridge | audit; retry reuse counter | request ID, prior/current attempt ID, job ID | reserved |
| `retry_duplicate_attempt_detected` | reconciliation worker | audit; safety counter | both attempt IDs, work item, mode, finding key | reserved |
| `repair_budget_exhausted` | bridge repair policy | audit; safety counter and intervention gauge | attempt/job identity, budget, reason code | produced |
| `human_approval_requested` / `human_approval_granted` | future explicit approval service | audit; counter and duration | `approval_id`, work-item/attempt identity, `actor_kind`, reason code | reserved |
| `human_blocked` / `human_override` / `human_rework_required` | future explicit approval or review service | audit; intervention and rework counters | work-item/attempt identity, `actor_kind`, reason code, related PR URL when applicable | reserved |
| lifecycle outbox row | lifecycle writer | audit; delivery counters and current dead-letter gauge | `outbox_key`, source/job identity, delivery state, reason/error code, timestamps | produced |

Future append-only records should also carry an integer schema version and a
stable producer event ID. Optional attempt, job, PR, or spec identities must be
omitted rather than fabricated when the producer cannot bind them. Payloads
remain bounded and enum-like; the privacy rules below apply before persistence.

The initial metric mapping is:

| Question | Projection | Current availability |
| --- | --- | --- |
| Ready-to-merged conversion and Ready-to-first-draft latency | first signature-verified eligible Linear event per work item, exactly joined to an attempt and authenticated exact PR outcome | available with explicit event-key, receipt-authenticity, and GitHub-outcome coverage |
| First-pass CI pass rate | first aggregate `pr_checks_observed` bound to the initial attempt head SHA | unavailable; individual check-suite observations are reported but never promoted to this metric |
| Review change-request rate | submitted review observations per exact PR | available with explicit review coverage |
| Human-rework rate | typed human rework events per attempt | unavailable |
| Repairs per merged PR | retry/repair attempts in the ancestor/descendant `prior_attempt_id` lineage seeded by attempts bearing the exact PR URL | available only when every signature-verified current merge has provider occurrence time and a pre-merge lineage seed; timestamp, exact-PR, lineage, and GitHub coverage remain explicit |
| Duplicate external-effect rate | duplicate effects divided by fully observed committed effects | unavailable; reconciliation coverage is incomplete |
| Closed-unmerged PR rate | latest authenticated exact GitHub observation per unique cohort PR after durable PR association | terminal-PR rate is canonical; observed-PR and all-eligible current-snapshot lower-bound denominators are emitted separately, with right-censoring explicit |
| Spec revisions before implementation | `spec_revised` count before `spec_used_for_implementation` | unavailable |
| Active-job reuse and duplicate Ready rates | authenticated `active_attempt_reused` outcomes divided by authenticated eligible Ready events; authenticated events beyond the first authenticated event per subject divided by all authenticated eligible Ready events | available with explicit authenticity and event-key coverage; raw all-eligible repeats are observation-only, and Ready repeats remain distinct from exact request replays and webhook redelivery |
| Exact webhook redelivery/conflict rate | append-only ingress receipt dispositions | full-window rate is available only after the durable instrumentation epoch covers the complete window; otherwise the observed-receipt conditional rate is labeled separately |
| Bridge conflict rate | typed rejected bridge operations divided by bridge submissions | unavailable |
| Ready rejection count by gate | rejected Linear ingress rows grouped by `reason_code` | available |

### Intake and provider observations

| Evidence | Producer | Stored in | Analytics use |
| --- | --- | --- | --- |
| Linear normalized row with `decision=ready_for_implementation` | event ingress | `ingress_events` | Eligible events and unique eligible work items |
| Other Linear normalized row | event ingress | `ingress_events` | Rejected/observe-only counts by `reason_code` |
| `hatchet_published` | ingress publisher | `ingress_events` | Published versus unpublished Linear events |
| GitHub normalized row with exact `prUrl`, `state`, and `merged` | event ingress | `ingress_events` | Latest observed merged or closed-unmerged PR outcome |

Ingress counts use `source`, `subject`, `decision`, `reason_code`,
`hatchet_published`, `published_at`, `publish_attempts`, `normalized_json`,
`provider_timestamp_source`, and `created_at`. Eligible Ready events correlate by exact `event_key` to the
attempt `request_id` and bounded `source_event_key(s)` retained on the linked
job. Provider-event time and Factory receipt time are reported separately.
`provider_ready_to_*` latency accepts only timestamps whose source is explicitly
`provider`; a receipt-time ordering fallback remains usable for latest-record
selection but never masquerades as provider latency evidence.
Canonical Ready correlation and GitHub outcome/check/review metrics accept only
event keys with at least one receipt whose configured provider HMAC was
verified. Unsigned local replay remains visible in a separate observation-only
projection and cannot raise canonical conversion or outcome coverage. The
payload-free receipt ledger retains the original digest and verification fact;
new events do not retain or return the raw provider body. Canonical event keys
use the signed payload digest; unsigned delivery and timestamp headers remain
transport metadata and cannot manufacture a fresh authenticated event.
Analytics returns counts, reason-code buckets, and aggregate durations, not
normalized payloads.

### Attempt lifecycle

| Attempt event | Producer | Meaning in analytics |
| --- | --- | --- |
| `intake_reserved` | intake reservation | Initial attempt |
| `retry_reserved` | reviewed retry operator | Retry attempt |
| `absence_retry_reserved` | reviewed absence-retry operator | Retry after reviewed absence evidence |
| `repair_reserved` | repair reservation | Repair attempt |
| `attempt_started` | bridge transition | Executor started |
| `attempt_execution_claimed` | bridge execution fence | Execution lease was claimed; backend-attribution eligibility begins only at `effect_authorized` |
| `effect_authorized` | bridge execution fence | The one external-effect authority was granted |
| `attempt_effect_committed` | fenced bridge commit | External-effect receipt is durable |
| `attempt_verification_pending` | bridge/verifier transition | Verification is pending |
| `attempt_verified_ready` | verifier | Verification passed and normal human review can begin |
| `attempt_failed` | bridge/verifier transition | Attempt reached terminal failure |
| `attempt_completed`, `attempt_canceled_before_effect`, `attempt_canceled_after_effect`, or `superseded` | bridge/operator | Other terminal markers used by attempt-to-terminal latency; a terminal attempt's `updated_at` is the fallback when a dedicated marker is absent |
| `attempt_execution_recovery_required` | bridge lease recovery | Operator/recovery attention is required |
| `effect_authorized_outcome_unknown` | bridge settlement | Conservative recovery-required attempt state after authorization; details distinguish a truly unknown outcome from an observed PR receipt that has not committed to the bridge |
| `execution_effect_commit_recovery_wake_enqueued` | bridge receipt recovery | The existing accepted-event outbox was durably rearmed so verification resumes without another backend invocation |
| `repair_budget_exhausted` | repair policy | No further automatic repair is allowed |

Attempt events are stored in `factory_attempt_events` with `event_id`,
`attempt_id`, `event_type`, sanitized `details_json`, and `created_at`. Attempt
identity and cohort fields come from `factory_attempts`: `attempt_id`,
`work_item_key`, `generation`, `mode`, `actor`, `job_id`, `status`, and
`created_at`. Job state, current PR URL, and the latest job reason come from
`bridge_jobs`; an in-progress repair's existing target is retained as
`factory_attempts.prior_pr_url`. Bridge markers use the `bridge_events` envelope: `event_id`,
`job_id`, `event_type`, `state`, sanitized `details_json`, and `created_at`.

`submit_deduplicated` is a bridge event that means an idempotent job submission
was safely reused. `intake_request_deduplicated` records an exact intake replay
once per request key, and `intake_active_job_reused` records a distinct Ready
request attached to an already-active attempt. Both intake events are
payload-free and deterministically idempotent. None of these events means a
duplicate external effect. `duplicate_bridge_branch` and
`duplicate_bridge_pr_url` reconciliation findings are reported separately as
duplicate-reference findings; they do not establish a duplicate-effect rate.

`intake.repeated_eligible_events` is the observation-only count of all eligible
events beyond the first for each Linear subject.
`intake.authenticated_repeated_eligible_events` applies the same rule only to
signature-verified event keys, and `repeated_eligible_event_rate` divides that
count by authenticated eligible Linear events.
`intake.correlation.active_attempt_reuse_events` counts authenticated eligible
requests whose exact key attached to an existing active attempt; its rate
denominator is authenticated eligible Ready events. These rates describe two
different intake behaviors and neither is the webhook receipt redelivery rate.

`safety.observed_receipt_commit_recovery_incidents` counts attempts that ever
recorded an effect-commit boundary failure. The present-tense
`safety.unresolved_observed_receipts` counts only those still in
`effect_outcome_unknown`; recovered incidents do not remain in that backlog.
`safety.observed_receipt_recovery_exhausted` counts attempts whose three
automatic read-only inspection attempts failed to produce matching evidence
and therefore require operator attention.
`safety.rejected_observed_receipts` counts attempts whose receipt did not bind
to the authorized job's repository and branch, or supplied an invalid commit
SHA. Factory retains the observed-effect marker and rejection event, leaves
the attempt outcome unknown for operator intervention, and neither commits the
receipt nor starts an inspection for its untrusted URL.

### Execution-boundary telemetry

Before feature-enablement and executor preflight, every new real draft-PR
attempt records one immutable `execution_route_selected` event. Its assignment
is bound to the attempt and may be replayed only with the identical full
payload. The current adapter requires `fallback_allowed=false`; actual backend,
transport, authentication label, and backend version must match the selected
route before the bridge accepts invocation telemetry. This preserves an
intent-to-treat cohort even when work fails before reaching Hermes.

Route dimensions include `assignment_id`, `experiment_id`, `variant`,
`assignment_method`, `intended_backend`, `intended_transport`, configured
provider/model/authentication labels, backend version, Factory release SHA and
version, routing-registry version, verifier-policy digest, and the fallback
flag. Missing configuration is recorded as `unknown` or `null`, never inferred
from credentials, a session, or a reason code.

The current executor has exactly one truthful route:
`factory-default-v1` / `hermes` / `static`. It ignores arbitrary experiment,
variant, and assignment-method environment strings. Route-lineage analytics
still reports identity or variant crossovers so a future reviewed router can
detect contaminated retry/repair cohorts; those fields become causal evidence
only after assignment is implemented and enforced.

The fenced execution path emits these append-only attempt events around the
dispatch, backend, and durable-commit boundaries:

| Event | When recorded | Required event-specific fields |
| --- | --- | --- |
| `execution_backend_invoked` | Immediately before invoking the execution backend | Common identity only |
| `execution_backend_completed` | The backend returned a concrete created/reused result | `backend_outcome`, `elapsed_ms` |
| `execution_backend_failed` | Backend execution failed after effect authorization and settlement records an unknown outcome | Failure fields and `elapsed_ms` |
| `execution_dispatch_failed` | Effect authorization or detached receipt-runner dispatch became ambiguous before a confirmed backend invocation | Failure fields and `elapsed_ms` |
| `execution_effect_commit_failed` | The backend returned a concrete PR receipt but the exact receipt could not be confirmed in the bridge after commit/read-after-write | Failure fields, `elapsed_ms`, canonical observed PR URL, hex commit SHA or `null`, job-bound branch, backend outcome, a required `null` session field, and optional policy-projected verification exit codes |

The worker records invocation and completion through the bridge-internal
`POST /jobs/{job_id}/factory-execution/analytics` endpoint with its active
lease identity. Failure telemetry is supplied to the existing fenced
`factory-execution/settle` operation so the unknown-effect state and failure
event commit atomically. Dispatch and effect-commit failures use distinct event
types, so neither is counted as a failed Hermes invocation. A concrete
uncommitted receipt also marks the execution lease effect-observed, preventing
an absence retry from treating that generation as safely unobserved. These are
worker APIs, not operator or public ingest surfaces.

Every execution telemetry event has this exact common identity:

| Field | Values and meaning |
| --- | --- |
| `analytics_schema_version` | Integer `1` |
| `invocation_id` | Stable, non-secret identifier scoped to its Factory attempt, at most 200 characters |
| `execution_backend` | Explicit backend label, currently `hermes` |
| `execution_transport` | Currently `grpc_uds`, `grpc_tcp`, or `local_command` |
| `backend_version` | Explicit configured version, or `null`; never inferred |
| `configured_auth_mode` | Operator-supplied route label: `chatgpt_codex_oauth`, `api_key`, or `unknown`; this is not observed provider evidence |

Dimension values are bounded lowercase identifiers. `backend_outcome` is
`created`, `reused`, or `unknown`; `elapsed_ms` is an integer from zero through
seven days. The bridge accepts an invocation/completion event only from the
matching unreleased, effect-authorized execution fence; an expired fence may
still record its receipt evidence but cannot transfer authority. An identical
event for the same attempt, phase, and invocation is idempotent; changing it is
an immutable-event conflict. Exactly one backend-invocation registration is
allowed per authorized attempt, and the receipt runner must receive
`recorded: true` before crossing the provider boundary. A duplicate or failed
registration exits recovery-required without invoking Hermes.

The detached runner receives only its explicit runtime allowlist. Host
`systemd-run` dispatch uses a unique mode-`0700` runtime directory and
mode-`0600` one-shot `EnvironmentFile`; credential values and the execution
fence never appear in dispatcher arguments or the transient unit's
`Environment=` property, and the source file is removed after `ExecStart`
begins. The direct/Docker fallback passes the same allowlist as the child
environment instead of inheriting the worker's complete environment.

The receipt runner derives `configured_auth_mode` only from the bounded
`DRIFTWARD_HERMES_AUTH_MODE` deployment label and defaults to `unknown`; it
does not inspect or copy a credential. `backend_version` similarly reflects an
optional configured version label, not runtime fingerprinting.
Backend-attribution coverage can therefore be complete while authentication
route attestation remains unavailable.

Successful backend receipts retain only the created/reused status, canonical
repository-bound PR URL and number, deterministic job branch, a 7–64 character
lowercase hex commit SHA or `null`, a static status summary, and verification
exit codes projected onto the exact checked-in local-command and secret-check
policy. Hermes session IDs, free-form summaries, model output, command output,
and unrecognized validation fields are discarded before bridge or Hatchet
writes. Secret-shaped provider, model, backend-version, and release-version
labels are likewise replaced with `unknown` or `null` before route recording.

The failure payload adds:

| Field | Meaning |
| --- | --- |
| `failure_domain` | Explicit component boundary: `factory`, `executor`, `gateway_transport`, `gateway`, `hermes_process`, `hermes_protocol`, `auth`, `provider`, `tool`, `github`, `verification`, or `unknown` |
| `failure_stage` | Explicit bounded stage such as `preflight`, `gateway_connect`, `hermes_spawn`, `hermes_exit`, `hermes_timeout`, `result_parse`, `model_request`, `effect_commit`, `verification_checks`, or `unknown` |
| `reason_code` | Stable machine-readable reason |
| `retryable` | `true`, `false`, or `null` when unknown |
| `failure_disposition` | `retry`, `terminal`, or `recovery_required` |
| `operator_intervention_required` | `true`, `false`, or `null` |
| `effect_outcome` | `none`, `observed`, or `unknown` |

Execution-boundary failures may additionally carry nullable
`transport_status`, `process_exit_code`, `process_signal`, `timed_out`, and
`buffer_exceeded`. Reason codes, transport statuses, and process signals use
fixed allowlists; malformed gateway metadata becomes a safe fallback or
`null`. These are bounded machine facts. Raw command output,
provider errors, prompts, and stack traces are not persisted. If the producer
cannot name a boundary from direct evidence it records `unknown`; the reporter
never derives a domain or stage from the reason-code text.

Backend failure evidence is persisted during fenced settlement, so telemetry
cannot release or transfer effect authority. Preflight, artifact-fence, and
verification failures use the same classification fields on their durable
failed bridge transition. Historical failures without those fields remain in
the `unknown` bucket.

`backends` groups only explicit telemetry by backend, transport, backend
version, configured authentication label, repository, and attempt kind. It
also reports the backend's explicit `created`/`reused` outcome and outcome
coverage; a missing label remains `unknown`. An effect-commit transition
carrying explicit backend dimensions and an `invocation_id` is the
compatibility fallback for a missing completion event: it counts that
invocation as completed and may add its bounded `backend_elapsed_ms` sample.
The strictly validated `execution_effect_commit_failed` record is also
completion evidence because it contains a concrete backend receipt; its
`elapsed_ms` is used only when the ordinary completion event is absent. An
invocation event remains required to measure event-timestamp latency when no
explicit elapsed duration exists.

An observed receipt never causes another backend invocation. Both the primary
commit endpoint and recovery settlement require the exact job repository and
deterministic branch plus a canonical 7-to-64-character lowercase hexadecimal
Git SHA. If a recovery receipt contains that SHA, fenced settlement commits
the exact receipt immediately. If the SHA is missing, Factory queues an exact
read-only inspection of the same PR;
matching repository, URL, branch, and inspected head SHA then commit the
existing receipt and append `execution_effect_commit_recovered`. Only a cached
result observed within the 120-second inspection capability window is consumed
immediately. Stale results, expired inspection capabilities, transient
unavailable results, or mismatching results cause up to three distinct
inspection requests. The existing 30-second Factory outbox dispatcher sweeps
silent pending inspections, expires them after the 120-second capability
window, and durably queues the next request without invoking the execution
backend. Exhaustion appends
`execution_effect_commit_recovery_exhausted` for root-operator escalation.
Matching inspection evidence rearms the attempt's existing accepted-event
outbox row, so the normal executor resumes verification on the next dispatcher
cycle without another backend invocation; reconciliation remains a fallback,
not the primary continuation path.
After checking the attempt events, a root operator can enqueue one additional
inspection with
`factory attempts refresh-receipt ATTEMPT-ID --reason TEXT --yes`. The action
is audited, reuses the exact stored receipt identity and original execution
fence, and never invokes Hermes or another execution backend.
Until matching evidence arrives, the original attempt remains conservatively
`effect_outcome_unknown`. Malformed verification context is discarded without
discarding the concrete receipt; verification recovery handles that later.

## Metrics and coverage semantics

Latency keys are defined as follows:

| Metric | Start | End |
| --- | --- | --- |
| `attempt_to_executor_started` | Attempt `created_at` | First `attempt_started` |
| `executor_started_to_effect_committed` | First `attempt_started` | First `attempt_effect_committed`, with linked `transition:effect_committed` as a compatibility fallback |
| `effect_committed_to_verified_ready` | First committed-effect marker under the preceding rule | First `attempt_verified_ready` |
| `attempt_to_terminal` | Attempt `created_at` | First recognized terminal attempt event |
| `intake.correlation.latency_ms.ready_received_to_*` | First eligible event's Factory `created_at` | Exactly correlated attempt milestone |
| `intake.correlation.latency_ms.provider_ready_to_*` | First eligible event's provider timestamp | Exactly correlated attempt milestone |
| `recovery.failure_to_retry_reserved_ms` | Failed source-attempt event | First descendant retry/repair reservation |
| `recovery.failure_to_verified_ready_ms` | Failed source-attempt event | First verified-ready descendant in `prior_attempt_id` lineage |
| `backends[].completed_invocation_latency_ms` | Backend invocation | Monotonic `elapsed_ms` on completion, with matching event timestamps only as a compatibility fallback |
| `backends[].failed_invocation_latency_ms` | Backend invocation | Monotonic `elapsed_ms` on failure |

Each distribution returns `samples`, nearest-rank `p50`, and nearest-rank
`p95`, rounded to whole milliseconds. Missing endpoints and invalid or
negative intervals are excluded. Where an endpoint population is defined, the
object also returns `eligible` and `coverage_rate`; do not interpret a latency
percentile without its sample coverage. A distribution with no samples has
`0` samples and `null` percentiles.

Recovery rates use a fixed seven-day horizon. Eligibility is determined only
by failure age (`failed_at` at least seven days before the report cutoff), not
by whether recovery happened. The numerator is verification in the connected
attempt lineage no later than seven days after that failure. Immature and
unaged failures are right-censored; an active retry is still a measured miss
once its seven-day horizon has elapsed. Snapshot-level eventual recoveries and
late recoveries are reported separately and never enter the seven-day rate.

Ready latency eligibility is start-based, never outcome-selected. Every
exactly correlated work item with a valid Factory receipt time is eligible for
each `ready_received_to_*` metric even when the endpoint is missing; provider
latencies have their own denominator of correlated work items with a valid,
non-future provider Ready timestamp. `latency_start_coverage`, per-metric
`eligible`, and `coverage_rate` expose both gaps, so one observed merge cannot
appear as 100% latency coverage for a much larger Ready cohort.

Coverage objects use:

```json
{"observed": 8, "eligible": 10, "rate": 0.8, "complete": false}
```

- Backend attribution eligibility begins at the durable `effect_authorized`
  marker; `coverage.execution_claimed_attempts` is reported separately.
  Observation requires explicit backend telemetry or explicit backend fields
  on the effect-commit transition.
- Route-assignment eligibility includes every real non-legacy cohort attempt,
  including preflight failures. Observation requires the immutable
  `execution_route_selected` event; historical attempts are not backfilled.
- `coverage.configured_auth_label` measures non-`unknown` configured labels
  among backend-attributed attempts. It is configuration coverage, not an
  attestation that the route was actually used or succeeded.
- Attempt failure attribution is observed only when both `failure_domain` and
  `failure_stage` are explicit.
- GitHub observation eligibility is a valid cohort GitHub PR URL; observation
  requires an exact matching ingress record backed by a signature-verified
  receipt. Unverified matching rows are reported separately.
- Mature merge-within-horizon rates are evidence-bounded. A positive
  authenticated merge inside the horizon is conclusive because merging is
  irreversible. A recognized authenticated state at or after the boundary can
  establish that no merge occurred inside the horizon. Without either, the PR
  remains right-censored and the aggregate merge rate is `null` until
  `outcome_evidence.complete` is true. Closing and reopening are reversible,
  so event observations cannot establish terminal or closed-unmerged state at
  an exact historical boundary; those two within-horizon rates are explicitly
  unavailable rather than inferred.
- Ready work-item GitHub buckets are mutually exclusive across retry/repair
  lineages: any authenticated merge wins; otherwise any open PR wins; a work
  item is closed-unmerged only when every candidate PR is observed closed;
  remaining mixed or missing states are unobserved. A merged item is never
  right-censored merely because another lineage attempt remains active. An
  unfinished, open, or unobserved Ready lineage is not converted into a
  numeric non-merge: `merge_outcome_evidence` and
  `right_censored_work_items` expose the settled and unsettled populations.
- `intake.correlation.work_item_conversion.maturity.outcome_within_horizon`
  reports `merge_outcome_evidence`. A positive authenticated merge is
  sufficient evidence; a measured non-merge requires exact correlation and
  authenticated state evidence for every durable candidate PR in the horizon.
  Incomplete evidence keeps `merged_within_horizon.rate` null.
- Check-suite observations are never interpreted as first-pass CI. Review and
  check-suite coverage is exact-PR coverage, while check conclusions and head
  bindings count every authenticated suite observation; neither is
  webhook-subscription attestation.
- Duplicate-effect observation is intentionally `complete: false`. The current
  reconciler does not prove that it sees every external effect.

Interpret values conservatively:

- `0` means the required evidence was available and the count was zero.
- `null` means the metric cannot be calculated from available evidence.
- `unknown` means a record exists but its dimension was not explicitly
  attributed.
- `available: false` means an optional source could not be read or lacked the
  required schema.
- `complete: false` means the evidence does not cover every eligible record,
  even when a partial rate is present.

An exactly empty eligible PR cohort has zero outcome counts and complete empty
coverage even when provider authenticity is unavailable: no outcome evidence
is required to establish that the denominator is zero. For a nonempty cohort,
missing authenticity or join evidence yields `null`, never a measured zero.
Repair-per-merged-PR averages are likewise `null` unless every authenticated
current merged PR has explicit provider merge time and at least one exact,
pre-merge lineage seed. Provider-timed partial counts remain diagnostic but
never shrink the canonical denominator. The entire
repair-efficiency subsection is `available: false` with null outcome-dependent
counts and coverage when authenticated GitHub outcomes are unavailable; a
missing optional ledger never becomes a measured zero-merge cohort.

Signature provenance and an exact receipt-to-event `event_key` join are
required evidence boundaries, not coverage hints. If either is unavailable,
authenticated Ready counts/correlation and canonical GitHub counts, rates,
maturity, checks, and reviews are unavailable or `null`—never zero. Counts of
matching rows with unknown or failed authenticity may still be shown only
under the explicitly observation-only projection.

The report always lists known evidence gaps in `unavailable_metrics`. Adding a
new metric should include its source, grain, eligibility denominator, and
coverage rule before it is used for an operational decision.

### Delivery, reconciliation, and source health

Delivery sections separate cohort performance from global current backlog:

- ingress reports decision-by-publication counts, eligible publication
  coverage, publish attempts/retries, created-to-published latency, and
  unpublished backlog age;
- Factory accepted-event and inspection outboxes report cohort
  acknowledgement coverage/latency/retries plus unacknowledged backlog age;
- lifecycle reports first-seen-to-delivered latency, delivery attempts/retries,
  current pending/retrying/claimed backlog, and dead letters; and
- inspection-outbox cohort membership is joined through the inspection request
  `prior_attempt_id`, not the separate inspection job ID.

`reconciliation` reports current, historical, and occurrence counts plus kind,
severity, scope, actionability, and last-seen lag. "Current" means observed in
the report's documented 30-minute freshness interval; it is not a statement
that a finding was explicitly resolved.

`sources` reports the detected schema contract, row counts, latest persisted
timestamp, age of that timestamp, malformed or future receipt timestamps, and
invalid or future provider timestamps. Future provider observations are
excluded from canonical outcomes. Canonical GitHub projection also counts
authenticated observations excluded because they predate Factory's durable PR
association. Bridge health checks both `created_at` and `updated_at` for the
attempt cohort plus every timestamp field used by delivery/reconciliation
projections. Receipt health validates `ingress_receipts.received_at`; malformed
or future receipts are excluded from the signature-verified event-key set and
cannot authenticate an outcome at the report cutoff. A large
lag can mean either a stalled producer or legitimately quiet input, so source
freshness must be read alongside expected traffic and delivery backlog. Source
files are snapshotted independently; the report is not a distributed atomic
transaction across SQLite ledgers. New ingress ledgers persist
`ingress_schema_meta` version 3; legacy ledgers without that singleton are
explicitly labeled as column-inferred compatibility sources until migrated.
An ingress ledger declaring a version newer than 3 is treated as an
unsupported forward schema: the source is `available: false`, its version is
reported for diagnosis, and no intake or GitHub values are inferred from it.

## Questions this version can answer

With adequate coverage, analytics v1 can answer:

- How many unique real work items entered the attempt, execution, durable PR,
  and verified-ready stages?
- Which eligible Ready events created a real attempt, reused active work,
  produced only a dry run, remained unlinked, or correlated ambiguously?
- How long provider-observed Ready and Factory-received Ready take to reach
  reservation, execution, PR, verification, and merge milestones?
- How many attempts are active, verified, failed, canceled, superseded, or in
  an unknown-effect state?
- Where explicitly classified terminal failures occur and which reason codes
  dominate?
- How long the measured attempt stages and completed backend invocations take?
- How many effect-authorized attempts have explicit backend attribution, and how many
  explicit backend invocations completed or failed?
- How many failures occurred at dispatch, backend invocation, or Factory
  effect-commit boundaries without collapsing them into one Hermes bucket?
- Which explicitly configured backend, transport, version, and authentication
  route handled those attempts?
- Which preflight intent-to-treat experiment/variant was assigned, including
  attempts that never reached the backend, and whether the observed invocation
  identity matched it?
- How initial, retry, absence-retry, and repair attempts perform?
- How many explicit bridge submit-deduplication events, unknown-effect outcomes,
  repair budget exhaustions, and exception handoffs occur?
- For PRs with exact ingress matches, how many are observed merged or closed
  unmerged, open, unobserved, or mature at 24 hours and 7 days?
- How many exact review observations requested changes, with explicit coverage,
  and what non-aggregate check-suite outcomes were observed?
- Whether lifecycle delivery is accumulating retries or dead letters, with
  source-event reasons separated from delivery error codes?
- Whether ingress publication, Factory accepted-event delivery, inspection
  delivery, reconciliation, and source freshness are accumulating backlog?

These are measured associations, not causal proof. A low-completion Hermes
group is a reason to inspect its classified failures and coverage, not by
itself proof that Hermes caused them.

## Questions this version cannot answer

Analytics v1 cannot currently answer:

- Whether ChatGPT Codex OAuth, the OpenAI provider, Hermes orchestration, or the
  local gateway was the root cause of a backend failure.
  `configured_auth_mode` records the configured route; it does not attest that
  an OAuth exchange succeeded or identify which downstream layer failed.
- Whether removing Hermes would improve reliability or latency. There is no
  comparable direct-Codex backend cohort or randomized routing experiment.
- Model time to first token. Hermes returns a buffered result to this path.
- A complete duplicate-external-effect rate. Current reconciliation finds some
  duplicate references but does not attest complete provider observation.
- First-pass CI pass rate. Individual `check_suite.completed` deliveries are
  not a complete required-check set and may refer to different head SHAs.
- Spec quality, approval duration, or actual human-rework time. Canonical
  producers for specification and human action events are not yet connected.
- Complete GitHub outcome/review rates until the GitHub webhook subscription is
  active and the report's exact-match coverage is adequate.

To isolate Hermes from ChatGPT Codex OAuth, keep the existing backend events
but add structured failure boundaries from the gateway/provider and run a
second explicitly labeled execution backend. Compare only contemporaneous,
similarly scoped work using the same success and verification definitions;
never derive the comparison from reason-code text.

## Privacy and data handling

The JSON report contains aggregate counts, bounded reason codes, timestamps,
durations, and explicit non-secret dimension labels. It must not contain:

- OAuth access or refresh tokens, API keys, cookies, or authorization headers;
- prompts, issue descriptions, comment bodies, model output, source code, or
  diffs;
- raw provider error text or stack traces;
- Hermes transcripts, browser state, or session material; or
- raw lifecycle comment bodies or markers.

JWT-shaped access tokens and long opaque OAuth-refresh-token-shaped strings are
rejected or redacted at installer, worker, transport-error, and bridge
validation boundaries; they are never valid release or backend labels.

`invocation_id` is correlation metadata, not a credential.
`configured_auth_mode` is an enum, not an authentication secret. Legacy bridge
rows may retain historical `hermes_session_id` values, but new bounded
execution receipts require that field to be null and the analytics projection
never returns it. Continue to protect the underlying ledgers and operator
socket as root-owned runtime data. The console exposes only the explicit
aggregate allowlist described above; future exports must preserve that boundary.
