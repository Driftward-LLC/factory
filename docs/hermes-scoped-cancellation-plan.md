# Scoped Hermes cancellation: proposed adapter

Status: **bounded read-only lookup implemented; experimental native owner
prototype tested on disposable resources, not wired or activated**. Inspected 2026-10-26 UTC.
Companion to the [verified runtime contract](hermes-runtime-contract.md).

## Problem and result of the investigation

Factory knows its invocation identity, but the inspected gateway journal does
not retain the owning native Hermes session/process identity. Its cancellation
marker does not ask that native owner to stop. We need an observable stop request
for exactly one invocation, without stopping other Hermes profiles or treating
process termination as proof that external writes settled.

The [dated investigation evidence](evidence/hermes-runtime/2026-10-26-ownership.json)
rechecked ENG-162 attempt `b0ac9017-1f27-49f9-9250-274d6b8b8b23` through the canonical
Factory CLI. Its invocation is
`a007ff3e-8062-4b8b-a8eb-ced9511c25a6:34684470-7c66-4043-8987-011866e6f27d:1`.

- Exact journal binding remains verified; intent exists, result does not.
- The admission/execution manifests contain Factory identifiers, not a native
  session or process identity. Their backend-version field is route metadata,
  not a deployed-source fingerprint.
- None of the 250 returned events contained a session/process/task/trace identity
  key. This bounded response is not guaranteed to cover the complete history.
- The attempt remains cancellation-requested; the profile status check reported
  the executor gateway active/listening and authentication ready. Availability
  does not establish this invocation's ownership or termination.

**Conclusion:** ownership is not established through the inspected Factory
interfaces. This is not proof that no native record or telemetry survives.
No private session database/checkpoint scan, PostHog query, signal, replay,
credential change or recovery confirmation was performed.

## Native constraints that shape the adapter

The pinned Hermes source is commit `818c13be1dc4fd28987e1e881a9408224afd4535`.
The runtime contract records which files were matched to installed source.

1. Foreground work is held in the owning Python process's
   [`_live_foreground`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/environments/base.py#L56)
   registry. Importing Hermes in a fresh process cannot enumerate that old
   in-memory registry. Its foreground shutdown helper is process-wide, so it
   must not run in a process shared by unrelated invocations.
2. Background ownership is the raw `owner_task_id`, not necessarily the
   environment/container's `task_id` key. See
   [`snapshot_running_ids` and `kill_all`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry.py#L2523).
   Never substitute a shared environment key, timestamp, issue label or profile
   name for an exact invocation-to-owner binding.
3. Native `close()` is best-effort, and background lifecycle cleanup skips
   `persist_on_release`. Explicit stop and ordinary session release are different
   policies. A returned count is not a complete termination receipt.
4. Upstream
   [`recover_from_checkpoint`](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry_checkpoint.py)
   can stop systemd scopes, re-enqueue watchers and rewrite checkpoints. It must
   **not** be called by a read-only inspector. This additional helper was read at
   the pinned upstream commit but was not in the 17-file deployed hash allowlist;
   its installed match remains unverified.

## Smallest proposed implementation

### Delivered diagnostic and disposable selection evidence

`factory profiles diagnose-execution ATTEMPT-ID --invocation-id JOB-ID:LEASE-ID:EPOCH`
now follows exact journal verification with a fixed-runtime process lookup. It
matches both Factory environment IDs and returns bounded PID/start-time/namespace
metadata with initial profile-home classification. It checks identity and
environment twice and reports unreadable/invalid metadata and limits. See the
[CLI limits](../deploy/CLI.md#read-only-executor-failure-diagnostics).
Initial environment can be copied or inherited and may predate Hermes profile
selection. Candidates are diagnostic correlation only, never verified native
session/raw owner identity, complete descendant coverage or stop authority.
This implementation does not create a new stop endpoint or ownership journal.

The [installed lookup evidence](evidence/hermes-runtime/2026-10-26-process-lookup.json)
records the ENG-162 check: 23 process entries scanned, no correlated candidates,
6 unresolved entries, no scan limit reached. This does not prove absence of live
work. Only the read-only helper was installed; gateway health and the Factory
application revision remained unchanged. No production stop or retry occurred.

The disposable test `tools/test_hermes_scoped_selection.py` executes a fingerprinted
copy of upstream `ProcessRegistry.kill_all` from pinned commit `818c13be` against
four real, isolated child processes using a synthetic termination callback limited
to those children's `Popen` handles. It proves raw `owner_task_id` selection,
rejection of the shared environment key, persistent-work handling for lifecycle
versus explicit stop, repeated-stop behavior, and survival of another invocation
and reviewer. It does **not** prove Hermes's real `kill_process`, process-tree
cleanup, runtime registration, dispatch fencing, remote backends, production
wiring or external-write settlement. No production process is touched by the test.

### Remaining adapter work

The disabled [experimental native owner](../deploy/experimental-native-executor/README.md)
now has a disabled gateway integration using its authenticated/private-journal
context, exact stored-lease claims and a registration/start barrier. Actual pinned
AIAgent/terminal execution is tested over recorded SDK transport, with filesystem
effect, successful tool result and native loading of all three fixed engineering
skills. Eighteen owner checks cover cleanup, persistent work, escaped descendants
and admission. A same-profile legacy fixture proves native checkpoint replacement
can erase another live interpreter's metadata. Experimental owners are serialized;
legacy coexistence remains an activation blocker. Gateway/controller tests use
fixture Docker transport, not a real container. Existing transport/journal trust
is preserved, not independent bridge attestation. Full capabilities, installed
provider/configuration parity and recovery remain unverified. Every aggregate
stop stays `unknown`; activation and historical recovery are not authorized.

Keep the existing Factory gRPC gateway and one-shot Hermes execution model.
Do not add another service or switch to the Hermes chat gateway. Add a thin
executor-side adapter inside each invocation's owning process, using a supported
Hermes lifecycle extension if it exposes the required hooks. Hook suitability
must be demonstrated before choosing an implementation; do not assume a native
out-of-process stop API already exists.

### 1. Persist ownership before permitting tool execution

Bind the existing invocation ID and immutable job fingerprint to the executor
profile, runtime instance identity, native session ID and raw task-owner IDs.
Record the owning process identity with PID/start-time and namespace or runtime
incarnation information; a PID alone is unsafe. Capture session rotations and
delegated child owners before they can execute tools.

Write this bounded record atomically in the executor-private journal. No prompts,
command lines, credentials, transcripts or lease secrets belong in operator
output or telemetry. If registration fails, do not start effects. A crash between
spawn and registration stays unresolved; do not classify it as never executed.

### 2. Deliver cancellation to that owner

Keep the durable cancellation request, but let the owning adapter consume it
through an executor-private authenticated channel. Require the same invocation,
lease epoch and fingerprint as the ownership record. Fence additional dispatch
before attempting cleanup; cancel-before-start and cancel-during-spawn must both
be handled. A best-effort observer callback alone is not a dispatch fence.

Reuse native interruption and cleanup mechanisms inside the owning process.
Stop only the recorded raw task owners and their registered descendants. Include
explicitly persisted background work in the explicit cancellation policy; do not
silently use a lifecycle sweep that skips it. Unsupported remote backends,
unresolved ownership, stale identity or unreachable owners return **unknown**,
not permission to signal a guessed process or shut down the shared runtime.

### 3. Persist bounded stop evidence, separate from completion

Record request identity, ownership-record digest, timestamps, runtime incarnation,
native cleanup results, survivor-check coverage, unresolved components and the
owning process's exit observation. Distinguish `requested`, `stopping`,
`stopped_verified` and `unknown` as proposed adapter evidence states, not new
Factory issue states. Define `stopped_verified` only for measured supported
resources; any unmeasured descendant/backend prevents an aggregate verified stop.

Do not use the agent's natural-language answer, a PostHog event, `close()` return,
a kill count, or an absent PID as the receipt. Do not rewrite a successful effect
receipt when completion races with cancellation. Retain both facts and settle
the race through the existing lifecycle rules.

### 4. Expose it through Factory and correlate telemetry

Extend the canonical CLI's exact-invocation diagnostic to report ownership and
stop-evidence coverage. Add bounded read-only native/telemetry lookup only through
that interface; no direct wrapper or database workaround in the operator flow.
Candidates from telemetry are leads requiring corroboration, not stop authority.

Send bounded invocation/attempt/session identifiers and stop stage/result to the
existing PostHog integration. Verify exact identifier ingestion in an isolated
canary, including missing/delayed telemetry. The private journal and lifecycle
ledger remain authoritative; telemetry failure must not change stop semantics.

## Acceptance checks before activation

Use disposable fixtures, not ENG-162 or another production task:

- Cancel before registration, during foreground execution and during background
  execution; assert no tool dispatch after the fence.
- Two concurrent invocations in one profile plus an unrelated reviewer: stopping
  one must leave the others running.
- Delegated children, native session rotation, persistent background work and
  escaped descendants: either verified coverage or explicit unresolved evidence.
- Stale/reused PID, wrong runtime incarnation, wrong epoch, duplicate cancellation
  and corrupted/missing ownership records: refuse unsafe action.
- Gateway/owner crash and restart, with cancellation/completion races: retain
  evidence and replay protection without inventing a result.
- Remote backend without survivor proof and delayed external writes: never mark
  complete containment from local process exit alone.
- Private diagnostics contain only allowlisted metadata; PostHog unavailable
  does not block cancellation or authorize recovery.

Run the existing worker build, focused gateway/journal/CLI tests and contract
checks for touched paths. Add regression tests for the cases above alongside
those implementations; no new CI threshold or service is proposed here.

## Historical recovery and readiness

The new adapter cannot retroactively create ENG-162's missing ownership proof.
First extend the canonical diagnostic with a bounded, read-only exact-ID lookup
for surviving native/telemetry evidence. Do not import recovery code, read prompt
contents, search by time alone, or export whole process/session records. Validate
any candidate against independent invocation evidence before allowing a targeted
stop. If no trustworthy mapping is found, report the remaining recovery boundary
instead of approving a retry.

The prospective journal/ownership design and disposable tests can proceed.
Production activation remains gated on verified native hook support, installed
source coverage, isolation/termination tests and reviewed operational authority.
External write settlement and workspace preservation remain separate requirements
of the [audited recovery flow](interrupted-execution.md).
