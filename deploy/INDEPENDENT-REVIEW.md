# Native Hermes independent reviewer

This dormant path uses a native Hermes execution profile behind the
existing host gateway. It starts no additional chat gateway, bot, daemon or
Codex CLI runtime. `ReadOnlyReview` still accepts only a reserved bridge job,
fixes `gpt-6-astra`, and returns the bounded `factory-independent-review-v1`
receipt with `backend: isolated_hermes`. The model supplies verdict/findings only.
Historical `isolated_codex` receipts remain readable.

The root-owned reviewer provisioning file may set `hermes_container` to a Docker
container name or full ID, for example `factory-hermes`. Omission preserves the
legacy `hermes-driftward-hermes-1` selection. The gateway inspects that selection,
requires it to be running with the configured `hermes_image`, and passes its
full container ID to both the reviewer relay and timeout cleanup. Replacing a
container under the same name cannot redirect an already resolved review.
This is container selection, not a new filesystem sandbox or a guarantee that
the same container cannot restart. Provision fresh authentication separately;
changing the binding does not copy credentials or enable reviews.

Each invocation creates a fresh `factory-reviewer-<UUID>` profile under
Hermes' dedicated `/opt/data/factory-reviewer-auth/profiles/` root.
Its generated `config.yaml` selects
Astra and disables fallback, plugins, MCP, Tool Search, connectors, memory and
compression. It copies no generator config, SOUL, memory, sessions, skills,
browser state, credentials or tools. Each fresh profile has one writer.
The SDK skips repository context, identity and background review. A profile
separates state; it is not a filesystem sandbox. The trusted SDK process runs
inside the existing Hermes container, with model authority enforced
by exactly seven prefixed source tools and native pre-request/tool-dispatch
guards. It has no local shell, filesystem, delegate, platform or tool-discovery
tools. Unrelated execution runtimes are never selected.

Hermes' supported routed-profile home and empty secret scope prevent default and
project dotenv loading. The reviewer requires a separate native ChatGPT sign-in
stored once at `/opt/data/factory-reviewer-auth/auth.json`. Each invocation sets
only its own process's `HERMES_HOME` to that custom root; Hermes' routed-profile
fallback and refresh locking use this dedicated auth domain, never generator
`/opt/data/auth.json`. Fresh execution profiles receive no copied tokens.
`auth.adopt_external_logins: false` in both the dedicated root and generated
profile disables external CLI adoption/recovery. A private-file, exact-root and
read-only native login-status preflight fails closed before model construction.
The adapter does not serialize, mount, export or copy OAuth tokens. HOME and
the runtime's default environment/config/model remain unchanged.

An operator must create the dedicated root mode 0700 and its `config.yaml` mode
0600 with `auth: {adopt_external_logins: false}` before starting sign-in. Use a
one-off environment-allowlisted native process rooted there and the installed
`hermes_cli.auth_codex._login_openai_codex(..., force_new_login=True)` with the
`openai-codex` provider registry entry. This native fresh-login entrypoint skips
both existing-login offers and Codex CLI imports; do not use `auth add`'s initial
pool-seeding path for setup. Only the user completes device/browser consent.
Never copy a generator token, reuse a profile clone, or rewrite global HOME or
CODEX_HOME. The earlier shared-login synthetic-source live canary is not proof
of this separate sign-in; repeat the bounded provider check only after consent.

Reviewer tracing uses the Factory-owned `review_observer.py`, explicitly attached
to four native Hermes lifecycle hooks while generic plugin discovery remains
suppressed. It captures only the existing native runtime's `POSTHOG_API_KEY` and
optional `POSTHOG_HOST` before environment scrubbing; it does not load dotenv or
forward telemetry credentials to the host relay, workers, source tests or browser.
The endpoint must be HTTPS without userinfo, query, fragment or a path prefix;
redirects are refused. No telemetry credentials are newly provisioned here.

New reviews can export `$ai_generation` and `$ai_span` events with an independent
UUIDv5 trace scoped to the durable review ID, repository, immutable head and pass.
Filter by `factory.role=reviewer` and `factory.review_id`; head, pass and repository
are explicit bounded metadata outside the prompt. A tool span's parent is its
actual native API request's generation span. This path does not guess generator
attempt/job relationships or export generator session identifiers. Existing
reviews are not backfilled, and no production tracing availability is claimed.

Events contain only these identities, fixed model/provider, timing, token counts,
tool names and bounded status. They exclude prompts, requirements, source,
completions, findings, tool arguments/results, screenshots, raw errors and
credentials. Export has a bounded queue and daemon worker; callbacks never wait
for HTTP. Native cleanup makes a bounded flush attempt before emitting the
assessment, because the relay then terminates the SDK process. Missing config,
invalid endpoints and delivery failures do not change the verdict or authorize
replay. A hard kill, queue overflow or flush timeout can lose events; traces are
observability evidence rather than completion or authorization receipts.

The executor's `observability/posthog` plugin also registers `factory_github` in
the existing `terminal` toolset, only in the named `factory-executor` profile
with a local terminal backend and an existing `gh` executable. New session
prompts prefer this tool for GitHub CLI/API work. For example, use
`{"args":["pr","view","123"],"workdir":"/path/to/checkout"}`. It executes an
argv array without a shell, runs the native terminal command approval guard,
and builds a native profile-scoped, scrubbed child environment. It does not
change credentials or inherit terminal session exports or working directory;
provide `workdir` explicitly. Input is noninteractive (empty stdin by default),
with a 1 MiB character limit, 256 arguments / 262,144 total characters, a
1–180 second timeout, and 100,000 captured bytes per output stream. Timeout or
excess output kills the dedicated process group. Native per-thread interruption
is checked before launch and at most every 100 ms during stream and process
waits; an interrupt kills and reaps that group. Normal stdout/stderr/exit code
remain in the local tool result and are never exported to PostHog.

The actual CLI execution produces a `$ai_span` child of the native tool span,
which is itself a child of its generation. `github.observation=cli_invocation`,
`github.cli_operation` (fixed allowlist, `help_or_version`, or `unknown`),
`github.cli_status` (`success`, `nonzero_exit`, `timeout`, `output_limit`, or
`spawn_error`, `interrupted_before_spawn`, or `interrupted`),
`github.process_started`, and optional integer `github.exit_code`
describe the local process only. `github.effect_status=unverified` always:
successful CLI exit is not evidence of a particular HTTP request or remote
mutation. Interruption before launch reports `process_started=false`; interruption
after launch reports `true` and the reaped process exit code. Help/version labels
recognize standalone commands or flags in simple command positions; ambiguous
later help flags fall back to `unknown`, and body/title values are not commands.
Arguments, endpoints, resource names, stdin, outputs and raw exception
text never enter this child event. Native approval denial or unavailable guards
returns before spawning; missing native middleware identity also fails closed.

Outer tool spans include `factory.external_operation_coverage`:
`controlled_github_cli` for `factory_github`, otherwise `unknown`. Inner GitHub
calls in `terminal`, `execute_code`, `process`, arbitrary scripts, direct HTTP,
`git`, alternate executables or remote terminal backends are not observed by this
boundary. Queue drops, process termination and delivery failures can also leave
gaps; these fields do not promise a complete external-action ledger. No old
events are backfilled. Installing this source requires a separately reviewed
profile upgrade; the existing guarded deployment/blob gates remain in force.
`factory profiles configure` rejects already-active roles. Use the separately
reviewed `factory profiles upgrade-observer preview|apply` path described in
[CLI.md](CLI.md) for the observer-only active-profile change. Its digest approval,
quiescence checks and consumed durable receipt preserve credentials, role
configuration, reviewer policy and paused execution. This host helper operation
does not change deployment/blob gates. The existing `canary executor` uses no
tools; `factory profiles canary github` exercises the fixed native version call
without a model request and returns the real parent/child IDs for verification.
If the exact observer target was selected but final service readiness failed,
the bounded `factory profiles recover-observer` preview/apply/status path can
restore the gateway and an explicitly selected previously running dispatcher.
It preserves the failed receipt and paused executor without repeating the
observer mutation; other failure stages require separate operator diagnosis.

`tools/check_factory_profile_runtime_native.py` exercises the pinned Hermes
catalog and real dispatcher offline with a synthetic `gh` subprocess, checking
the generation → tool → CLI parent IDs and output secrecy. It does not prove
PostHog ingestion. After an approved profile upgrade, use the installed
`factory profiles canary github`, then verify its exact session, trace, tool-call,
parent and `help_or_version` child IDs server-side. The spans are labeled
validation-only and the receipt reports `server_verification_pending`. This
needs no GitHub API write; activation and delivery verification remain separate.

The host starts one credential-free source container with an immutable Git
bundle/database, independent checkout, test tools and fresh Playwright browser.
It has no provider/platform credentials, outbound network, shared Hermes data,
Docker socket or host root. Its image is read-only, with bounded tmpfs/process/
CPU/memory, dropped capabilities and no-new-privileges. The host's one-shot stdio
relay forwards only the seven typed source operations through `docker exec`.
No network is attached to the shared Hermes container; no provider proxy or
reviewer egress network is required. Source dependencies contain Playwright,
not Codex. Screenshots become native Hermes multimodal image parts in the
Responses request.

Build and pin `deploy/independent-reviewer/Dockerfile` for the source role.
It keeps the reviewed Node base digest and Playwright MCP 0.0.83 lock, Git/Python
and Chromium. Project-specific offline test dependencies may require a reviewed
derived image. The image does not provide the reviewed project's npm or pip
dependency trees: the synthetic HTML canary does not verify its full UI/build.
Read-only review remains useful when the changed code and available focused
checks suffice; missing required browser/dependencies/checks must yield
inconclusive. Source networks are fresh internal Docker 28+ IPv4 bridges with
gateway mode `isolated` and IPv6 disabled, avoiding a reachable host bridge.

The adapter targets the installed Hermes v0.21.6 (2026.9.24) API. Before startup
it checks a SHA-256 over the public SDK/auth/dotenv/profile-scope/tool/dispatch
files listed in `native_agent.API_FILES`. This pins private subclass guard
points and auth behavior; it is an additional compatibility check, not a
whole-image integrity claim. Root-owned provisioning also pins the existing
Hermes container image ID. An upgrade requires reviewing the adapter, rerunning
its synthetic probe and repeating the real canary. Unexpected tools/API fail
closed.

Named-profile activation binds the gateway's trusted
`DRIFTWARD_HERMES_PROFILE=factory-executor` environment and the provisioning
`auth_root` to `/opt/data/profiles/factory-independent-reviewer` while execution
is drained. The executor wrapper and runtime metadata probes select the same
profile. An unset executor profile and omitted reviewer `auth_root` preserve
the legacy paths. No other executor profile or reviewer auth root is accepted.
The reviewer named root holds its separate sign-in; every review still creates
a fresh child profile with empty secret scope and disabled generic plugins.
Launch and independent cleanup carry the same auth root. Existing root/parent
symlinks are refused. The reviewed activation moves the existing separate
reviewer sign-in into the named root; it does not adopt a generator login.
Reviewer traces add `factory.profile=factory-independent-reviewer` only when
that named auth root is selected, preserving the existing review trace identity.

Activation remains a separate reviewed operator change. Root-owned mode-0600
`/etc/driftward/factory-reviewer.json` must contain:

```json
{
  "schema": "factory-hermes-reviewer-provisioning-v2",
  "image": "reviewer-source@sha256:REVIEWED_SOURCE_IMAGE_DIGEST",
  "hermes_image": "sha256:REVIEWED_EXISTING_DRIFTWARD_HERMES_IMAGE_ID",
  "canary_digest": "DIGEST_OF_RETAINED_OPERATOR_CANARY_EVIDENCE",
  "policy_digest": "CURRENT_FACTORY_REVIEW_POLICY_DIGEST",
  "review_skill_digest": "SHA256_OF_REVIEW_CODE_SNAPSHOT",
  "verify_skill_digest": "SHA256_OF_VERIFY_APP_SNAPSHOT",
  "repositories": {"OWNER/REPO": {"base_ref": "main"}}
}
```

Managed instruction snapshots are root-owned mode-0600
`/etc/driftward/factory-reviewer/review-code.md` and `verify-app.md`. Their verified
contents enter the prompt explicitly; installed plugin presence is insufficient.
Source fetch separately requires a source-only GitHub read credential at
`/etc/driftward/factory-reviewer/source-token`. The public
`source_token_broker.py` uses the existing `factory-hermes` App inside the native
runtime: its App/installation IDs and private key stay under
`/opt/data/github-apps/factory-hermes`. It signs in place and requests only
the broker's fixed repository allowlist with `contents:read`; GitHub's implicit
`metadata:read` is accepted, all other permissions/repos are rejected. The
broker still contains installation-specific repository and App assumptions;
review its source before provisioning another installation. It never reads the native
generator token cache or copies a private key. This is the same App identity,
with a narrower credential, not a new grant or App.

GitHub documents repository/permission downscoping and the one-hour lifetime in
[its installation-token guide](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).
The host's `renew_source_token.py` captures the token only through private
subprocess IPC, independently validates returned scope/expiry, and atomically
replaces the mode-0600 token. Its private metadata file contains only scope,
expiry and a token checksum. A local lock prevents concurrent minting; a valid
credential with over 30 minutes remaining is reused. Issuance/validation failure
leaves the previous token unchanged, with no fallback to broader auth. The
oneshot logs only status/expiry or a fixed failure message and has no internal
retry. A 20-minute timer renews before expiry; failure remains visible and cannot
keep an expired token valid. Missing existing App access to either repository is
an activation blocker, not permission to broaden its grant automatically.

The host installer packages the two public helper files and systemd templates;
it does not enable the renewal timer. After reviewing the deployment and canary,
install the rendered `driftward-factory-reviewer-source-token.service` and timer
from `independent-reviewer/systemd/` in the shared package, replacing
`__FACTORY_SHARE_DIR__` with `/usr/local/share/driftward-factory`. Ensure the
existing `/etc/driftward/factory-reviewer` directory is root-owned mode 0700,
then run the oneshot once and inspect only its status and non-secret expiry
metadata before enabling `driftward-factory-reviewer-source-token.timer`.
The service can access only the local Docker socket and reviewer credential
directory; its fixed broker executes inside `hermes-driftward-hermes-1`.

Only trusted host Git
subprocesses consume it, never workers, the reviewer profile, tests or browser.
Do not copy generator GitHub write auth into a profile. Private bare mirrors at
`/var/lib/driftward-factory-reviewer/repositories/OWNER/REPO.git` contain no shared
auth. Locked fixed-HTTPS fetches update the PR and configured base; immutable
snapshots bind inspection, and a final fetch checks the head again. New commits
require a new review ID.
Only each credential-free immutable snapshot is explicitly made readable to the
source UID (directories 0755, files 0444), along with its ephemeral MCP token
(0444). Their containing journal remains 0700. The service's restrictive 0007
umask and private source/platform credentials are not relaxed.

The host installer packages public assets under
`/usr/local/share/driftward-factory/independent-reviewer/`, preserving build-context
paths; `runner.py` and `native_agent.py` run there. The portable release does not
install/enable the host gateway. The gateway retains Docker authority; workers
retain only its private socket. Bridge/inspection credentials stay on the host
gateway, outside both reviewer/model and source execution boundaries.

Run the credential-free SDK compatibility probe and focused tests with:

```bash
python3 tools/probe_factory_native_reviewer.py hermes-driftward-hermes-1
python3 -m unittest tools.test_factory_independent_reviewer -v
python3 -m unittest tools.test_factory_reviewer_observer -v
python3 -m unittest tools.test_factory_reviewer_source_token -v
```

The probe uses only a disposable `/tmp` Hermes root, synthetic client credentials
and blocked network. It checks the installed constructor, exact tools, real
registry text/image conversion into a Responses request, and denial of model
drift/local tools. A real native conversation loop with fake provider responses
also verifies generation/tool hooks, usage counts, parent/request identities,
metadata-only fake HTTP export and flush-before-assessment ordering. Disabled
and failing exporters preserve the verdict and the number of model requests.
It does not prove real Astra access, OAuth refresh or live PostHog delivery.
It also verifies dedicated-root fallback, missing-login/wrong-root denial and
external CLI adoption refusal using only fake credentials. Build the workers
before the source-container probe; that probe uses the actual gateway snapshot
readability helper and should also be run under `umask 0007`.
Tests cover partial relay-line timeout, gateway SIGKILL with independent exact
native termination, preservation of unrelated processes, and refusal of
mismatched PID/starttime. Source inspection waits for startup checkout/browser
readiness and checks the actual checkout SHA. Sender/receiver share a 1 MiB
UTF-8 request-frame limit; full Unicode requirements and verified instruction
snapshots are measured before SDK launch, and oversized frames fail explicitly.

A real reviewed canary must independently demonstrate exact Astra access through
native auth, source refresh with the read-only identity, source/browser/tests,
absence of ambient generator context/platform auth, source egress denial,
SHA/finding binding, native timeout/cancellation cleanup, and one invocation
across response-loss retries. Retain non-secret raw evidence and its digest
before enabling `DRIFTWARD_FACTORY_REVIEWER_ENABLED=1`. Bridge review, enrollment
and automatic-repair policy gates stay off until this canary and the compatible
Factory deployment are approved.

The private journal reserves each exact review ID before launch and checks
bridge capability/policy/source/head authorization before provider use and
completion. Same-ID intent drift conflicts; ambiguous reservations never restart
a model call. Native SDK process groups are separate from the shared runtime.
A hard 540-second native watchdog survives a lost Docker client; the relay has
its own deadline and confirms termination by exact session argv, PID, process
start time and group before deleting profile state. Missing/mismatched markers
or unconfirmed termination fail closed and retain state for operator inspection.
The gateway independently invokes that exact-session cleanup if the relay is
killed or fails, outside the exhausted invocation deadline. A completed relay
already confirms native cleanup; missing markers after a lost relay response
remain inconclusive rather than authorizing another invocation.
Never stop shared Hermes to clean one reviewer; never delete reservations to
retry it. The existing host boundary cleans source containers/bundles/requests.
Normally only bounded receipts persist locally; native profile/log/prompt/session state
is removed after confirmed termination. An unavailable backend's inconclusive
isolation fields describe the configured boundary, not proof that it ran.
