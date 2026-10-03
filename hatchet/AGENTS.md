# Driftward Factory Hatchet Instructions

This directory is the Factory-owned Hatchet Lite source tree for Driftward
bridge-backed intake and control-plane operation. For work under
`/root/driftward/repos/factory/hatchet`, this file and the Hatchet README files
are more specific than the parent repository `AGENTS.md`.

Authoritative docs:

- `README.md` for runtime scope, services, operations, secrets, and upgrade
  path.
- `bridge/README.md` for the Hatchet-to-Hermes bridge and execution ledger.
- `workers/README.md` for worker boundaries, event names, idempotency, and
  worker runtime rules.
- `event-ingress/README.md` for provider webhook ingress and eligibility
  gates.
- `contracts/README.md` for event contract update flow.
- `workers/catalog.json` for worker and workflow purpose, effects, event
  inputs/outputs, and explicit non-goals.

## Scope

- Runtime root: `/docker/driftward/factory`
- Compose project: `driftward-factory`
- Main systemd unit: `driftward-factory.service`
- Local dashboard/API: `http://127.0.0.1:8888`
- Private dashboard/API URL:
  `https://srv1379035.tail5c6a78.ts.net:8888/`
- Local Factory Console: `http://127.0.0.1:19881`
- Worker gRPC: `127.0.0.1:7077`
- Health port: `127.0.0.1:8733`
- Secret env: `/etc/driftward/hatchet.env`
- Full smoke test: `factory-docker-smoke`

Hatchet is a control plane only. Keep it local-only and low-volume. The
dashboard must stay exposed through Tailscale Serve, not a public listener, and
the container binding must remain on `127.0.0.1`.

## Credential Boundary

Do not mount or copy these into Hatchet, its workers, event ingress, or the
bridge:

- Driftward Hermes provider credentials
- Browserbase or browser session/profile state
- GitHub credentials
- Linear credentials
- MCP credentials
- Model provider credentials
- Google Workspace, Railway, Notion, Gmail, or other platform credentials
- Docker socket access
- Host root filesystem mounts
- Host repository mounts
- Generic shell-command payloads from Hatchet

Workers may use Hatchet client settings plus the bridge URL/token. The
`bridge-executor-worker` is the only worker that may contact the typed Hermes
gateway, and only for allowlisted bridge actions after a bridge job row exists
and a deterministic branch has been reserved. Containerized workers must not
invoke `driftward-hermes` directly. Legacy deployments use the host gateway;
fresh bundled deployments run the typed gateway inside their isolated executor
and perform its allowlisted Git/GitHub operations locally without Docker access.

Root-owned host Codex uses the separate verified `VPS-Agent` Linear identity.
Its renewable token and OAuth client credentials remain under
`/root/.config/linear`; do not copy or mount them into Hatchet.

Real PR creation requires the explicit Driftward Hermes execution gate and the
expected GitHub App identity in
`/etc/driftward/hatchet-worker-bridge-executor.env`. By default, do not allow
human Derek credentials or author identity for real PR creation.

## Architecture Boundary

Use this responsibility split:

```text
Hatchet workflow
  durable orchestration, retries, waits, events, dashboard state

Hatchet worker
  registered tasks, Hatchet client token, bridge URL/token only

Hatchet Hermes bridge
  idempotency key, job row, append-only events, external-effect fencing

Driftward Hermes
  agent execution, credentials, MCP, browser/platform tools, sessions
```

Hatchet workers submit bounded jobs to the bridge. The bridge is not a generic
command runner and must not receive Hermes credentials or provider/session
state. Driftward Hermes owns execution authority and credentials.

## Driftward Hermes Session Retention

Driftward Hermes sessions are part of the bridge audit trail when a bridge job
stores `hermes_session_id`. The bridge database remains the durable source of
job state, PR URL, commit SHA, verification context, and append-only lifecycle
events, but the linked Hermes session transcript is kept in Driftward Hermes'
own session store.

Do not manually delete Driftward Hermes session files, `state.db`, or profile
state from Hatchet, bridge, worker, or host cleanup tasks. Use the
`driftward-hermes` wrapper and Hermes' built-in session commands only.

Before pruning Driftward Hermes sessions:

```bash
driftward-hermes hermes sessions stats
driftward-hermes hermes sessions list --limit 20
driftward-hermes hermes backup --quick --label before-session-prune
```

Safe default pruning policy:

```bash
driftward-hermes hermes sessions prune --older-than 90
driftward-hermes hermes sessions optimize
```

Do not prune at shorter windows such as 7, 14, 30, or 60 days unless
bridge-linked `hermes_session_id` values have first been identified and
preserved, or Derek explicitly approves losing those transcripts. Bridge jobs
can remain valid without the transcript, but forensic debugging and audit
review are degraded.

## Source Map

- `bridge/`: local-only execution ledger and bridge API.
- `event-ingress/`: local-only provider webhook ingress and normalization.
- `workers/`: Hatchet worker and workflow source.
- `contracts/events.json`: canonical event contract.
- `tools/hatchet-contracts.py`: contract generation and validation.

Current workers:

- `intake-worker`
- `execution-dispatch-worker`
- `bridge-executor-worker`
- `pr-event-worker`
- `reconciliation-worker`

Do not build merge, deploy, release, broad repair, or protected-branch workers
until draft PR creation, checks, reviews, idempotency, and reconciliation are
proven.

## Workflow Naming And Catalog

Hatchet workflow names appear in the dashboard, logs, retries, and operator
debugging. They must describe the workflow's responsibility boundary, not just
the general feature being touched.

Use this naming shape for new or renamed workflows:

```text
<domain>-<action>-<object>[-mode]
```

Use these domain prefixes consistently:

```text
intake-*      consumes external or normalized events and decides eligibility
bridge-*      submits, reads, correlates, or transitions bridge jobs
executor-*    performs fenced side effects or calls Hermes/providers
verify-*      validates an existing effect
reconcile-*   observes or repairs drift
lifecycle-*   writes operator-facing lifecycle status
pr-*          observes pull-request events
```

Before adding or renaming a workflow, update `workers/catalog.json` with:

- `name`, `worker`, and `stage`.
- `purpose`, written as one concrete sentence.
- `does_not`, naming important boundaries the workflow refuses to cross.
- `tasks`, `consumes_events`, `produces_events`, and `next_workflows` when
  applicable.

Prefer catalog metadata and clear names over source comments. Add a short source
comment above a workflow declaration only when a dashboard-visible name could
still be confused with an adjacent workflow.

## Event Ingress

Linear, GitHub, and other external provider webhooks must enter through
Driftward event ingress before Hatchet workflows. Do not wire provider
webhooks directly to Hatchet workflow webhook URLs.

Ingress is observation first and execution second. It verifies provider
signatures/timestamps when configured, normalizes payloads, dedupes events in
SQLite, applies gates, and emits Driftward-owned Hatchet events. Ingress must
not dispatch Hermes directly.

Fresh implementation dispatch from Linear is eligible only when all gates pass:

```text
team = Engineering
status = Ready
no active blockedBy relation
status is not Done, Canceled, In Review, Needs Operator, or Blocked
delegate is not Driftward Factory
```

Legacy factory metadata is not a trigger. Linear workflow status is the source
of truth; labels such as `Feature`, `Bug`, `Improvement`,
`ready-to-implement`, and `has-active-pr` are metadata only.

## Idempotency

Every task that can cause an external effect must use one stable idempotency
key per intended effect. The bridge must reserve the idempotency key before
execution.

Ingress remains per provider event. Bridge execution should be per active work
item for stateful effects such as PR creation:

```text
linear:issue:<issue-key>:update:<delivery-id>
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:active
```

Submitting the same bridge key with the same action and payload must return
the existing job. Submitting the same key with different intent is a conflict
and should stop. Dry-run and real PR creation use separate active keys.

## Contracts

`contracts/events.json` is canonical for Driftward-owned Hatchet event IDs,
wire names, default decisions, reason codes, bridge lookup statuses, and simple
provider action mappings.

Use readable IDs in code and analytics, and keep `driftward.*` prefixes on
Hatchet wire names. Treat deployed wire names as effectively immutable.

After editing contracts, regenerate and check:

```bash
python3 tools/hatchet-contracts.py generate
python3 tools/hatchet-contracts.py check
```

Do not edit generated files by hand:

```text
event-ingress/hatchet_event_ingress/contracts.py
workers/shared/src/contracts.ts
```

Run `python3 tools/hatchet-contracts.py check` before committing contract,
catalog, ingress, or worker event changes.

## Operations

Use `factory` as the main operator entry point for the current Hatchet Lite
factory path:

```bash
factory status
factory mode
factory mode dry-run
factory mode real --yes
factory logs all
factory logs ingress
factory logs intake
factory logs executor
factory logs pr
factory logs reconciliation
factory logs bridge
factory logs linear -f
factory attempts list ENG-62
```

`factory mode real --yes` allows Ready Linear issues to reserve real
factory-attempt-backed bridge jobs for Driftward Hermes draft PR execution. It
does not revive the retired
unattended factory/controller stack, delegates, labels, comment commands,
review/repair/landing flows, or controller automation.

Use direct loopback APIs only for focused debugging after `factory status`,
`factory logs`, or the smoke test points at a component:

```bash
curl -fsS 'http://127.0.0.1:19878/events?limit=10' | jq
curl -fsS -H "Authorization: Bearer $(awk -F= '$1 == \"HATCHET_HERMES_BRIDGE_TOKEN\" { print $2; exit }' /etc/driftward/hatchet-bridge.env)" 'http://127.0.0.1:19877/jobs?limit=10' | jq
```

Check runtime health and smoke through the factory front door first:

```bash
factory status
factory-docker-smoke
```

After changing Hatchet runtime, worker, bridge, ingress, service, Compose, or
Tailscale behavior, run the relevant focused checks and then
`factory-docker-smoke` when practical.

## GitHub PR Workflow

Always use the `$driftward-gh-pr` skill before creating, updating, publishing,
labeling, triaging, or handing off a pull request in any Driftward-LLC
repository.

For meaningful Codex-authored work, prefer a branch and early draft PR as the
collaboration surface. If a user says to push from `main` or another default
branch and does not explicitly request a direct push, create a branch, push it,
and open or update a draft PR instead.

Before ending a turn with meaningful branch work, ensure one outcome is clear:

- A draft PR is open or updated.
- The branch is explicitly local-only or exploratory.
- The branch is deleted, or the user has been asked before leaving an
  abandoned branch around.

PRs must have durable metadata:

- Add `codex-authored` for Codex-created or Codex-substantially-edited PRs.
- Add one type label, usually `documentation`, `enhancement`, or `bug`.
- Link related Linear work with plain text such as `Refs Linear ENG-41`.
- Link related GitHub issues with `Fixes #N`, `Closes #N`, or `Resolves #N`
  only when merging the PR should close the issue; otherwise use `Refs #N`.

Do not use GitHub workflow labels such as `agent-handoff`, `codex-check`, or
`needs-review` for Linear-owned work. Put readiness, review, blocked, handoff,
and follow-up state in Linear.

## Secrets

Keep these files mode `0600` and never commit or paste their contents into
Linear, GitHub, Codex, logs, or docs:

- `/etc/driftward/hatchet.env`
- `/etc/driftward/hatchet-bridge.env`
- `/etc/driftward/hatchet-workers.env`
- `/etc/driftward/hatchet-worker-bridge-executor.env`
- `/etc/driftward/hatchet-event-ingress.env`

The key files under `/docker/driftward/factory/keys/hatchet` are generated by
the Hatchet admin image and mounted read-only into `hatchet-lite`.

## Development

Build all worker packages:

```bash
npm install --prefix workers
npm run build --prefix workers
```

Install or refresh the Factory Compose runtime:

```bash
bash ../deploy/install-docker.sh
```

Use `hatchet worker dev --profile prod` only for local development from an
individual worker directory. Direct worker systemd units are migration
artifacts, not the host runtime.

<!-- hatchet-skills:start -->
## Hatchet Agent Skills

Hatchet agent skills are installed in `skills/hatchet-cli/`. When working with this project's Hatchet workflows, read the relevant reference:

- **Setup CLI**: `skills/hatchet-cli/references/setup-cli.md`
- **Start worker**: `skills/hatchet-cli/references/start-worker.md`
- **Trigger & watch**: `skills/hatchet-cli/references/trigger-and-watch.md`
- **Debug a run**: `skills/hatchet-cli/references/debug-run.md`
- **Replay a run**: `skills/hatchet-cli/references/replay-run.md`

Full skill: `skills/hatchet-cli/SKILL.md`
<!-- hatchet-skills:end -->
