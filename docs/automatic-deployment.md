# Automatic production deployment

`Hatchet Quality` uploads the exact Docker image it tested, only for pushes to
`main`. `Factory Deploy` runs after that **entire** workflow succeeds (including
browser checks). It requests an image-only rollout through a deployment-only SSH
account. No general-purpose GitHub runner, application credentials, or Docker
socket is added to Factory containers. Hermes stays running and is not the
deployment scheduler; GitHub Actions owns this deterministic release sequence.

The host independently verifies the repository, successful workflow identity,
push event, branch, current `main` SHA, immutable artifact digest, image archive
checksum, OCI revision and platform. It loads an image saved by ID, not a mutable
production tag. Old/superseded revisions cannot be deployed. GitHub concurrency
and the existing installation/tenant locks serialize deployments with operators.
The protected-tree fingerprint is independently computed from GitHub's complete
source tree using the root-installed policy, before downloading the artifact.
Changing the CI packager cannot approve its own policy changes.

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

## Operation

- In GitHub Actions, `Factory Deploy` is the operator-facing deployment result.
- `status` over the restricted SSH key checks connectivity/configuration only;
  it does not claim a release is healthy or restart anything.
- `/srv/factory-auto-deploy/status.json` records the private rollout phase;
  `backup-<commit>-<run-id>/` retains the cold backup. Never publish its contents.
- Active/ambiguous work requires the existing reviewed recovery procedure. Once
  resolved, rerun the deploy workflow job if that SHA is still current `main`.
  New commits trigger new checks; blocked runs are not retried in a hidden loop.
- Disable `FACTORY_AUTO_DEPLOY_ENABLED` **and** host `enabled` to suspend future
  requests. Revoking the dedicated authorized key immediately removes ingress.
  Neither action interrupts a rollout already in progress.

Acceptance of a merge is authorization for routine, compatible image deployment,
not permission to clear held issues, migrate state or change executor identity.
