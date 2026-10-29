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
  -> driftward-hermes wrapper -> docker compose exec ... hermes
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
