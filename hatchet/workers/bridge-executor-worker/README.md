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
the end-to-end workflow. Host `systemd-run` dispatch passes the allowlisted
runner environment through a protected one-shot `EnvironmentFile`, never
through command-line `--setenv` values; direct/Docker dispatch uses the same
explicit allowlist instead of inheriting the full worker environment.

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
DRIFTWARD_HERMES_GRPC_TARGET=unix:///run/driftward-hermes-factory/hermes.sock
DRIFTWARD_HERMES_AUTH_MODE=unknown
DRIFTWARD_HERMES_PROVIDER=unknown
DRIFTWARD_HERMES_MODEL=unknown
DRIFTWARD_HERMES_VERSION=
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

`DRIFTWARD_HERMES_AUTH_MODE` is a non-secret analytics label. Set it to
`chatgpt_codex_oauth` or `api_key` only when the deployed route is known;
otherwise leave the default `unknown`. It does not inspect or expose an OAuth
token and cannot identify whether Hermes, OAuth, or the provider caused an
execution failure.

The provider, model, and backend version values are also non-secret route
labels. They are recorded before preflight so dispatch and policy failures
remain in the selected cohort. Unknown values stay explicit; the worker never
reads Hermes authentication or runtime state to fill them in. Labels must use
bounded lowercase identifiers (`a-z`, digits, `.`, `_`, or `-`). The current
installers remove secret-shaped values (including common `sk-` and provider-key
prefixes) from these fields; version labels are omitted and provider/model
labels become `unknown`. Existing unsafe env-file values are neutralized on
install.

The current executor truthfully emits the fixed route `factory-default-v1` /
`hermes` / `static`; setting arbitrary experiment labels cannot manufacture an
A/B cohort. A future router must perform and enforce the actual assignment.

When `DRIFTWARD_HERMES_GRPC_TARGET` is set, the bridge executor and detached
receipt runner call the typed Hermes Factory gRPC gateway instead of invoking
the Hermes wrapper directly. The default production target is a Unix domain
socket at `/run/driftward-hermes-factory/hermes.sock`; TCP transport is opt-in
with `DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1`. Remote TCP requires
`DRIFTWARD_HERMES_GRPC_TLS=1` **and** caller authentication, using either:

- A shared `DRIFTWARD_HERMES_GRPC_AUTH_TOKEN` on server and client. Set
  `DRIFTWARD_HERMES_GRPC_SERVER_KEY_PATH` and
  `DRIFTWARD_HERMES_GRPC_SERVER_CERT_PATH` on the server; set
  `DRIFTWARD_HERMES_GRPC_CA_PATH` on the client if the server certificate uses
  a private CA.
- Mutual TLS: additionally set `DRIFTWARD_HERMES_GRPC_CA_PATH` on the server
  to a dedicated client CA and configure the client with
  `DRIFTWARD_HERMES_GRPC_CLIENT_KEY_PATH` and
  `DRIFTWARD_HERMES_GRPC_CLIENT_CERT_PATH`. The gateway requires and verifies
  client certificates signed by this CA; trust only authorized Factory
  callers. Server-only TLS is rejected. When a token is also configured,
  both the certificate and token are required.

Keep gateway tokens and TLS private keys in protected runtime configuration,
not command-line arguments. Provider credentials still belong only to Hermes.
The existing `DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP=1` development escape
hatch is limited to literal `127.0.0.1:PORT` or `[::1]:PORT` targets and still
requires a token. Unix sockets retain their existing local permission boundary.
Authenticated transport alone does not provide durable remote execution or
make an unknown execution outcome safe to retry.

`DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN`,
`DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME`, and
`DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL` are required for real PR
execution. They must match GitHub's App bot signature for the dedicated
Driftward Hermes GitHub App. The bridge validates the configured Git author and
GitHub repository access from inside the isolated Driftward Hermes runtime
through the gateway before it asks Hermes to create or reuse a PR.

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
  declared changed-file scope, aggregate GitHub check status, and reported
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
