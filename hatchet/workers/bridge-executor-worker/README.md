# Bridge Executor Worker

Consumes accepted bridge jobs and advances bridge state. Dry-run jobs complete
locally. Real `create_draft_pr` jobs are gated behind an explicit environment
flag and invoke Driftward Hermes with a deterministic branch plus bridge
idempotency marker.

## Workflow

- Workflow: `bridge-executor-create-draft-pr`
- Event: `driftward.hermes.job.accepted`
- Tasks:
  - `execute-create-draft-pr`
  - `await-hermes-receipt`

Dry-run jobs retain the historical local-only transition:

```text
accepted -> running -> effect_committed -> completed
```

Successful real jobs instead transition:

```text
accepted -> running -> effect_committed -> verification_pending -> ready_for_human_review
```

For real jobs, `execute-create-draft-pr` performs pre-effect validation,
claims/authorizes the factory execution fence, and starts a detached Hermes
receipt runner. The Hatchet workflow then remains open in the durable
`await-hermes-receipt` task until the runner emits a scoped
`driftward.hermes.job.effect_committed` or `driftward.hermes.job.failed` event,
or until the configured receipt wait expires. This keeps variable Hermes
runtime out of the worker task timeout while preserving Hatchet visibility for
the end-to-end workflow.

The real worker emits:

```text
driftward.hermes.job.effect_committed
driftward.hermes.job.verification_pending
driftward.hermes.job.ready_for_human_review
```

Dry-run jobs then emit:

```text
driftward.github.pr.creation_simulated
```

A failed verification emits `driftward.hermes.job.verification_failed` plus the
terminal failed event with a stable reason code. Real Hermes-created or reused
PR jobs emit:

```text
driftward.github.pr.created
```

The simulated event is analytics/control-plane evidence only. It must not be
treated as a real GitHub side effect.

## Real PR Execution

Real PR execution is disabled unless:

```bash
DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED=1
```

Optional environment:

```bash
DRIFTWARD_HERMES_COMMAND=driftward-hermes
DRIFTWARD_HERMES_ALLOWED_REPOS=Driftward-LLC/driftward-host
DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN=factory-hermes[bot]
DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME=factory-hermes[bot]
DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL=4491454+factory-hermes[bot]@users.noreply.github.com
DRIFTWARD_HERMES_FORBIDDEN_GITHUB_LOGINS=derek-nguyen
DRIFTWARD_HERMES_FORBIDDEN_GIT_AUTHOR_EMAILS=derek@driftward.io
DRIFTWARD_HERMES_CREATE_DRAFT_PR_TIMEOUT_MS=1200000
DRIFTWARD_HERMES_ASYNC_RECEIPTS_ENABLED=1
DRIFTWARD_HERMES_ASYNC_CREATE_DRAFT_PR_TIMEOUT_MS=14400000
DRIFTWARD_HERMES_RECEIPT_WAIT_DURATION=4h
DRIFTWARD_HERMES_MAX_BUFFER_BYTES=2097152
DRIFTWARD_HERMES_PR_VERIFICATION_TIMEOUT_MS=180000
DRIFTWARD_HERMES_PR_VERIFICATION_POLL_MS=15000
```

`DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN`,
`DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME`, and
`DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL` are required for real PR
execution. They must match GitHub's App bot signature for the dedicated
Driftward Hermes GitHub App. The bridge validates the configured Git author and
GitHub repository access from inside the isolated Driftward Hermes runtime
before it asks Hermes to create or reuse a PR.

The bridge payload must include:

```json
{
  "repo": "Driftward-LLC/driftward-host",
  "work_item_id": "ENG-35",
  "linear_issue": "ENG-35",
  "implementation_prompt": "bounded implementation request"
}
```

`instructions` may be used instead of `implementation_prompt`. A Linear issue
alone is accepted only when Hermes can use its Linear tools to inspect the
issue.

Hermes must use its own workspace under `/opt/data/work` for clones and
branches. Do not pass host paths such as `/root/driftward/repos/factory` or
`/root/driftward/repos` as `repo_path`.

Retry behavior:

- `accepted` jobs transition to `running` before Hermes starts.
- `running` jobs with an authorized factory execution wait for the scoped
  Hermes receipt event instead of blocking one Hatchet worker task on the whole
  implementation runtime.
- The detached receipt runner commits the provider receipt through the original
  factory execution lease, then emits the scoped Hatchet event that wakes the
  durable waiter.
- If the receipt runner errors after authorization, it settles the execution as
  unobserved so reconciliation treats the provider outcome as unknown instead
  of authorizing a duplicate PR path.
- Hermes must check for an existing branch or PR from the deterministic branch
  before creating a new PR.
- After Hermes returns a PR, the bridge records the artifact and enters
  `verification_pending`. The bounded Hermes runtime then verifies the expected
  repository/base/branch/draft state, Factory App author, exact Linear link,
  declared changed-file scope, all GitHub checks, and reported
  local-command/secret-check evidence. A missing,
  malformed, pending, or failed item leaves the bridge job failed while
  preserving the PR URL; only green evidence reaches `ready_for_human_review`.
  Incomplete checks are polled only within the bounded verification timeout;
  this is verification waiting, never an implementation repair or a new PR
  attempt.
- Timeout or malformed Hermes output leaves the bridge job in `running` for
  reconciliation/retry rather than marking a possible side effect as failed.
- Pre-execution validation failures, such as a repo allowlist miss, transition
  to `failed`.
