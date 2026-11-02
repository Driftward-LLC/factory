# PR Event Worker

Consumes PR-related Hatchet events and returns normalized read-only state. This
worker does not mutate GitHub, Linear, bridge jobs, or repository files.

## Workflow

- Workflow: `pr-event-observer`
- Events:
  - `factory.github.pr.created`
  - `factory.github.pr.synchronized`
  - `factory.github.pr.reopened`
  - `factory.github.pr.closed_unmerged`
  - `factory.github.pr.closed_merged`
  - `factory.github.checks.completed`
  - `factory.github.review.submitted`
- Task: `normalize-pr-event`

PR events are observation-only and set `dispatchEligible=false`; they must not
dispatch Hermes. `factory.github.pr.synchronized` and
`factory.github.pr.reopened` set `requiresFreshVerification` so later workers
can decide whether existing evidence is stale.

The worker also performs a read-only bridge lookup for reconciliation context.
It uses `bridgeJobId` when present, otherwise it scans recent bridge jobs for
the same PR URL. Lookup failures are recorded on the task output and do not
drop the provider event.
