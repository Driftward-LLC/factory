# Hermes execution contract: evidence and limits

Owner: Factory maintainers. Last inspected: **2026-10-25 UTC**.
Status: evidence-backed description, **not a containment attestation or recovery approval**.

This is the maintained reference for how Factory uses Hermes. Update it
through ordinary repository pull requests, with the evidence described below.
Source references describe the reviewed behavior, not the health or configuration
of every installation. The [interrupted-execution runbook](interrupted-execution.md)
governs recovery; operators retain installation-specific evidence privately.

## Invocation binding (protocol v2)

The current source adds a durable link from each new execution lease to its exact
runtime. The older source audit below remains pinned to its stated revision;
this addition does not establish that an installation has been upgraded.

```text
attempt -> job -> lease ID + epoch
                 -> execution_runtime_bound event
                    -> backend + full container ID + image digest + incarnation
```

The receipt runner asks the gateway to prepare an invocation. The gateway
reserves its private journal and identifies the Docker container, then pauses
before agent execution. The runner saves the same bounded identity in Factory's
existing attempt journal under the authorized lease. Only then does it register
the invocation and send the start request with that binding. Workers receive
metadata only; Docker access and execution credentials stay with the runtime.

The profile adapter resolves its Compose service once, then uses the full
container ID for commands. A guard inside each command checks an incarnation
digest derived from the host boot ID and container PID 1 start time before
executing it. A container replacement or restart therefore cannot inherit the
invocation. The experimental native adapter records the same binding for its
reserved container before releasing its controlled agent start.

Status and cancellation compare the bound runtime and lease. Receipt commits,
including commit-failure recovery, must match the original runtime. The binding
is visible as `execution_runtime` on the bridge job and active/recent attempt
summary, and as an `execution_runtime_bound` attempt event. It contains no
credentials, session data, prompts, or host paths. No runtime registry or database
migration is introduced.

Preparation waits at most 60 seconds for the start acknowledgement. A lost
acknowledgement, gateway restart, or incomplete reservation remains fenced for
recovery; it never causes an automatic second execution. Completed receipts can
still be recovered from the original journal after a gateway restart.

Rollout requires matching protocol-v2 gateway and worker builds. Older gateways
and the unfenced direct-command execution route fail preflight. Upgrade the
separately installed gateway together with the application while execution is
paused; changing this source does not enable execution. Historical invocations
remain unbound and cannot acquire a guessed runtime during recovery. This link
does not prove descendant cleanup or external-effect settlement, and does not
add process termination to the profile adapter's cancellation marker.

## Findings in plain language

1. Factory's executor is a **named Hermes profile**, not a different agent engine.
   Its command is launched inside the Hermes runtime. A profile selects
   a home/configuration/state boundary; this launch path does not create a new
   container or OS identity for each profile.
2. Hermes does have scoped process cleanup: foreground process groups, tracked
   background processes, identity checks and termination/survivor handling.
   Saying “Hermes cannot stop individual work” would be incorrect.
3. Factory's current `cancelInvocation` handler **records a cancellation marker**.
   It does not call Hermes's process registry or signal a native process. Factory
   cancellation, Hermes cleanup and external-effect settlement are separate facts.
4. A missing profile-local `auth.json` is **not proof of missing authentication**.
   The reviewed Hermes code supports root-store fallback. A readiness check does
   not identify every credential used by a historical invocation.
5. These source contracts do not prove whether a failed invocation left a
   surviving process or settled all its effects. Neither a shared-runtime shutdown
   nor a safe retry follows automatically.

## Evidence levels and revision boundary

Use these labels when updating a claim:

- **Observed:** bounded output from the installed `factory` CLI at a recorded time.
- **Source verified:** behavior traced in a pinned source file. Establish any
  installation match separately by comparing the file's SHA-256.
- **Inference:** conclusion combining those observations; state its assumptions.
- **Unknown:** not measured or not available. Do not turn this into “absent.”

Hermes references below are pinned to `v0.21.6`, commit
[`818c13be1dc4fd28987e1e881a9408224afd4535`](https://github.com/NousResearch/hermes-agent/tree/818c13be1dc4fd28987e1e881a9408224afd4535).
Run the source audit against the actual installation before relying on a
deployment match. Matching allowlisted files does not attest the entire image,
every plugin, or the source already loaded by a long-running process.

Factory source references are pinned to the reviewed revision
`675682deb51b9e5ab599ecd79315215d741497ba`. The separately installed compiled
gateway must be verified independently of the application image. Host helper
updates and application deployments are separate operations.

## Actual control paths

```text
Factory attempt / execution lease
  -> Factory worker -> private gRPC socket
  -> Factory's custom Hermes gateway -> invocation journal
  -> factory-hermes wrapper -> docker compose exec ... hermes
  -> Hermes one-shot CLI --profile factory-executor ... -z
  -> selected terminal / browser / MCP tools
```

This gRPC server is **Factory code**, not the native Hermes messaging gateway.
Hermes's native `hermes -p NAME gateway ...` facility is a different control
surface. A profile can have its own gateway state without Factory's one-shot
invocations being launched or controlled through that native gateway.

Factory source: [`runHermesCreateDraftPrViaCommand`, `hermesProfileArguments`,
`hermesRuntimeDockerArgs`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/workers/bridge-executor-worker/src/hermes/create-draft-pr.ts)
and [`createServer`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/workers/bridge-executor-worker/src/hermes/hermes-gateway-server.ts).
The host wrapper is a separately provisioned integration. Verify its invocation
and metadata forwarding behavior when configuring a real execution installation;
the portable release does not supply that execution runtime.

## Profiles, authentication and isolation

| Boundary | Verified behavior | What it does not establish |
| --- | --- | --- |
| Profile home | `--profile` is resolved before CLI imports and sets `HERMES_HOME`. Factory selects `factory-executor`. | A separate container, process namespace or per-invocation OS identity. |
| Configuration/state | Hermes profile homes hold separate configuration and state; root/profile lookup is explicit. | That every external tool, filesystem path or credential is private to that profile. |
| Provider authentication | Provider state prefers the profile store and can fall back to the root store. | GitHub/MCP write-credential origin, validity or revocation. LLM OAuth and external write authority are not interchangeable. |
| Reviewer | Factory uses the named reviewer profile and a separate configured auth root with restricted source-review tools. | The executor's isolation properties, or an independent physical runtime. |
| Terminal backend | Hermes supports local and multiple sandbox/remote backends. | Which backend or detached processes a particular failed invocation actually used. |

Proof:

- [`hermes_cli/main.py::_apply_profile_override`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/hermes_cli/main.py#L593)
  resolves the profile and sets the home before normal imports.
- [`hermes_constants.py::get_hermes_home` and `get_default_hermes_root`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/hermes_constants.py#L111)
  distinguish the active profile home from the default/root home. For a custom
  root, `<root>/profiles/<name>` resolves its default root to `<root>`.
- [`auth.py::_load_provider_state_with_source`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/hermes_cli/auth.py#L830)
  implements profile-first/root-fallback lookup; `read_credential_pool` also has
  provider-level fallback. Source inspection does not require opening an auth store.
- [`inventory()`](../deploy/factory-profile-runtime.py) distinguishes
  `auth_present` from `executor_auth_ready`:
  the first field checks a named-profile file, while readiness uses
  the native authentication-status API. It is not an exact failed-run credential
  provenance check.
- [`native_agent.py::dedicated_auth`, `review`, `stop`](../deploy/independent-reviewer/native_agent.py)
  implement Factory's distinct reviewer path, including per-review session
  markers and a process-group stop check. Do not generalize these reviewer
  controls to the executor: they are different adapters.

The [upstream profiles guide](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
is useful background for separate homes, gateways and shared OAuth fallback,
but changes independently of the reviewed implementation. Pinned source supports
implementation claims; a matching installation audit establishes deployment scope.

## Installation role-profile bootstrap

Factory startup ensures native `factory-executor` and
`factory-independent-reviewer` profiles exist inside the installation Hermes
root. It leaves the active profile and existing profile configuration unchanged,
keeps native bundled skills, and relies on native installation-root authentication
fallback without copying auth stores. Native profile creation can inherit the
installation's model settings; Factory assigns no new model or authentication.
The bootstrap rejects inline credentials in that model seed, as the existing
Factory profile-creation path does. This creates role profiles only: it does not
enable execution or satisfy the independent-review adapter's separate dedicated
authentication and tool restrictions.

## What Hermes actually does when work stops

### Foreground terminal work

[`LocalEnvironment._run_bash`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/environments/local.py#L1060)
starts a new session/process group and records a group/start-time identity on
POSIX. Its termination helper uses TERM/KILL escalation and checks identity
before signaling; it also attempts to account for snapshotted escaped descendants.
Ambiguous or recycled process identity is intentionally not permission to kill
an unrelated process.

[`base.py::kill_live_foreground_processes`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/environments/base.py#L56)
tracks foreground work **inside the current Python process**. This is not an
external inventory of every process that ever belonged to an invocation.
[`terminal_tool_lifecycle.py::cleanup_all_environments`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/terminal_tool_lifecycle.py#L265)
calls that termination path before cleaning environments.

### Background work

[`process_registry.py`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry.py)
tracks background process sessions, persists a profile-relative `processes.json`
checkpoint, and provides `kill_process`/`kill_all` operations with ownership
filtering. This file was **not read from a live profile** during the audit.
[`kill_all`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry.py#L2532)
distinguishes lifecycle cleanup from an explicit stop: jobs marked
`persist_on_release` can survive lifecycle sweeps. A successful process-stop
result is also conditional on scope/survivor checks; see
[`process_registry_termination.py`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry_termination.py).
Non-local sandbox sessions do not have the same host-visible survivor proof.

### CLI shutdown

[`cli.py::_run_cleanup`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/cli.py#L502)
attempts terminal, delegation, browser and MCP cleanup. Its ordered steps suppress
specified exceptions. [`cli_shutdown.py`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/hermes_cli/cli_shutdown.py#L57)
also provides a bounded exit watchdog that tries foreground termination before
hard exit. These mechanisms refute “Hermes has no cleanup,” but they are **not
an incident-specific receipt proving cleanup completed**. SIGKILL, inaccessible
processes, backend failures, persistent background work and already accepted
remote requests have to be evaluated separately.

No destructive cleanup probe or signal was sent to production in this audit.

## Factory cancellation does not currently invoke that cleanup

In the inspected Factory [`createServer`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/workers/bridge-executor-worker/src/hermes/hermes-gateway-server.ts#L109),
`cancelInvocation` calls `inspectInvocation(request, true)`, which calls
[`InvocationJournal.status(job, true)`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/workers/bridge-executor-worker/src/hermes/invocation-journal.ts#L63).
That method writes a `cancel` marker. It neither sends a signal nor invokes a
Hermes process-registry operation. In this protocol `cancellationAcknowledged`
becomes true only with a matching completed result and cancellation marker;
without a result it remains false. It is not a generic “all children stopped”
receipt.

The journal does prevent executing the same reserved job again without a
completion result. Its in-memory active map is removed on success or failure;
an on-disk intent without a result is therefore `outcome_unknown` after failure
or restart. This is replay protection, not provider-side fencing.

Factory's command adapter launches the wrapper with Node `execFile`, timeout and
output bounds. The wrapper invokes Docker `exec`; no reviewed per-invocation
container-retirement or native task-stop handshake appears in this launch path.
The adapter classifies a nonzero process exit but does not retain the underlying
failed stdout/stderr in the invocation journal. A native cleanup capability
cannot be treated as wired into Factory merely because Hermes implements it.

## Unknown outcomes

A failed command without a retained result receipt does not establish the
original exit error, cleanup completion, native ownership, surviving work, or
external-write settlement. Preserve available workspace and invocation evidence.

PostHog correlation is useful diagnostic evidence, but missing events are not
proof of non-execution. Telemetry is not the lifecycle or recovery source of truth.

## Recovery implications, not authorization

See the [scoped cancellation proposal](hermes-scoped-cancellation-plan.md) for the
follow-up ownership investigation, proposed adapter and activation gates.

The next engineering step is to assess a scoped adapter from Factory invocation
identity to Hermes's native session/process ownership and termination evidence.
Prefer reusing verified native mechanisms over inventing another process killer.
For historical attempts, first determine whether a trustworthy ownership mapping
remains available; adding better markers now cannot recreate lost proof.

Separate process stop, replay prevention, external-write settlement and workspace
preservation. The existing recovery API accepts a root attestation only after
those facts are established; it does not establish them itself. Read
[`interrupted_resolution.py`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/bridge/hatchet_hermes_bridge/interrupted_resolution.py)
and the [runbook](interrupted-execution.md) before using it.

Neither shutting down all Hermes work nor revoking shared credentials
is justified solely by profile co-location. Conversely, stopping a wrapper or
gateway, observing an expired lease, or seeing an unchanged PR is insufficient
to manufacture a containment attestation. Any broader action needs a measured
scope and explicit operational authority.

## How to maintain this document

1. Run the installed canonical CLI **sequentially** (commands use the installation
   lock): `factory version`, `factory profiles status`, and
   `factory profiles source-audit`. The last command reads only a fixed allowlist
   of public source files; it does not dump source text or private profile data.
   See [CLI documentation](../deploy/CLI.md). Never use the native wrapper to
   bypass a missing Factory operator interface.
2. Retain UTC collection time, collector revision, bounded status, identifiers,
   source hashes, and sizes in the installation's private audit records. Do not
   publish live task/session identifiers, prompts, outputs, credentials,
   auth/config files, session rows, or full process command lines. Public source
   claims should cite upstream code or sanitized synthetic evidence.
3. Independently compare each available hash with the corresponding file in a
   pinned upstream checkout (for example `sha256sum hermes_constants.py`). If a
   hash differs, mark the claim unverified for that deployment until the changed
   source is inspected. A tag label alone is insufficient. Do not silently fall
   back to current upstream docs or erase the prior dated evidence.
4. Re-read affected functions and their callers. Update claim, proof link, evidence
   level, limitations and the last-inspected date together. Use full commit URLs,
   not moving `main` links. Add a short change record below.
5. Revisit after Hermes upgrades, profile/auth changes, wrapper/gateway changes,
   terminal backend changes, or cancellation/recovery changes. A changed relevant
   hash invalidates that claim's deployment match; elapsed time alone does not
   convert a snapshot into current proof.
6. Have an independent reviewer check consequential claims against retained
   evidence. Documentation review does not authorize a restart, credential
   change, lifecycle override or replay. Use bounded disposable fixtures for
   behavior tests, never a production kill test just to complete this document.

### Change record

- 2026-10-25: initial audit; 17 Hermes files matched pinned v0.21.6 source.
  Corrected the assumption that profile co-location implies a required shared
  shutdown, and distinguished Hermes's native cleanup from Factory's marker-only
  cancellation integration. Historical containment requires separate evidence.

## Opt-in installation-local gateway

`deploy/compose.local-executor.yml` wires the existing Factory gateway inside
its installation's Hermes container. It launches native `hermes --profile
factory-executor` through the same protocol-v2 prepare/bind/start journal;
provider credentials remain in Hermes. Only the private gateway socket directory
is shared with the executor worker. This overlay does not enable execution,
promote a portable installation, or change cancellation semantics.

The operator prepares `gateway/` owned by uid 10000 with mode 0700 and a root-owned
`runtime/` directory under the installation directory, plus a private
`factory-hermes-execution.env`. After the gateway container starts, the
read-only observation helper generates its binding:

```bash
python3 deploy/local-runtime-binding.py --container <running-hermes-container> \
  --installation-id <installation-uuid> \
  --output <installation-directory>/runtime/runtime-binding.json
```

The helper checks Docker installation/service labels and observes the full
container ID, image digest and PID1 incarnation using that exact ID. Hermes
checks the installation UUID, its default Docker hostname and incarnation before
binding and every local command; each command repeats the incarnation guard
inside the process that executes it. Replacements and restarts fail closed until
the operator observes them again. The entire metadata directory is mounted
read-only so atomic replacement remains visible without granting agent writes.
The Docker operator and metadata directory owner are trusted to supply the image
digest; the container cannot independently inspect its Docker image. Custom
container hostnames are unsupported.

Gateway wiring alone is insufficient for a real canary: deterministic GitHub
receipt/source access, explicit execution configuration, independent review and
scoped native cancellation still require separate validation. The existing
portable wrapper remains dry-run only and production cutover checks remain
unchanged. No live execution was used to validate this source addition.

### Native source-only independent review

For the first native PostHog observer setup on already authenticated role
profiles, stop the installation's Hermes container and retain a private backup
of its data. Run `deploy/provision-native-observer.py --runtime-dir
<hermes-data-directory> --credentials <private-collector-env>` as the operator.
It previews hashes only, derives the native ingestion host from the approved US
or EU PostHog traces endpoint, and does not create a Collector. Review that
preview, then repeat with `--apply-preview <snapshot_digest>`. A changed profile,
auth file, plugin or credential produces a different digest and refuses apply.
The operation adds the executor observer and two private profile dotenv entries;
models, MCP, unrelated settings and credentials are retained. The independent
reviewer reads only those two entries and keeps generic plugins disabled. Existing
telemetry entries or conflicting observer code require a separate rotation or
upgrade operation. Start Hermes only after reviewing the resulting preserved
hashes; ingestion still requires exact session/trace readback.

The opt-in local gateway can use the existing native reviewer SDK adapter with
its dedicated `factory-independent-reviewer` authentication domain. This mode
narrows its tools to immutable Git source inspection, file reads, diffs and
finding-location validation. It launches no source container, executable
checkout, shell, browser, test command or source preview. The reviewer model
cannot access general filesystem, terminal, network or executor MCP tools.
Existing native capability guards, SDK fingerprint, fresh session identity,
watchdog, shutdown and exact-session stop remain required.

A local provisioning file uses schema `factory-hermes-source-reviewer-v1`, the
named `auth_root` `/opt/data/profiles/factory-independent-reviewer`, a canonical
`policy_digest`, `review_skill_digest`, `verify_skill_digest`, and a
`repositories` map with each enrolled repository's `base_ref`. Provisioning and
skill files must pass the existing private-path ownership checks. The Compose
overlay points to `config/reviewer-provisioning.json` and `config/reviewer/`.
This configuration alone does not enable the reviewer or execution policy.

Source-only receipts carry the exact scope:
`{"source_only":true,"checks_executed":false,"browser_exercised":false}`.
This is independent source review, without independent execution of validation
or browser checks. Existing review intents provide acceptance criteria and the
exact head, not generator runtime validation evidence. The reviewer must return
inconclusive when absent evidence is material; enabling this adapter does not
relax repository validation or mandatory-review policy. Public repository source
is fetched without GitHub credentials, bound to the requested PR head before and
after review. Private repositories remain unsupported in this local mode.

### Deterministic Composio GitHub reads

The local gateway's `FACTORY_HERMES_GITHUB_TRANSPORT=composio` route reads GitHub
identity, PR metadata, complete file lists, check runs, status contexts and retry
branch/PR fences through fixed Composio tool calls. The reader resolves the
existing executor profile's `factory-composio` native MCP session URL and the
referenced profile-local API key. It follows no redirects and emits only bounded
GitHub evidence. Workers receive that evidence through the private gateway; they
receive no Composio credentials. No `gh` executable, GitHub App credential or
agent-authored tool result is used for these deterministic reads.

This route requires an explicit configured GitHub principal and Git author name
and email. It checks the authenticated Composio principal and exact accessible
repository, retains the existing prohibited-identity checks, and compares PR
authorship to that principal. Legacy execution routes retain their GitHub App
bot requirements. A dedicated Composio connection may still authenticate a human
GitHub principal; its organizational location does not change that identity.

PR checks/statuses must match the exact observed head and pagination totals.
Diffs use the existing bounded immutable Git reader, with credential helpers
disabled, against public source; file paths must agree with GitHub and revisions
are reread afterward. Private source remains unsupported. A missing branch is
accepted only from the reference tool's machine-readable HTTP 404 after the same
session has proved repository access; other failures remain unknown. The route
is opt-in and does not enable execution, configure intake or change cancellation.

### Scoped local CLI interruption

The local adapter retains the exact child it launched through the incarnation
guard. It executes `/opt/hermes/.venv/bin/python -m hermes_cli.main` directly,
with the native named profile; no shell or Docker client stands between the
held child and Hermes. A matching journal cancellation sends this current child
SIGTERM once. A cancellation already recorded before launch prevents spawning.
Lost handles after gateway restart are never recovered from a PID or process
name. Legacy profile cancellation retains its existing marker-only behavior.

The bounded `nativeStop.interruption` observation identifies the
`gateway_child` scope, SIGTERM dispatch result and whether that child exited.
It retains `state: unknown`, null dispatch-quiescence and false native-owner
proof fields. It does not establish descendant cleanup, external-effect
settlement or completion. Cancellation acknowledgement still requires the
existing completed receipt; this addition does not weaken that protocol.

Only one local invocation may hold admission for a runtime incarnation. Its
private directory is released only after the journal has validated and durably
stored a normal receipt. Cancellation, command/parse/receipt-write failures and
gateway crashes retain the hold, blocking other jobs in the same incarnation.
A real container restart changes the verified incarnation and permits a fresh
admission only after the operator observes its new binding. That restart does
not resolve the original job's uncertain external effects or authorize replay.
No automatic restart or recovery override is introduced.

Source verified against Hermes `818c13be`: one-shot CLI signal handlers request
native hard interruption and graceful cleanup; cleanup suppresses individual
errors, the exit watchdog can force exit, and lifecycle sweeps may retain
explicitly persistent background work. Consequently, a SIGTERM or child exit
is useful scoped evidence and cannot become a blanket cleanup attestation.
Disposable process tests exercise signal delivery, foreground termination and
an unrelated live control. They do not establish live provider/MCP settlement
or a real installation restart/cancellation canary.
