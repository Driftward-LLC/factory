# Portable Factory: first Linux milestone

## Optional execution telemetry

Telemetry stays off unless configured explicitly. Both `factory-portable.py`
and `factory-production.py` support the same `telemetry` command. Use the wrapper
that owns the installation; enabling telemetry does not authorize real execution.
The optional Collector is another service in the same Compose application, not
another operator-managed deployment. It needs no durable volume.

1. Pull the reviewed Collector image before maintenance:
   `docker pull otel/opentelemetry-collector-contrib@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1`.
2. Provision a private 0600 generated dotenv file outside the installation with
   only `POSTHOG_TRACES_ENDPOINT='https://us.i.posthog.com/i/v1/traces'` (or EU)
   and `POSTHOG_PROJECT_TOKEN='...'`. Never copy unrelated executor credentials.
3. Take a new cold snapshot with the matching wrapper's `backup PATH --apply`.
   This stops services. Check active execution before approving this outage.
4. Preview, then apply:
   `python3 deploy/factory-production.py --directory /srv/factory-candidate telemetry --snapshot /srv/NEW-SNAPSHOT --credentials /etc/driftward/factory-otel-collector.env --port 4318 [--apply]`.
   The port is loopback-only. Configure `--paused` initially if dashboard or
   matching gateway deployment acceptance is still pending.
5. Run `preview` (production), then resume via `activate --approve-effects --apply`
   (production) or `up` (portable). Deploy the matching host gateway separately;
   it receives only `FACTORY_OTEL_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces`,
   never a PostHog key. Verify synthetic ingestion and dashboard readback before
   claiming end-to-end coverage. See [tracing](../docs/factory-opentelemetry.md).

For subsequent enable/pause operations omit `--credentials`, take another cold
snapshot, and use the same command with/without `--paused` and the desired port.
Pausing removes application exporters on recreation; it retains the Collector
and its credential in backup inventory. Disable the independent gateway's
endpoint too. Do not restore databases or replay attempts to turn tracing off.
Credential rotation and uninstall are separate reviewed operations.

Snapshots include the private Collector env, exact image ID, and both deployment
file hashes. Restore uses the same bundle, leaves services stopped, and sets the
Collector loopback port to `4318 + --port-offset`. Never resume a retired source
after token refresh or other external effects without reconciliation. A crash
between credential creation and marker replacement fails closed; retain the cold
snapshot and review the pending marker/extra credential before retrying.

Local acceptance (synthetic key; exporters paused, no PostHog calls):

```bash
python3 tools/check_factory_portable.py --image driftward/factory:local --telemetry
```

## Base installation

This release installs the existing Factory control plane on a fresh Linux
Docker host. It does not depend on this VPS's source checkout, `/etc/driftward`,
host Node/npm, systemd, Hatchet CLI profile, or Hermes socket. It is deliberately
**dry-run only**: real agent execution and Linear lifecycle delivery are not
enabled by this profile. The existing VPS installer remains the migration and
real-execution deployment path; do not point this bootstrap at its state.

Moving the existing Compose deployment is a separate workflow: see
[same-host cutover preparation](CUTOVER.md). Its explicit production overlay
does not change this guide's dry-run defaults or authorize starting a migrated
candidate alongside its source.

## Requirements and boundaries

- Linux amd64, Docker Engine, Docker Compose 2.24.4 or newer, Python 3.11+.
- Access to the private `ghcr.io/driftward-llc/factory` image (authenticate Docker
  with your own package-read identity). No model/provider credentials needed.
- Capacity for PostgreSQL, Hatchet and Factory services; use a separate test
  host or distinct project name and loopback ports when validating.
- A private installation directory on a local filesystem and a unique
  `factory` or `factory-NAME` project. Do not use shared/NFS SQLite storage.

Only Hatchet's dashboard and the Factory console are published, on loopback.
The console still requires Tailscale Serve's authenticated identity header.
Connect it through your own trusted Tailscale Serve deployment; do not publish
it directly, spoof identity headers, or disable authentication for deployment.
The optional Linear proxy is not started by this profile.

Factory containers never receive the Docker socket, host repository mounts,
Hermes state, provider credentials, or root GitHub authentication. The operator
socket stays inside a named volume; privileged remote operator mutations are
not part of this milestone.

## Install a release

Download the `factory-VERSION-linux-amd64.tar.gz` release asset and `SHA256SUMS`
from the same trusted GitHub release. Verify before extracting:

```bash
sha256sum --check SHA256SUMS
tar -xzf factory-VERSION-linux-amd64.tar.gz
cd factory-VERSION-linux-amd64
python3 deploy/factory-portable.py --directory /srv/factory init --project factory
python3 deploy/factory-portable.py --directory /srv/factory init --project factory --apply
python3 deploy/factory-portable.py --directory /srv/factory pull
python3 deploy/factory-portable.py --directory /srv/factory up
python3 deploy/factory-portable.py --directory /srv/factory status
```

The parent `/srv` must exist and be writable by your operator. Use another
absolute directory if appropriate. The first `init` is a preview. `--apply`
creates mode-0700 directories and mode-0600 configuration without printing
secrets. Repeating the same initialization preserves identities and secrets;
foreign, incomplete, changed-image, or symlinked installations are rejected.
Use `--port-offset 20000` on both init calls to run beside another installation.
The `factory` project has its own volumes and network; it does not rename or
reuse the existing `driftward-factory` deployment. Resource ownership checks
still reject foreign resources with an exact target name.

Release metadata selects a digest-pinned image. For an explicitly local source
build only, pass `init --image driftward/factory:my-test-tag`; deployments do not
build source. Validation-only release artifacts are rejected.

`up` stops only this named project before migrations. It uses the pinned
Hatchet image's native key generator, database bootstrap and tenant token
command. Factory's own bridge and ingress initializers then run before workers.
Partial keysets are never regenerated, and existing worker tokens are retained.
The native Hatchet token command defaults to a 90-day token lifetime. `up`
does not rotate an existing token; expiry requires an explicit credential
rotation procedure before long-lived production use.
If bootstrap fails, configuration and volumes remain for diagnosis; there is
no automatic deletion or destructive retry.

Hatchet administrator credentials live in the private installation `.env`.
Never paste that file, raw Compose configuration, keys, or token files into
logs or issue reports. Tokens are isolated in per-service env files; consumers
receive only the credentials needed by their existing responsibilities.

## Operate and recover

```bash
python3 deploy/factory-portable.py --directory /srv/factory logs intake-worker
python3 deploy/factory-portable.py --directory /srv/factory stop
python3 deploy/factory-portable.py --directory /srv/factory up
```

`stop` preserves every volume. Status checks this deployment's containers and
health checks, not the original VPS's systemd units or Hermes dashboard. This
is service health, not proof of external integration readiness. Log output can
contain work-item data; review before sharing.

Cold backup and restore use the portable command's `backup` and `restore`
subcommands. Preview first, then pass `--apply`. Backup stops this project and
leaves it stopped so the database copies form a coherent recovery point.
Resume explicitly with `up` after a successful backup. Snapshots include
credentials and encryption keys: keep them private, encrypt them before
off-host transfer, and control retention separately.

```bash
python3 deploy/factory-portable.py --directory /srv/factory backup /srv/factory-snapshot
python3 deploy/factory-portable.py --directory /srv/factory backup /srv/factory-snapshot --apply
python3 deploy/factory-portable.py --directory /srv/factory-restored restore /srv/factory-snapshot --project factory-restored --port-offset 1000
python3 deploy/factory-portable.py --directory /srv/factory-restored restore /srv/factory-snapshot --project factory-restored --port-offset 1000 --apply
python3 deploy/factory-portable.py --directory /srv/factory-restored up
```

Restore is only into a new directory and new Compose namespace. Existing
volumes are never overwritten. The snapshot verifies file checksums and exact
image identities; it is a same-version disaster-recovery mechanism, not an
arbitrary database downgrade. Restore leaves services stopped. Never run a
restored real-execution deployment alongside its original executor authority.

## Validation and remaining work

The disposable acceptance check is `python3 tools/check_factory_portable.py
--image driftward/factory:ci` from a source checkout after building that image.
It owns an isolated project and cleans up only its own test resources. It must
not receive production credentials or target existing installations.

This milestone does not claim Docker Desktop/ARM64, multi-tenancy, generic
organization policy, a packaged remote Hermes runtime, or independently
resumable remote execution. Keep those as explicit subsequent acceptance gates.
TCP gateway authentication is hardened now, but real remote execution still
needs durable invocation/result lookup and a documented execution envelope.
Do not enable real execution by removing the dry-run guard.
