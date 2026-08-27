# Hatchet Worker Catalog

This directory owns Driftward Hatchet worker and workflow source. Keep worker
code, worker documentation, event names, and local smoke harnesses here so all
Hatchet execution behavior is discoverable from one tree.

Hatchet workers are dispatcher and coordination processes. They must not mount
Driftward Hermes credentials, model provider keys, Browserbase credentials,
GitHub credentials, Linear credentials, MCP credentials, browser sessions, or
Hermes profile/session state.

## Repository Routing

Ready Linear intake resolves a GitHub repository and execution permission only
from the immutable normalized Linear project ID. `routing-registry.json` is the
reviewed, versioned routing and execution-policy authority. Every route must
explicitly set `execution_enabled`; missing, unmapped, malformed, duplicate,
ambiguous, or disabled policy fails before a bridge job is submitted. Names,
titles, descriptions, and labels are never routing inputs. The intake and
Hermes executor allowlists remain independent defense-in-depth checks.

## Boundary

```text
Hatchet workflow
  durable orchestration, retries, waits, events, dashboard state

Hatchet worker
  registered tasks, HATCHET_CLIENT_TOKEN, bridge URL/token only

Hatchet Hermes bridge
  idempotency key, job row, append-only events, external-effect fencing

Driftward Hermes
  agent execution, credentials, MCP, browser/platform tools, sessions
```

Workers should call the bridge for external effects. The only worker that may
invoke `driftward-hermes` is `bridge-executor-worker`, and only for allowlisted
bridge actions after a job row exists and a deterministic branch has been
reserved.

## First Workers

Build these before adding merge, deploy, or broad repair authority.

| Worker | Purpose | Allowed Effects |
| --- | --- | --- |
| `intake-worker` | Consume ready Linear issues and submit mode-controlled implementation jobs to the bridge. | `POST /jobs` and Hatchet events only. |
| `execution-dispatch-worker` | Submit an implementation job to the bridge and return the bridge job state. | `POST /jobs` only. |
| `bridge-executor-worker` | Consume accepted bridge jobs, dispatch Driftward Hermes, record committed effects, and emit Hatchet completion events. | Bridge transitions, Driftward Hermes bounded execution, Hatchet events. |
| `pr-event-worker` | React to PR-related events and advance read-only workflow state. | Read GitHub/bridge state through Hermes or narrow APIs; no mutation. |
| `reconciliation-worker` | Inspect bridge ledger consistency and emit reconciliation findings. | Bridge reads and Hatchet events only. |

This first set proves the core loop:

```text
Linear webhook
  -> event ingress gates
  -> intake-worker
  -> bridge accepted job
  -> bridge-executor-worker completion according to Hatchet intake mode

Manual Hatchet dispatch
  -> execution-dispatch-worker
  -> bridge accepted job
  -> bridge-executor-worker
  -> Driftward Hermes creates draft artifact
  -> bridge effect_committed/verification_pending
  -> bounded Hermes-side verification
  -> bridge ready_for_human_review only when evidence is green
  -> Hatchet event
  -> pr-event-worker advances state
  -> reconciliation-worker detects stale or inconsistent ledger state
```

## Workflow Naming And Catalog

Workflow names are operator-facing API. They show up in Hatchet runs, logs,
retry UI, and incident notes, so the name must identify the responsibility
boundary.

Use this shape for new or renamed workflows:

```text
<domain>-<action>-<object>[-mode]
```

Use these domain prefixes:

| Prefix | Use For |
| --- | --- |
| `intake-*` | Consuming external or normalized events and deciding eligibility. |
| `bridge-*` | Submitting, reading, correlating, or transitioning bridge jobs. |
| `executor-*` | Performing fenced side effects or invoking Hermes/providers. |
| `verify-*` | Validating an existing side effect or artifact. |
| `reconcile-*` | Observing or repairing drift between durable systems. |
| `lifecycle-*` | Writing operator-facing lifecycle status. |
| `pr-*` | Observing pull-request events and advancing read-only PR state. |

Avoid names such as `execution-dispatch-*` when the workflow only submits or
reuses a bridge job. Use `bridge-submit-*` for submitter workflows and
`executor-*` for workflows that can cross the Hermes/provider-effect boundary.

Every registered workflow must be represented in `catalog.json`. Include:

- `name`, `worker`, and `stage`.
- `purpose`, as one concrete sentence.
- `does_not`, for important boundaries the workflow refuses to cross.
- `tasks`, `consumes_events`, `produces_events`, and `next_workflows` where
  applicable.

Source comments are secondary. Add a short comment above a workflow declaration
only when the dashboard-visible name could still be confused with a neighboring
workflow.

## Later Workers

Add these only after draft PR creation, event handling, and reconciliation are
stable.

| Worker | Purpose | Notes |
| --- | --- | --- |
| `triage-worker` | Decide actionable, duplicate, blocked, unsafe, or needs human. | No code changes. |
| `spec-worker` | Ask Hermes for acceptance criteria, risks, tests, and stop rules. | Artifact-only. |
| `verification-worker` | Run or dispatch lint, tests, smoke checks, screenshots, and evidence capture. | Exact commands and artifacts required. |
| `review-worker` | Perform independent review from fresh context. | Must be separate from implementation session. |
| `repair-worker` | Dispatch bounded repair attempts. | Requires repair budget and prior failure evidence. |
| `reconciliation-worker` later comparators | Compare Hatchet, GitHub, Linear, and runtime state beyond bridge-ledger consistency. | Should detect missed events, stale leases, and partial effects without expanding worker credentials. |

Do not build merge, deploy, release, or protected-branch workers until draft PR
creation, checks, reviews, idempotency, and reconciliation are proven.

## Event Names

Use stable readable event IDs in code and analytics. Hatchet still routes on
the `driftward.*` wire names listed below. The canonical contract is
`hatchet/contracts/events.json`; after editing it, run:

```bash
python3 tools/hatchet-contracts.py generate
python3 tools/hatchet-contracts.py check
```

| Event | Producer | Consumer | Purpose |
| --- | --- | --- | --- |
| `driftward.hermes.job.accepted` | Bridge | Hatchet workflows | Job row exists and retries are fenced. |
| `driftward.hermes.job.effect_committed` | Bridge executor | Hatchet workflows | External artifact exists, such as branch or PR. |
| `driftward.hermes.job.verification_pending` | Bridge executor | Operators | Draft PR exists but has not passed deterministic verification. |
| `driftward.hermes.job.verification_failed` | Bridge executor | Operators | Draft PR evidence failed closed with a stable reason code. |
| `driftward.hermes.job.ready_for_human_review` | Bridge executor | Operators | Deterministic draft-PR verification passed; human review is next. |
| `driftward.hermes.job.completed` | Bridge executor | Hatchet workflows | Dry-run-only executor completion; never a real implementation-success state. |
| `driftward.hermes.job.failed` | Bridge executor | Hatchet workflows | Terminal failure with reason code. |
| `driftward.github.pr.creation_simulated` | Bridge executor | Analytics or operators | Dry-run PR creation path completed without a real GitHub side effect. |
| `driftward.github.pr.created` | GitHub webhook or bridge executor | `pr-event-worker` | A real PR exists and should be inspected. |
| `driftward.github.pr.synchronized` | GitHub webhook | `pr-event-worker` | PR head changed and evidence may be stale. |
| `driftward.github.pr.closed_unmerged` | Event ingress | `pr-event-worker` | A PR was closed without merge; record observation and do not dispatch Hermes. |
| `driftward.github.pr.closed_merged` | Event ingress | `pr-event-worker` | A PR was merged; record observation and do not dispatch Hermes. |
| `driftward.github.checks.completed` | GitHub webhook | `verification-worker` | CI/check state changed. |
| `driftward.github.review.submitted` | GitHub webhook | `review-worker` | Review state changed. |
| `driftward.linear.issue.observed` | Event ingress | Operators or later intake workflows | A Linear issue changed but is not eligible for implementation dispatch. |
| `driftward.linear.issue.ready_for_implementation` | Event ingress | `intake-worker` | A Linear Engineering issue passed implementation-readiness gates. |
| `driftward.work_item.intake.received` | `intake-worker` | Operators or later triage workflows | A ready Linear issue was converted into a bridge-backed work item. |
| `driftward.reconciliation.detected` | `reconciliation-worker` | Operators or later repair workflows | Stale or inconsistent control-plane state was observed. |

## Event Ingress Gates

External events are allowed to enter Hatchet before they are allowed to dispatch
Hermes. Treat event ingress as observation first and execution second.

Hatchet's native webhook triggers may be used for trusted internal/manual
workflows or simple non-provider triggers. Linear, GitHub, and other external
provider webhooks must go through Driftward event ingress first so signatures,
timestamps, provider event keys, payload normalization, and eligibility gates
are applied before a Hatchet event is emitted.

Fresh implementation dispatch from Linear is eligible only when the normalized
issue snapshot satisfies every gate:

```text
team = Engineering
status = Ready
no active blockedBy relation
status is not Done, Canceled, In Review, Needs Operator, or Blocked
delegate is not Driftward Factory
project execution policy is enabled for the immutable project ID
```

Legacy factory metadata is not a trigger. In particular, the retired
`Driftward Factory` delegate and old factory-era labels must not cause workflow
dispatch. Linear workflow status is the source of truth; labels such as
`Feature`, `Bug`, `Improvement`, `ready-to-implement`, and `has-active-pr` are
metadata only.

The installed command named `factory` is only the current Hatchet operator CLI
for logs and intake mode toggles. It does not revive retired delegates, labels,
comment commands, review/repair/landing flows, or controller automation.

Events that fail an execution gate should still be useful. Normalize and record
them as intake or reconciliation observations, then stop before bridge
submission. Example:

```text
Linear issue changed
  -> record an ingress event
  -> emit driftward.linear.issue.observed or driftward.linear.issue.ready_for_implementation
  -> do not dispatch Hermes from ingress

GitHub PR closed unmerged
  -> find linked Linear issue
  -> record an ingress event and emit driftward.github.pr.closed_unmerged
  -> do not dispatch Hermes
```

The Driftward Software Factory project is intentionally execution-disabled in
the reviewed registry. Its signed Linear events remain observable, but ingress
does not emit a ready event and intake rejects stale or manually delivered ready
events before any bridge lookup or submission. This policy applies in both
dry-run and real mode and does not modify existing bridge jobs.

## Idempotency

Every worker task that can cause an external effect must use one stable
idempotency key per intended effect.

Ingress remains per provider event. Bridge execution should be per active work
item when the effect is stateful, such as PR creation:

```text
linear:issue:<issue-key>:update:<delivery-id>          # ingress event key
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:active
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:attempt:<generation>
```

The bridge must reserve a factory attempt and its key before execution. The
first dry-run or real generation uses the active key; later generations use the
generationed attempt key. Repeated eligible Linear deliveries merge bounded
source-event correlation onto the active job instead of creating another
attempt. Dry-run and real PR creation use separate attempt lineages so a real
run cannot reuse a completed dry-run job. Reused active jobs should not re-emit
executor-triggering events.

For PR creation, derive deterministic external identifiers from the key:

```text
idempotency_key -> deterministic branch -> marker file/body -> existing PR lookup
```

## Worker Environment

Allowed:

- `HATCHET_CLIENT_TOKEN`
- Hatchet host/port/TLS profile settings
- Bridge URL, for example `http://127.0.0.1:19877`
- Bridge bearer token path or env var
- Non-secret worker names, queues, event names, and default budgets

Forbidden:

- Model provider credentials
- Driftward Hermes env files or profile/session state
- GitHub, Linear, Browserbase, Google Workspace, Railway, Notion, Gmail, or
  other platform credentials
- Docker socket access
- Host root filesystem mounts
- Host repository mounts; Hermes clones and writes under `/opt/data/work`
- Generic shell-command payloads from Hatchet

Real PR creation requires:

- `DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED=1`
- `DRIFTWARD_HERMES_ALLOWED_REPOS`, default
  `Driftward-LLC/driftward-host,Driftward-LLC/capykit`
- `DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN`
- `DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME`
- `DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL`
- A bridge payload with `repo`, plus `implementation_prompt`, `instructions`,
  or `linear_issue`

By default the executor forbids `derek-nguyen` and `derek@driftward.io` for
real PR creation. Driftward Hermes needs its own GitHub credential and Git
author identity before this flag is enabled. The expected identity must use the
GitHub App bot signature, for example `factory-hermes[bot]` and
`4491454+factory-hermes[bot]@users.noreply.github.com`.

The executor leaves timed-out Hermes jobs in `running` so Hatchet retries can
re-enter through the same idempotency key, deterministic branch, and PR marker.
Pre-execution validation failures transition to `failed` because no external
effect has started.

## Source Layout

The worker source is an npm workspace:

```text
hatchet/workers/
  README.md
  catalog.json
  package.json
  tsconfig.base.json
  shared/
    src/
      bridge-client.ts
      contracts.ts
      events.ts
      types.ts
  intake-worker/
    README.md
    package.json
    hatchet.yaml
    src/
      worker.ts
      workflows/
  execution-dispatch-worker/
    README.md
    package.json
    hatchet.yaml
    src/
      worker.ts
      workflows/
      triggers/
  bridge-executor-worker/
    README.md
    package.json
    hatchet.yaml
    src/
      worker.ts
      workflows/
      triggers/
  pr-event-worker/
    README.md
    package.json
    hatchet.yaml
    src/
      worker.ts
      workflows/
      events/
  reconciliation-worker/
    README.md
    package.json
    hatchet.yaml
    src/
      worker.ts
      workflows/
      triggers/
```

Build all worker packages:

```bash
npm install --prefix hatchet/workers
npm run build --prefix hatchet/workers
```

## Managed Runtime

Install or refresh the managed worker services:

```bash
bash hatchet/workers/install.sh
```

The installer builds the npm workspace, seeds root-only env files from the
`prod` Hatchet profile when available, installs systemd units, and enables:

```text
driftward-hatchet-hermes-gateway.service
driftward-hatchet-worker-execution-dispatch.service
driftward-hatchet-worker-intake.service
driftward-hatchet-worker-bridge-executor.service
driftward-hatchet-worker-pr-event.service
driftward-hatchet-worker-reconciliation.service
```

Runtime env files:

```text
/etc/driftward/hatchet-workers.env
  Hatchet client token, API URL, gRPC host/port, TLS strategy, bridge URL,
  intake repo allowlist

/etc/driftward/hatchet-worker-bridge-executor.env
  Explicit real-PR gate, Hermes gRPC target, Driftward Hermes command fallback,
  allowed repos, expected factory-hermes GitHub App identity
```

Use the factory CLI first when operating the worker path:

```bash
factory status
factory logs intake
factory logs executor
factory logs pr
factory logs reconciliation
factory logs linear -f
```

Use direct worker units only for focused worker debugging or remediation after
`factory status` or the smoke test points at a specific service:

```bash
systemctl status driftward-hatchet-worker-execution-dispatch.service --no-pager
systemctl status driftward-hatchet-worker-intake.service --no-pager
systemctl status driftward-hatchet-worker-bridge-executor.service --no-pager
systemctl status driftward-hatchet-worker-pr-event.service --no-pager
systemctl status driftward-hatchet-worker-reconciliation.service --no-pager

systemctl restart driftward-hatchet-worker-execution-dispatch.service
systemctl restart driftward-hatchet-worker-intake.service
systemctl restart driftward-hatchet-worker-bridge-executor.service
systemctl restart driftward-hatchet-worker-pr-event.service
systemctl restart driftward-hatchet-worker-reconciliation.service
```

Run the full Hatchet smoke, including a managed worker dry-run job:

```bash
driftward-hatchet-smoke
```

Use `hatchet worker dev --profile prod` only for local development from an
individual worker directory. It is not the host runtime.
