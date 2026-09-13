# Driftward Factory

Standalone source repository for the Driftward Factory app and Hatchet-backed
runtime.

This repo was seeded from the Factory-owned surface that previously lived in
`Driftward-LLC/driftward-host`. It owns the operator CLI, Hatchet workflow and
worker source, event ingress, Hatchet-to-Hermes bridge, lifecycle writer,
contracts, desired-state manifest, and drift tooling.

Factory deploys through Docker Compose from this repository. The Compose app
owns Hatchet Lite/Postgres plus Factory bridge, ingress, worker, lifecycle,
Linear token-renewal, and operator services. The host should own Docker,
systemd supervision, the runtime root, protected env files, and migrated key
placement. Durable Factory state lives in Docker named volumes.

Historical bridge, ingress, lifecycle, and Hatchet control-plane records are
intentionally not copied into the repository. They should be migrated into the
Factory Docker volumes during the standalone deployment cutover.

## Layout

- `factory/`: operator-facing CLI.
- `hatchet/`: control plane, bridge, workers, event ingress, lifecycle writer,
  systemd units, and desired-state manifest.
- `deploy/`: Docker image, Compose runtime, systemd wrapper, install helper,
  smoke test, and operations docs.
- `docs/`: architecture and deployment boundary docs.
- `tools/`: contract generation, doctor checks, and repo-level tests.

## Common Commands

```bash
factory status
factory console
factory analytics --days 30
factory doctor hatchet --profile prod
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
```

Validate the Docker deployment artifacts:

```bash
HATCHET_POSTGRES_PASSWORD=ci-postgres-password \
  HATCHET_SERVER_AUTH_COOKIE_SECRETS=ci-cookie-secret \
  HATCHET_ADMIN_PASSWORD=ci-admin-password \
  FACTORY_SOURCE_ROOT="$PWD" \
  docker compose -f deploy/docker-compose.yml config >/dev/null
docker build -f deploy/Dockerfile -t driftward/factory:local .
```

Install the Docker runtime wrapper:

```bash
bash deploy/install-docker.sh
```

Deploy a tagged release:

```bash
gh workflow run factory-release.yml -f release_tag=v2026.9.2
run_id="$(gh run list --workflow factory-release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id"
git fetch --tags origin
git switch --detach v2026.9.2
bash deploy/install-docker.sh --image-tag v2026.9.2 --start
git switch main
git pull --ff-only
```

See [docs/factory-release-runbook.md](docs/factory-release-runbook.md) for the
full release, deploy, verification, and rollback procedure.

See [docs/factory-analytics.md](docs/factory-analytics.md) for the analytics
cohort, event definitions, coverage rules, privacy boundary, and known evidence
gaps.

The Docker runtime lives under `/docker/driftward/factory` and is supervised by
`driftward-factory.service`. The older direct host/systemd installers remain in
tree for migration and recovery work, but Docker is the standalone Factory app
deployment path.
