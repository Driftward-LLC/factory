# Factory work evidence v1

New Factory attempts record immutable execution manifests and work checkpoints
in the existing bridge ledger. Inspect one through the root-only operator CLI:

```bash
factory attempts evidence ATTEMPT-ID
```

This calls `GET /v1/attempts/{attempt_id}/evidence` on the authenticated operator
Unix socket. The response is `factory-work-evidence-v1`; it accepts no query
parameters and returns at most two manifests and three checkpoints. The worker
API and browser console do not expose this endpoint. These are observations of
existing transitions; they do not grant execution, verification or merge authority.

## Console verification view

The issue inspection view adds `verification_evidence` to the existing
authenticated `GET /api/lifecycle/{issue}` response. It is a separate allowlisted
`factory-console-verification-v1` projection for the **newest returned attempt**,
bound by `attempt_id`; it does not expose the root-only evidence endpoint or
serialize full manifests. The lifecycle, checkpoints and inspection are read in
one SQLite snapshot. Reading the view never requests a new inspection or
changes any attempt, receipt, lease or outbox.

The view shows specification, implementation and verification recording states,
the full verified SHA, verification time, policy/receipt digests and recorded
check counts. It labels local checks as executor-reported, not independently
executed. Independent review remains unavailable in this projection.

For a valid recorded verification, compare its full SHA only with the newest
stored PR inspection for the same repository and exact PR URL:

- `same_head`: the inspection was recorded after verification and matches its SHA.
- `head_changed`: that later inspection records a different SHA. The historical
  verification remains immutable and does not cover the inspected revision.
- `inspection_predates_verification`: the inspection cannot establish a
  post-verification head comparison.
- `unavailable`: missing, pending, expired, invalid, unknown or short-SHA
  evidence cannot establish a comparison.

Inspection freshness is independent of SHA comparison. An old matching
inspection stays visibly stale; page refresh only rereads the ledger and never
certifies live GitHub state. A newer pending inspection is not silently replaced
by an older successful result. Observed open/closed/merged state is historical,
not a current review or merge decision. GitHub webhook/review aggregation is not
part of this projection, so "latest" refers specifically to the stored inspection
requests, not every possible artifact observation.

Dry runs cannot establish real verification. Older attempts without checkpoints
stay unavailable; later repair attempts never inherit the previous attempt's
verification result. Unknown contract versions, invalid digests and malformed
verification references are not presented as passed evidence. This view adds
no database migration, verification producer, reviewer or approval authority.

## Producers and contracts

| Record | Producer boundary | Meaning |
| --- | --- | --- |
| Admission manifest | New intake, explicit retry or automatic repair reservation | Frozen input hash, requirements hash/source, submitted policy hashes, attempt/job identity, mode, generation and prior attempt |
| Execution manifest | Existing validated `execution_route_selected` operation, before preflight | Admission document plus the immutable route envelope, linked to its parent manifest |
| `specification_snapshot` | Same transaction as admission | Requirements content was present or unavailable; approval remains unknown |
| `implementation_receipt` | Durable `effect_committed` transition | Recorded repository, exact PR and full commit SHA plus receipt digest; verification remains pending |
| `verification_receipt` | Claimed verifier's terminal transition | Recorded pass/fail, SHA, reason and, on pass, policy/verifier identity, receipt digest and check counts |

Schemas and field construction live in
`hatchet/bridge/hatchet_hermes_bridge/work_evidence.py`. Every row has contract
version 1, a deterministic ID, a SHA-256 digest of its canonical JSON document
(sorted keys, compact separators, ASCII escaping), and a bridge recording time.
Checkpoint `manifest_id` identifies the execution manifest when it exists, or
the admission manifest otherwise; foreign keys bind these references to the
same attempt. The parent manifest identifies the original admission snapshot.
`created_at` is the recording time, not a provider occurrence timestamp.

Each `(attempt, phase)` manifest and `(attempt, type)` checkpoint is write-once.
Identical internal writes reuse the existing ID; changed content conflicts.
SQLite triggers reject updates, deletes and replacement inserts. Duplicate
webhooks and route submissions retain the original rows and timestamps.
Webhook correlation changes do not alter the original effect-input hash.

Admission's job, attempt, evidence and dispatch outbox commit together. A
checkpoint failure rolls back the associated lifecycle transition. Existing
execution fences, cancellation handling, verifier leases and policy validation
remain the producers' authority checks. Dry runs get admission evidence only.
Failed verification records its checkpoint before any bounded repair reservation;
the repair gets its own manifest and retains the prior attempt relationship.

## Evidence limits and privacy

The requirement snapshot hashes the actual selected `implementation_prompt` or
fallback `instructions`. It is not an approved specification or a claim that
acceptance criteria were satisfied. The input digest covers the existing stored
effect payload; it excludes webhook correlation fields. A submitted policy hash
identifies the supplied policy and does not certify that policy as valid.
Verification continues to enforce the checked-in policy independently.

Local command results are labeled `executor_reported`. A passed checkpoint means
the existing verifier accepted its evidence at that SHA; review approval remains
unknown. Failed verification has no successful receipt digest or check counts.
Malformed/missing artifact references are represented as unavailable, never
inferred from a successful process exit or a branch name.

Manifests and checkpoints contain hashes, bounded identifiers, enumerated values
and counts. They do not copy prompts, requirements text, command output, diffs,
source files, session identifiers, transcripts, credentials or arbitrary artifact
URLs. All new records pass the bridge's existing sanitization check before
insertion. Hermes continues to own execution, skills, sessions and credentials.

## Replay coverage

`replay.available` remains false in this slice. `replay.missing` lists the absent
inputs explicitly: the measured checkout/base SHA, runtime skill/profile/prompt
bundle digest, environment image digest, retrievable input artifact, and any
missing requirements, policy or execution-route evidence. An expected repair
head is recorded separately and never relabeled as the actual starting SHA.
Configured model/provider values are route intent, not observed invocation data.

The current gateway does not supply the missing execution snapshots. Future
producers must capture them at the execution boundary with approved retention
and artifact access. Hashes alone cannot recreate historical inputs. There is no
replay runner, evaluation judge, deployment/incident ingestion or automated
configuration change in this slice.

## Migration and validation

Bridge schema 6 adds `factory_execution_manifests` and
`factory_work_checkpoints` using the existing installer-only migration
transaction. Runtime connections now require schema 7 (which also adds durable
reconciliation scan progress); they perform no DDL.
Deployment uses the existing reviewed release, backup and migration procedure.
Rollback across the schema change requires the matching pre-upgrade database
snapshot and release, as described in the deployment runbooks.

Existing rows are preserved. No manifests or checkpoints are backfilled: old
attempts return `available: false` and `replay.missing: ["admission_manifest"]`.
In-flight older attempts continue under existing safety rules without invented
admission history. New generations begin recording evidence after upgrade.

Focused verification uses disposable databases, including a schema-5 upgrade,
duplicate deliveries, stale leases, automatic repair, privacy-shaped fixtures,
append-only enforcement and injected transaction failures:

```bash
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer \
  python3 -m unittest discover -s hatchet/bridge/tests -p 'test_work_evidence.py'
```
