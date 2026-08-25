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
factory doctor hatchet --profile driftward
factory mode
factory logs linear -f
factory attempts list ENG-62 --json
```

Current scope:

```bash
factory status
factory doctor hatchet --profile driftward
factory logs linear -f
factory logs lifecycle
factory mode
factory mode dry-run
factory mode real --yes
factory retry ENG-62 preview --supersede-pr https://github.com/OWNER/REPO/pull/123
factory retry ENG-62 preview --supersede-job BRIDGE-JOB-ID
factory retry ENG-62 --reason "prior draft PR was closed unmerged" --request-id eng-62-retry-20260816 --snapshot-digest PREVIEW_DIGEST --supersede-pr https://github.com/OWNER/REPO/pull/123 --yes
factory retry ENG-62 --reason "orphaned bridge job is obsolete" --request-id eng-62-orphan-20260823 --snapshot-digest PREVIEW_DIGEST --supersede-job BRIDGE-JOB-ID --yes
factory attempts list ENG-62 --json
factory attempts show ATTEMPT-ID
factory attempts cancel ATTEMPT-ID --reason "operator canceled before execution" --yes
```

`factory logs lifecycle` follows the root-owned Shipwright lifecycle writer.
It publishes bounded Linear status only; it never executes work or changes
intake mode, and its token is unavailable to Hatchet workers, the bridge,
ingress, and Hermes.

`factory status` is the one-glance health check for the current factory path.
It reports intake mode, required Hatchet/Hermes/lifecycle systemd units,
loopback health endpoints, and advisory recovery/operator components. Required
failures return a nonzero exit status; advisory warnings are shown without
failing the command.

`factory doctor hatchet` is a read-only desired-state drift check. It collects
live Hatchet inventory with `--profile`, or compares a saved sanitized inventory
with `--live-json`, against
`hatchet/desired-state/factory-hatchet.json` and reports unknown active
workflows, retired workflows that remain active, unexpected event triggers, and
task actions without active repo-managed workers. It does not pause, remove, or
register workflows.

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
