# Intake Worker

Consumes gated Linear issue events and turns them into bounded bridge jobs.

## Workflow

- Workflow: `intake-linear-ready-dispatch`
- Task: `reserve-ready-linear-issue-attempt`
- Event consumed: `driftward.linear.issue.ready_for_implementation`
- Events emitted:
  - `driftward.work_item.intake.received`

The default mode reserves dry-run `create_draft_pr` attempts through the
factory attempt ledger. Real mode uses the same reservation path, then the
factory outbox emits `driftward.hermes.job.accepted` for the bridge executor.
Use `factory mode` to inspect or change intake mode.

Ingress remains per event: every Linear delivery is recorded with its own
event key. Bridge execution is per active issue and factory attempt: repeated
`Ready` deliveries for the same issue reuse the active attempt-backed bridge job
while preserving source-event correlation. Fresh dry-run and real attempts use
these generation-one idempotency keys:

```text
hatchet:work-item:linear:<issue-key>:create_draft_pr:<dry-run|real>:active
```

Generation two and later use `...:attempt:<generation>`. That keeps webhook
audit history complete while preventing repeated status toggles from creating
multiple active PR attempts.

When the active bridge job already exists, the worker records the intake
observation but does not enqueue another accepted event. That avoids waking the
executor for completed or already-running work.

## Boundary

The worker does not read Linear, GitHub, Hermes, Browserbase, model-provider,
operator-socket, or Docker credentials. It trusts only the already-normalized
ingress event and the local bridge API.

## Environment

- `HATCHET_CLIENT_TOKEN` and Hatchet connection settings from the worker env.
- `HATCHET_HERMES_BRIDGE_URL`, default `http://127.0.0.1:19877`.
- `HATCHET_HERMES_BRIDGE_TOKEN`, optional bearer token for the local bridge.
- `HATCHET_INTAKE_ROUTING_REGISTRY_PATH`, default
  `hatchet/workers/routing-registry.json`; non-secret override for deployment
  and tests.
- `HATCHET_INTAKE_ALLOWED_REPOS`, comma-separated repo allowlist.
- `HATCHET_INTAKE_DRY_RUN`, default `true`; set through `factory mode`.

Repository selection and execution permission are exact and fail-closed: the
worker resolves only the normalized Linear `projectId` through the versioned
routing registry. Every route must explicitly set `execution_enabled`; a
disabled, missing, or malformed policy rejects the event before bridge lookup
or submission in both dry-run and real mode. Project names, titles,
descriptions, and labels are diagnostics only and cannot select a repository.
The bridge payload records the resolved repository plus the project ID,
diagnostic project name, and registry version.
