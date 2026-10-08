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

For a fresh Linux/amd64 dry-run installation, use the image-pinned release
bundle and [portable installation guide](deploy/PORTABLE.md). This path creates
isolated configuration and state without the VPS's systemd, checkout, or
Hermes socket. The host installer below remains the existing deployment's
migration/real-execution path; it is not greenfield bootstrap.

For a staged move from the existing Compose runtime into a new isolated
installation, use the [same-host cutover preparation guide](deploy/CUTOVER.md).
Its default is read-only preview; an approved cold copy leaves the candidate
stopped and does not switch live traffic.

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
npm ci --prefix factory/console
npm run build --prefix factory/console
npm run storybook --prefix factory/console
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

For the configured production candidate, see
[automatic deployment after successful main CI](docs/automatic-deployment.md).
This image-only path preserves the existing deployment wrapper and stops for
compatibility changes or active/unresolved execution; it never uses the retired
host installer to update the production candidate.

See [docs/factory-analytics.md](docs/factory-analytics.md) for the analytics
cohort, event definitions, coverage rules, privacy boundary, and known evidence
gaps.

See
[docs/execution-backend-evaluation.md](docs/execution-backend-evaluation.md)
for the Hermes-versus-direct-Codex measurement and canary design.

See [docs/factory-work-evidence.md](docs/factory-work-evidence.md) for immutable
execution manifests, checkpoint receipts, and explicit replay-data gaps.

See [docs/factory-capability-dashboard-plan.md](docs/factory-capability-dashboard-plan.md)
for the proposed capability roadmap and first operator-dashboard implementation slice.

See [DESIGN.md](DESIGN.md) for the operator interface's design direction and checks.

See [docs/design-system.md](docs/design-system.md) for the shared `@driftward/ui`
React library, Storybook catalog, component and asset documentation, and agent MCP access.

The Docker runtime lives under `/docker/driftward/factory` and is supervised by
`driftward-factory.service`. The older direct host/systemd installers remain in
tree for migration and recovery work, but Docker is the standalone Factory app
deployment path.
