# Deploying a published release

Factory's public repository builds and publishes software. Each installation
chooses when to deploy a release. Pull requests and pushes to `main` run checks;
they do not publish a production image or contact a deployment host.

The release workflow publishes `ghcr.io/driftward-llc/factory:VERSION` and a
portable installation bundle. Both the bundle and managed deployment metadata
pin the image by registry digest. Installation settings, credentials, deployment
approvals, backups and runtime data remain private on the installation host.

## Select and apply a release

For an existing managed production installation with the reviewed host CLI:

```bash
factory deploy status --json
factory deploy preview --release v2026.10.29 --json
factory deploy approve --preview PREVIEW_DIGEST --reason "Reviewed release and compatibility" --yes --json
factory deploy apply --preview PREVIEW_DIGEST --yes --json
factory deploy status --json
```

Use an actually published version and the exact digest returned by preview.
Preview reads release evidence and records a private snapshot; it changes no
services. Review its protected changes and eligibility before approval. Approval
covers one unchanged snapshot. Apply runs locally, pulls the public image without
registry credentials, retains a cold backup, updates the image pin, and checks
service health. The host does not need a source checkout or build tools.

A stopped execution worker stays stopped. Deployment does not clear work-item
holds, activate real execution, change executor credentials or migrate databases.
Fresh installations use the [portable guide](../deploy/PORTABLE.md); this managed
upgrade path expects an existing production installation and reviewed CLI binding.

## What is verified

The permanent GitHub Release asset `deployment.json` binds the release version,
source commit, image manifest/config digests, protected-source fingerprint, and
release workflow run and attempt. It is independent of the seven-day retention
of ordinary CI screenshots and catalogs.

The host checks:

- The published release and its unique, bounded metadata asset and asset digest.
- The annotated version tag and exact source commit.
- GitHub's signed artifact attestation for the metadata, restricted to this
  repository's release workflow, `main` source ref and exact source commit.
- Successful completion of that release workflow and its recorded run attempt.
- The independently computed protected-source fingerprint and installed helpers.
- The registry digest, single Linux amd64 image, config digest, and OCI source,
  revision and release-version labels.

Approval and apply re-read the evidence. A changed tag, asset, run attempt,
configuration, installed helper or runtime state invalidates the preview.
The selected release can remain deployable after `main` advances; it cannot
silently follow `latest` or a moved tag. A missing producer record or unavailable
attestation fails closed even when the release download still exists.

The GitHub CLI must support `gh attestation verify`, including `--source-ref`,
`--source-digest` and `--signer-workflow`. The managed operator uses the host's
existing read-only GitHub identity for provenance queries. That identity is never
passed to Factory containers or used to authenticate the public image pull.

## Existing installation transition

The historical filename `factory-auto-deploy.py` and private configuration path
`/etc/driftward/factory-auto-deploy.json` are retained for installed-system
compatibility. The public repository no longer contains `Factory Deploy` or
requires a deployment SSH key. There is no replacement scheduler.

Before using `--release`, install the reviewed versions of the deployment helper,
policy module, operator module and canonical launcher together. Review associated
profile/artifact helpers when their installed inventory requires it. Follow the
[canonical CLI binding](../deploy/CLI.md); never execute downloaded release scripts
as a way to update privileged host tooling automatically.

The initial transition changes protected deployment and CI policy. It therefore
requires a separately reviewed host-helper and compatibility transition; an image
release cannot approve its own policy. Do not copy a target fingerprint into the
configuration merely to bypass a refusal. `factory deploy baseline` is for
reviewed adoption after a completed audited rollout, not a bypass for blocked
initial upgrades.

Existing `--run` records and private image support remain for inspection and
recovery of older installations. Their old workflow/SSH callback is not used by
the public release path. Retire an installation's old deployment key, account,
repository variables and workflow enablement through its private operations
configuration after the transition has been verified.

## Failure and recovery

Deployment checks for active or ambiguous attempts, unreleased execution leases
and unfinished bridge jobs. A SQLite writer barrier closes the check/stop race.
Installation and tenant locks serialize deployments with operators. Compatibility
or quiescence refusals leave running services alone.

Apply consumes approval durably before pulling the image. A failed or interrupted
apply is retained for operator review; retries cannot silently reuse approval.
After quiescing, the existing production wrapper backs up all seven volumes,
records the image upgrade, resumes the previously enabled services, and verifies
health and resource provenance. Backups stay private and retained. The operation
requires at least 12 GiB free before rollout.

There is no automatic database restore, blind image downgrade or retry loop.
External effects and token renewal may have advanced. Preserve the backup and
private receipts and follow the [operations runbook](containerized-service-runbook.md)
for an explicit recovery review. Never publish raw status files, generated env
files, database snapshots or execution journals in issues or release assets.
