# Factory

Main operator-facing CLI for Driftward factory operations.

This sits beside `hatchet/` and `deploy/` because it is not the workflow engine,
the deployment wrapper, or the execution runtime. It is the human control
surface over those systems.
The current `factory` CLI/front-door architecture replaces the retired
unattended factory/controller stack for this host.

ENG-97 tracks extracting this surface into a standalone Factory application
and repository. The proposed boundary and migration plan live in
`docs/factory-standalone-app-boundary.md`; the transitional Hatchet desired
state lives in `hatchet/desired-state/factory-hatchet.json`.

## Shape

The replacement factory path is the working composition of three separate
service boundaries:

```text
factory CLI
  -> Hatchet intake
  -> bridge ledger
  -> Driftward Hermes execution
```

Keep those roots separate:

- `factory/`: operator command and front-door documentation.
- `hatchet/`: control plane, workers, event ingress, and bridge source.
- `deploy/`: Docker image, Compose runtime, Hatchet Lite/Postgres topology,
  Linear token renewer, systemd wrapper, and smoke test.
- Driftward Hermes: isolated execution runtime outside this repository.

Do not move `hatchet/`, `deploy/`, or Driftward Hermes runtime state under
`factory/`. That would make the front-door command look like the owner of both
the control plane and execution runtime and blur the service boundaries that
keep the replacement architecture understandable.

Use this command first when managing the current factory path:

```bash
factory status
factory console
factory analytics --days 30
factory doctor hatchet --profile prod
factory mode
factory logs linear -f
factory attempts active
factory lifecycle ENG-62
factory attempts list ENG-62 --json
```

Current scope:

```bash
factory status
factory console
factory analytics
factory analytics --days 30 --json
factory doctor hatchet --profile prod
factory logs linear -f
factory logs lifecycle
factory mode
factory mode dry-run
factory mode real --yes
factory retry ENG-62 preview --supersede-pr https://github.com/OWNER/REPO/pull/123
factory retry ENG-62 preview --supersede-job BRIDGE-JOB-ID
factory retry ENG-62 --reason "prior draft PR was closed unmerged" --request-id eng-62-retry-20260816 --snapshot-digest PREVIEW_DIGEST --supersede-pr https://github.com/OWNER/REPO/pull/123 --yes
factory retry ENG-62 --reason "orphaned bridge job is obsolete" --request-id eng-62-orphan-20260823 --snapshot-digest PREVIEW_DIGEST --supersede-job BRIDGE-JOB-ID --yes
factory lifecycle ENG-62
factory lifecycle ENG-62 --json --limit 100
factory lifecycle dead-letters list
factory lifecycle dead-letters preview OUTBOX-KEY
factory lifecycle dead-letters archive OUTBOX-KEY --reason "reviewed historical record" --snapshot-digest PREVIEW_DIGEST --yes
factory attempts active
factory attempts list ENG-62 --json
factory attempts show ATTEMPT-ID
factory attempts events ATTEMPT-ID
factory attempts cancel ATTEMPT-ID --reason "operator canceled before execution" --yes
factory attempts refresh-receipt ATTEMPT-ID --reason "retry exact PR receipt inspection" --yes
```

`factory logs lifecycle` follows the root-owned Shipwright lifecycle writer.
It publishes bounded Linear status only; it never executes work or changes
intake mode, and its token is unavailable to Hatchet workers, the bridge,
ingress, and Hermes.

`factory status` is the one-glance health check for the current factory path.
It reports intake mode, the active Docker Compose runtime or legacy systemd
layout, loopback health endpoints, public Linear webhook readiness, and
advisory recovery/operator components.
Required failures return a nonzero exit status; advisory warnings are shown
without failing the command. Reconciliation findings for active jobs and
system inventory are actionable; findings attached to terminal jobs are shown
separately as historical debt and do not fail live health.

`factory console` prints the private URL for the read-only Factory Operations
Console. The console shows active and recent attempts, actionable versus
terminal reconciliation findings, lifecycle dead-letter counts, and the
complete sanitized lifecycle for an Engineering issue. It does not expose
operator mutations or credentials. Production requests must arrive through Tailscale
Serve so the console receives an authenticated `Tailscale-User-Login` header.
The console's FastAPI schema is available to authenticated operators at
`/api/openapi.json`.

`factory analytics` is the root-only, read-only performance and quality report.
It projects real, non-legacy attempt cohorts from the bridge ledger and joins
optional ingress and lifecycle evidence without creating a new analytics
store. Use `--days N` for a 1-to-365-day attempt-creation window and `--json`
for the versioned `factory-analytics-v1` result. Missing evidence remains
explicitly unknown or unavailable; in particular, the configured
`chatgpt_codex_oauth` route does not by itself prove whether Hermes, OAuth, or
the provider caused a failure. See
[`docs/factory-analytics.md`](../docs/factory-analytics.md) for metric grains,
coverage, and privacy constraints.

`factory doctor hatchet` is a read-only desired-state drift check. It collects
live Hatchet inventory with `--profile`, or compares a saved sanitized inventory
with `--live-json`, against
`hatchet/desired-state/factory-hatchet.json` and reports unknown active
workflows, retired workflows that remain active, unexpected event triggers, and
task actions without active repo-managed workers. It does not pause, remove, or
register workflows.

`factory attempts active` is the root-only answer to "is Factory running
anything right now?" It reads active attempt records through the operator socket
and includes the associated bridge job and execution lease summary when present.
`factory lifecycle ENG-X` shows the issue-level Factory record: attempts,
bridge jobs, execution leases, and a bounded timeline of attempt and bridge
events. Use `--json` on either command for scripts or deeper inspection.

`factory lifecycle dead-letters` is a root-only review surface. List and
preview return sanitized metadata without comment bodies or markers. Archive
requires an exact outbox key, a bounded reason, the latest preview digest, and
`--yes`; it retains the row and records the local socket actor, timestamp, and
reason instead of deleting or replaying the event.

Use `factory status` first, then direct `systemctl`, `curl`, Docker, or
service-specific commands only for
focused debugging, installer work, or break-glass recovery after `factory`
identifies the component involved.

## Fresh retries

`factory retry` is a root-only, explicit fresh-attempt control. It talks only
to `/run/driftward-factory-operator/operator.sock` with the separate token in
`/etc/driftward/factory-operator.env`; it never uses the bridge or verifier
token. Preview, list, and show use SQLite read-only mode and never migrate or
backfill the ledger. Reserve requires a bounded reason, a caller-stable request
ID, the fresh preview snapshot digest, and `--yes`, then transactionally records one generationed attempt, one
deterministic branch, one bridge job, and append-only audit events.

The operator uses the latest sanitized Linear ingress projection rather than
labels, comments, or a manually supplied project/repository. It requires an
Engineering/Ready, nonterminal, unblocked snapshot routed through an enabled
canonical project policy, plus healthy ingress, bridge, and executor services.
It blocks every active attempt across modes. It never closes a PR, deletes a
branch, or changes Linear.

Before reusing an issue with a prior artifact, a durable bridge outbox invokes
the fixed read-only Hermes action `inspect_draft_pr_state` with the exact
canonical repository and matching PR URL/number. The CLI/operator never runs
Docker, Hermes, or arbitrary commands. While that bounded request is pending
or unavailable, retry blocks. Open, unknown, and missing artifacts block. A merged
artifact requires a new issue. A closed-unmerged artifact requires the exact
`--supersede-pr` URL; the old attempt is then recorded as superseded and no
provider mutation is made. Cancellation only fences future executor work; if
an effect already exists it records `canceled_after_effect` and leaves that PR
untouched for human handling.

When a historical bridge job is terminal but does not belong to any factory
attempt, use `--supersede-job BRIDGE-JOB-ID` with preview and reserve. The
operator verifies the job is a terminal `create_draft_pr` record for the same
Linear issue and canonical repository, records an append-only
`job_superseded_by_attempt` event on the old job, and carries the superseded
job evidence into the fresh attempt payload. Factory-owned jobs are rejected
here; use the attempt retry/supersede path for those.

If an authorized executor disappears after the final effect gate, the attempt
enters `effect_outcome_unknown` and ordinary retry stays blocked. The only
break-glass retry path is:

```bash
factory retry ENG-X force-absence preview --attempt-id ATTEMPT-ID
factory retry ENG-X force-absence --attempt-id ATTEMPT-ID \
  --reason "reviewed GitHub branch and PR absence" \
  --request-id "operator:ENG-X:force-absence:YYYYMMDD" \
  --snapshot-digest DIGEST-FROM-PREVIEW \
  --yes
```

The CLI first asks the operator for the exact canonical repository and prior
head branch, then gathers fixed GitHub evidence with `gh pr list --head` and a
branch lookup. The operator accepts only fresh evidence proving the expected
branch does not exist and no PR exists for that head, verifies the old attempt
is `effect_outcome_unknown` with an expired authorized lease and no prior PR or
recovery record, records `absence_superseded_unknown_attempt`, and reserves the
next generation in one transaction. This path is for reviewed incidents only;
it does not delete remote artifacts or mutate Linear.

The operator obtains the authenticated local actor from Linux socket
`SO_PEERCRED` (uid, pid, and best-effort account lookup); it never accepts an
actor from CLI JSON. This proves the local peer, not an interactive user or
terminal session. Retry accepted events and inspection requests are delivered
from leased, durable outboxes. An executor takes an atomic execution lease and
rechecks it immediately before Hermes; cancellation of a claimed execution is
cooperative. Expired execution leases block retry/reclaim pending artifact
recovery, preventing a second PR path.

Rollout: keep Factory dry-run, run `bash hatchet/bridge/install.sh`, confirm
the operator socket and bridge/ingress/executor health, then use a preview
against a non-production fixture. Roll back by returning Factory to dry-run,
stopping `driftward-factory-operator.service`, and preserving the attempt
ledger for reconciliation; do not delete attempts or artifacts.
