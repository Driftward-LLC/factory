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
