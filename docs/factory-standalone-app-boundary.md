# Factory Standalone App Boundary

Status: Proposed
Last reviewed: 2026-08-25
Linear: ENG-97

## Decision To Approve

Use the dedicated `Driftward-LLC/factory` repository for the
Factory product and runtime surface. Keep `driftward-host` as the host-level
bootstrap owner for VPS-wide prerequisites, Docker availability, secret-file
placement, and documented recovery commands. The standalone Factory app should
deploy through Docker Compose from this repository.

The checked-in Hatchet desired state is
`hatchet/desired-state/factory-hatchet.json`, and the read-only drift check is
available through:

```bash
factory doctor hatchet --profile driftward
```

The doctor command does not pause, remove, or register workflows. It collects
read-only Hatchet inventory through the CLI, or compares a saved sanitized
inventory snapshot with `--live-json`, and reports unknown workflows, retired
workflows that are still active, unexpected event triggers, and task actions
without active repo-managed workers.

## Proposed Repository Boundary

The standalone Factory repository should own:

- Factory operator CLI behavior and UX.
- Docker image, Compose runtime, systemd wrapper, smoke test, and deploy docs.
- Hatchet workflow and task source.
- Hatchet worker packages, manifests, and worker runbooks.
- Event ingress normalization, routing policy, and event contract files.
- Hatchet-to-Hermes bridge API, ledger schema, recovery tools, and lifecycle
  writer.
- Desired-state manifests for active workflows, task actions, workers, event
  triggers, runtime units, and retired workflow history.
- Doctor/reconcile commands that can run in `--dry-run` mode before any apply.
- Migration scripts for preserving bridge, ingress, Hatchet, and lifecycle
  history.

`driftward-host` should keep owning:

- Root-owned bootstrap, package installs, and shared host CLI publication.
- Docker engine readiness and host-level prerequisites.
- Secret-file paths and file modes, without owning secret contents.
- Runtime root creation under `/docker/driftward/*` and `/var/lib/*` where
  the host is the deployment substrate.
- Cross-service docs that explain how Driftward Hermes, Hatchet, Linear,
  GitHub, and the VPS fit together.

Driftward Hermes should keep owning:

- Agent execution, Codex/OpenAI credentials, MCP connections, browser state,
  GitHub credentials, and Hermes session retention.
- The allowlisted execution actions reached through the bridge.
- Hermes-specific smoke tests such as `driftward-hermes-smoke`.

## Migration Plan

1. Approve the repository name and boundary in this document.
2. Seed `Driftward-LLC/factory` with no secrets, session state, or
   host-local credentials.
3. Move source from these transitional roots into the Factory repository:
   `factory/`, `hatchet/bridge/`, `hatchet/contracts/`,
   `hatchet/event-ingress/`, `hatchet/factory-lifecycle-writer/`, and
   `hatchet/workers/`. This initial source copy is complete; future work should
   happen directly in `Driftward-LLC/factory`.
4. Use the Docker deployment path in `deploy/` as the standalone Factory
   packaging and release path.
5. Publish a versioned desired-state manifest from the Factory repository and
   make `factory doctor hatchet` compare that manifest with live Hatchet state.
6. Run the doctor command in dry-run mode and review every unknown or retired
   active workflow before applying any pause/remove action.
7. Migrate historical state with explicit exports and checksums. Include the
   bridge ledger, ingress ledger, lifecycle outbox state, Hatchet workflow/run
   history, and any future cloud store such as Supabase.
8. Switch host bootstrap to install the Factory app release artifact from this
   repository instead of using incidental source under `driftward-host`.
9. Leave forwarding docs in `driftward-host` that point Factory lifecycle,
   workflow, and worker ownership to the standalone repository.

## Desired State And Drift Rules

Every active Hatchet workflow must appear in
`hatchet/desired-state/factory-hatchet.json` with:

- workflow name, source file, worker, and runbook;
- task action names registered by that workflow;
- event triggers, where the workflow consumes Hatchet events;
- recommended future name when the current name predates naming rules.

Every active task action must be served by at least one active repo-managed
worker named in the manifest. If live Hatchet state shows a task action with no
active worker, or with only workers absent from the manifest, that is drift.

Temporary quickstart and smoke workflows must not remain active in the shared
Hatchet control plane. Known retired workflows are listed in the manifest so a
doctor report can distinguish "known retired but still active" from "unknown
active workflow."

Apply commands that pause or remove workflows and event triggers are out of
scope for this first pass. They need an explicit reviewed apply path that
preserves audit history and names the exact workflow, event trigger, actor,
timestamp, and before/after state.

## Storage Direction

The bridge ledger and ingress ledger are currently SQLite files under
`/var/lib`. Hatchet control-plane history is in Hatchet PostgreSQL. Those are
acceptable for the host-local pilot, but the standalone Factory app should own
the schema and migration plan for durable product/runtime records.

A cloud store such as Supabase is a reasonable later candidate for Factory
records that should survive host replacement or support dashboards, but it is
not just a database swap. Before adopting one, document schema ownership,
backup/restore, access roles, row-level security, local development fixtures,
and rollback.

## First-Pass Acceptance Mapping

- Proposed repo/app boundary: this document.
- Canonical desired state: `hatchet/desired-state/factory-hatchet.json`.
- Unknown live workflows: reported by `factory doctor hatchet`.
- Task actions without active repo-managed workers: reported by
  `factory doctor hatchet`.
- Stale temporary workflows: represented as retired desired-state entries.
- `driftward-host` docs: forwarding docs should point Factory lifecycle,
  workflow, and worker ownership here.
- Secrets and host-local state: migration plan explicitly excludes credentials,
  session material, and secret contents.
