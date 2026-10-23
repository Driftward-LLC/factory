# Contributing to Factory

Read [README.md](../README.md) and [AGENTS.md](../AGENTS.md) first, then the
README and agent instructions for the area you change. Use a clean branch or
isolated checkout; preserve existing work and keep runtime state out of Git.

## Ownership and entry points

Factory owns the operator CLI, Hatchet workflows and workers, event ingress,
bridge ledgers, lifecycle writer, contracts, desired state, console and Compose
deployment. `driftward-host` owns host bootstrap, shared tooling and protected
secret placement. Shared Driftward Hermes owns its execution credentials,
sessions, MCP and browser state.

The current portable installation is dry-run only; real execution requires the
separately provisioned execution runtime. Packaging does not authorize real
execution or migration of shared Hermes data. Workers receive minimal Hatchet/bridge
configuration and private gateway access: never executor data, provider
credentials, host root mounts or the Docker socket. See the
[portable guide](../deploy/PORTABLE.md) and [deployment docs](../deploy/README.md).

For an installed system, use the canonical `factory` binding documented in
[deploy/CLI.md](../deploy/CLI.md); it follows the active installation and image.
Start with `factory version`, `factory status`, and `factory help`.
`factory doctor hatchet --profile prod` checks drift read-only. A source checkout
is not an instruction to reinstall host tooling or operate a live deployment.

Engineering plugin behavior is maintained in
[`Driftward-LLC/driftward-engineering`](https://github.com/Driftward-LLC/driftward-engineering),
not a copied host plugin cache. Factory's profile runtime pins its reviewed
plugin release; see [profile configuration](../deploy/CLI.md). Changing that pin
or activating profiles is separate reviewed operational work.

## Develop and validate

Run commands from the repository root. Install only the dependencies needed for
the area you touch, using checked-in lockfiles:

```bash
npm ci --prefix hatchet/workers
npm ci --prefix factory/console
```

Choose focused checks in proportion to the change. Common runtime checks are:

```bash
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer python3 -m unittest discover -s hatchet/bridge/tests
PYTHONPATH=hatchet/event-ingress python3 -m unittest discover -s hatchet/event-ingress/tests
PYTHONPATH=hatchet/bridge:hatchet/factory-lifecycle-writer python3 -m unittest discover -s hatchet/factory-lifecycle-writer/tests
```

Add focused regression coverage for changed behavior. Match relevant worker test
commands and dependency setup to [.github/workflows/hatchet-quality.yml](../.github/workflows/hatchet-quality.yml).
For documentation alone, verify links and commands against source and run
`git diff --check`; runtime rehearsals are unnecessary locally.

For console work, read [DESIGN.md](../DESIGN.md) and the
[design system](design-system.md). Reuse `@driftward/ui`, inspect component
source and stories (or connected Storybook docs), add representative states,
and run the console and catalog checks:

```bash
npm run build --prefix factory/console
npm run check:ui --prefix factory/console
npm run build-storybook --prefix factory/console
```

When changing workflows or events, follow [hatchet/AGENTS.md](../hatchet/AGENTS.md)
and [hatchet/README.md](../hatchet/README.md). Keep worker registration,
`hatchet/workers/catalog.json`, and `hatchet/desired-state/factory-hatchet.json`
consistent. Edit canonical `hatchet/contracts/events.json`, then run
`python3 tools/hatchet-contracts.py generate` and commit its generated outputs.
Preserve ingress signature validation, bridge idempotency, execution fences and
reconciliation after uncertain effects. Source changes never migrate historical
records or authorize live workflow registration, removal or execution.

Use existing PostHog tracing and bounded metadata where relevant. Verify ingestion
by exact trace, session, job or attempt IDs when safe. Telemetry is not lifecycle
or audit truth; never export prompts, outputs or credentials, or enable new
production telemetry as part of an ordinary code change.

## Review, CI and deployment

A PR should explain the concrete problem, resulting behavior, checks actually
run, and any limitations or operational follow-up. Use the canonical
`driftward-engineering:prepare-pr` body template when preparing the PR. Include relevant documentation
and generated files, but no secrets, runtime databases, profiles or session data.
Follow the model routing and review limits supplied in the task's repository
instructions; report implementation and review models when requested.

[Hatchet Quality](../.github/workflows/hatchet-quality.yml) has five blocking gates:

1. **Cheap preflight:** whitespace and pinned, redacted secret scanning.
2. **Code checks:** contracts, builds, unit and transport checks.
3. **Browser and UI checks:** real console behavior and component catalog checks.
4. **Required evidence publication:** both browser evidence uploads and their IDs.
5. **Release readiness:** tested image plus isolated install, restart,
   backup/restore and stopped-state cutover rehearsals.

Code and browser checks run in parallel after preflight; release readiness waits
for all preceding gates. Green browser assertions alone do not satisfy missing
evidence publication. The workflow filters PR paths (currently including
`README.md`, but not `docs/**`); every push to `main` runs the complete graph.

During main's successful release-readiness job, the tested image is published
to private GHCR. After the entire quality workflow succeeds,
[Factory Deploy](automatic-deployment.md) requests the configured image-only
rollout. Routine compatible changes use that automatic path. Protected source
changes stop for operator review: audited release approval/application and
compatibility baseline adoption are distinct operations documented in
[deploy/CLI.md](../deploy/CLI.md). A release approval does not adopt a baseline,
and automatic rollout never upgrades its own host helpers. Do not copy a failed
fingerprint to bypass review or substitute the legacy host installer.

Merging compatible code does not authorize clearing execution holds, migrating
state, changing executor identity or enabling real execution. Use the relevant
[release runbook](factory-release-runbook.md), deployment and recovery docs for
separately authorized operations.
