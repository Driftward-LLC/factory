# Driftward Factory Operations

This repository owns the Driftward Factory product/runtime surface extracted
from `Driftward-LLC/driftward-host`.

## Boundaries

- `factory/`: operator CLI and front-door documentation.
- `hatchet/`: Hatchet control plane source, workers, event ingress, bridge,
  lifecycle writer, desired state, and deployment helpers.
- `docs/`: Factory boundary, Hatchet/Hermes control-plane notes, and service
  runbooks.
- `tools/`: checked-in validation and contract tooling.

`driftward-host` remains responsible for host-level bootstrap, shared VPS
tooling, root Codex state, host package installs, and secret-file placement.
Driftward Hermes remains responsible for agent execution, credentials, MCP,
browser state, and Hermes session retention.

## Safety

- Do not commit secrets, tokens, webhook URLs, cookies, Hatchet profile files,
  runtime SQLite/PostgreSQL data, or Hermes session material.
- Keep Hatchet workers credential-minimal. Workers may use Hatchet client
  settings and bridge-local credentials; they must not receive provider,
  GitHub, Linear, Browserbase, MCP, or Hermes credentials.
- `factory doctor hatchet` is read-only. Pause/remove/apply paths for live
  Hatchet workflows require explicit reviewed operator approval.
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
