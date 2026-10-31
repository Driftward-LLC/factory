# Factory architecture boundary

Factory is distributed as versioned container images and release bundles.
Each installation selects a release and owns its configuration, credentials,
backup policy, and durable state. Publishing a release does not deploy it or
authorize agent execution.

## Factory

This repository owns:

- The operator CLI, web console, and shared UI components.
- Container images, Compose configuration, installation helpers, and runbooks.
- The Hatchet Lite/PostgreSQL topology, workflows, and workers.
- Event normalization, routing policy, and versioned event contracts.
- The execution bridge, effect fencing, ledger schemas, and recovery tools.
- Lifecycle delivery and its dedicated token-renewal service.
- Desired-state manifests, read-only diagnostics, and explicit migration tools.

Durable state belongs to the installation's named Docker volumes, not its image
or source checkout. Replacing an image does not migrate historical records by
itself. Follow the [release runbook](factory-release-runbook.md) for upgrades and
the [cutover guide](../deploy/CUTOVER.md) when moving an existing installation.

## Installation host

The operator supplies Docker, supported host prerequisites, protected secret
files, storage, network exposure, and service supervision. The portable profile
does not require a source checkout or a particular host-management repository.
The optional managed-deployment helpers have their own documented prerequisites.

The console currently trusts Tailscale Serve identity on a loopback-published
listener. Do not expose that listener directly or accept arbitrary identity
headers from the public network.

## Execution runtime

The supported portable profile is dry-run only. Its `factory-hermes` container
uses the same image as the control plane and starts idle with a fresh private
volume. Provider/application authentication and a reviewed gateway binding are
still needed before real execution. Existing production installations retain
their separate Hermes runtime. The gateway exposes the allowlisted operations
used by Factory; packaging does not change its lease or incarnation checks.

Workers receive only the configuration and private gateway access they need.
Never mount provider credentials, executor data, host GitHub authentication,
the host root filesystem, or the Docker socket into Factory workers. See the
[runtime contract](hermes-runtime-contract.md) for profile and cancellation
limits, and [deployment documentation](../deploy/README.md) for host integration.

## Desired state and drift

`hatchet/desired-state/factory-hatchet.json` lists managed workflows, task
actions, workers, triggers, and retired workflows. Inspect a configured control
plane with:

```bash
factory doctor hatchet --profile prod
```

Use a profile configured for the installation; `prod` is the example profile
name. The command is read-only. It reports unknown or retired active workflows,
unexpected triggers, and task actions without active managed workers. It can
also compare a sanitized inventory snapshot supplied through `--live-json`.

Registration, pause, removal, state migration, and real execution require their
explicit operational paths. Preserve the affected identifiers, approval,
before/after state, and recovery evidence when applying changes.
