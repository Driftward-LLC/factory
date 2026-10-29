# Container operations

Factory installations consume published images. Use the matching release bundle
and [portable installation guide](../deploy/PORTABLE.md) for installation,
status, backup, and restore. The [release runbook](factory-release-runbook.md)
covers upgrades and rollback; the optional
[deployment automation](automatic-deployment.md) remains installation-owned.

## Separate source, configuration, and state

- Keep source checkouts separate from installed release files.
- Keep credentials in protected files outside Git and release assets.
- Keep durable state in the installation's named volumes.
- Keep backups outside the installation and protect them as credentials: they
  include configuration, keys, and runtime records.

Use distinct installation directories, Compose project names, and ports for
independent environments. Never share production volumes with tests or reuse
another installation's credentials to make a test pass.

## Runtime requirements

Use the image digest recorded in the release bundle. Preserve the provided
health checks, restart policies, resource limits, volume ownership, and
loopback network bindings. Record the previous image and a verified backup
before upgrading.

The console requires trusted Tailscale Serve identity; exposing its port
directly bypasses that trust boundary. Expose provider webhooks only through a
narrow proxy with signature and timestamp validation. Administrative and ledger
endpoints must remain private.

Factory workers do not receive executor credentials, executor state, or the
Docker socket. Real execution is configured separately and remains disabled
until an operator explicitly enables it.

## Verification and recovery

After installation or upgrade, use the installation wrapper and `factory status`
to check the selected image, service health, and expected durable state. Verify
the console through its trusted proxy. Test external integrations with bounded,
reviewed work; a running container alone does not prove that credentials or
workflow routing are correct.

Do not print environment files or credential values while investigating a
failure. Preserve logs and receipts, distinguish application health from
execution authority, and use the documented recovery path before retrying work
whose external effects are uncertain.

Do not prune volumes or replace databases as part of an ordinary restart.
Before retiring an installation, stop intake and execution, preserve required
audit records and backups, and verify that no dependent service still uses its
volumes, gateway, or credentials.
