# Hermes execution contract: evidence and limits

Owner: Factory maintainers. Last inspected: **2026-10-25 UTC**.
Status: evidence-backed description, **not a containment attestation or recovery approval**.

This is the maintained reference for how Factory uses Driftward Hermes. Update it
through ordinary repository pull requests, with the evidence described below.
The [dated evidence bundle](evidence/hermes-runtime/2026-10-25.json) is a historical
snapshot; it is not a live health dashboard. The existing
[interrupted-execution runbook](interrupted-execution.md) still governs recovery.

## Findings in plain language

1. Factory's executor is a **named Hermes profile**, not a different agent engine.
   Its command is launched inside the Driftward Hermes runtime. A profile selects
   a home/configuration/state boundary; this launch path does not create a new
   container or OS identity for each profile.
2. Hermes does have scoped process cleanup: foreground process groups, tracked
   background processes, identity checks and termination/survivor handling.
   Saying “Hermes cannot stop individual work” would be incorrect.
3. Factory's current `cancelInvocation` handler **records a cancellation marker**.
   It does not call Hermes's process registry or signal a native process. Factory
   cancellation, Hermes cleanup and external-effect settlement are separate facts.
4. The executor's missing profile-local `auth.json` is **not proof of missing
   authentication**. Inspected Hermes code supports root-store fallback, and the
   live CLI reported executor authentication ready. That check does not identify
   every credential used by a particular historical invocation.
5. Nothing inspected proves that ENG-162 left a surviving process, or that all its
   effects settled. Neither a shared-runtime shutdown nor a safe retry follows
   automatically from these findings.

## Evidence levels and revision boundary

Use these labels when updating a claim:

- **Observed:** bounded output from the installed `factory` CLI at a recorded time.
- **Source verified:** behavior traced in a pinned source file. For Hermes files
  below, its SHA-256 also matched the file on disk inside the runtime.
- **Inference:** conclusion combining those observations; state its assumptions.
- **Unknown:** not measured or not available. Do not turn this into “absent.”

The evidence collector was `1f203ca5272ae196ee24a94fe26fb0d901b1fdd4`.
All **17/17** allowlisted Hermes files matched upstream tag `v0.21.6`, commit
[`818c13be1dc4fd28987e1e881a9408224afd4535`](https://github.com/NousResearch/hermes-agent/tree/818c13be1dc4fd28987e1e881a9408224afd4535).
The manifest records each filename, byte count and SHA-256. This establishes the
selected **installed files**, not the entire image, every plugin, the revision
already loaded by a long-running process, or what was installed when an earlier
incident happened. The collector therefore deliberately returns revision
`unknown` and `running_process_version: not_established`.

`factory version` observed application revision
`675682deb51b9e5ab599ecd79315215d741497ba`. The four Factory source paths cited for
launch, gateway cancellation, journal semantics and interrupted resolution were
unchanged between that revision and main `41f2d0a2ab72495ca0682c118acea8f5d8a6c20f`
(`git diff` over those paths was empty). The separately installed compiled gateway
was not attested to a source revision by this audit; its behavior is described as
a **source contract**, not proof of the loaded gateway binary. Host CLI/helper
updates and application image deployments must not be conflated.

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
The inspected host wrapper `/usr/local/bin/driftward-hermes` had SHA-256
`2cc113d6bd4176d2db42d04575f1dacccef63a7fe29bca14937326093fc5aa71`:
its normal CLI branch invokes Compose `exec`, not a new per-profile container;
it forwards only four validated Factory observability fields from host context.
The wrapper's source was read, not invoked directly during this audit.

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
  provider-level fallback. This audit did not open any auth store.
- The live `factory profiles status` observation reported executor
  `auth_present: false` but `executor_auth_ready: true`, executor profile selected,
  gateway active/listening, and reviewer `auth_present: true`/routed.
  [`inventory()`](../deploy/factory-profile-runtime.py) makes the distinction
  explicit: the first field checks a named-profile file, while readiness uses
  the native authentication-status API. It is not an exact failed-run credential
  provenance check.
- [`native_agent.py::dedicated_auth`, `review`, `stop`](../deploy/independent-reviewer/native_agent.py)
  implement Factory's distinct reviewer path, including per-review session
  markers and a process-group stop check. Do not generalize these reviewer
  controls to the executor: they are different adapters.

The [upstream profiles guide](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
is useful background for separate homes, gateways and shared OAuth fallback,
but changes independently of this deployment. Pinned, hash-matched source is
the authority for this audit's implementation claims.

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

## ENG-162: what is proved, and what remains unknown

The dated bundle binds attempt `b0ac9017-1f27-49f9-9250-274d6b8b8b23` to job
`a007ff3e-8062-4b8b-a8eb-ced9511c25a6` and invocation
`a007ff3e-8062-4b8b-a8eb-ced9511c25a6:34684470-7c66-4043-8987-011866e6f27d:1`.
The live diagnostic verified the private intent's exact binding, exit code 1,
timeout/output-limit flags false, and no result receipt. It explicitly reported
underlying output unavailable and containment/authentication cause not established.

The previous turn recorded cancellation at `2026-10-25T06:16:11.724675Z`; this is
context, not an attested process-stop result. No recovery decision was made here.

Still unknown:

- The original exit error and whether native cleanup completed in that run.
- The exact Hermes session/task/process IDs mapped to this Factory invocation.
- Any surviving local/detached/remote work or the terminal backend actually used.
- The scope and outstanding use of external write credentials and accepted requests.
- A preserved workspace snapshot and settlement evidence sufficient for recovery.

Do not assert that any survivor exists. Do not assert that none exists. PostHog
correlation is useful diagnostic evidence, but missing events are not proof of
non-execution, and telemetry is not the lifecycle or recovery source of truth.

## Recovery implications, not authorization

The next engineering step is to assess a scoped adapter from Factory invocation
identity to Hermes's native session/process ownership and termination evidence.
Prefer reusing verified native mechanisms over inventing another process killer.
For this historical attempt, first determine whether a trustworthy ownership
mapping remains available; adding better markers now cannot recreate lost proof.

Separate process stop, replay prevention, external-write settlement and workspace
preservation. The existing recovery API accepts a root attestation only after
those facts are established; it does not establish them itself. Read
[`interrupted_resolution.py`](https://github.com/Driftward-LLC/factory/blob/675682deb51b9e5ab599ecd79315215d741497ba/hatchet/bridge/hatchet_hermes_bridge/interrupted_resolution.py)
and the [runbook](interrupted-execution.md) before using it.

Neither shutting down all Driftward Hermes work nor revoking shared credentials
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
2. Record UTC collection time and collector revision in a new dated JSON file
   under `docs/evidence/hermes-runtime/`. Retain only bounded status, identifiers,
   source hashes and sizes. No prompts, outputs, tokens, auth/config files,
   session rows or full process command lines. Review the file before committing.
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
  cancellation integration. Historical ENG-162 containment remains unproven.
