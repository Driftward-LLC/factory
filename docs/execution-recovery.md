# Durable execution handoff

Factory records one executor invocation per bridge job. The gateway persists a
private reservation before calling Hermes and a bounded receipt before replying.
Repeated create requests cannot invoke Hermes again for that job. A completed
request returns its original receipt only when its intent and execution fence
match. A reservation without a receipt remains an unknown outcome after restart.

The executor now creates receipts independently of the agent's final wording.
After a normal command exit it observes the exact repository/branch in GitHub
and binds one open draft PR to its head SHA, base, bot author, Linear reference,
and bridge marker (or the exact prior PR for repairs). A prose response cannot
invent an artifact or hide a published one. The optional agent validation report
is still **executor-reported**, not independent test evidence: it must bind to
the observed URL, branch and full head SHA, and the existing verification policy
still gates readiness. Missing validation is partial delivery, not success.

After a normal command exit, the gateway saves an executor-private
`factory-artifact-observation-v1` checkpoint before querying GitHub. It contains
the exit time, bounded pre-execution PR identities and the policy-bounded
validation claim, not raw output. If GitHub inspection is unavailable, the
invocation remains unknown and reconciliation can retry **only the fixed,
read-only inspection**, including after gateway restart. It never reruns Hermes.
The recovered observation must pass the same repository, branch, full SHA,
author, base and issue binding checks; a saved claim is not an artifact receipt.
Concurrent lookups share one inspection in the gateway. A missing or invalid
checkpoint grants no inspection recovery or execution authority.

If execution throws, exits without a PR, or publishes without complete validation,
the gateway durably writes a bounded `factory-executor-outcome-v1` receipt before
replying. It records `incomplete` or `failed`, the job identity, finish time,
allowlisted reason, observed process exit/signal/timeout fields, and any bounded
artifact observed by the adapter. It never stores model prose, raw errors,
credentials or transcript data. Unknown process details stay null. In particular,
this CLI adapter cannot mechanically distinguish a turn-budget stop from other
normal exits without a deliverable; it does not infer that from prose.

Terminal failure receipts always retain `effect_outcome=unknown` and
`containment=unconfirmed`. A stopped wrapper does not prove its remote descendants
stopped or that every external write settled. Cancellation remains unacknowledged,
the original effect fence remains held, and retry/manual editing remain guarded.
Reconciliation records the terminal receipt once and stops repeatedly polling
for a success receipt. An abrupt gateway kill before a receipt still leaves
`outcome_unknown`; only a saved observation checkpoint permits read-only
recovery. No old checkpoint or receipt is manufactured retroactively.

New reservations store fingerprint version 2. Their identity includes the job,
idempotency key, action, branch and complete payload **except** the four top-level
bridge correlation fields: `source_event_key`, `source_delivery_id`,
`source_event_keys`, and `source_delivery_ids`. Adding webhook references is not
new execution intent. Instructions, repository, policy, attempt/repair lineage,
nested fields and unknown payload fields remain bound. The original execution
fence must still match before a completed receipt can be returned by create.

Unversioned reservations retain the original version-one full-payload comparison.
They are never rewritten or promoted during inspection or repeated create
requests. A legacy record whose correlation fields already changed still fails
closed; this fix cannot recover its receipt or authorize a retry. Unsupported
fingerprint versions also fail closed before cancellation or receipt handling.

## Recovery and manual control

The existing reconciliation workflow queues receipt lookup for stale authorized
executions. Take manual control also queues lookup for an invocation that has
already started. Both use the existing accepted-event outbox, with coalesced
delivery and the same attempt/job. The executor worker inspects the gateway;
it never calls create for an authorized, unknown, or cancellation-requested
attempt. Recovery requires the original fence, job, repository, branch, and PR
binding. Changed intent, a superseded attempt, or conflicting receipt fails
closed. Recovered effects proceed through the existing verification path.

Cancellation after authorization is a **drain request**. The gateway records
the request, allows the already-authorized command to settle, and acknowledges
the handoff only when its completed receipt exists. It does not claim that
killing a host wrapper stopped a process inside another runtime. Factory accepts
the original receipt, records `canceled_after_effect`, releases the lease, and
then permits manual editing. It does not verify or repair held work. Existing
pre-effect cancellation still prevents execution.

The console shows the latest bounded receipt observation and its timestamp.
Observation alone never permits editing. `not_found`, `outcome_unknown`, and
an expired lease remain blocked. Old attempts such as ENG-134 cannot acquire
receipts retroactively: their executor and GitHub evidence still need reviewed
operator recovery. This release does not clear them or authorize new attempts.

For held interrupted executions without completion receipts, with a known prior
PR, a bounded partial PR observation, or
fresh evidence that the create-PR branch and PR are absent,
see the separate [root-attested resolution procedure](interrupted-execution.md).
It requires reviewed external containment, not merely an expired lease or an
unchanged PR. It records unknown outcome rather than fabricating a receipt.

## Storage and rollout

The host gateway owns `/var/lib/driftward-hermes-factory/invocations`, provisioned
through its systemd `StateDirectory`. Directories are private to the executor
owner and files are mode `0600`. `DRIFTWARD_HERMES_INVOCATION_DIRECTORY` may
select another absolute, persistent private directory. For the bundled executor
role, the default is `HERMES_HOME/factory-invocations` in its existing private data
volume. Workers access the typed gateway, never this directory.

Records contain an immutable intent hash, original execution fence, and bounded
PR receipt. They exclude prompts, command output, session material, and provider
credentials. The execution fence is sensitive: do not expose records through
the console, logs, artifacts, or Git. Keep the journal when restarting or
upgrading the gateway. Back it up with the quiesced executor and Factory state;
do not restore an older journal against newer bridge state or delete reservations
as a way to retry. There is no automatic retention cleanup in this version.

This receipt update requires a coordinated gateway/worker/bridge rollout, not a
journal migration. It needs a separately reviewed gateway rollout; upgrading only
the Factory worker image does not update a separately installed host gateway.
Older gateway code cannot read version-two hashes (the version is included in
the hash). Retain compatible gateway code and all reservations when rolling back
workers; never remove or rewrite journal entries to make a downgrade work.

Deploy the gateway before enabling the updated worker. The identity preflight
requires invocation protocol version 2, so an old gateway blocks new execution
before the authorization boundary. Upgrade bridge and workers together for
the recovery endpoints. Use the existing reviewed release/deployment procedure;
this source change does not deploy or activate execution. Rollbacks must retain
the journal and quiesce work, not route unresolved invocations through old code.

## Failure checks

Tests use disposable journals, SQLite databases, gRPC sockets and browser data:

- Duplicate delivery invokes the executor once; changed intent/fence is rejected.
- A lost gRPC reply is retrieved after gateway restart using its original fence.
- Process death before a receipt remains unknown and cannot execute again.
- Cancellation during execution remains pending until the receipt settles.
- Receipt observations cannot release leases; wrong fences fail settlement.
- Recovery wakes coalesce, survive delivery retries, and do not create attempts.
- Held work settles to manual control without entering verification.
- Prose output and missing validation produce durable partial outcomes, not fabricated success.
- Failure receipts survive restart, carry bounded artifact observations, and stop success polling without releasing fences.
- GitHub outages and post-checkpoint crashes recover through inspection with the original fence and one agent invocation.
- Partial PR observations feed root-attested recovery; fresh matching heads and containment remain mandatory.

Run the worker, bridge, console, and catalog checks documented in `AGENTS.md`.
Gateway/process loss after an external write but before receipt persistence is
still an ambiguous outcome. The journal prevents duplicate execution; it does
not provide an atomic transaction with GitHub or arbitrary agent tools.
