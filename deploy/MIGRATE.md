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

The obsolete systemd helper `hatchet/driftward-hatchet-smoke` now exits without
checking or changing services. Use the Factory-owned `factory-docker-smoke` for
its supported production Compose layout and the portable acceptance harness for
a fresh portable profile. Smoke checks read the configured team identity.

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

The existing gateway/runtime binding is deliberately preserved. Rebind the host
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
