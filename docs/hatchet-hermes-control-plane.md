# Hatchet To Hermes Control Plane Boundary

Status: Draft
Last reviewed: 2026-08-09
Linear: ENG-35

## Decision

Hatchet is a candidate durable workflow substrate for human-dispatched or
read-only bridge-backed intake pilots. It is not approved as an unattended
factory runtime and must not receive Driftward Hermes LLM provider credentials,
browser credentials, GitHub credentials, Linear credentials, MCP credentials,
or broad shell access.

If adopted, Hatchet owns durable waits, retries, workflow state, rate and
concurrency limits, worker registration, and workflow observability. Driftward
Hermes remains the execution runtime that owns agent profiles, sessions,
skills, provider authentication, MCP integrations, browser tooling, and
company automation credentials.

The safe initial shape is:

```text
Hatchet control plane
  workflow state, waits, retries, schedules, queueing, dashboard
  no LLM provider credentials

Hatchet worker
  HATCHET_CLIENT_TOKEN only
  no OPENAI_API_KEY, ANTHROPIC_API_KEY, browser, GitHub, or Linear credentials
  registered dispatcher tasks only

Hermes bridge
  local allowlisted API
  validates request class, operating mode, identity, budget, and idempotency
  maps Hatchet workflow/run IDs to Hermes profile/session/job IDs

Driftward Hermes
  provider credentials, sessions, skills, MCP, browser tooling, execution
  existing runtime health and credential smoke tests remain authoritative
```

## Non-Goals

- Do not restart the retired unattended factory implementation, verification,
  review, approval, landing, sentinel, canary, state-audit, factory GitHub App,
  or factory Linear App automation.
- Do not recreate the retired Mission Control controller or comment-command
  approval grammar.
- Do not run Hatchet workers inside the Hermes container.
- Do not make Hatchet an arbitrary command runner against the host or Hermes
  filesystem.
- Do not treat Hatchet workflow success as proof that GitHub, Linear, or Hermes
  external effects reached the intended state.

## Source Findings

Hatchet's self-hosting docs define a control plane composed of API server,
engine, PostgreSQL database, optional RabbitMQ, and dashboard. Workers are
separate processes that connect to that control plane and execute workflow
steps. This distinction is useful for Driftward because the worker can be
credential-minimal while Hermes keeps execution authority.

Hatchet worker docs show `HATCHET_CLIENT_TOKEN` as the worker connection token.
That token is not an LLM provider key. Driftward should store it separately,
for example at `/etc/driftward/hatchet-worker.env` with mode `0600`, and should
not mount it into Driftward Hermes unless a future Hermes-native Hatchet client
is explicitly approved.

Hatchet's durable-task docs fit the desired control-plane role: durable tasks
wait, spawn children, checkpoint, and replay from durable event history. They
also carry an important constraint: durable-task code must be deterministic and
must push external API/database side effects into child tasks. Driftward should
treat every bridge call as a child side-effect task with its own idempotency
key and evidence record.

Hatchet supports worker slots, concurrency limits, rate limits, and beta
idempotency controls. These are useful admission controls, but they do not
replace Driftward's required factory invariants: atomic claims, leases,
fencing tokens, idempotent effects, exact-SHA evidence, structured approvals,
transactional lifecycle/outbox records, monotonic reconciliation, budgets, and
a tested kill switch.

Hatchet webhooks are useful workflow trigger surfaces, but provider webhooks
from Linear, GitHub, and similar systems should not target Hatchet directly.
They must enter through Driftward event ingress first so provider signatures,
timestamps, event keys, payload normalization, and eligibility gates are
checked before Hatchet receives a Driftward-owned event.

Relevant sources:

- Hatchet self-hosting: https://docs.hatchet.run/self-hosting
- Hatchet workers: https://docs.hatchet.run/v1/workers
- Hatchet webhooks: https://docs.hatchet.run/v1/webhooks
- Hatchet durable tasks: https://docs.hatchet.run/v1/durable-tasks
- Hatchet durable execution: https://docs.hatchet.run/v1/durable-execution
- Hatchet support-agent cookbook: https://docs.hatchet.run/cookbooks/workflow-support-agent
- Hatchet concurrency: https://docs.hatchet.run/v1/concurrency
- Hatchet rate limits: https://docs.hatchet.run/v1/rate-limits
- Hatchet idempotency: https://docs.hatchet.run/v1/idempotency

## Credential Boundary

Allowed in Hatchet worker environment:

- `HATCHET_CLIENT_TOKEN`
- Hatchet connection settings needed for the chosen self-hosted or cloud
  endpoint, such as TLS strategy and host/port variables
- Bridge endpoint path, such as a Unix socket path or localhost URL
- Non-secret configuration: worker name, queue name, default budgets, and
  operating mode source

Forbidden in Hatchet worker environment:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, model-provider OAuth/session material,
  or any other LLM provider secret
- Driftward Hermes `.env` files or profile/session state
- Root GitHub CLI token files, factory GitHub App credentials, or GitHub
  installation tokens
- Linear user, Hermes app, or retired factory app credentials
- Browserbase tokens, browser contexts, cookies, or local browser session state
- Google Workspace, Railway, Notion, Gmail, or other platform credentials
- Docker socket access, host root filesystem mounts, or a generic shell command
  endpoint

## Bridge Contract

The Hermes bridge is a typed gRPC service. Production should use a Unix domain
socket owned by a dedicated group:

```text
unix:///run/driftward-hermes-factory/hermes.sock
```

The protobuf contract is transport-neutral, so a future private TCP endpoint is
allowed only as a deployment change with `DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1`
and either TLS or `DRIFTWARD_HERMES_GRPC_AUTH_TOKEN`. Localhost HTTP is
acceptable only for one-off prototypes when bound to `127.0.0.1`, protected by
a shared secret or mTLS-equivalent local credential, and blocked from public
ingress. A CLI wrapper is acceptable for a manual pilot, but should not become
the durable API because command execution and structured error handling are
harder to constrain.

Minimum request fields:

```text
request_id
idempotency_key
hatchet_tenant_id
hatchet_workflow_id
hatchet_workflow_run_id
hatchet_step_run_id
hatchet_worker_name
work_item_id
repo
requested_action
operating_mode
expected_head_sha
claim_id
fencing_token
budget
payload
```

Minimum response fields:

```text
request_id
idempotency_key
accepted
status
reason_code
hermes_profile_id
hermes_session_id
hermes_job_id
artifact_ref
commit_sha
pr_url
started_at
completed_at
```

Allowed bridge actions for the first pilot:

- `inspect_issue`: read issue metadata and return a bounded summary.
- `draft_spec`: create or update a draft spec artifact without opening a PR.
- `run_read_only_review`: inspect a repository and emit a schema-bound review
  artifact without comments or code changes.
- `dispatch_human_requested_session`: start a Hermes session only after an
  explicit human request, and only with no merge/release authority.

Reserved to Hermes or humans:

- Provider/model calls and agent execution.
- Browser and MCP tool use.
- GitHub branch creation, commits, PRs, labels, comments, reviews, and merges.
- Linear mutations other than explicit, human-directed comments from the
  current actor.
- Production deploys, releases, or protected-branch writes.
- Changes to Hermes configuration, credentials, skills, MCP settings, browser
  contexts, or factory policy.

## Binding Record

Store the binding table in a Driftward-owned control store, not inside Hatchet
metadata alone. A SQLite database is acceptable for a local-only pilot if it
uses transactions, WAL mode, backups, and a clear migration path. PostgreSQL is
preferred if Hatchet is already self-hosted with PostgreSQL and Driftward wants
shared transactional semantics. Linear and GitHub are projections, not binding
stores.

Minimum binding schema:

```text
binding_id
created_at
updated_at
state
operating_mode

hatchet_tenant_id
hatchet_workflow_id
hatchet_workflow_run_id
hatchet_step_run_id
hatchet_worker_name

work_item_id
linear_issue_id
github_repo
github_issue_url
github_pr_url
expected_head_sha

claim_id
lease_id
fencing_token
transition_version
attempt
repair_round

hermes_profile_id
hermes_session_id
hermes_job_id
hermes_artifact_ref

idempotency_key
requested_action
budget_json
terminal_reason
```

Every external effect should include `binding_id`, `claim_id`,
`fencing_token`, `idempotency_key`, and `expected_head_sha` where the action is
SHA-sensitive. Replays must return the stored outcome for the same idempotency
key or fail with a conflict if the requested outcome differs.

## Placement

If Hatchet is adopted as a durable host runtime, follow
`docs/containerized-service-runbook.md` before adding Docker or systemd state.
The expected scope is `host-level` because Hatchet would support shared
bridge-backed work coordination across repos.

Recommended paths:

```text
/docker/driftward/hatchet/              # Hatchet control-plane runtime
/docker/driftward/hatchet/data/         # Hatchet durable state
/etc/driftward/hatchet.env              # Hatchet control-plane env, mode 0600
/etc/driftward/hatchet-worker.env       # Worker token only, mode 0600
/var/lib/driftward-hatchet-bridge/      # Binding/control store for pilot
/usr/local/bin/driftward-hatchet-smoke  # Host smoke test
```

Bind all service ports to localhost unless a separate ingress, authentication,
TLS, and alerting plan exists.

## Required Invariants Before Unattended Work

Unattended implementation remains disabled until all of these are implemented
and tested:

- Explicit operating mode: `human-augmented`, `allowlisted-unattended`, or
  `paused`.
- Atomic claim acquire, renew, release, and takeover with compare-and-swap.
- Renewable leases plus monotonically increasing fencing tokens.
- Idempotent effects with stable keys and persisted outcomes.
- Exact full commit SHA on verification, review, approval, and landing
  evidence.
- Transactional lifecycle state plus outbox events before projection updates.
- Structured approvals bound to action, actor, work item, PR, full SHA, repair
  round, timestamp, expiry, and nonce.
- Separate delivery retry, stage retry, and repair-round budgets.
- Monotonic reconciliation that cannot revive terminal attempts or overwrite
  newer versions.
- A tested kill switch that stops new claims without corrupting existing
  evidence.
- Fault injections for duplicate delivery, crash after effect, expired lease,
  stale worker resume, API outage, commit drift, and bridge rejection.

## Safe Pilot

The first pilot should be read-only:

1. A human starts a Hatchet workflow for one Linear/GitHub work item.
2. The Hatchet worker records the workflow/run IDs and calls the bridge with
   `inspect_issue` or `run_read_only_review`.
3. The bridge validates mode `human-augmented`, request idempotency, and
   allowlisted action.
4. Hermes performs the read-only analysis with its existing credentials and
   returns an artifact reference.
5. The binding store records the Hermes session/job/artifact IDs.
6. Hatchet marks the workflow complete only after the stored outcome exists.
7. No branch, PR, merge, deploy, or release action is allowed.

Pilot smoke tests:

- Worker environment audit proves no LLM/provider, GitHub, Linear, Browserbase,
  or Hermes env files are present.
- Bridge rejects an unrecognized action.
- Bridge rejects missing or stale fencing data for guarded actions.
- Duplicate Hatchet delivery with the same idempotency key returns the original
  outcome.
- A simulated bridge crash after Hermes acceptance reconciles to one binding
  record and one terminal outcome.
- A simulated Hermes failure produces a typed Hatchet failure without retrying
  an unsafe effect.
- Hatchet dashboard, binding table, Linear issue, and any GitHub artifact agree
  on the same work item and artifact reference.

## Decision Matrix

| Option | Strength | Risk | Recommendation |
| --- | --- | --- | --- |
| Hatchet plus Hermes bridge | Durable waits, retries, scheduling, dashboard, worker identity, queue controls | New service and token surface; can become a command runner if bridge is vague | Good candidate for read-only or human-dispatched pilot |
| Hermes-native only | Fewer moving parts; keeps execution and credentials in one runtime | Rebuilds durability, retries, dashboard, and recovery logic inside Hermes | Prefer for simple cron/MCP/browser workflows |
| Custom control store plus Hermes | Maximum control over claims, fencing, evidence, approvals | More Driftward-owned code and operational burden | Needed if factory invariants outgrow Hatchet primitives |
| Retired Mission Control/factory stack | Prior context and scripts exist in archive | Was removed; risks recreating retired identities and comment-command triggers | Do not revive |

## Open Questions

- Is the first Hatchet deployment local-only Hatchet Lite, Docker Compose with
  PostgreSQL/RabbitMQ, or Hatchet Cloud?
- Should the binding store share Hatchet's PostgreSQL database or stay as a
  separate Driftward-owned SQLite/PostgreSQL database?
- Which additional Hermes bridge actions should graduate into the gRPC
  contract after draft PR creation, PR observation, and retry inspection?
- Which Hermes profile should own a read-only pilot, and how is its session ID
  returned durably?
- Should Hatchet workflow metadata store only binding IDs, or duplicate selected
  non-secret facts for dashboard usability?
