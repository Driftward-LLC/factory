# Reconciliation Worker

Inspects Hatchet-to-Hermes bridge ledger state and compares it with bounded,
sanitized GitHub and Linear observations without reading platform credentials
or mutating external systems.

## Workflow

- Workflow: `reconciliation-bridge-state-check`
- Task: `inspect-bridge-ledger`
- Event emitted: `driftward.reconciliation.detected`
- Schedule: every 15 minutes (`*/15 * * * *`)
- Manual trigger: `hatchet trigger bridge-check --profile prod`

The worker checks bounded bridge, sanitized ingress, and Hatchet inventories.
Bridge scans rotate oldest-first through independent active and historical
cohorts, ordered by immutable `(created_at, job_id)`. The default 250-job budget
reserves 200 slots for active work and 50 for history (a one-job budget inspects
only active work and is explicitly incomplete). Each sweep freezes its upper
bound, so new arrivals cannot indefinitely postpone older jobs. Two progress
rows in the existing bridge database retain pending page IDs and an opaque
token across worker restarts; retries reread those IDs with current job state.
Only successful inspection, finding persistence, recovery dispatch and event
publication acknowledge a page. Failed work leaves that page pending.
Retries retain the original page size even if the requested budget changes.
Pending-page replay republishes its findings at least once, including findings
persisted before a failed publication; consumers deduplicate by stable finding
key. Ordinary new pages publish newly recorded findings only.

`coverage.active` and `coverage.history` report scanned/total counts,
`complete`, and `sweepComplete`. Reaching the end of a multi-page sweep does
not mean the current invocation checked the whole ledger. `complete`/`clean`
remain false unless both cohorts and every other evidence source are complete
in that invocation. Absence checks use bounded full-ledger correlation queries,
and duplicate branch/PR lookup includes jobs outside the current page.
It never reports clean when an inventory is unavailable or reaches its safe
limit. It compares every scanned bridge job and its append-only events for stale
active jobs, missing event history, missing Hatchet provenance, missing PR URLs
after committed PR effects, failed jobs that still record external effect refs,
and duplicate branch or PR references. It also detects stored-but-unpublished
ingress, eligible published ingress without a bridge job, and Hatchet runs
without bridge correlation. For every scanned bridge job it
compares the bridge PR URL and deterministic branch with the latest GitHub
ingress observation, active bridge jobs with the latest Linear status and
blockers, and real Hermes effects with their persisted Hermes session reference
and append-only `effect_committed` event.

Hermes receipt checks are isolated from backend-neutral comparisons; this does
not enable another executor. After verified handoff, normal draft promotion is
allowed, while a changed head or closed-unmerged PR is a historical-evidence
warning. Repository, PR URL and branch identity mismatches stay critical.
Missing fields, truncated histories and observations older than the recorded
effect/handoff cannot establish a current mutable-state contradiction.

Ingress owns provider verification and retains raw payloads. The worker uses
only loopback `GET /observations/latest` and `/reconciliation/observations`,
which return sanitized projections. The bridge records stable `reconciliation:v1:*`
finding keys over schema, subject, drift kind, expected state, and observed
state. A retry updates occurrence counts, while a changed branch, SHA, status,
or event creates a new finding key.

Bridge event history is read with a separate bounded limit per job. Any bridge,
ingress, Hatchet, or event-history limit reached is an explicit incomplete
finding and prevents a clean result. Finding writes are sorted and chunked to
the bridge's 250-record cap; a failed chunk is reported in worker telemetry
while later chunks continue.
An unsuccessful write fails the invocation and retains scan progress for retry.

This is not a read-only bridge client: it persists findings and scan/ACK state,
and currently reserves verification leases and republishes accepted events for
eligible stale verification jobs. It never creates a new job/branch/PR for
recovery. Scan endpoints are authenticated bridge-local POSTs at
`/reconciliation/scan` and `/reconciliation/scan/ack`; the read-only batched
`/reconciliation/correlations` POST accepts at most 250 keys per field.

It does not call GitHub, Linear, Driftward Hermes, Docker, or arbitrary shell.
Missing or unavailable observations are warnings; the reconciliation worker
does not perform the legacy direct bridge-row repair that used root `gh`
inspection. The old bridge reconciler service/timer is retired from the
scheduled runtime and must stay disabled; run its CLI only as a reviewed
break-glass recovery from the immutable bridge release.

## Input

```json
{
  "requestId": "manual-check",
  "maxJobs": 250,
  "maxEventsPerJob": 100,
  "staleAfterMinutes": 30,
  "emitWhenClean": false
}
```

`emitWhenClean` is useful for smoke tests because it proves the worker can emit
an event even when no findings exist.

The bridge exposes a 30-minute current projection separately from the complete
append-only findings history. This makes recent drift actionable without
discarding earlier evidence.
An empty current finding projection is not proof of a complete clean sweep.

## Deployment

Bridge schema 7 adds the two-cohort progress table and lookup indexes. Use the
existing reviewed backup and installer-only migration procedure before starting
the new worker/bridge together. Runtime connections perform no DDL. A rollback
to schema-6 code requires the matching pre-upgrade database snapshot; do not
delete scan rows or edit the live ledger to bypass the schema check. This source
change does not alter the registered workflow name, schedule or deployment.

## Environment

- `HATCHET_CLIENT_TOKEN` and Hatchet connection settings from the worker env.
- `HATCHET_HERMES_BRIDGE_URL`, default `http://127.0.0.1:19877`.
- `HATCHET_HERMES_BRIDGE_TOKEN`, required bearer token for the managed local bridge.
- `HATCHET_EVENT_INGRESS_URL`, loopback-only by default, default
  `http://127.0.0.1:19878`. Docker Compose sets
  `HATCHET_RECONCILIATION_ALLOW_DOCKER_INGRESS=1` so the worker may call the
  internal `http://event-ingress:19878` service hostname.
- `RECONCILIATION_OBSERVATION_TIMEOUT_MS`, bounded to 250-3000ms, default `1500`.
- `RECONCILIATION_OBSERVATION_MAX_AGE_MS`, bounded to 1 minute through 7 days,
  default `86400000`; stale snapshots are distinct warning findings.
- `RECONCILIATION_HATCHET_TIMEOUT_MS` and `HATCHET_HERMES_BRIDGE_TIMEOUT_MS`,
  bounded deadlines for read-only Hatchet and bridge calls.

The workflow is registered with one process-wide Hatchet run at a time, and
all observation clients in the worker process share one serialized ingress
gate. A caller's deadline begins only after it owns that gate, which preserves
the serial Python ingress server's capacity without inflating normal read
deadlines or turning timeout findings into absence findings.

No Hermes, GitHub, Linear, Browserbase, model-provider, or Docker credentials
belong in this worker.

Loopback disconnects are transport outcomes: ingress closes the response
quietly when a caller deadline expires, while reconciliation records a distinct
`*_observation_timeout` finding. A timeout is never treated as an absent
provider observation.
