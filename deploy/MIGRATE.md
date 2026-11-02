# Migrate an existing production installation to the unified image

`profile_migration.py` prepares a **new, stopped project** from a complete cold
backup. It never stops or starts the source installation, changes routing,
updates CLI bindings, enables execution, or adopts a deployment compatibility
baseline. The normal image-only upgrader still refuses this layout transition.

The supported source is the existing `same-host-production` profile with a
standard PostgreSQL 15 cluster (the `hatchet` role, `hatchet` database, default
tablespaces and US English UTF-8 locale). The target must contain identical
Hatchet binaries. A Hatchet version or database schema change needs separate
migration work. This is a host operator procedure; no Docker socket is mounted
into a Factory service.

## Prepare and review

1. Verify the target release, attestations and digest using the release runbook.
   Keep its deployment bundle in a root-protected permanent directory. Pull the
   image by digest and obtain its exact local `sha256:...` image ID. Keep the
   old bundle and images available for rollback.
2. Disable automatic deployment and any host service that could restart the
   source project or its dedicated Hermes container. Verify there are no active
   attempts, unresolved jobs or execution leases. Keep execution paused.
3. Stop the source through its original production wrapper, disable restart on
   its exact reviewed container IDs, and take a **fresh cold backup** through
   that same wrapper. Do not resume it between backup and migration. Stop and
   disable restart on the dedicated Factory Hermes container too. Do not select
   a personal or shared Hermes runtime; the preview names the selected container,
   image and data volume explicitly. It requires service `hermes` in a `factory`
   or `factory-*` Compose project, with a volume named for that project; personal
   and shared legacy Hermes projects are refused.
4. Prepare exactly four private reviewed organization/policy JSON files using
   [ORGANIZATION.md](ORGANIZATION.md). For backups made before deployment-owned
   configuration existed, `--configuration` is required; no identity or execution
   permission is inferred from old source defaults. Existing configured source
   files are preserved unless an explicit replacement directory is supplied.
   Preview using the new bundle. Paths below are installation-specific examples:

   ```bash
   python3 /srv/factory-bundles/NEW/deploy/profile_migration.py \
     --source-bundle /srv/factory-bundles/OLD/deploy \
     --source /srv/factory-current \
     --snapshot /srv/backups/factory-cold \
     --destination /srv/factory-unified \
     --project factory-unified \
     --image sha256:EXACT_LOCAL_IMAGE_ID \
     --configuration /srv/factory-reviewed-config \
     --hermes-container factory-hermes
   ```

Review the preview's source identity, image pins, target, ports, bundle hashes,
backup digest, supplied configuration hashes and dedicated Hermes source. Preview checks all backup hashes and
SQLite integrity/quiescence; it runs only disposable, unmounted, network-disabled
containers to compare Hatchet binaries. It does not start any service.

Repeat that exact command with `--apply --approve-digest PREVIEW_SHA256` after
review. Apply holds source, destination and tenant locks, requires every source
service and the selected Hermes container stopped with restart disabled, and
compares the stopped source volume archives with the backup. A backup made
before a restart is refused even when its configuration is unchanged.

The command copies the SQLite/configuration volumes and dedicated Hermes data
into new owned volumes, checking contents and ownership after copying. Hermes
symlinks are preserved only when their targets stay within `/opt/data` and contain
no `..` path components; links outside that volume, hard links, special files
and entries beneath a symlink are refused. Other state archives continue to
reject all links. It runs
`pg_dumpall` from a network-disabled copy of the old PostgreSQL archive and
restores it with `ON_ERROR_STOP` into a fresh target volume. Rebuilding through a
logical restore accommodates the new image's libc/collation version. Rows,
logical column order and definitions, indexes, constraints, sequences, role
passwords/settings and database settings must match; target collation versions
must be current. Dropped physical column slots and the known equivalent explicit
NULL timestamp partition defaults are normalized for comparison. No Factory
worker or Hermes process is started by the migration.

Original source-container fencing and image history are preserved. A migration
receipt records old and new pins, bundle hashes, supplied configuration hashes, backup and preview digests, the
Hermes archive digest and PostgreSQL verification. Private SQL dumps and evidence
remain under `DESTINATION/migration-evidence/`; these contain credentials and
production data and must never be committed or published.

The obsolete systemd helper `hatchet/factory-hatchet-smoke` now exits without
checking or changing services. Use the Factory-owned `factory-docker-smoke` for
its supported production Compose layout and the portable acceptance harness for
a fresh portable profile. Smoke checks read the configured team identity.

## Markerless naming transition

For an installation predating `factory-neutral-v1`, the same command additionally
requires `--naming-map /PRIVATE/naming-map.json` and
`--gateway-journal-archive /PRIVATE/gateway-journal-cold.tar`. The preserved
bundle validates its own source metadata and credentials; the target bootstrap
is never taught to accept old-generation installations. Preview binds both
bundles, the source backup, supplied organization files, naming-map bytes and
journal archive digest into the approval digest. Changes require a new preview.

The private map has exactly this structure. These are synthetic namespace
examples; supply the exact recorded deployment keys and labels after inspecting
the preserved bundle. Do not infer them from a branding prefix.

```json
{
  "format_version": 1,
  "source_identity_label": "io.example.installation",
  "source_tenant_label": "io.example.tenant",
  "source_environment_prefix": "EXAMPLE_",
  "environment_keys": {
    "EXAMPLE_HERMES_CREATE_DRAFT_PR_ENABLED": "FACTORY_HERMES_CREATE_DRAFT_PR_ENABLED",
    "EXAMPLE_HERMES_GRPC_TARGET": "FACTORY_HERMES_GRPC_TARGET"
  },
  "environment_values": {
    "unix:///run/example-gateway/hermes.sock": "unix:///run/factory-hermes-factory/hermes.sock"
  },
  "agent_trace_seed": "example-hermes:trace:attempt:{attempt_id}",
  "gateway_policy": "unbound"
}
```

Every source environment key in the supplied old prefix needs an explicit
mapping. Collisions and non-path value substitutions are refused. Value changes
match a whole environment value; historical JSON and immutable records are not
searched/replaced. The target always sets retained create-PR/reviewer gates to
`0` and drops retired model/provider selectors. The supplied organization files
must disable GitHub, every routing execution gate, independent review and
automatic repair. A reviewed Linear identity can remain configured. Secrets
stay in their existing role-specific files; the private map contains no secrets.
The source environment, including its execution gate, is left unchanged.

Take the gateway journal archive only after the original gateway is stopped and
its supervision fenced, using its original path and operator procedure. Keep
that archive private alongside the cold backup. Preserve all entries, including
intents without results and cancellation records. Preview reports aggregate
counts and the archive hash, not receipt contents. Unknown outcomes require
separate operator review before any later execution or rollback that could
resume an invocation. The archive is evidence, not an active target journal.

The naming preview also restores the PostgreSQL archive into a uniquely owned,
network-disabled disposable volume and starts only a temporary PostgreSQL
process to check every supported scheduler/dispatch store. The source volumes
are never mounted by this check. It removes only its own disposable container
and volume; no Factory service, worker, Hermes process or provider call runs.
Apply repeats these checks before and after logical restore. It refuses rather
than modifying pending work or enabled cron/scheduled triggers. A remaining
lifecycle dead letter likewise requires the audited original-release archive
procedure, never a direct database edit by this migration.

The migration receipt retains original ownership/tenant labels and every
predecessor identity inherited from the markerless source, including earlier
restores. Later restores keep that recorded set unchanged and append new
identities separately, so production fencing chooses the original label for
every old identity and the current label for new ones. Tenant scans use both
recorded source and current label namespaces.
Do not relabel source containers or volumes. Keep source restarts and the
original gateway disabled throughout target validation.

## Validate and switch

The result is prepared, not deployed. Run the new bundle's production wrapper
against the destination:

```bash
python3 /srv/factory-bundles/NEW/deploy/factory-production.py \
  --directory /srv/factory-unified preview
```

The original source project and dedicated Hermes source must stay stopped and
restart-disabled. After reviewing the installation and delivery effects, use
`resume-paused --apply --approve-effects` with that same wrapper. This creates
an execution-worker container without starting it and resumes the other roles,
including idle Hermes. It preserves the original loopback ports and public URL;
routing or supervision changes remain explicit operator work. Check health and
history before rebinding the canonical CLI using [CLI.md](CLI.md). Update both
CLI and disabled auto-deploy configuration to the new permanent bundle,
installation path and installation ID. Do not enable auto-deploy or copy a new
compatibility fingerprint to bypass its protected-change review.

For ordinary same-generation profile migration, the existing gateway/runtime
binding is deliberately preserved. For a markerless naming transition the
target stays unbound, full startup is refused, and the journal is archival only.
`resume-paused` still requires a separate explicit approval because control-plane
integrations and token refresh can produce effects, even with execution stopped.
 Rebind the host
Hermes wrapper and gateway to the new dedicated Compose container through the
existing reviewed runtime-binding procedure before any later execution approval.
The old Hermes supervision must remain disabled. Packaging and `resume-paused`
do not authorize a live agent canary or production execution.

## Failure and rollback

An interrupted migration retains partial destination state for diagnosis and
refuses to reuse its directory/project. It never deletes original data. Stop any
remaining containers named `NEW_PROJECT-migration-*-pg` only after checking their
installation label; they have no network or restart policy. Retain the private
backup and evidence. Remove only reviewed destination resources before retrying
with a new project. There is no automatic retry of a partially restored database.

Before destination startup, rollback means retaining/removing the unused
candidate and resuming the original installation with its original bundle and
previously enabled roles, keeping execution paused. Restore its supervision only
after checking the candidate is stopped and restart-disabled. After destination
startup, integrations may have produced effects or refreshed tokens: stop and
fence the destination and reconcile new records/token state before rolling back.
Never run both installations or both Hermes runtimes against the same tenant.

Backups/restores of a completed unified installation include all eight durable
volumes. Subsequent reviewed image-only upgrades update every role's image pin
together; protected image/deployment changes still require the normal review.

## Isolated rehearsal

`python3 tools/check_factory_naming_migration.py --directory /NEW/PRIVATE/DIRECTORY`
uses synthetic cold SQLite stores and a synthetic journal to verify backup hashes,
metadata-only translation, historical identity retention, unchanged source state,
and byte-identical rollback. It does not contact Docker or production.

The full container rehearsal is `tools/check_factory_profile_migration.py --naming`
with the same four exact local image arguments as its ordinary profile check. It
creates unique synthetic projects, isolated PostgreSQL copies, and a new stopped
target; verifies cold backup/restore and old-label fencing; and removes only its
own resources. Never use production paths, credentials, volumes or providers in
either rehearsal. Local source images and the target release image must already
exist; rehearsal does not pull images.

Add `--source-bundle /PRESERVED/PUBLIC/deploy` to copy and exercise the actual
source release's bootstrap, metadata validation, ownership labels and cold-backup
wrapper against disposable fixtures. The original bundle is read-only and its
hashes must remain unchanged. Without this flag, the harness synthesizes a prior
namespace in a copy of the current bundle. Both modes use minimal database
schemas to test migration regressions; neither substitutes for validation of a
real cold source snapshot before a separately approved cutover. The disk rehearsal
proves rollback of its synthetic original archive/SQLite state; the Docker check
proves backup/restore of the prepared stopped candidate, not a live rollback.
