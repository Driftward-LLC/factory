# Publishing and installing Factory releases

Factory releases are the distribution boundary. The public project tests and
publishes a versioned image; every installation pulls that same release and keeps
its own configuration, credentials and persistent data private.

## Release contents

- Public image: `ghcr.io/driftward-llc/factory:VERSION` (Linux amd64).
- Exact installation pin: `ghcr.io/driftward-llc/factory@sha256:DIGEST`.
- Assets: `factory-VERSION-linux-amd64.tar.gz`, `deployment.json`, and `SHA256SUMS`.
- The archive contains an allowlisted deployment bundle and `factory.env` with
  the release version, source commit and immutable image reference.
- GitHub artifact attestations bind the assets to the release workflow and source.

No host checkout, provider credentials, databases, browser state or executor
sessions belong in an image or release asset. Fresh installations start in dry-run
mode; real execution requires a separately provisioned execution runtime and
explicit activation. See the [portable guide](../deploy/PORTABLE.md).

## Maintainer setup

Make the source repository public and explicitly set the `factory` GHCR container
package to **Public** in its package settings. Package visibility is separate
from repository visibility. Grant the publishing repository access to that
package. The release workflow verifies public package identity before publishing;
it never changes visibility itself.

Standard GitHub-hosted CI is free for public repositories. GitHub's artifact
attestations are also available for public repositories on the Free plan. The
release job uses its short-lived `GITHUB_TOKEN` and OIDC identity; no registry
personal access token, deployment key or application secret is required in CI.

## Validate and publish

Use date-based tags `vYYYY.M.D`, with `.N` for additional same-day releases.
Select a main-branch commit whose entire **Hatchet Quality** workflow succeeded.
Its code, browser/UI, evidence publication and installation/restore rehearsals
remain blocking. Quality checks do not publish images or deploy hosts.

```bash
release_tag=v2026.10.29
gh workflow run factory-release.yml --repo Driftward-LLC/factory --ref main \
  -f release_tag="$release_tag"
gh run list --repo Driftward-LLC/factory --workflow factory-release.yml
# Inspect the exact run and commit returned above, then:
gh run watch RELEASE_RUN_ID --repo Driftward-LLC/factory
```

The release checks out the triggering commit rather than a moving `main`. It
requires a successful complete quality run for that exact commit, repeats the
release checks, builds and rehearses the image, pushes the tested single-platform
image, records its digests, attests the assets, creates an annotated tag and
publishes the GitHub Release. Publication never contacts an installation host.

For validation without publication, add `-f dry_run=true`. Dry runs do not log in
to GHCR, push images, sign release assets, create tags or publish releases. Their
local bundle is explicitly validation-only and cannot be installed.

The workflow refuses existing image tags and releases. If it fails after pushing
an image, investigate and use a new release version; do not overwrite an existing
tag or attach a bundle for a different digest. A partially published image is not
a completed release. Do not use a source-only release to bypass failed checks.

## Runtime compatibility evidence before publication

Run the existing disposable portable acceptance check against the exact candidate
image before publication. It admits a credential-free synthetic native plugin on
an initialized profile and verifies native environment currency plus skill and
linked-asset resolution before and after cold backup/restore. The fixture uses
public runtime APIs and contains no private plugin artifact or credentials.
Archive regression tests separately cover link escapes, hardlink targets,
symlink-parent writes, duplicates and special files.

For an operator-supplied execution plugin, retain an additional isolated rehearsal
with its reviewed immutable manifest: fresh and already initialized profile
provisioning, native dependency admission followed by offline readiness
verification, real activation approval and
stale-approval refusal, stopped restart, cold backup/restore and native readiness
on restored state. Record the exact source, image digest, deployment bundle and
check outcomes before approving that runtime for the installation. Keep private
packages and installation evidence outside public source and release assets.

A source check, a synthetic image rehearsal, a released-image rehearsal and a live
observation establish different facts. Label them explicitly. Reports should
identify which phase failed and whether a complete snapshot manifest exists;
never infer a successful restore from archive creation or file presence. Share
sanitized classifications and bounded results publicly; retain configuration,
authentication, profile inventories and detailed recovery paths privately.

## Independent review release acceptance

Before accepting independent-review behavior on a release, use the existing
portable acceptance and console browser harnesses for disposable image/schema
and UI checks. These credential-free fixtures prove bounded contracts only.
They do not prove native provider execution or a completed independent review.

A separately authorized live canary may use an isolated installation or an
explicitly approved maintenance window in the existing installation. Both must
use the published immutable image. Record the release version, image digest,
image source commit, deployment bundle digest and exact configured review policy.
Use supported authenticated executor and reviewer profiles with their separate
identities preserved; follow the [native backend runbook](../deploy/INDEPENDENT-REVIEW.md).
Existing valid profiles need no duplicate sign-in or token copies. Publication
alone does not establish production acceptance.

For an existing-installation canary, approve and record this bounded sequence:

1. Inventory active jobs, effects, reviews and the Hatchet queue. Pause ordinary
   admission through supported installation controls, drain active work and
   settle outstanding deliveries/leases before stopping writers. Prove those
   controls permit only the designated canary issue to execute, including queued
   dispatch. Stopping the intake worker alone does not contain already queued
   work. If supported controls cannot establish this boundary, use an isolated
   installation instead. Preserve all preexisting holds and paused controls.
2. With writers stopped, retain a verified cold backup of every durable volume,
   configuration and native profile/state store, a validated complete snapshot
   manifest, and the original immutable image and deployment bundle. Follow the
   [backup/restore procedure](../deploy/PORTABLE.md) and validate a disposable
   restore. Run the explicit schema-11 migration with writers stopped and its
   disposable-copy and historical-row preservation checks described below.
3. Deploy the compatible published image and typed gateway together. Keep
   ordinary admission paused. Review only the policy/configuration needed for
   the designated test; do not widen global execution or repository policy to
   make it pass. Keep automatic repairs disabled for this bounded canary. Any
   required configuration change must use the existing authentic preview and
   activation approval flow; retain original and canary policy/configuration
   digests for restoration.
4. Run one minimal real task and a genuine operator-approved retry, retaining
   the complete acceptance evidence below. Run negative tests in the disposable
   harness: do not mutate shared production policy or PR heads for fault
   injection. Keep the canary bounded to the designated issue.
5. After successful verification, recheck active work and the Hatchet queue,
   restore the prior policy/configuration through its supported approval flow,
   and restore only controls this maintenance window changed. Preserve prior
   holds. Resume ordinary admission only after these checks succeed.

On failure, stop test dispatch and keep ordinary work paused. Preserve evidence,
post-backup state and remote artifact IDs before any restore: restoring a backup
does not undo GitHub or Linear effects. Rollback uses the matching old image and
complete pre-upgrade backup through the reviewed restore procedure; never
downgrade schema metadata or delete audit rows. Reconcile remote effects before
any later resumption.

Retain this linked evidence for the same retry attempt lineage:

1. Root operator retry preview and approval, including its recorded enrollment
   decision and unchanged policy digest at admission.
2. Real execution reaching recorded verification with the exact PR URL and full
   verified head SHA. Deterministic verification alone does not pass this gate.
3. The Factory-owned native independent reviewer starting for that exact head,
   with its bounded pass number, reviewer model and safe session reference.
4. A real reviewer receipt persisted and validated against the request, review
   ID, head and receipt digest. Synthetic receipts and mocked provider calls do
   not satisfy this gate.
5. Console evidence from the same published image showing the independent result
   separately from execution verification, with the full reviewed SHA and recorded
   receipt details. Retain disposable-harness evidence for awaiting scheduling,
   policy-change operator stop, historical unknown enrollment and changed-head
   invalidation without showing a review pass.

Link sanitized harness results and console captures to these identities; retain
credentials and raw session material privately. Direct review of a historically
verified draft via explicit enrollment alone does not validate retry enrollment.
A changed recorded PR head must invalidate the prior review. Missing, stalled,
unavailable or mismatched evidence leaves this acceptance gate incomplete. Record which phase remains pending;
source tests, catalog checks and image dry runs must never be reported as a live
provider pass. This procedure itself grants no execution, retry, publication,
migration or deployment approval.

## Install on a fresh host

Download all three assets from one trusted release:

```bash
gh release download "$release_tag" --repo Driftward-LLC/factory \
  --pattern "factory-$release_tag-linux-amd64.tar.gz" \
  --pattern deployment.json --pattern SHA256SUMS
sha256sum --check SHA256SUMS
gh attestation verify "factory-$release_tag-linux-amd64.tar.gz" \
  --repo Driftward-LLC/factory \
  --signer-workflow Driftward-LLC/factory/.github/workflows/factory-release.yml \
  --source-ref refs/heads/main --deny-self-hosted-runners
tar -xzf "factory-$release_tag-linux-amd64.tar.gz"
cd "factory-$release_tag-linux-amd64"
python3 deploy/factory-portable.py --help
```

Alternatively download the assets from the release page. Verify checksums before
extracting. A current GitHub CLI supports the optional additional attestation
check above. Checksums alone detect corruption; acquire them through the trusted
release rather than an unrelated mirror.

Follow the bundle's portable guide for preview-first initialization, pulling and
startup. Public image pulls do not require registry credentials. Keep the digest
pin and supply integration credentials only through private installation files.
Do not rebuild the release on the deployment host.

## Upgrade an existing managed installation

Use the host-local release selector:

```bash
factory deploy preview --release "$release_tag" --json
factory deploy approve --preview PREVIEW_DIGEST --reason "Reviewed release" --yes --json
factory deploy apply --preview PREVIEW_DIGEST --yes --json
factory deploy status --json
```

The [deployment guide](automatic-deployment.md) describes provenance validation,
compatibility review, helper installation, cold backups and failure handling.
A release remains selectable when main advances. It does not automatically apply
schema changes, update privileged helpers, migrate state or enable execution.
Older releases without `deployment.json` remain usable through their documented
portable installation path but cannot use the new managed release selector.

Routine CI screenshots and catalogs retain their seven-day policy. Release assets
are durable GitHub Release attachments, so expiry of routine CI evidence does not
remove the release's deployment metadata. Producer runs and attestations must
remain available for managed verification. Do not delete release runs as a way
to reduce artifact storage.

## Bridge schema 10

Building or merging a release does not migrate the live ledger or authorize an
unstarted-review recovery. Schema 10 adds an empty, append-only independent
review delivery table; its migration preserves all historical review, job,
event and outbox rows. It does not rearm reviews or dispatch work.

Deploy this schema in a separately reviewed maintenance window. Quiesce intake,
bridge/operator/dispatcher consumers and workers, confirm no active executions
or unsettled leases/deliveries, and retain a cold, restorable backup of the
ledger and release configuration. Run the explicit installer migration and
its disposable-copy, integrity and historical-row preservation checks before
starting all consumers from the same schema-10 release. Runtime and read-only
connections perform no DDL and refuse schema 9 until migration completes.
Follow the existing [bridge migration procedure](../hatchet/bridge/README.md#legacy-publication-and-migration-boundary)
for the supported migration gates; do not apply ad hoc SQL against live state.

After deployment, a root operator must separately inspect, preview and confirm
each eligible unstarted review using `factory review-recovery`. Neither the
migration nor a background sweep performs that approval.

Schema 9 binaries cannot open a schema-10 ledger. A rollback across this boundary
requires a reviewed restore/migration plan and quiescence; preserve any work
recorded after the backup. Do not point the old image at the upgraded ledger.

## Bridge schema 11

Schema 11 adds an append-only ledger for explicit enrollment of existing verified
drafts. New admission decisions are recorded as versioned evidence in the admitted
job payload. Historical attempts are not backfilled and retain unknown enrollment
when no decision exists. A recorded exclusion is distinct from a review pass.

Use the same quiesced, cold-backup and explicit installer migration gates described
above, with all consumers on the schema-11 image. Read-only runtime connections
perform no migration. Validate a disposable copy and historical-row preservation
before starting consumers. Older binaries must not open the upgraded ledger;
rollback requires a separately reviewed cold restore/migration plan preserving
post-backup work. Source changes do not authorize a live migration or retry.
