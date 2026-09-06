# Contribution Guide

Use this guide to get productive quickly in the Factory repository and to make
workflow, task, event, worker, bridge, ingress, lifecycle, CLI, and deployment
changes without crossing runtime boundaries.

Factory is the product/runtime surface for Hatchet-backed Driftward Factory
work. It owns Hatchet workflows, workers, event ingress, the Hatchet-to-Hermes
bridge, the lifecycle writer, the operator CLI, desired state, contracts, and
Docker Compose deployment artifacts. Driftward Hermes still owns agent
execution, credentials, sessions, MCP, browser state, and provider integrations.

## Fast Start

Start from the repository root:

```bash
cd /root/driftward/repos/factory
git status --short
npm install --prefix hatchet/workers
```

Useful checks while developing:

```bash
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
```

Run focused tests for the area you touched:

```bash
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer \
  python3 -m unittest discover -s hatchet/bridge/tests
PYTHONPATH=hatchet/event-ingress \
  python3 -m unittest discover -s hatchet/event-ingress/tests
PYTHONPATH=hatchet/factory-lifecycle-writer \
  python3 -m unittest discover -s hatchet/factory-lifecycle-writer/tests
```

Check live Hatchet drift read-only:

```bash
factory doctor hatchet --profile prod
```

Use `factory status`, `factory mode`, and `factory logs <component>` as the
operator front door before reaching for direct service commands.

## Repository Map

- `factory/`: operator-facing CLI and front-door documentation.
- `hatchet/contracts/`: canonical Driftward-owned Hatchet event contract.
- `hatchet/desired-state/`: desired Hatchet workers, workflows, tasks, event
  triggers, and retired workflow history.
- `hatchet/workers/`: TypeScript Hatchet worker and workflow source.
- `hatchet/event-ingress/`: provider webhook verification, normalization,
  event ledger, eligibility gates, and Hatchet event publishing.
- `hatchet/bridge/`: local bridge API, idempotency ledger, factory attempts,
  outboxes, recovery tools, and Hermes boundary.
- `hatchet/factory-lifecycle-writer/`: lifecycle projection and Linear comment
  outbox.
- `deploy/`: Docker image, Compose runtime, systemd wrapper, install helper,
  smoke test, and deployment docs.
- `docs/`: architecture, boundary, release, and service runbooks.
- `tools/`: contract generation, doctor checks, and repo-level tests.

Read the local README before changing an area. For Hatchet-specific work, also
read `hatchet/AGENTS.md` and the relevant subdirectory README.

## Non-Negotiable Boundaries

- Do not commit secrets, tokens, webhook URLs, cookies, Hatchet profile files,
  runtime databases, or Hermes session material.
- Do not give Hatchet workers provider credentials, GitHub credentials, Linear
  credentials, Browserbase state, MCP credentials, Hermes env files, Docker
  socket access, host root mounts, or generic shell-command authority.
- External provider webhooks must enter through `hatchet/event-ingress/`, not
  direct Hatchet workflow webhooks.
- Workers do not dispatch external effects directly. They submit, observe, or
  transition bounded bridge jobs.
- Every external effect needs a stable idempotency key reserved before the
  effect can run.
- `bridge-executor-worker` is the only worker allowed to cross into bounded
  Hermes execution, and only through the reviewed bridge/Hermes gateway path.
- `factory doctor hatchet` is read-only. Pause, remove, register, apply, or
  live migration paths require explicit reviewed operator approval.
- Factory deploys through the Docker Compose path in this repository. Do not
  resurrect retired host-local controller paths.

## Workflow And Task Development

Use this checklist when creating or changing a Hatchet workflow or task.

1. Pick the right worker boundary.

   Add a workflow to an existing worker when it shares that worker's credential
   and effect boundary. Create a new worker only when the boundary, queue, or
   operational ownership is materially different.

2. Name the workflow for its responsibility.

   Use:

   ```text
   <domain>-<action>-<object>[-mode]
   ```

   Supported prefixes:

   | Prefix | Use for |
   | --- | --- |
   | `intake-*` | Consuming external or normalized events and deciding eligibility. |
   | `bridge-*` | Submitting, reading, correlating, or transitioning bridge jobs. |
   | `executor-*` | Performing fenced side effects or invoking Hermes/providers. |
   | `verify-*` | Validating an existing side effect or artifact. |
   | `reconcile-*` | Observing or repairing drift between durable systems. |
   | `lifecycle-*` | Writing operator-facing lifecycle status. |
   | `pr-*` | Observing pull-request events and advancing read-only PR state. |

   Task names should be stable, explicit action names such as
   `reserve-ready-linear-issue-attempt`, `execute-create-draft-pr`, or
   `normalize-pr-event`. Treat deployed workflow names, task names, and event
   wire names as operator-facing API.

3. Add or update worker source.

   Typical files:

   ```text
   hatchet/workers/<worker>/src/worker.ts
   hatchet/workers/<worker>/src/workflows/<workflow>.ts
   hatchet/workers/<worker>/src/triggers/<trigger>.ts
   hatchet/workers/<worker>/src/events/<event-helper>.ts
   ```

   Register the workflow in `src/worker.ts`. Keep business rules in named
   helpers that are easy to unit test. Keep task code deterministic where
   Hatchet may replay it, and move side effects into explicit child tasks or
   bridge calls with idempotency.

4. Use shared contracts instead of string copies.

   Import event constants and helpers from `@driftward/hatchet-worker-shared`
   where possible:

   ```ts
   import { HATCHET_EVENTS } from '@driftward/hatchet-worker-shared';
   ```

   Do not hand-edit generated files:

   ```text
   hatchet/event-ingress/hatchet_event_ingress/contracts.py
   hatchet/workers/shared/src/contracts.ts
   ```

5. Update catalog and desired state.

   Update `hatchet/workers/catalog.json` with the workflow or worker purpose,
   stage, allowed effects, forbidden effects, consumed events, produced events,
   task names, and next workflows.

   Update `hatchet/desired-state/factory-hatchet.json` for active repo-managed
   workers and workflows. Include source file, worker, task actions, event
   triggers, and runbook path. If replacing an existing workflow, preserve it
   under `retired_workflows` with a reason and audit-history note.

6. Document the runbook.

   Every active worker should have a short README with:

   - workflow name;
   - task names;
   - consumed and emitted events;
   - allowed effects;
   - forbidden effects;
   - required non-secret environment;
   - local or managed runtime commands;
   - failure, retry, idempotency, and reconciliation behavior.

7. Build and test.

   At minimum, run:

   ```bash
   npm run build --prefix hatchet/workers
   python3 tools/hatchet-contracts.py check
   python3 -m unittest discover -s tools -p 'test_*.py'
   ```

   Add or update tests near the code you changed. For worker logic, prefer
   small tests around routing, payload shaping, idempotency keys, and fail-closed
   branches. For bridge, ingress, and lifecycle changes, run the focused Python
   unittest command for that subsystem.

## Event Contract Changes

`hatchet/contracts/events.json` is canonical for event IDs, `driftward.*` wire
names, default decisions, reason codes, reconciliation kinds, bridge lookup
statuses, and provider action mappings.

Use readable IDs:

```text
github_pr_closed_merged
linear_issue_ready_for_implementation
```

Keep wire names under the `driftward.*` namespace:

```text
driftward.github.pr.closed_merged
```

Update flow:

```bash
python3 tools/hatchet-contracts.py generate
python3 tools/hatchet-contracts.py check
```

Commit `hatchet/contracts/events.json` plus regenerated contract outputs when
the contract changes.

## Event Ingress Changes

Ingress is observation first and execution second. Provider events should be
recorded even when they are not eligible to dispatch work.

When adding or changing a provider event path:

- verify signatures and timestamps when configured;
- normalize provider payloads into a Driftward-owned event shape;
- generate a stable provider event key for dedupe;
- preserve raw payloads only in the local ingress ledger;
- decide eligibility from explicit normalized fields, not labels or free text;
- emit only contract-backed Hatchet events;
- keep Hermes dispatch out of ingress.

Linear implementation dispatch is eligible only when every gate passes:

```text
team = Engineering
status = Ready
no active blockedBy relation
status is not Done, Canceled, In Review, Needs Operator, or Blocked
delegate is not Driftward Factory
project execution policy is enabled for the immutable project ID
```

## Bridge And External Effects

Use the bridge whenever workflow work can cause an external effect. The bridge
owns:

- idempotency keys;
- factory attempts and generations;
- deterministic branch reservation;
- bridge job state;
- append-only events;
- execution and verification leases;
- outboxes used for Hatchet and lifecycle projections.

External-effect tasks must fail closed before authorization when inputs are
invalid. Once an effect may have started, preserve enough state for retry and
reconciliation instead of authorizing a duplicate attempt.

Use these idempotency shapes for the current implementation path:

```text
linear:issue:<issue-key>:update:<delivery-id>
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:active
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:attempt:<generation>
```

Dry-run and real lineages are separate. Repeated eligible Linear deliveries for
one active issue should correlate with the active job instead of creating
another active PR attempt.

## CLI And Operations Changes

The `factory` CLI is the operator front door. Favor it for status, logs, intake
mode, attempts, lifecycle, and read-only Hatchet drift checks.

CLI changes should:

- be safe by default;
- support `--json` when useful for automation;
- avoid printing secrets;
- make dry-run/read-only behavior explicit;
- require `--yes` for risky live operations;
- include tests in `tools/test_factory_cli_static.py` or nearby focused tests.

## Deployment Changes

Factory deploys as a Docker Compose app from this repository. Before changing
Docker, systemd, runtime paths, env files, health checks, or exposed ports, read
`docs/containerized-service-runbook.md` and `docs/factory-release-runbook.md`.

Validate deployment artifacts with:

```bash
HATCHET_POSTGRES_PASSWORD=ci-postgres-password \
  HATCHET_SERVER_AUTH_COOKIE_SECRETS=ci-cookie-secret \
  HATCHET_ADMIN_PASSWORD=ci-admin-password \
  FACTORY_SOURCE_ROOT="$PWD" \
  docker compose -f deploy/docker-compose.yml config >/dev/null
docker build -f deploy/Dockerfile -t driftward/factory:local .
```

Production releases use date tags:

```text
vYYYY.M.D
vYYYY.M.D.N
```

Do not deploy from a dirty checkout, and do not include secrets, runtime
databases, profile files, or Hermes session material in release artifacts.

## Review Checklist

Before opening or handing off a change, confirm:

- the touched area has a README or doc update when behavior changed;
- new workflows/tasks appear in `hatchet/workers/catalog.json`;
- active workflows/tasks/workers appear in
  `hatchet/desired-state/factory-hatchet.json`;
- new events are in `hatchet/contracts/events.json` and generated outputs are
  fresh;
- worker credential boundaries did not expand;
- provider webhooks still flow through event ingress;
- external effects still reserve through the bridge first;
- idempotency keys are stable across retries;
- failure paths are fail-closed before external effects and reconcilable after
  possible effects;
- relevant unit, build, contract, Docker, or smoke checks were run and noted.

