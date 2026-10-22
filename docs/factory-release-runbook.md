# Factory Release Runbook

Use this runbook to publish and deploy a Driftward Factory release. The release
workflow builds one Linux amd64 runtime image, publishes it to GHCR, and attaches
a source-free deployment bundle pinned to that image's registry digest. It does
not deploy or migrate an existing runtime.

## Scope

- Published image: `ghcr.io/driftward-llc/factory:<release-tag>`
- Installed image pin: `ghcr.io/driftward-llc/factory@sha256:<digest>`
- Release assets: `factory-<release-tag>-linux-amd64.tar.gz` and `SHA256SUMS`
- Bundle: `factory.env` release metadata and the allowlisted portable deployment
  files under `deploy/`; no source checkout or host Node/npm build is required.
- Platform: Linux amd64 with Docker Engine, a supported Compose plugin, and
  Python 3. See `deploy/PORTABLE.md` in the bundle for exact prerequisites.

GHCR package visibility follows repository/package settings. Private package
access requires registry authentication on the deployment host; a GitHub release
does not automatically make its image publicly accessible. The workflow token
needs package write permission and the package must grant this repository access.

The existing VPS migration/recovery path remains available separately. It uses
`/root/driftward/repos/factory`, `/docker/driftward/factory`, the
`driftward-factory.service` systemd unit, and locally built
`driftward/factory:<release-tag>` images. It is not the fresh-host installer.

Do not put secrets, tokens, Hatchet profile files, runtime database files, or
Hermes session material in the release.

The bundle also contains preview-first [same-host cutover preparation](../deploy/CUTOVER.md)
and an explicit production Compose overlay. These do not alter fresh-install
dry-run defaults. A migrated candidate preserves its source images and remains
stopped; release publication does not approve a cold copy or live cutover.

## Versioning

Use date-based release tags:

```text
vYYYY.M.D
vYYYY.M.D.N
```

Use `vYYYY.M.D` for the first release on a date. Use `vYYYY.M.D.N` for
additional same-day releases.

## Preconditions

Start from a clean, current `main` checkout:

```bash
cd /root/driftward/repos/factory
git switch main
git pull --ff-only
git status --short
```

`git status --short` must print no file changes before tagging.

## Validate

Run the focused release checks before creating the tag:

```bash
python3 tools/hatchet-contracts.py check
python3 -m unittest discover -s tools -p 'test_*.py'
npm run build --prefix hatchet/workers
HATCHET_POSTGRES_PASSWORD=ci-postgres-password \
  HATCHET_SERVER_AUTH_COOKIE_SECRETS=ci-cookie-secret \
  HATCHET_ADMIN_PASSWORD=ci-admin-password \
  FACTORY_SOURCE_ROOT="$PWD" \
  docker compose -f deploy/docker-compose.yml config >/dev/null
```

For changes touching bridge, ingress, lifecycle writer, or worker behavior, run
the targeted tests named in `AGENTS.md` before release.

## CI efficiency and artifact retention

Hatchet Quality cancels superseded runs only for the same pull request. Every
main push keeps a unique run group so validation and image publication finish.
Both jobs cache npm and pip package downloads using their lockfiles and bridge
requirements; dependency installation and all checks still run on every job.
The caches exclude installed dependencies, browser binaries, and build outputs.

Hatchet Quality retains routine UI evidence (`driftward-ui-storybook` and
`factory-console-fixtures`) for seven days. Production image metadata
(`factory-production-<commit>`) are separate deployment artifacts with their own
existing seven-day retention; GitHub release assets are separate again.

Changing workflow retention affects only newly uploaded artifacts. Cleanup of
existing artifacts is a manual, audited operation against an explicit allowlist
of artifact IDs and names. Do not delete workflows or workflow runs to clean up
artifacts; preserve their logs and check history.

## Create The Release

Pick the next tag and run the GitHub release workflow from `main`:

```bash
release_tag=v2026.9.2
gh workflow run factory-release.yml -f release_tag="$release_tag"
run_id="$(gh run list --workflow factory-release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id"
```

The workflow validates the release request, runs the release checks, builds and
tests the package shape, checks a disposable fresh installation/restart/restore,
publishes the already-built image, packages its registry
digest, creates the annotated tag, and creates the GitHub release with checksums.
It refuses to overwrite an existing image tag or GitHub release. It does not
deploy the VPS runtime.

For a validation-only check, pass `dry_run=true`:

```bash
gh workflow run factory-release.yml -f release_tag="$release_tag" -f dry_run=true
```

Dry runs do not log in to GHCR, push images, create tags, or create GitHub
releases. Their local preview uses an unpublished image ID, has a
`-validation-only` filename, is marked non-installable in `factory.env`, and is
not uploaded.

If publication fails after the image push, no deployment has occurred. Inspect
the failed workflow and use a new release tag; do not overwrite the existing
image tag or attach a bundle with a different digest. Do not create a source-only
release as a substitute for a failed portable publication.

## Fresh-Host Deployment

Download both release assets, verify the archive, and extract it into a new
directory. For example, from a directory dedicated to this release:

```bash
gh release download "$release_tag" --repo Driftward-LLC/factory \
  --pattern "factory-$release_tag-linux-amd64.tar.gz" --pattern SHA256SUMS
sha256sum --check SHA256SUMS
tar -xzf "factory-$release_tag-linux-amd64.tar.gz"
cd "factory-$release_tag-linux-amd64"
python3 deploy/factory-portable.py --help
```

Follow `deploy/PORTABLE.md` in the extracted bundle for preview-first
initialization and startup. The wrapper reads the adjacent `factory.env` release
metadata by default. Keep that digest pin; do not replace it with `latest`.
Generate deployment credentials on the destination host and keep them separate
from release assets. Attaching a Hermes executor and enabling real integrations
is a separately reviewed step; downloading a release does not transfer this
VPS's credentials, state, or execution permissions.

Checksums detect a corrupted download; obtain both assets through the trusted
repository release. They are not an independent signature or a portability
acceptance test. This milestone does not claim ARM64, Docker Desktop, or live
remote-executor support.

## Existing VPS Migration/Recovery Deployment

Deploy from the exact release tag:

```bash
git fetch --tags origin
git switch --detach "$release_tag"
bash deploy/install-docker.sh --image-tag "$release_tag" --start
```

For release-shaped tags, the installer verifies that `HEAD` matches the tag and
that the checkout is clean before building. It then writes the systemd image
pin, embeds the release commit and version in the image, and reloads
`driftward-factory.service`. Compose recreates changed services without first
stopping PostgreSQL or other unchanged containers. The installer retries
`factory-docker-smoke` until healthy and restores the previously running image
automatically after a failed validation. The default timeout is 90 seconds.
Override it with `DRIFTWARD_FACTORY_SMOKE_TIMEOUT` only for a specific
operator-approved deploy.
Once smoke passes, the installer disables the retired direct-host Factory
units; Compose remains the only active runtime.

## Verify The Existing VPS Deployment

After the installer completes, verify the runtime:

```bash
factory status
factory-docker-smoke /docker/driftward/factory
docker compose --env-file /etc/driftward/hatchet.env \
  --project-directory /docker/driftward/factory ps
systemctl status driftward-factory.service --no-pager
```

Confirm the Factory containers use the intended image tag:

```bash
docker compose --env-file /etc/driftward/hatchet.env \
  --project-directory /docker/driftward/factory images
```

`factory status` also verifies that the bridge-reported commit matches the OCI
image revision label and that live Hatchet workflows/workers conform to the
checked-in desired state.

## Bridge schema 10

Building or merging a release does not migrate the live ledger or authorize an
unstarted-review recovery. Schema 10 adds an empty, append-only independent
review delivery table; its migration preserves all historical review, job,
event and outbox rows. It does not rearm reviews or dispatch work.

Deploy this schema in a separately reviewed maintenance window. Quiesce intake,
bridge/operator/dispatcher consumers and workers, confirm no active executions
or unsettled leases/deliveries, and retain a cold, restorable backup of the
ledger and release configuration. Run the explicit installer migration and
its disposable-copy, integrity and historical-row preservation checks before
starting all consumers from the same schema-10 release. Runtime and read-only
connections perform no DDL and refuse schema 9 until migration completes.
Follow the existing [bridge migration procedure](../hatchet/bridge/README.md#legacy-publication-and-migration-boundary)
for the supported migration gates; do not apply ad hoc SQL against live state.

After deployment, a root operator must separately inspect, preview and confirm
each eligible unstarted review using `factory review-recovery`. Neither the
migration nor a background sweep performs that approval.

Schema 9 binaries cannot open a schema-10 ledger. A rollback across this boundary
requires a reviewed restore/migration plan and quiescence; preserve any work
recorded after the backup. Do not point the old image at the upgraded ledger.

## Rollback The Existing VPS Deployment

Rollback is a deployment of the previous known-good tag:

```bash
git fetch --tags origin
git switch --detach <previous-tag>
bash deploy/install-docker.sh --image-tag <previous-tag> --start
```

Preserve the `driftward-factory-*` Docker volumes and
`/docker/driftward/factory/keys/*` unless there is a separately approved record
migration or restore plan.

## Return To Development

After deployment, return the source checkout to `main`:

```bash
git switch main
git pull --ff-only
```

## Linear token publication

The token refresher publishes `writer.env` atomically in the dedicated
`driftward-factory-linear-published-token` volume. Only the refresher mounts this
volume writable; the lifecycle writer mounts it read-only and reloads the token
through `FACTORY_LIFECYCLE_ENV_FILE`. Client credentials remain in the refresher,
and its app-token cache stays in the separate token-state volume.

Do not bind-mount the individual publication file: atomic replacement of a
file that is itself a mount point fails with `EBUSY`. The shared directory also
lets the running writer see the replacement inode. On first start, the refresher
populates this volume from its own token provider; no ledger migration is needed.
The refresher health check requires both publication and fresh actor verification.
