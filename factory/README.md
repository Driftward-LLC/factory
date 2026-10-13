# Factory

Main operator-facing CLI for Driftward factory operations.

Factory is the operator surface. Hermes is an internal execution layer;
operators do not need to chat with it. Use interactive development sessions for
deeper investigation and PostHog for agent diagnostics.

### Agent trace links

Set `FACTORY_CONSOLE_POSTHOG_PROJECT_URL` in the active installation's
`env/factory-operator.env` (legacy host: `/etc/driftward/factory-operator.env`)
to your browser project URL, for example `https://us.posthog.com/project/12345`.
This is public navigation configuration, not an API key. US, EU and legacy app
PostHog Cloud hosts are supported; ingestion hosts and self-hosted URLs are not.
Recreate the operator service through the reviewed deployment procedure to load
configuration changes. No executor credentials or PostHog query key belong in
Factory. The console makes no PostHog requests until the user follows the link.

Overview, Work and issue history show **Capture unconfirmed** with **Find trace
in PostHog** for real attempts whose sealed job lineage matches the attempt.
This is a lookup, not a claim that a trace exists. Each retry has its own trace ID.
Dry runs, missing lineage, and absent/invalid project configuration are explicit
non-link states. Links use the Hermes observer's attempt UUIDv5 contract and
PostHog's [trace route](https://github.com/PostHog/posthog/blob/master/products/ai_observability/manifest.tsx).
Links include the first recorded backend invocation/start time, or the attempt
creation time if execution timing is absent, so old runs are not restricted to
PostHog's current date window. Missing or invalid timestamps produce no link.
Navigation not configured, missing correlation/timing, and dry runs are distinct
from a recorded `factory_receipt_analytics_setup_failed` telemetry setup failure.
That reason does not establish a PostHog capture/upload failure. No current
ledger field confirms ingestion, so missing evidence stays unknown, never
"not captured" or "capture failed". Older runs may predate
correlation, events may be missing, and PostHog access/retention still apply.
Telemetry cannot authorize manual editing or change Factory ownership.

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
factory attempts evidence ATTEMPT-ID
factory attempts events ATTEMPT-ID
factory attempts cancel ATTEMPT-ID --reason "operator canceled before execution" --yes
factory attempts refresh-receipt ATTEMPT-ID --reason "retry exact PR receipt inspection" --yes
factory attempts resolve-interrupted ATTEMPT-ID inspect --yes
factory attempts resolve-interrupted ATTEMPT-ID preview --attestation FILE [--absence-evidence FILE] --reason "reviewed containment"
```

Root-attested interrupted-execution resolution has a separate
[safety contract and confirmation procedure](../docs/interrupted-execution.md).
It requires proven external containment and preserved workspace evidence;
lease expiry or an unchanged PR is not sufficient. It never retries execution
or marks the implementation successful.

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
returned sanitized lifecycle for an Engineering issue. Its Analytics tab shows
cohort delivery, PR-quality evidence coverage, repair outcomes, failures, and
stage timing for the selected attempt-creation window. It does not expose
operator mutations or credentials. Production requests must arrive through Tailscale
Serve so the console receives an authenticated `Tailscale-User-Login` header.
The console's FastAPI schema is available to authenticated operators at
`/api/openapi.json`.

Analytics reads an explicit aggregate projection at `/api/analytics?days=N`
using the existing ledgers, with missing evidence and coverage shown directly.
The browser does not receive the full operator report, raw events, backend or
authentication configuration, or source paths. Recorded verification and
observed check suites do not establish reviewer approval or first-pass CI.

The dark operator workspace follows Linear's public app references, with compact
icon navigation, status indicators, and a toolbar for issue lookup. The Overview
page leads with unresolved-execution, pending-verification, and recent-failure
review queues, then active work. Recent activity and system status share the
lower desktop view. Queue links and sidebar shortcuts open matching Work filters; failed
attempts can have newer active generations. Runtime and ledger totals are
expandable, and system findings remain separate from work queues. See
[`DESIGN.md`](../DESIGN.md) for the interface direction. Work
filters by issue/repository/branch text, stage, repository, and execution mode.
It combines all active attempts with the most recent 30 and deduplicates by
attempt ID; it is not a complete Linear backlog or historical search.
Quick Work scope links preserve the other filters. Filters and issue selection
are shareable in the URL, including the existing
`?issue=ENG-N` links, and support browser back/forward navigation.
Selecting an issue opens its inspection view directly; closing it restores the
page and filters. Narrow screens show compact attempt entries with expandable
metadata instead of horizontally scrolling tables.

Verification labels describe recorded evidence, not a PR's current head,
merge state, or outstanding review. Issue inspection shows immutable checkpoint
states for the newest returned attempt and compares its verified SHA with the
latest stored PR inspection. Changed heads, inspections predating verification,
stale observations and missing evidence are explicit. Refreshing does not call
GitHub or request a new inspection. Local checks remain executor-reported and
independent review remains unavailable in this view. See
[`work evidence`](../docs/factory-work-evidence.md) for the contract.
Attempt age and recorded span are not
model-runtime measurements. The issue timeline shows at most 80 returned
events; source/API limits also apply. Unknown and unavailable data remain
explicit, and a failed refresh retains the last successful snapshot with a
stale indicator. Automatic refresh runs every 30 seconds while the tab is
visible.

The frontend uses React components and hooks in `factory/console/src`, bundled
with esbuild. FastAPI serves the generated `app.js` alongside the existing HTML
and CSS; all assets stay local under the current content security policy.
Docker builds the bundle and excludes frontend build dependencies from the
runtime image. The generated bundle is not committed.

Build before running the console from a source checkout or its API/browser tests:

```bash
npm ci --prefix factory/console
npm run build --prefix factory/console
```

For local edits, `npm run dev --prefix factory/console` watches and rebuilds the
bundle. Refresh the existing local console to see changes. This command does
not start another server or connect to production.

Run the console API tests with the bridge's pinned Python dependencies:

```bash
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer python3 -m unittest discover -s hatchet/bridge/tests -p test_console.py
```

The browser regression check uses disposable API fixtures, never live jobs.
Playwright is a test-only dependency; install it outside the repository:

```bash
npm install --prefix /tmp/factory-console-checks --no-save --ignore-scripts playwright@1.55.0
node /tmp/factory-console-checks/node_modules/playwright/cli.js install chromium --only-shell
NODE_PATH=/tmp/factory-console-checks/node_modules node tools/test_factory_console_browser.cjs
```

An existing compatible Chromium executable can be selected with
`FACTORY_CONSOLE_CHROMIUM`. Set `FACTORY_CONSOLE_EVIDENCE_DIR` to retain all
console, ownership-control, backend and Storybook screenshots in a named evidence
directory; the default is the system temporary directory. Screenshots are
fictional test data. Each check closes its browser and local server afterward.
The ordinary browsing check asserts that every request is read-only and runs
with the production Content Security Policy, including `style-src 'self'`.
The ownership checks exercise the explicit preview and confirm flow separately:

```bash
NODE_PATH=/tmp/factory-console-checks/node_modules node tools/test_factory_issue_control_browser.cjs
NODE_PATH=/tmp/factory-console-checks/node_modules node tools/test_factory_issue_control_backend_browser.cjs
```

The first uses synthetic HTTP fixtures. The second uses the real console API,
disposable SQLite stores and a loopback test identity proxy; it verifies one
authorized fresh attempt, one outbox record and two ownership audit entries.
Set `FACTORY_CONSOLE_TEST_PYTHON` to the Python environment containing the bridge's
pinned dependencies. Neither check contacts the deployed console or an executor.

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
