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
  `driftward-hermes.service`.
- Durable data:
  - `/docker/driftward/factory/data/hatchet/postgres`
  - `/docker/driftward/factory/data/hatchet/config`
  - `/docker/driftward/factory/data/bridge`
  - `/docker/driftward/factory/data/ingress`
  - `/docker/driftward/factory/data/lifecycle`
  - `/docker/driftward/factory/data/linear-token`
- Hatchet key material: `/docker/driftward/factory/keys/hatchet`
- Local operator socket: `/run/driftward-factory-operator/operator.sock`.

## Boundaries

Factory containers must not mount Driftward Hermes data, provider credentials,
browser state, root GitHub auth, the Docker socket, or the host root filesystem.
Hatchet Lite and its Postgres database are part of the Factory Compose app.
Real agent execution still belongs to Driftward Hermes. The host systemd unit
orders Factory after Hermes, but Dockerized Factory records and fences work
through the bridge, then reaches Hermes only through an explicit approved
gateway/command.

The Docker Compose default is safe for source deployment and dry-run intake:

```text
HATCHET_INTAKE_DRY_RUN=true
DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED=0
```

Before enabling real effects in Docker, provide a container-safe Hermes gateway
or wrapper and set the `DRIFTWARD_HERMES_*` execution variables explicitly. Do
not solve that by copying Hermes credentials or session state into Factory.

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
Factory runtime, migrate the existing Hatchet Postgres data and key material
from the current Hatchet runtime into:

```text
/docker/driftward/factory/data/hatchet/postgres
/docker/driftward/factory/data/hatchet/config
/docker/driftward/factory/keys/hatchet
```

Then stop the legacy host-managed Hatchet unit:

```bash
systemctl disable --now driftward-hatchet.service
```

`deploy/install-docker.sh --start` fails closed while
`driftward-hatchet.service` is active or the migrated Hatchet DB/key material is
missing. That prevents accidentally starting a second empty Hatchet control
plane on the same ports.

## Install

Install the Compose wrapper without starting it:

```bash
bash deploy/install-docker.sh
```

Build and start through systemd:

```bash
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory build
systemctl enable --now driftward-factory.service
factory-docker-smoke
```

Install and start in one step:

```bash
bash deploy/install-docker.sh --start
```

## Operations

```bash
systemctl status driftward-factory.service --no-pager
journalctl -u driftward-factory.service -n 100 --no-pager
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory ps
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory logs --tail=100
systemctl restart driftward-factory.service
factory-docker-smoke
```

Rollback to the previous image or source commit by checking out the prior
commit in `/root/driftward/repos/factory`, rebuilding the image, and restarting
`driftward-factory.service`. Preserve `/docker/driftward/factory/data/*` unless
there is a separately approved record migration or restore plan.
