# Native Hermes independent reviewer

This dormant path uses a native Driftward Hermes execution profile behind the
existing host gateway. It starts no additional chat gateway, bot, daemon or
Codex CLI runtime. `ReadOnlyReview` still accepts only a reserved bridge job,
fixes `gpt-6-astra`, and returns the bounded `factory-independent-review-v1`
receipt with `backend: isolated_hermes`. The model supplies verdict/findings only.
Historical `isolated_codex` receipts remain readable.

Each invocation creates a fresh `factory-reviewer-<UUID>` profile under
Driftward Hermes' `/opt/data/profiles/`. Its generated `config.yaml` selects
Astra and disables fallback, plugins, MCP, Tool Search, connectors, memory and
compression. It copies no generator config, SOUL, memory, sessions, skills,
browser state, credentials or tools. Each fresh profile has one writer.
The SDK skips repository context, identity and background review. A profile
separates state; it is not a filesystem sandbox. The trusted SDK process runs
inside the existing Driftward Hermes container, with model authority enforced
by exactly seven prefixed source tools and native pre-request/tool-dispatch
guards. It has no local shell, filesystem, delegate, platform or tool-discovery
tools. Personal Hermes is never selected.

Hermes' supported routed-profile home and empty secret scope prevent default and
project dotenv loading. Native provider resolution reuses Driftward Hermes'
existing OpenAI Codex OAuth login through its root-auth fallback, including native
shared-store refresh locking. The adapter does not read, copy, mount, export or
serialize OAuth tokens. It preserves the runtime's HOME and root HERMES_HOME;
only the routed profile home changes. No reviewer `auth.json` is provisioned.
Default-profile config/model settings are not changed.

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
derived image. Missing required browser/dependencies/checks must yield
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
  "repositories": {"Driftward-LLC/factory": {"base_ref": "main"}}
}
```

Managed instruction snapshots are root-owned mode-0600
`/etc/driftward/factory-reviewer/review-code.md` and `verify-app.md`. Their verified
contents enter the prompt explicitly; installed plugin presence is insufficient.
Source fetch separately requires a source-only GitHub read credential at
`/etc/driftward/factory-reviewer/source-token`. This remains an activation gate;
native provider-login reuse does not provision it. Only trusted host Git
subprocesses consume it, never workers, the reviewer profile, tests or browser.
Do not copy generator GitHub write auth into a profile. Private bare mirrors at
`/var/lib/driftward-factory-reviewer/repositories/OWNER/REPO.git` contain no shared
auth. Locked fixed-HTTPS fetches update the PR and configured base; immutable
snapshots bind inspection, and a final fetch checks the head again. New commits
require a new review ID.

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
```

The probe uses only a disposable `/tmp` Hermes root, synthetic client credentials
and blocked network. It checks the installed constructor, exact tools, real
registry text/image conversion into a Responses request, and denial of model
drift/local tools. A real native conversation loop with fake provider responses
also verifies generation/tool hooks, usage counts, parent/request identities,
metadata-only fake HTTP export and flush-before-assessment ordering. Disabled
and failing exporters preserve the verdict and the number of model requests.
It does not prove real Astra access, OAuth refresh or live PostHog delivery.
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
