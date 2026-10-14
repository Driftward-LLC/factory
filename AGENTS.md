# Driftward Factory Operations

This repository owns the Driftward Factory product/runtime surface extracted
from `Driftward-LLC/driftward-host`.

## Orientation

Read `README.md` first for the current repo overview, layout, deployment
model, and common commands. Use this file for agent-specific boundaries, safety
rules, and validation expectations.

For console UI work, read `DESIGN.md` for Factory's operator priorities, visual
conventions, responsive behavior, and design validation expectations.

For shared components and assets, read `docs/design-system.md`. Reuse the
`@driftward/ui` workspace in `factory/console/ui/`. Inspect its Storybook docs
and stories before adding UI: use `docs-list` and `docs-show` through the local
Storybook MCP endpoint when connected, or read the component source and stories
directly. Verify properties rather than inventing them. Add representative
stories for new components/states and run both console and catalog checks.

## Boundaries

- `factory/`: operator CLI and front-door documentation.
- `hatchet/`: Hatchet control plane source, workers, event ingress, bridge,
  lifecycle writer, desired state, and deployment helpers.
- `deploy/`: Docker image, Compose runtime, systemd wrapper, smoke test, and
  container deployment docs.
- `docs/`: Factory boundary, Hatchet/Hermes control-plane notes, and service
  runbooks.
- `tools/`: checked-in validation and contract tooling.

Factory deploys through Docker Compose from this repository. The Compose app
owns Hatchet Lite/Postgres plus Factory bridge, ingress, worker, lifecycle,
Linear token-renewal, and operator services. `driftward-host` remains
responsible for host-level bootstrap, shared VPS tooling, root Codex state,
Docker availability, host package installs, and secret-file placement.
Driftward Hermes remains responsible for agent execution, credentials, MCP,
browser state, and Hermes session retention.

## Safety

- Do not commit secrets, tokens, webhook URLs, cookies, Hatchet profile files,
  runtime SQLite/PostgreSQL data, or Hermes session material.
- Keep Hatchet workers credential-minimal. Workers may use Hatchet client
  settings and bridge-local credentials; they must not receive provider,
  GitHub, Linear, Browserbase, MCP, or Hermes credentials.
- Do not mount Driftward Hermes data, provider credentials, browser state, root
  GitHub auth, the Docker socket, or the host root filesystem into Factory
  containers.
- The opt-in UI-verification executor is a separate credential boundary: only
  its Codex role may receive a newly provisioned verifier API credential. Its
  preview, browser and checker roles never receive model/provider credentials.
  Do not import interactive Codex or Hermes auth, or treat pilot observations
  as Factory verification receipts. See `deploy/UI-VERIFICATION.md`.
- `factory doctor hatchet` is read-only. Pause/remove/apply paths for live
  Hatchet workflows require explicit reviewed operator approval.
- Lifecycle dead-letter archive must use the root-only operator preview and
  snapshot digest. Retain the audited row; do not delete or edit outbox data
  directly from the browser console.
- Historical bridge, ingress, lifecycle, and Hatchet records are not migrated
  by source-copy changes. Migrate records only after the standalone deployment
  path is approved.

## Validation

Before changing Factory contracts, workers, bridge, ingress, lifecycle writer,
or CLI behavior, run the focused checks that fit the touched area. Useful
defaults:

```bash
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer python3 -m unittest discover -s hatchet/bridge/tests
PYTHONPATH=hatchet/event-ingress python3 -m unittest discover -s hatchet/event-ingress/tests
PYTHONPATH=hatchet/factory-lifecycle-writer python3 -m unittest discover -s hatchet/factory-lifecycle-writer/tests
```
