# Contributing to Factory

Read [README.md](../README.md) and [AGENTS.md](../AGENTS.md) first, then the
README and agent instructions for the area you change. Use a clean branch or
isolated checkout; preserve existing work and keep runtime state out of Git.

## Ownership and entry points

Factory owns the operator CLI, Hatchet workflows and workers, event ingress,
bridge ledgers, lifecycle writer, contracts, desired state, console and Compose
deployment. Installation operators supply host prerequisites and protected
secret files. The separately provisioned Hermes runtime owns its execution credentials,
sessions, MCP and browser state.

The current portable installation is dry-run only; real execution requires the
separately provisioned execution runtime. Packaging does not authorize real
execution or migration of shared Hermes data. Workers receive minimal Hatchet/bridge
configuration and private gateway access: never executor data, provider
credentials, host root mounts or the Docker socket. See the
[portable guide](../deploy/PORTABLE.md) and [deployment docs](../deploy/README.md).

For an installed system, use the canonical `factory` binding documented in
[deploy/CLI.md](../deploy/CLI.md); it follows the active installation and image.
Start with `factory version`, `factory status`, and `factory help` to discover
supported commands. A source checkout is not an instruction to reinstall host
tooling or operate a live deployment.

Factory's managed execution profile pins an engineering plugin and instruction
snapshots; see [profile configuration](../deploy/CLI.md). This integration is
separate from building or installing the portable dry-run profile. Changing its
pin or activating profiles requires a reviewed operational change.

## Integration portability

The portable dry-run installation is independent of a source checkout. Real
execution integrations still contain installation-specific assumptions:

- Linear project IDs and repository policies in the routing registry.
- Linear workspace/team identity checks, the `ENG-` issue-key validation, and
  console issue-link construction.
- Repository and App allowlists in execution and source-review helpers.
- The externally provisioned Hermes wrapper, profile plugin, and instruction pins.

Review these against the source and your own installation before enabling real
work. Supplying credentials alone does not adapt these policies. Preserve
identity checks and execution gates when changing them; documentation examples
do not override the deployed policy.

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
run, and any limitations or operational follow-up. Include relevant documentation
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

Release publication tests and publishes versioned images to
`ghcr.io/driftward-llc/factory`, together with a digest-pinned installation
bundle. Installations choose the release they consume. Managed
[release deployment](automatic-deployment.md) runs on the installation host and
retains image provenance, backup, quiescence, and compatibility checks.
Audited release approval/application and compatibility baseline adoption remain
distinct operations documented in [deploy/CLI.md](../deploy/CLI.md). A release
approval does not adopt a baseline, and image rollout never upgrades its
own host helpers. Do not copy a failed fingerprint to bypass review or
substitute the legacy host installer.

Merging compatible code does not authorize clearing execution holds, migrating
state, changing executor identity or enabling real execution. Use the relevant
[release runbook](factory-release-runbook.md), deployment and recovery docs for
separately authorized operations.
