# PR Event Worker

Consumes PR-related Hatchet events and returns normalized read-only state. This
worker does not mutate GitHub, Linear, bridge jobs, or repository files.

## Workflow

- Workflow: `pr-event-observer`
- Events:
  - `driftward.github.pr.created`
  - `driftward.github.pr.synchronized`
  - `driftward.github.pr.reopened`
  - `driftward.github.pr.closed_unmerged`
  - `driftward.github.pr.closed_merged`
  - `driftward.github.checks.completed`
  - `driftward.github.review.submitted`
- Task: `normalize-pr-event`

PR events are observation-only and set `dispatchEligible=false`; they must not
dispatch Hermes. `driftward.github.pr.synchronized` and
`driftward.github.pr.reopened` set `requiresFreshVerification` so later workers
can decide whether existing evidence is stale.

The worker also performs a read-only bridge lookup for reconciliation context.
It uses `bridgeJobId` when present, otherwise it scans recent bridge jobs for
the same PR URL. Lookup failures are recorded on the task output and do not
drop the provider event.
