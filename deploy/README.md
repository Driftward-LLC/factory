# Factory Docker Deployment

Factory should deploy as a Docker Compose application owned by this repository.
The host keeps Docker, the Compose wrapper, systemd supervision, and protected
env files; the Factory Compose app owns Hatchet Lite/Postgres plus the CLI,
Hatchet workers, bridge, ingress, lifecycle writer, Linear token renewer,
manifests, and checked-in runtime code.

## Runtime

- Scope: service-scoped Driftward runtime.
- Source: `/root/driftward/repos/factory`.
- Runtime root: `/docker/driftward/factory`.
- Compose file: `/docker/driftward/factory/docker-compose.yml`.
- Systemd wrapper: `driftward-factory.service`, ordered after
  `driftward-hermes.service` and `driftward-hermes-factory-gateway.service`.
- Hermes gateway: `driftward-hermes-factory-gateway.service`, listening on
  `unix:///run/driftward-hermes-factory/hermes.sock`.
- Durable data lives in Docker named volumes:
  - `driftward-factory-hatchet-postgres-data`
  - `driftward-factory-hatchet-config-data`
  - `driftward-factory-bridge-data`
  - `driftward-factory-ingress-data`
  - `driftward-factory-lifecycle-data`
  - `driftward-factory-linear-token-data`
- Hatchet key material: `/docker/driftward/factory/keys/hatchet`
- Local operator socket: `/run/driftward-factory-operator/operator.sock`.
- Local Factory Console: `http://127.0.0.1:19881/`.
- Local Hermes gateway socket: `/run/driftward-hermes-factory/hermes.sock`.

## Boundaries

Factory containers must not mount Driftward Hermes data, provider credentials,
browser state, root GitHub auth, the Docker socket, or the host root filesystem.
Hatchet Lite and its Postgres database are part of the Factory Compose app.
Real agent execution still belongs to Driftward Hermes. The host systemd unit
orders Factory after Hermes, but Dockerized Factory records and fences work
through the bridge, then reaches Hermes only through an explicit approved
gRPC gateway. The Factory Compose app mounts only
`/run/driftward-hermes-factory`; it must not mount the Docker socket, Hermes
data, provider credentials, browser state, or host root filesystem.
The retry operator also mounts Factory ingress state read-only so it can fence
manual retry eligibility against the latest trusted Linear observation.

The Docker worker defaults are safe for source deployment and dry-run intake
when protected env files are absent:

```text
HATCHET_INTAKE_DRY_RUN defaults to true
DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED defaults to disabled
```

Real Docker effects use the host-local Hermes Factory gateway. The installer
seeds `DRIFTWARD_HERMES_GRPC_TARGET` in
`/etc/driftward/hatchet-worker-bridge-executor.env` when absent:

```text
DRIFTWARD_HERMES_GRPC_TARGET=unix:///run/driftward-hermes-factory/hermes.sock
```

The gateway may bind TCP later by setting `DRIFTWARD_HERMES_GRPC_TARGET` or
`DRIFTWARD_HERMES_GRPC_BIND_TARGET` plus `DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1`
and either TLS or `DRIFTWARD_HERMES_GRPC_AUTH_TOKEN`. Do not solve Factory to
Hermes access by copying Hermes credentials or session state into Factory.
The gateway is bundled during install and requires a host Node.js 18+ binary;
override the default `/usr/bin/node` with `DRIFTWARD_FACTORY_NODE_BIN` if the
host keeps Node elsewhere.

## Required Env Files

The Compose app reads existing protected env files under `/etc/driftward`:

```text
/etc/driftward/hatchet-workers.env
/etc/driftward/hatchet.env
/etc/driftward/hatchet-bridge.env
/etc/driftward/hatchet-verifier.env
/etc/driftward/hatchet-worker-bridge-executor.env
/etc/driftward/hatchet-worker-reconciliation.env
/etc/driftward/hatchet-event-ingress.env
/etc/driftward/factory-operator.env
/etc/driftward/factory-lifecycle-writer.env
/etc/driftward/factory-linear-token.env
```

Keep those files mode `0600`. Do not commit them or paste their values.
`/etc/driftward/factory-linear-token.env` should contain the Factory-owned
Linear OAuth client values as `FACTORY_LINEAR_CLIENT_ID` and
`FACTORY_LINEAR_CLIENT_SECRET`. Do not mount root's `/root/.config/linear`
credential directory into Factory containers.

The Compose app also needs migrated Hatchet key material under
`/docker/driftward/factory/keys/hatchet`. The private and master keys are
runtime secrets and must not be checked into this repository.

## Cutover Gate

This PR does not migrate historical records. Before starting the standalone
Factory runtime, migrate existing Hatchet state into Docker named volumes and
key material into the runtime root:

```text
driftward-factory-hatchet-postgres-data
driftward-factory-hatchet-config-data
driftward-factory-bridge-data
driftward-factory-ingress-data
driftward-factory-lifecycle-data
driftward-factory-linear-token-data
/docker/driftward/factory/keys/hatchet
```

Legacy host-bind state under `/docker/driftward/factory/data/*`, if present,
is a migration source or archive only. It is not the live Docker runtime store.

Then stop the legacy host-managed Hatchet unit:

```bash
systemctl disable --now driftward-hatchet.service
```

`deploy/install-docker.sh --start` fails closed while
`driftward-hatchet.service` is active or the migrated Hatchet DB/key material is
missing. If legacy bridge, ingress, or lifecycle SQLite files exist under the
old host-bind path, start also fails until matching files exist inside the
Docker volumes. That prevents accidentally starting an empty Factory control
plane on the same ports and ledgers.

## Release Deployment

Factory releases are source-built Docker image tags. Create the release tag and
GitHub release with the manual GitHub Actions workflow:

```bash
gh workflow run factory-release.yml -f release_tag=v2026.9.2
run_id="$(gh run list --workflow factory-release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id"
```

After the workflow succeeds, deploy from a clean checkout of the exact release
tag and pass that same tag to the installer:

```bash
git fetch --tags origin
git switch --detach v2026.9.2
bash deploy/install-docker.sh --image-tag v2026.9.2 --start
```

For release-shaped tags such as `v2026.8.26`, the installer verifies that the
checkout is clean and that `HEAD` matches the requested tag before building.
It writes the selected image into
`/etc/systemd/system/driftward-factory.service.d/10-image-tag.conf`, reloads
systemd, builds `driftward/factory:<tag>` with immutable commit/version labels,
and reloads `driftward-factory.service`. Compose recreates only changed
services; unchanged PostgreSQL and Hatchet containers remain running. The
installer retries `factory-docker-smoke` until healthy and restores the
previously running image automatically if validation fails.
After validation succeeds, it disables the retired direct-host worker, bridge,
ingress, lifecycle, token, operator, outbox, and reconciliation units so the
Compose application remains the only active Factory runtime.
The smoke check also calls the retry operator's read-only preview endpoint for
a sentinel ENG issue, so missing ingress mounts fail before a release is
reported healthy.

To pin a full image reference instead of the default `driftward/factory:<tag>`
shape:

```bash
bash deploy/install-docker.sh --image driftward/factory:v2026.8.26 --start
```

Use [../docs/factory-release-runbook.md](../docs/factory-release-runbook.md)
for the full release, deploy, verification, and rollback procedure.

## Install

Install the Compose wrapper without starting it:

```bash
bash deploy/install-docker.sh
```

Install and start the current checkout in one step:

```bash
bash deploy/install-docker.sh --start
```

By default this pins and builds `driftward/factory:local`. Use
`--image-tag TAG` for a named Factory image tag or `--image IMAGE` for a full
image reference.

## Operations

The Factory operator container also serves the read-only Factory Console on
loopback port `19881`. Publish it privately through Tailscale Serve, never
through Funnel or a public listener:

```bash
tailscale serve --bg --https=19881 http://127.0.0.1:19881
factory console
```

The console requires the `Tailscale-User-Login` identity header on every page
and API request; `/health` is the only unauthenticated endpoint. For local
development only, set `FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED=1`.

```bash
systemctl status driftward-factory.service --no-pager
systemctl status driftward-hermes-factory-gateway.service --no-pager
journalctl -u driftward-factory.service -n 100 --no-pager
journalctl -u driftward-hermes-factory-gateway.service -n 100 --no-pager
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory ps
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory logs --tail=100
systemctl reload driftward-factory.service
systemctl restart driftward-hermes-factory-gateway.service
factory-docker-smoke
```

Rollback to the previous release by checking out the prior tag and running
`bash deploy/install-docker.sh --image-tag <tag> --start`. Preserve the
`driftward-factory-*` Docker volumes and `/docker/driftward/factory/keys/*`
unless there is a separately approved record migration or restore plan.

`factory status` treats container OOM state, unexpected restarts, failed Docker
health checks, Hatchet readiness, desired-state drift, release provenance, and
actionable reconciliation findings for active work or system inventory as
first-class signals. Findings attached to terminal jobs are reported
separately as nonfatal reconciliation debt. The ingress health
endpoint is unhealthy while persisted events await Hatchet publication; the
shared outbox dispatcher retries those rows with a lease every 30 seconds.
Lifecycle status reports permanent dead letters by bounded reason code. The
operator mounts only the lifecycle named volume needed for root-only,
snapshot-fenced dead-letter archival; the FastAPI console remains read-only.
Reconciliation runs natively in Hatchet every 15 minutes.
