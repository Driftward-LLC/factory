# Same-host Factory cutover preparation

This is a staged migration from the existing `driftward-factory` Compose app
into a new, independently named installation. It is not a fresh bootstrap,
runtime upgrade, remote-executor rollout, or permission to enable live traffic.
The current `factory` project at `/srv/factory` is an occupied dry-run pilot:
do not initialize over it, reuse its volumes, or rename the live source in place.

`factory-cutover.py` previews the migration by default. Its explicit `--apply`
path copies a **previously stopped, quiescent** source into a new candidate and
leaves the candidate stopped. It never stops the source, starts services,
changes webhooks, or grants execution authority.

## Preparation and rehearsal

Use the same Linux/amd64 Docker host, Docker Compose 2.24.4+, Python 3.11+, and
the reviewed deployment bundle. Keep the source's exact Factory, Hatchet, and
Postgres images available locally. Migration does not upgrade any of them;
the bundle's advertised release image is not substituted for the source image.

First review the source service inventory, image identities, protected config,
and pending work. Record every job awaiting human review, PR link, branch,
effect receipt, execution fence, and lifecycle delivery state. A review-pending
job is not permission to rerun it. Do not assume earlier job counts remain
current.

Run the checked-in cutover tests with synthetic state before using live state.
A rehearsal must use disposable namespaces, synthetic credentials, and no live
Hermes or Linear access. A cold-copy test alone does not prove that real agent
execution or lifecycle delivery works.

From a source checkout with an existing local Factory image:

```bash
python3 tools/check_factory_cutover.py --image driftward/factory:v2026.9.29-pilot
```

This creates only synthetic, stopped source containers and uniquely named test
volumes, copies all seven volumes, checks six synthetic review jobs plus terminal
history, renders the production Compose configuration, and removes only its owned
test resources. Its Hatchet/Postgres volumes contain markers, not a bootable
historical database; no Factory or integration service starts. This is a
raw-migration rehearsal, not the real canary.

Preview the intended production migration without stopping or copying anything:

```bash
python3 deploy/factory-cutover.py \
  --source-project driftward-factory \
  --source-env-dir /etc/driftward \
  --source-keys /docker/driftward/factory/keys/hatchet \
  --directory /srv/factory-candidate \
  --project factory-candidate \
  --snapshot /srv/factory-cutover-snapshot \
  --port-offset 22000
```

The destination and snapshot must be new private paths with existing parents;
the destination project must have no containers, network, or named volumes.
The example ports are independent of both existing deployments; check their
availability again before any later start. Do not publish raw Compose config,
env files, database contents, or credential-bearing command output as evidence.

Some pre-Compose legacy volumes have no labels (six were observed on the
existing VPS). After separately reviewing their exact names and canonical mounts
on the verified source containers, append `--allow-unlabelled-source-volumes` to
the preview and, later, the approved apply command. This flag accepts only empty
labels; conflicting ownership labels are always rejected. It never changes the
source volume labels.

## Reviewed cold-copy checkpoint

Obtain approval for the exact source, destination, snapshot, maintenance window,
and rollback boundary before pausing intake or stopping production. Then:

1. Pause all source intake routes and prevent new manual retries. Drain or
   explicitly reconcile outstanding execution and verification. Uncertain
   outcomes require evidence, not clearing leases or changing database rows.
2. Stop the source deployment and its supervision. Confirm no legacy host
   services, containers, scheduled jobs, or other writers can resume against
   its state. Keep the Hermes execution boundary intact; do not copy its state.
3. Repeat the preview, then append `--apply` to the same command. The tool
   requires the source already stopped, no mounted running writers, and
   quiescent execution records. A refusal is a stop condition, not a reason to
   bypass the check.
4. Validate the candidate and snapshot while **both deployments remain
   stopped**. Retain the source unchanged until the approved rollout and
   recovery window are complete.

The copy preserves these seven source volumes under the new project prefix:

```text
hatchet-postgres-data
hatchet-config-data
bridge-data
ingress-data
lifecycle-data
linear-token-data
linear-published-token
```

Hatchet tenant identity, encryption keys, worker credentials, integration
secrets, deduplication records, execution evidence, and lifecycle outbox records
remain continuous. Configuration is normalized into the portable private-file
layout; the candidate gets a new installation identity, namespace, and local
ports. Operator sockets and maintenance locks are transient and are not copied
as authority to resume work.

The snapshot uses the existing cold-backup format, including file checksums
and exact image identities. Preserve the same deployment bundle for recovery.
Snapshots contain private keys and credentials: keep them root/operator-only,
encrypt before off-host transfer, and manage retention separately. If copying
fails, retain the partial candidate and snapshot for diagnosis; do not start
them or retry over them. Source data must not be deleted to complete migration.

## Production profile and separate startup approval

`compose.production.yml` is an explicit overlay on `docker-compose.yml` and
`compose.portable.yml`. It restores the reviewed same-host Unix Hermes gateway
connection and Linear services without changing portable dry-run defaults.
It is configuration, not a startup or live-routing command.

The overlay requires the migrated per-service env files rather than accepting
missing files. It does not set execution-enable flags: preserved configuration
and the separate activation review determine those permissions. Its gateway
target is `unix:///run/driftward-hermes-factory/hermes.sock`; the host socket
directory must already exist and is mounted read-only.

Only the approved gateway socket directory may be mounted for executor access.
Provider, GitHub, browser, MCP, and Hermes credentials/state stay outside Factory.
Linear OAuth credentials belong only to the token refresher; the lifecycle
writer consumes the separately published token read-only. Hatchet workers
remain credential-minimal. Do not enable real execution by removing the
portable command's dry-run guard. **All `factory-portable.py` lifecycle commands
are unsupported for the production candidate**, not only `up`: they select the
dry-run Compose files, exclude Linear services, and do not use the production
infrastructure image pins. The production installation marker fails closed on
that wrapper. A later reviewed production wrapper/activation path must own
startup, stop, status, and backup/restore operations.

The candidate records `profile: same-host-production` in its installation
marker and `activation_approved: false` in `.factory-cutover.json`, together
with the production overlay hash and source-image provenance. Its
`FACTORY_IMAGE`, `FACTORY_POSTGRES_IMAGE`, and `FACTORY_HATCHET_IMAGE` values
are exact source image IDs, not new release tags.

Before approval to start, review the resolved three-file Compose plan privately:
exact source image IDs, installation labels, volume names, port availability,
Unix socket permissions, lifecycle settings, and per-service credentials.
Decide how source supervision remains disabled across restart/reboot and how
the new deployment will be supervised. Confirm preserved human-review jobs,
deduplication receipts, and outbox rows remain represented; do not re-enqueue
them merely because the deployment changed.

The later startup checkpoint must identify one approved canary repository/task,
its execution authority, ingress routing, and observability. Verify its draft PR,
execution receipt, and lifecycle updates before opening normal intake. Existing
review PRs must remain reviewable with their original evidence. Only **one**
deployment may authorize execution or deliver lifecycle effects at any time.

## Recovery boundary

Before the candidate starts or makes external effects, the untouched source is
the recovery point. With the candidate confirmed stopped, the operator can
review resuming the source and its intake routes.

After the candidate creates a PR, receives new events, or delivers lifecycle
updates, simply restarting the old snapshot is unsafe: it may forget new
deduplication/effect records or repeat delivery. Stop new intake, reconcile
candidate effects and state, and approve a recovery plan before resuming any
executor. Do not delete outbox rows, reset fences, or run both installations
to "catch up."

## Separate readiness work

Private GHCR read access and deployment of the published digest, the known `qs`
dependency advisory, and explicit Hatchet credential rotation remain separate
release/operations work. A successful cold-copy rehearsal does not close those
items. This same-host profile also does not claim restart-safe remote gateway
invocations or fresh-host execution portability.
