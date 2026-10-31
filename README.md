# Factory

Factory is a self-hosted control plane for agent-assisted software work. It
accepts work requests, schedules workflows with Hatchet, records execution and
review evidence, and gives operators a CLI and web console to inspect progress
and recover interrupted work.

Factory publishes versioned container images at
`ghcr.io/driftward-llc/factory`. Installations pull a selected release and keep
their configuration, credentials, and persistent data outside the repository.

## Install a release

Start with the [portable installation guide](deploy/PORTABLE.md). Release
bundles contain the Compose configuration and installation helper, with the
Factory image pinned by digest. The supported portable profile runs on
Linux/amd64 and starts in **dry-run mode**.

One release image contains Factory, Hatchet Lite, PostgreSQL 15, Hermes, and
OpenTelemetry Collector. Compose runs them in separate containers using that
same image, with separate commands, credentials, and volumes. Fresh portable
installations also start an idle `factory-hermes` container with its own data.
Real agent execution still requires provider authentication, a reviewed gateway
binding, and explicit execution configuration. Installing Factory does not
enable it. Existing production installations retain their recorded database
and Hatchet images until a separate migration is approved.

The console currently requires a trusted Tailscale Serve proxy. GitHub and
Linear integrations also require installation-specific credentials and routing
configuration; the portable profile does not provision those connections.
Some integration policies and Linear links still assume the original workspace.
Real execution in another workspace requires reviewing those source defaults;
see [integration limits](docs/contribution.md#integration-portability).

- [Installation and backup/restore](deploy/PORTABLE.md)
- [CLI setup and operator commands](deploy/CLI.md)
- [Releases, upgrades, and rollback](docs/factory-release-runbook.md)
- [Managed release upgrades](docs/automatic-deployment.md)
- [Moving an existing installation](deploy/CUTOVER.md)

## Use Factory

Once the CLI is bound to an installation:

```bash
factory version
factory status
factory console
factory analytics --days 30
factory help
```

The [operator CLI reference](factory/README.md) covers lifecycle inspection,
retries, review, and recovery. The [architecture boundary](docs/factory-standalone-app-boundary.md)
explains which responsibilities belong to Factory, the host, and the executor.

## Develop

Read the [contribution guide](docs/contribution.md) for dependency setup,
focused checks, and review expectations. Common checks from a source checkout:

```bash
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm ci --prefix hatchet/workers
npm run build --prefix hatchet/workers
npm ci --prefix factory/console
npm run build --prefix factory/console
```

Build a local image with:

```bash
docker build -f deploy/Dockerfile -t factory:local .
```

## Repository layout

- `factory/`: operator CLI, web console, and shared UI components.
- `hatchet/`: workflows, workers, ingress, bridge, lifecycle writer, and contracts.
- `deploy/`: container packaging, Compose configuration, and installation tools.
- `docs/`: architecture, operations, and contribution guides.
- `tools/`: validation, compatibility checks, and tests.

## More documentation

- [Analytics and time measurements](docs/factory-analytics.md)
- [Execution tracing](docs/factory-opentelemetry.md)
- [Work evidence and replay limits](docs/factory-work-evidence.md)
- [Hermes runtime contract](docs/hermes-runtime-contract.md)
- [Execution backend evaluation](docs/execution-backend-evaluation.md)
- [Console design](DESIGN.md) and [component catalog](docs/design-system.md)

## License

Factory is available under the [MIT License](LICENSE).
