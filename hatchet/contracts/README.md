# Hatchet Contracts

`events.json` is the canonical contract for Factory-owned Hatchet event IDs,
wire names, default decisions, reason codes, bridge lookup statuses, and simple
provider action mappings.

Use readable IDs in code and analytics:

```text
github_pr_closed_merged
linear_issue_ready_for_implementation
```

Keep `driftward.*` prefixes on Hatchet wire names:

```text
driftward.github.pr.closed_merged
```

The wire name is the external Hatchet routing value. Treat it as effectively
immutable once deployed.

## Update Flow

Edit:

```bash
hatchet/contracts/events.json
```

Regenerate constants:

```bash
python3 tools/hatchet-contracts.py generate
```

Check contract validity and generated-file freshness:

```bash
python3 tools/hatchet-contracts.py check
```

Generated files:

```text
hatchet/event-ingress/hatchet_event_ingress/contracts.py
hatchet/workers/shared/src/contracts.ts
```

Do not edit generated files by hand.
