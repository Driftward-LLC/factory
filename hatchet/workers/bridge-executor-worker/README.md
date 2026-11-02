# Bridge Executor Worker

Consumes accepted bridge jobs and advances bridge state. Dry-run jobs complete
locally. Real `create_draft_pr` jobs are gated behind an explicit environment
flag and invoke Hermes with a deterministic branch plus bridge
idempotency marker.

## Workflow

- Workflow: `bridge-executor-create-draft-pr`
- Event: `factory.hermes.job.accepted`
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
`factory.hermes.job.effect_committed` or `factory.hermes.job.failed` event,
or until the configured receipt wait expires. This keeps variable Hermes
runtime out of the worker task timeout while preserving Hatchet visibility for
the end-to-end workflow. Host `systemd-run` dispatch passes the allowlisted
runner environment through a protected one-shot `EnvironmentFile`, never
through command-line `--setenv` values; direct/Docker dispatch uses the same
explicit allowlist instead of inheriting the full worker environment.

The real worker emits:

```text
factory.hermes.job.effect_committed
factory.hermes.job.verification_pending
factory.hermes.job.ready_for_human_review
```

Dry-run jobs then emit:

```text
factory.github.pr.creation_simulated
```

A failed verification emits `factory.hermes.job.verification_failed` plus the
terminal failed event with a stable reason code. Real Hermes-created or reused
PR jobs emit:

```text
factory.github.pr.created
```

The simulated event is analytics/control-plane evidence only. It must not be
treated as a real GitHub side effect.

## Real PR Execution

Real PR execution is disabled unless:

```bash
FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED=1
```

Configuration example. Replace repository and identity placeholders with the
reviewed installation values; legacy defaults are installation-specific:

```bash
FACTORY_HERMES_GRPC_TARGET=unix:///run/factory-hermes-factory/hermes.sock
FACTORY_HERMES_AUTH_MODE=unknown
FACTORY_HERMES_VERSION=
FACTORY_HERMES_COMMAND=factory-hermes
FACTORY_HERMES_ALLOWED_REPOS=OWNER/REPO
FACTORY_HERMES_EXPECTED_GITHUB_LOGIN=your-app[bot]
FACTORY_HERMES_EXPECTED_GIT_AUTHOR_NAME=your-app[bot]
FACTORY_HERMES_EXPECTED_GIT_AUTHOR_EMAIL=APP-ID+your-app[bot]@users.noreply.github.com
FACTORY_HERMES_FORBIDDEN_GITHUB_LOGINS=your-human-login
FACTORY_HERMES_FORBIDDEN_GIT_AUTHOR_EMAILS=operator@example.com
FACTORY_HERMES_CREATE_DRAFT_PR_TIMEOUT_MS=1200000
FACTORY_HERMES_ASYNC_RECEIPTS_ENABLED=1
FACTORY_HERMES_ASYNC_CREATE_DRAFT_PR_TIMEOUT_MS=14400000
FACTORY_HERMES_RECEIPT_WAIT_DURATION=4h
FACTORY_HERMES_MAX_BUFFER_BYTES=2097152
FACTORY_HERMES_PR_VERIFICATION_TIMEOUT_MS=180000
FACTORY_HERMES_PR_VERIFICATION_POLL_MS=15000
```

`FACTORY_HERMES_AUTH_MODE` is a non-secret analytics label. Set it to
`chatgpt_codex_oauth` or `api_key` only when the deployed route is known;
otherwise leave the default `unknown`. It does not inspect or expose an OAuth
token and cannot identify whether Hermes, OAuth, or the provider caused an
execution failure.

The backend version is a non-secret deployment label; secret-shaped values are
removed by the installers. Provider/model env labels are retired and ignored.
New route records retain their legacy provider/model fields as `unknown`.
After identity checks, the executor reads Hermes's native configuration and
pins that model/provider on the CLI invocation. A bounded, job-bound gRPC
header reports `execution_model_selected` through the existing fenced ledger
endpoint, including when the invocation subsequently fails. Workers never read
Hermes configuration or credentials themselves. Missing, invalid, or unavailable
metadata remains unknown and cannot block execution or receipt settlement.

This is startup selection, not evidence that a provider call succeeded or that
every later call used that model. Hermes's native PostHog observer reports
individual calls, using the same Factory attempt/job correlation. PostHog is
not queried by execution or recovery. See the `models` section of
`factory analytics --json` for selection coverage; historical route labels
are preserved but never substituted for missing runtime evidence.

The current executor truthfully emits the fixed route `factory-default-v1` /
`hermes` / `static`; setting arbitrary experiment labels cannot manufacture an
A/B cohort. A future router must perform and enforce the actual assignment.

When `FACTORY_HERMES_GRPC_TARGET` is set, the bridge executor and detached
receipt runner call the typed Hermes Factory gRPC gateway instead of invoking
the Hermes wrapper directly. The default production target is a Unix domain
socket at `/run/factory-hermes-factory/hermes.sock`; TCP transport is opt-in
with `FACTORY_HERMES_GRPC_ALLOW_TCP=1`. Remote TCP requires
`FACTORY_HERMES_GRPC_TLS=1` **and** caller authentication, using either:

- A shared `FACTORY_HERMES_GRPC_AUTH_TOKEN` on server and client. Set
  `FACTORY_HERMES_GRPC_SERVER_KEY_PATH` and
  `FACTORY_HERMES_GRPC_SERVER_CERT_PATH` on the server; set
  `FACTORY_HERMES_GRPC_CA_PATH` on the client if the server certificate uses
  a private CA.
- Mutual TLS: additionally set `FACTORY_HERMES_GRPC_CA_PATH` on the server
  to a dedicated client CA and configure the client with
  `FACTORY_HERMES_GRPC_CLIENT_KEY_PATH` and
  `FACTORY_HERMES_GRPC_CLIENT_CERT_PATH`. The gateway requires and verifies
  client certificates signed by this CA; trust only authorized Factory
  callers. Server-only TLS is rejected. When a token is also configured,
  both the certificate and token are required.

Keep gateway tokens and TLS private keys in protected runtime configuration,
not command-line arguments. Provider credentials still belong only to Hermes.
The existing `FACTORY_HERMES_GRPC_ALLOW_INSECURE_TCP=1` development escape
hatch is limited to literal `127.0.0.1:PORT` or `[::1]:PORT` targets and still
requires a token. Unix sockets retain their existing local permission boundary.
Authenticated transport alone does not provide durable remote execution or
make an unknown execution outcome safe to retry.

`FACTORY_HERMES_EXPECTED_GITHUB_LOGIN`,
`FACTORY_HERMES_EXPECTED_GIT_AUTHOR_NAME`, and
`FACTORY_HERMES_EXPECTED_GIT_AUTHOR_EMAIL` are required for real PR
execution. They must match GitHub's App bot signature for the dedicated
Hermes GitHub App. The bridge validates the configured Git author and
GitHub repository access from inside the isolated Hermes runtime
through the gateway before it asks Hermes to create or reuse a PR.

The bridge payload must include:

```json
{
  "repo": "OWNER/REPO",
  "work_item_id": "ENG-123",
  "linear_issue": "ENG-123",
  "implementation_prompt": "bounded implementation request"
}
```

`instructions` may be used instead of `implementation_prompt`. A Linear issue
alone is accepted only when Hermes can use its Linear tools to inspect the
issue.

Hermes must use its own workspace under `/opt/data/work` for clones and
branches. Do not pass host paths such as `<source-checkout>` or
`<host-repositories>` as `repo_path`.

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
