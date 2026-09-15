# Factory Release Runbook

Use this runbook to cut and deploy a Driftward Factory release from the
repository source. Factory releases are source-built Docker image tags that are
pinned into the installed systemd runtime.

## Scope

- Source repo: `/root/driftward/repos/factory`
- Runtime root: `/docker/driftward/factory`
- Systemd service: `driftward-factory.service`
- Image shape: `driftward/factory:<release-tag>`
- Image pin drop-in:
  `/etc/systemd/system/driftward-factory.service.d/10-image-tag.conf`

Do not put secrets, tokens, Hatchet profile files, runtime database files, or
Hermes session material in the release.

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

## Create The Release

Pick the next tag and run the GitHub release workflow from `main`:

```bash
release_tag=v2026.9.2
gh workflow run factory-release.yml -f release_tag="$release_tag"
run_id="$(gh run list --workflow factory-release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id"
```

The workflow validates the release request, runs the release checks, creates the
annotated tag, pushes it to GitHub, and creates the GitHub release. It does not
deploy the VPS runtime.

For a validation-only check, pass `dry_run=true`:

```bash
gh workflow run factory-release.yml -f release_tag="$release_tag" -f dry_run=true
```

If GitHub Actions is unavailable, the manual fallback is to create an annotated
tag from clean current `main`, push it, then create a GitHub release with notes
that include the source commit, validation performed, and deployment caveats.

## Deploy

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

## Verify

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

## Rollback

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
