# Automatic production deployment

`Hatchet Quality` pushes the exact Docker image it tested to the **private** GHCR
package `ghcr.io/driftward-llc/factory-production`, only for same-repository pushes
to `main` after its validation gates. The separate release workflow's package is
unchanged. CI requires the production package to be a private container with the
expected name, and refuses supplied foreign owner/repository identities or an
unverifiable response. A missing package is created with GHCR's private default;
the same checks run again after publication. Omitted/null owner and repository
fields are accepted with the workflow/image provenance checks described below,
and do not prove repository linkage. No visibility settings are changed.
`Factory Deploy` runs after that **entire** workflow succeeds (including
browser checks). It requests an image-only rollout through a deployment-only SSH
account. No general-purpose GitHub runner, application credentials, or Docker
socket is added to Factory containers. Hermes stays running and is not the
deployment scheduler; GitHub Actions owns this deterministic release sequence.

The host independently verifies the repository, successful workflow identity,
push event, branch, current `main` SHA, immutable metadata-artifact digest, exact
registry manifest and config digests, OCI revision and platform. Old/superseded
revisions cannot be deployed. GitHub concurrency
and the existing installation/tenant locks serialize deployments with operators.
The protected-tree fingerprint is independently computed from GitHub's complete
source tree using the root-installed policy, before downloading the artifact.
Changing the CI packager cannot approve its own policy changes.
The Actions artifact contains only bounded `deployment.json` metadata (format 2),
binding the image digest to the commit, run ID, run attempt and compatibility
fingerprint. No image tarball is uploaded. CI uses a run/attempt tag for retention;
the host pulls only the recorded digest. Tag movement cannot retarget deployment.
Single-platform Linux/amd64 manifests are required. The registry config digest
must match CI's metadata, and Docker's local repository digest must match the
requested reference. Classic Docker uses the config digest as its local ID;
containerd can use the manifest digest. Only those two verified local IDs are
accepted, and the existing production wrapper and canonical CLI continue to use
the immutable local ID. A rerun changing the attempt during download is refused.

The validation job has static `packages: write` permission because it must publish
the tested local image without another large artifact transfer. Trusted same-repo
PR jobs therefore have that capability, while login/publication are guarded to
same-repo main pushes and fork tokens remain read-only. The deployment job has
only `packages: read` and sends its short-lived `GITHUB_TOKEN` and actor name over
the existing SSH stdin. The host validates the bounded envelope and writes only
its GHCR auth entry into a mode-0600 file in a temporary mode-0700 Docker
configuration directory. The nonempty auth map disables Docker's native
credential-helper autodetection; no login command or host keychain is used.
The host removes that configuration before rollout (including on pull failure). The token never enters
arguments, logs, application configuration or worker environments. Host `gh`
authorization for source/Actions reads remains separate; it needs no package scope.

Publication verifies the fixed organization/package REST endpoint reports a
private container with the expected name. GitHub's REST package schema permits
`repository` and `owner` to be omitted or null; when supplied, their identities
must match. An absent repository field does not prove repository linkage.
Provenance instead also relies on the canonical main-push workflow using its
repository-scoped `github.token`, the tested image's OCI source/revision labels,
and the pushed manifest's config digest matching that image. Local environment
variables alone do not authenticate a publisher. Internal same-repository PRs
run the same package check with their workflow token through the read-only
`--verify-package` command; it never publishes an image or deployment metadata.
Diagnostics report only fixed verification results and optional-field presence.

## What runs automatically

The first version allows known image-only surfaces: console, analytics, selected
worker workflows, documentation and tests. Everything else contributes to a
root-approved compatibility fingerprint. This deliberately includes executor and
journal code, shared contracts, database implementation, image dependencies,
Compose, deployment code and CI policy. Added/deleted unknown files also stop the
rollout. This is a conservative file gate, **not** semantic migration detection.
An operator must review such changes, deploy their components/migrations, and
then update the compatibility baseline. Never just copy a failed artifact's hash
to bypass the gate. Auto-deployment never updates its own host installation.

The reviewed host toolchain can retain its newer audited deployment CLI,
manual-rollout fences, and read-only executor authentication readiness check
while the release image embeds older copies. `HOST_TARGET_BLOB_PAIRS` in the
root-installed operator pins exactly three installed/target Git blob pairs:
`factory-auto-deploy.py` (with the opaque-token fix), `factory-cli.py`, and
`factory-profile-runtime.py`. The target and any existing baseline entry must be
regular blobs with their expected mode; an absent baseline entry permits only
the same exact reviewed installed/target pair. A changed installed or target blob, deletion, or
mode/type change requires a separate review. These pairs do not update host
files, adopt a compatibility baseline, or bypass the existing exact-preview
approval, stale-snapshot checks, cold backup, or execution fences.

Before stopping services, the host requires no active/unknown attempts, no
unreleased execution lease and no unfinished bridge jobs. An expired lease or
historical cancellation is not evidence of executor containment. A short SQLite
writer barrier closes the check/stop race; it does not update ledger rows. The
executor and dispatch workers stop while that barrier is held. Then the existing
production wrapper cold-backs up all seven volumes, records the new image pin,
starts services and checks all configured health checks and resource provenance.
The gateway, journals, keys and ownership holds are untouched.

Blocked preflight leaves running services alone. A failure after quiescing is
latched in the private deployment status and requires operator recovery. There
is **no automatic database restore, blind image rollback, or repeated restart**:
external effects and token renewal may have advanced. Preserve the cold backup
and recover through the production runbook. Backups are retained; deployment
stops when less than 12 GiB is free. No automatic backup deletion is authorized.

## One-time host setup

Install the reviewed scripts `deploy/factory-auto-deploy.py` and
`deploy/factory-deploy-ssh.py` as root-owned executable files at
`/usr/local/libexec/factory-auto-deploy` and
`/usr/local/libexec/factory-deploy-ssh`. Python 3.11+, Docker Compose and an
authenticated host `gh` CLI with read access to this repository's Actions
artifacts are required. The SSH caller never receives that host credential.
Install the shared policy module `deploy/auto_deploy_artifact.py` alongside them
at `/usr/local/libexec/auto_deploy_artifact.py`, root-owned mode 0644. It is part
of the reviewed host installation, not imported from a downloaded artifact.

Create a locked-password `factory-deploy` account with no Docker group membership
and a root-owned home/authorized-keys file. Generate a dedicated Ed25519 key; its
authorized-keys entry must use:

```text
restrict,command="/usr/local/libexec/factory-deploy-ssh" ssh-ed25519 <public-key>
```

Grant only this command through a root-owned, mode-0440 sudoers file (validate
with `visudo -cf` before installing):

```text
factory-deploy ALL=(root) NOPASSWD: /usr/local/libexec/factory-auto-deploy
```

The helper accepts only `status` or `deploy RUN_ID FULL_SHA`; the privileged
entrypoint repeats validation. No client-supplied paths, repository, command,
environment or image are accepted. Use the existing SSH endpoint; do not expose
the console, databases or gateway publicly. Pin the host's actual SSH public key
out-of-band; never turn off host-key verification or trust runtime `ssh-keyscan`.

Create `/etc/driftward/factory-auto-deploy.json`, root-owned mode 0600:

```json
{
  "enabled": false,
  "installation": "/srv/factory-candidate",
  "production_bundle": "/srv/factory-bundles/4e032de-telemetry-support/deploy",
  "work_directory": "/srv/factory-auto-deploy",
  "compatibility": "<reviewed protected-tree SHA-256>"
}
```

The work directory must be root-owned mode 0700. Compute the fingerprint using
`auto_deploy_artifact.compatibility()` from an exact reviewed checkout; confirm
that its protected runtime surfaces match the already installed components.
The current VPS's Compose image and separately installed gateway can have
different revisions. Do not substitute the retired `/docker/driftward/factory`
installation or a different source bundle.

Configure repository variables `FACTORY_DEPLOY_HOST`, `FACTORY_DEPLOY_USER`,
`FACTORY_DEPLOY_KNOWN_HOSTS` (the pinned public host-key line), and
`FACTORY_AUTO_DEPLOY_ENABLED`. Store the deployment-only private key in
`FACTORY_DEPLOY_SSH_KEY`. Do not store application or root SSH credentials there.
Keep the repository toggle and host `enabled` flag false until the scripts,
policy baseline, SSH forced-command restriction, negative cases and safety gates
have been verified. Environment/branch protection can be added when the GitHub
plan supports it; host authorization checks do not depend on those features.

### Migrating an existing archive deployment

This is a reviewed host compatibility change, not a routine image rollout.
Format-1 image archives are deliberately rejected by the new helper, and the old
helper cannot consume format-2 metadata. Before enabling requests, install the
reviewed helper/shared policy together, validate the ephemeral credential path
through the forced command and sudo, confirm Docker supports `image push
--platform` in CI and private digest pulls on the host, and approve the new
protected-tree baseline against the actual installed runtime. Verify the new
package is private, has the expected identity/provenance described above, and is readable by its deployment
workflow token without changing the host's existing credentials. Keep any
operator execution hold in effect; this change does not authorize a live rollout.

The tiny metadata artifact still uses Actions storage. If legacy image archives
have exhausted its quota, publication alone cannot make a run deployable: wait
for an approved cleanup/expiry and a successful metadata upload before activation.
No deletion or retention-policy change is included here. GHCR versions and local
images are not automatically pruned; retain current/previous pins for recovery.
Pending operator snapshot work (PR #79) uses the old archive/get-image interface;
it must acquire equivalent short-lived package credentials and adopt this
manifest/run-attempt contract before integration. No archive fallback is allowed.

## Operation

- In GitHub Actions, `Factory Deploy` is the operator-facing deployment result.
- `status` over the restricted SSH key checks connectivity/configuration only;
  it does not claim a release is healthy or restart anything.
- `/srv/factory-auto-deploy/status.json` records the private rollout phase;
  `backup-<commit>-<run-id>/` retains the cold backup. Never publish its contents.
- Active/ambiguous work requires the existing reviewed recovery procedure. Once
  resolved, rerun the deploy workflow job if that SHA is still current `main`.
  If retrying `Hatchet Quality`, rerun **all jobs**: retrying only a failed browser
  job advances GitHub's run attempt without republishing the validation job's
  metadata. The host intentionally refuses that stale producer attempt.
  New commits trigger new checks; blocked runs are not retried in a hidden loop.
- Disable `FACTORY_AUTO_DEPLOY_ENABLED` **and** host `enabled` to suspend future
  requests. Revoking the dedicated authorized key immediately removes ingress.
  Neither action interrupts a rollout already in progress.

Acceptance of a merge is authorization for routine, compatible image deployment,
not permission to clear held issues, migrate state or change executor identity.
