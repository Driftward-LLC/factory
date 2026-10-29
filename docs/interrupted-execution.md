# Resolve an interrupted execution into manual ownership

This root-only operator operation closes an interrupted execution's authority;
it does **not** declare that the implementation succeeded or that no external
effect occurred. It is operator-assisted reconciliation with fenced ownership
transfer, not an executor receipt, rollback, retry, or force-unlock.

Use automatic receipt recovery first. This exception is for held real attempts
in `cancellation_requested`, with an expired authorized lease, a running job,
no observed completion receipt, and either a known prior PR or a create-PR job
with freshly inspected branch/PR absence. Other cases remain blocked. Absence
is not proof that no external effect ever happened or that execution stopped.

## Trust and safety contract

Factory's operator has no executor data, Docker socket, or provider credentials.
It cannot independently prove external quiescence. This operation explicitly
trusts a root operator's containment attestation. The preview labels that basis
`trusted_root_attestation_not_machine_verified`. Digests bind reviewed evidence;
they do not prove that its contents are true. Never generate an attestation
merely to satisfy the API. Missing proof means **keep handoff pending**.

Before attesting, the operator must establish and retain evidence for all of:

1. **Executor and descendants stopped.** Identify the exact runtime instance
   and invocation. Account for detached child processes and any remote work it
   started. An expired lease, process-list snapshot, or wrapper exit alone does
   not establish this.
2. **Restart and replay blocked.** Either permanently retire an isolated runtime
   instance (`isolated_runtime_retired`), including its restart/replay paths, or
   revoke the old invocation's external write authority (`write_authority_revoked`).
   A stopped container that can restart with valid credentials is insufficient.
   Revocation must include cached/in-flight execution credentials, not just a
   setting that would affect the next token refresh. Both strategies also
   require stopped descendants. Do not disrupt unrelated shared-runtime work or
   rotate shared credentials without separate scoped approval.
3. **External writes settled.** Account for outstanding GitHub/tool requests,
   then inspect the affected artifact. A database fence does not fence GitHub;
   a request already accepted by a provider may outlive its caller. If the
   external system cannot give adequate settlement evidence, do not attest.
4. **Workspace preserved.** Take a retained snapshot of uncommitted and untracked
   work, including relevant local commits; record its checksum. Never reset,
   delete, or overwrite the workspace to make it look clean. Preserve existing
   Hermes state in its own runtime; do not copy sessions into Factory.

Retain a protected, sanitized evidence bundle and its SHA-256 on the operator
host. Do not include provider credentials or raw transcripts in the attestation.
PR head observation alone does not establish items 1–3. This operation does not
make arbitrary agent side effects exactly-once.

## Preview and confirm

First take manual control using the console. Then use the CLI and protected
operator socket/env for the **active installation**, not a retired host wrapper.
The Unix endpoint checks both its bearer and root `SO_PEERCRED`; browser and
ordinary bridge credentials cannot invoke this operation. No actor is accepted
from request JSON.

After containment is established, prepare one root-owned mode `0600` JSON
attestation file, at most 4096 bytes. These placeholders describe the schema;
they are not evidence and must not be submitted as a real attestation:

```json
{
  "schema": "factory-interrupted-resolution-v1",
  "attempt_id": "<exact attempt UUID>",
  "job_id": "<exact bridge job UUID>",
  "lease_id": "<exact lease UUID, not its secret fence token>",
  "lease_epoch": 1,
  "observed_at": "<UTC timestamp of the containment review>",
  "runtime_ref": "<exact retired or write-disabled runtime instance>",
  "containment_method": "isolated_runtime_retired",
  "evidence_sha256": "<SHA-256 of retained containment evidence>",
  "workspace_snapshot_sha256": "<SHA-256 of retained workspace snapshot>",
  "checks": {
    "executor_and_descendants_stopped": true,
    "restart_and_replay_blocked": true,
    "external_writes_settled": true,
    "workspace_preserved": true
  }
}
```

Request a typed, read-only inspection of the PR already bound to the attempt:

```bash
factory attempts resolve-interrupted ATTEMPT-ID inspect --yes
factory attempts resolve-interrupted ATTEMPT-ID preview \
  --attestation /srv/factory-recovery/attestation.json --reason "Reviewed containment and workspace snapshot"
```

For a known prior PR, `inspect` queues only the existing `inspect_draft_pr_state`
action, never agent execution. Wait for its completion, then preview. Preview reads a single SQLite
snapshot without writing. The attestation must be no more than five minutes
old, after cancellation, and the inspection must be fresh (two minutes) and
recorded **after** containment review. Missing or stale inspection blocks.
The exact repository, prior PR, branch and observed head are bound into the
preview digest, together with ownership revision and execution lease identity.

For a create-PR job with **no prior PR**, `inspect` instead returns the attempt's
repository and job branch to the CLI, which uses the host's existing GitHub CLI
identity to inspect that branch and PRs in **all states**. It queues no work and
does not pass GitHub credentials to Factory. After containment review, save the
output in a new root-owned `0600` file in a protected directory:

```bash
umask 077
factory attempts resolve-interrupted ATTEMPT-ID inspect --yes > /srv/factory-recovery/absence.json
factory attempts resolve-interrupted ATTEMPT-ID preview \
  --attestation /srv/factory-recovery/attestation.json --absence-evidence /srv/factory-recovery/absence.json \
  --reason "Reviewed containment and workspace snapshot"
```

The file must contain one `github_branch_pr_absence_v1` object, no more than
4096 bytes: exact `repository`, `head_branch`, `branch_exists: false`,
`pull_requests: []`, and `observed_at`. The Unix operator trusts this root-supplied
observation; it does not independently query GitHub. The preview explicitly
labels its inspection source `trusted_root_absence_attestation`. Evidence must
be at most two minutes old and recorded after containment review. An existing
branch, any PR (including closed/merged), wrong identity, or stale observation
blocks retirement. A known prior PR cannot use this absence path, and a repair
job without its prior PR remains blocked. If GitHub access is denied or the
repository cannot be inspected, stop; do not interpret that as absence.

For this path, add the **same** `--absence-evidence /srv/factory-recovery/absence.json`
to confirmation below. Do not regenerate it between preview and confirmation;
changed evidence requires a new preview. Reinspect and preview if it expires.

Review the preview and confirm the identical reason and evidence:

```bash
factory attempts resolve-interrupted ATTEMPT-ID confirm \
  --attestation /srv/factory-recovery/attestation.json --reason "Reviewed containment and workspace snapshot" \
  --request-id operator-resolution-UNIQUE-ID --snapshot-digest PREVIEW-DIGEST --yes
```

Confirmation rechecks the evidence and ownership in one `BEGIN IMMEDIATE`
transaction. Changed observations, receipts, ownership, or execution identity
require a new preview. A repeated identical request ID returns the stored
resolution even after evidence expires or the operator process restarts;
different intent with that ID is rejected. Replay returns the **original**
result, not current safety permission: refresh console control before editing.

## Result and limits

- Job and attempt become `failed`, with reason
  `operator_interrupted_outcome_unknown`; historical unknown-outcome events stay.
- The old lease is released without setting `effect_observed_at` or creating a
  successful receipt. Its original identity remains for audit. Late commits,
  heartbeats and reclaims are rejected by the existing fence checks.
- Outstanding accepted-event deliveries are suppressed. The issue stays held,
  with a new ownership revision. Other unresolved attempts still block editing.
- The existing recovery ledger stores the evidence, inspection, digest, actor
  and original response. Both job and attempt timelines record
  `execution_interrupted_resolved`; the lifecycle writer emits no Linear update.
- No PR is altered, merged or closed; no verification or new attempt is started.
  Returning to Factory remains a separate reviewed action with all existing
  artifact and Linear eligibility checks.

Deploy matching bridge/operator and lifecycle-writer code through the reviewed
release procedure before use. This change needs no schema migration or new
service. No production invocation, attestation, or historical task resolution is
authorized merely by landing this source change. Rollback must preserve the
recovery ledger; never reopen the resolved lease or replay its old invocation.

Design references: [Hatchet cancellation](https://docs.hatchet.run/v1/cancellation),
[fencing at the write boundary](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html),
[idempotent request semantics](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
and [operator intervention in distributed recovery](https://learn.microsoft.com/en-us/azure/architecture/patterns/compensating-transaction).

## Retire an absence-superseded orphan before deployment

Older reviewed absence retries may have left the prior attempt `superseded`
while its job remains `running` and its expired authorized lease remains
unreleased. Cancellation and manual-hold resolution above cannot be used for
that historical state: do not fabricate cancellation or change the issue's
ownership to make it eligible.

Use the reviewed source checkout's root-only maintenance command below only
after establishing the same four containment and workspace-preservation checks
above. Retain the real evidence bundle and snapshot; an expired lease or absent
branch alone is insufficient. The old invocation and all replay paths must be
permanently contained even while services restart for deployment. Prepare the
same attestation schema and a fresh branch/PR absence object identifying the
old job's exact repository and branch. Evidence must be recorded after
supersession, with absence inspected after containment review. Root-owned
`0600` evidence files must reside in a root-owned `0700` directory.

```bash
python3 tools/factory-retire-superseded.py preview \
  --database /absolute/active-installation/bridge.db \
  --attempt-id OLD-ATTEMPT-ID --successor-attempt-id VERIFIED-SUCCESSOR-ID \
  --attestation /root/recovery/attestation.json \
  --absence-evidence /root/recovery/absence.json \
  --reason "Reviewed containment of superseded invocation and retained workspace"
```

The database must be an existing, migrated, root-owned regular file in a
root-owned directory, without symlinks or other write permission. Preview opens
it read-only. It requires exactly the latest direct successor in
`verified_ready`, with a `ready_for_human_review` job and durable absence-fence
and supersession-event identities matching the old lease. Any other unresolved
execution on the issue blocks. Observed completion receipts, existing artifacts,
or a prior recovery decision block retirement. No PR lookup or executor work is
queued by this command; the root operator supplies the inspected absence.

Review the preview and use the identical arguments/evidence with `confirm`,
adding `--request-id UNIQUE-ROOT-REQUEST-ID --snapshot-digest PREVIEW-DIGEST --yes`.
Confirmation acquires SQLite's writer barrier with `BEGIN IMMEDIATE` and
rechecks the exact old rows, audit history, successor and issue-control snapshot.
This barrier protects the database transaction; the retained containment
evidence establishes external quiescence. The command runs no migration.

Only the old job becomes `failed` with
`operator_interrupted_outcome_unknown`, its lease is released, and its accepted
delivery is suppressed. The old attempt stays `superseded`; the verified
successor, all attempt fields, and ownership rows stay unchanged. No observed
effect timestamp, receipt, success, retry, generation, or Linear update is
created. Existing released-lease and superseded-attempt fences reject late
commits, heartbeats, and reclaims. The existing recovery ledger and both old
timelines retain `execution_interrupted_resolved` with the distinct
`factory-superseded-retirement-v1` operation, evidence digests and unknown outcome.
Identical request replay returns the original result after expiry or restart;
changed intent is rejected. A failure before audit commit rolls everything back.

This maintenance path does not broaden automatic recovery or authorize a
production invocation merely by landing the source. Run the ordinary deployment
quiescence check afterward, and retain its cold backup and this recovery ledger.
Never reopen or replay the retired lease.
