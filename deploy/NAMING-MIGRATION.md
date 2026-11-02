# Naming generation boundary

This release uses the `factory-neutral-v1` naming generation for internal
workflow events, configuration, paths, resource labels, and state namespaces.
Fresh installations record `naming_generation` in `.factory-install.json`.
Installation operations and snapshot restore require that exact generation
before starting containers or importing state.

Existing installations and snapshots without this marker are unsupported by
this bundle. Renaming source strings does not migrate persisted Hatchet
subscriptions, queued events, lifecycle records, database values, or credentials.
Do not add the marker to existing state to bypass this boundary. The historical
cutover helper permits read-only preview, but refuses application before
inspection, snapshot creation, or state mutation with this release. Profile migration likewise requires a source already using
this generation.

For a new deployment, initialize a fresh private installation directory and a
unique Compose project with empty volumes. Configure organization identity and
integrations using `ORGANIZATION.md`, and keep real execution disabled until
explicitly approved. Preserve the old installation, its original deployment
bundle, and backups for rollback; do not attach its volumes to the new project.

Continuing an existing deployment requires a separately reviewed migration
that inventories and translates all persisted namespaces, verifies pending
work and ledger consistency, and documents rollback before marking the output
as this generation. This release provides no such migration. Use the original
bundle to operate or back up the existing installation until that work is
approved.

Historical source/systemd installers are disabled before taking locks, publishing
files, changing permissions, or stopping/starting services. Their renamed unit
inventories cannot establish that an existing installation has no older writers.
This also applies to the source Docker wrapper installer and public webhook
installer. Use the portable installation path for fresh deployments. Re-enabling
a source rollout requires its own reviewed provisioning and migration design;
there is no environment variable or marker override for this refusal.

External executor plugins keep their own verified package identities. Profile
configuration obtains the skill namespace from the pinned plugin archive and
returns `skill_namespace`; it also stores the value in the executor profile.
When binding an executor profile, set `FACTORY_HERMES_SKILL_NAMESPACE` in the
gateway environment to that exact returned namespace. A selected executor
profile refuses to run without a valid explicit namespace. This configuration
contains an external plugin identifier, not credentials.
