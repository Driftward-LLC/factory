# Factory analytics v1

Factory analytics answers operational questions from the durable evidence the
Factory already owns. It is a read-only projection over the bridge, ingress,
and lifecycle SQLite ledgers; it is not a second event store, a tracing system,
or a dashboard.

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

The CLI calls `GET /v1/analytics?days=N` through the root-owned Factory
operator Unix socket. The endpoint requires the operator bearer token, accepts
only one optional `days` query parameter, and returns the
`factory-analytics-v1` JSON object. It is not exposed by the public ingress,
the worker-facing bridge API, or the read-only web console.

The operator opens the existing database files read-only. The bridge ledger is
required; an unavailable bridge ledger fails the request. Ingress and lifecycle
are optional sources: an unavailable or incompatible optional ledger produces
`available: false` and `null` dependent metrics without suppressing bridge
results.

The top-level JSON sections are:

| Key | Contents |
| --- | --- |
| `schema`, `generated_at`, `window` | Report version and cohort bounds |
| `coverage` | Source availability, exclusions, and attribution completeness |
| `intake` | Linear eligibility, rejection, and publication counts |
| `funnel` | Unique-work-item delivery stages |
| `outcomes` | Attempt status and exact-match GitHub outcomes |
| `latency_ms` | Attempt-stage duration distributions |
| `failures` | Attempt failure evidence and backend-invocation classifications |
| `recovery`, `safety`, `human` | Retry/repair performance, effect safety, and review readiness |
| `backends` | Explicit backend/transport/version/auth groups |
| `lifecycle` | Lifecycle outbox delivery and dead-letter state |
| `unavailable_metrics` | Questions the current evidence cannot support |

Source ownership and query paths are:

| Source | Docker runtime path | Tables read | Query surface |
| --- | --- | --- | --- |
| Bridge ledger | `/var/lib/driftward-hatchet-bridge/bridge.db` | `factory_attempts`, `factory_attempt_events`, `bridge_jobs`, `bridge_events`, `reconciliation_findings` | Required source for every report |
| Event-ingress ledger | `/var/lib/driftward-hatchet-event-ingress/ingress.db` | `ingress_events` | Optional intake and GitHub outcome projection |
| Lifecycle ledger | `/var/lib/driftward-factory-lifecycle-writer/outbox.db` | `lifecycle_outbox` | Optional delivery/dead-letter projection |

There is no analytics table, write path, periodic ETL job, or historical
backfill. Query the report through `factory analytics`; use the underlying
ledgers directly only for focused forensic debugging.

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
| `funnel` | unique `work_item_key` | A work item reaches a stage if any cohort attempt has the required durable marker |
| `outcomes.by_status` | attempt | Current status of each cohort attempt at the bridge read snapshot |
| `latency_ms` | attempt with both endpoint events | One nonnegative duration sample per qualifying attempt |
| `failures.attempts_with_failure_evidence` | attempt | Latest `transition:failed` classification, including an attempt later recovered or superseded; current `failed` status is a compatibility fallback when the event is absent |
| `failures.backend_invocations` | unique `(attempt_id, invocation_id)` | Explicit `execution_backend_failed` evidence only; invocation IDs are attempt-scoped |
| `backends` | backend/transport/version/auth group | Attempts and unique attempt-scoped invocations carrying explicit backend telemetry |
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
| `draft_pr_available` | At least one linked bridge job has a valid GitHub pull-request URL; it may have been created earlier or reused |
| `verified_ready` | At least one `attempt_verified_ready` event |

GitHub merge and closed-unmerged outcomes use the canonical latest GitHub
ingress observation by `provider_updated_at_us` and `event_id` whose `prUrl`
exactly matches a PR URL in the cohort. They are not guessed from bridge state.
`outcomes.github_observation_coverage` reports how many unique cohort PR URLs
have such an observation; `outcomes.github_grain` is `unique_pr_url`, separate
from the attempt-grain status counts.

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
| `intake_active_job_reused` | intake worker through bridge | audit; reuse counter | `source_event_key`, `attempt_id`, `job_id`, `mode`, `reason_code` | reserved |
| `intake_request_deduplicated` | bridge | audit; duplicate-ready counter | `source_event_key`, `attempt_id`, `job_id`, `deduplication_key` | reserved |
| `spec_created` | future spec worker | audit; counter and revision duration start | `spec_id`, `work_item_key`, `revision`, `producer_version` | reserved |
| `spec_revised` | future spec worker | audit; revision counter | `spec_id`, `work_item_key`, `revision`, `reason_code` | reserved |
| `spec_approved` / `spec_rejected` | future spec worker or explicit human-approval service | audit; outcome counter and duration end | `spec_id`, `work_item_key`, `revision`, `actor_kind`, `reason_code` | reserved |
| `spec_used_for_implementation` | intake worker | audit; spec-use counter | `spec_id`, `revision`, `attempt_id`, `job_id`, immutable spec digest | reserved |
| `attempt_started` | bridge executor transition | audit; execution counter and duration start | attempt/job identity, event timestamp | produced |
| `execution_backend_invoked` / `execution_backend_completed` / `execution_backend_failed` | fenced bridge executor | audit; invocation counters and durations | execution identity plus the fields defined below | produced |
| `execution_dispatch_failed` / `execution_effect_commit_failed` | fenced bridge settlement | audit; boundary-failure counters | execution identity, failure classification, effect outcome | produced |
| `attempt_effect_committed` | fenced bridge commit | audit; effect counter and duration end | attempt/job identity, event timestamp | produced |
| `ingress_events[source=github]` | event ingress | audit; PR outcome counters | `event_id`, exact `prUrl`, provider timestamp, normalized state/merge fields | produced |
| `pr_checks_observed` | PR event worker | audit; first-pass CI counter | `attempt_id`, `job_id`, exact `pr_url`, `head_sha`, check-set digest, aggregate result | reserved |
| `pr_review_requested` / `pr_changes_requested` | PR event worker | audit; review and rework counters | exact `pr_url`, `head_sha`, provider event ID, provider timestamp | reserved |
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
| Ready-to-merged conversion and Ready-to-first-draft latency | unique eligible Linear work item joined to its attempt and exact PR outcome | unavailable until ingress persists the attempt correlation |
| First-pass CI pass rate | first `pr_checks_observed` for each initial attempt | unavailable |
| Review change-request and human-rework rates | typed review/human events per exact PR or attempt | unavailable |
| Repairs per merged PR | repair attempts joined to an exactly observed merged PR | partial; repair lineage exists, merge coverage is explicit |
| Duplicate external-effect rate | duplicate effects divided by fully observed committed effects | unavailable; reconciliation coverage is incomplete |
| Closed-unmerged PR rate | latest exact GitHub observation per unique cohort PR | available when GitHub observation coverage is nonzero |
| Spec revisions before implementation | `spec_revised` count before `spec_used_for_implementation` | unavailable |
| Active-job reuse and duplicate Ready rates | typed intake reuse/dedup records divided by eligible intake requests | unavailable |
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
`hatchet_published`, `normalized_json`, and `created_at`. Analytics returns
counts and reason-code buckets, not normalized payloads.

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
`created_at`. Job state, PR URL, and the latest job reason come from
`bridge_jobs`. Bridge markers use the `bridge_events` envelope: `event_id`,
`job_id`, `event_type`, `state`, sanitized `details_json`, and `created_at`.

`submit_deduplicated` is a bridge event that means an idempotent job submission
was safely reused. `safety.bridge_submit_deduplicated_events` counts only that
exact evidence; ordinary intake reservation reuse is not durably counted and
the full intake deduplication rate is unavailable. It is not a duplicate
external effect. `duplicate_bridge_branch` and `duplicate_bridge_pr_url`
reconciliation findings are reported separately as duplicate-reference
findings; they do not establish a duplicate-effect rate.

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

The fenced execution path emits these append-only attempt events around the
dispatch, backend, and durable-commit boundaries:

| Event | When recorded | Required event-specific fields |
| --- | --- | --- |
| `execution_backend_invoked` | Immediately before invoking the execution backend | Common identity only |
| `execution_backend_completed` | The backend returned a concrete created/reused result | `backend_outcome`, `elapsed_ms` |
| `execution_backend_failed` | Backend execution failed after effect authorization and settlement records an unknown outcome | Failure fields and `elapsed_ms` |
| `execution_dispatch_failed` | Effect authorization or detached receipt-runner dispatch became ambiguous before a confirmed backend invocation | Failure fields and `elapsed_ms` |
| `execution_effect_commit_failed` | The backend returned a concrete PR receipt but the exact receipt could not be confirmed in the bridge after commit/read-after-write | Failure fields, `elapsed_ms`, and bounded observed PR URL, commit SHA, branch, Hermes session reference, backend outcome, and optional verification context |

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

The receipt runner derives `configured_auth_mode` only from the bounded
`DRIFTWARD_HERMES_AUTH_MODE` deployment label and defaults to `unknown`; it
does not inspect or copy a credential. `backend_version` similarly reflects an
optional configured version label, not runtime fingerprinting.
Backend-attribution coverage can therefore be complete while authentication
route attestation remains unavailable.

The failure payload adds:

| Field | Meaning |
| --- | --- |
| `failure_domain` | Component boundary reporting the failure, such as `factory`, `executor`, `executor_backend`, `github`, or `verification` |
| `failure_stage` | Bounded stage such as `preflight`, `artifact_fence`, `backend_execution`, `verification_checks`, or `verification_recovery` |
| `reason_code` | Stable machine-readable reason |
| `retryable` | `true`, `false`, or `null` when unknown |
| `failure_disposition` | `retry`, `terminal`, or `recovery_required` |
| `operator_intervention_required` | `true`, `false`, or `null` |
| `effect_outcome` | `none`, `observed`, or `unknown` |

Backend failure evidence is persisted during fenced settlement, so telemetry
cannot release or transfer effect authority. Preflight, artifact-fence, and
verification failures use the same classification fields on their durable
failed bridge transition. Historical failures without those fields remain in
the `unknown` bucket.

`backends` groups only explicit telemetry by backend, transport, backend
version, and configured authentication label. An effect-commit transition
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
| `backends[].completed_invocation_latency_ms` | Backend invocation | Monotonic `elapsed_ms` on completion, with matching event timestamps only as a compatibility fallback |
| `backends[].failed_invocation_latency_ms` | Backend invocation | Monotonic `elapsed_ms` on failure |

Each distribution returns `samples`, nearest-rank `p50`, and nearest-rank
`p95`, rounded to whole milliseconds. Missing endpoints and invalid or
negative intervals are excluded. A distribution with no samples has `0`
samples and `null` percentiles.

Coverage objects use:

```json
{"observed": 8, "eligible": 10, "rate": 0.8, "complete": false}
```

- Backend attribution eligibility begins at the durable `effect_authorized`
  marker; `coverage.execution_claimed_attempts` is reported separately.
  Observation requires explicit backend telemetry or explicit backend fields
  on the effect-commit transition.
- `coverage.configured_auth_label` measures non-`unknown` configured labels
  among backend-attributed attempts. It is configuration coverage, not an
  attestation that the route was actually used or succeeded.
- Attempt failure attribution is observed only when both `failure_domain` and
  `failure_stage` are explicit.
- GitHub observation eligibility is a valid cohort GitHub PR URL; observation
  requires an exact matching ingress record.
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

The report always lists known evidence gaps in `unavailable_metrics`. Adding a
new metric should include its source, grain, eligibility denominator, and
coverage rule before it is used for an operational decision.

## Questions this version can answer

With adequate coverage, analytics v1 can answer:

- How many unique real work items entered the attempt, execution, durable PR,
  and verified-ready stages?
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
- How initial, retry, absence-retry, and repair attempts perform?
- How many explicit bridge submit-deduplication events, unknown-effect outcomes,
  repair budget exhaustions, and exception handoffs occur?
- For PRs with exact ingress matches, how many are observed merged or closed
  unmerged?
- Whether lifecycle delivery is accumulating retries or dead letters, with
  source-event reasons separated from delivery error codes?

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
- Eligible-intake-to-attempt conversion. Ingress does not persist a mode-bound
  attempt correlation.
- Exact webhook redelivery rate. Duplicate ingress deliveries are not durably
  counted.
- A complete duplicate-external-effect rate. Current reconciliation finds some
  duplicate references but does not attest complete provider observation.
- Spec quality, review quality, approval duration, or requested-changes cycles.
  Canonical producers for specification and human-review events are not yet
  connected.

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

`invocation_id` is correlation metadata, not a credential.
`configured_auth_mode` is an enum, not an authentication secret. Although a
bridge job may retain a
`hermes_session_id` for audited debugging, the analytics projection does not
return it. Continue to protect the underlying ledgers and operator socket as
root-owned runtime data, and expose only the aggregate report to any future
dashboard or export.
