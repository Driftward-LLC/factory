# Driftward Factory

Standalone source repository for the Driftward Factory app and Hatchet-backed
runtime.

This repo was seeded from the Factory-owned surface that previously lived in
`Driftward-LLC/driftward-host`. It owns the operator CLI, Hatchet workflow and
worker source, event ingress, Hatchet-to-Hermes bridge, lifecycle writer,
contracts, desired-state manifest, and drift tooling.

Historical bridge, ingress, lifecycle, and Hatchet control-plane records are
intentionally not copied into the repository. They should be migrated during
the standalone deployment cutover.

## Layout

- `factory/`: operator-facing CLI.
- `hatchet/`: control plane, bridge, workers, event ingress, lifecycle writer,
  systemd units, and desired-state manifest.
- `docs/`: architecture and deployment boundary docs.
- `tools/`: contract generation, doctor checks, and repo-level tests.

## Common Commands

```bash
factory status
factory doctor hatchet --profile driftward
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
```

Install or refresh the managed Hatchet runtime from this repository:

```bash
bash hatchet/install.sh
```

The installer publishes `/usr/local/bin/factory`,
`/usr/local/bin/driftward-hatchet-smoke`, and the non-secret desired-state
assets under `/usr/local/share/driftward-factory`.
