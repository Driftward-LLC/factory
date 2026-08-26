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

Pick the next tag and create an annotated tag from `main`:

```bash
release_tag=v2026.8.26.1
git tag -a "$release_tag" -m "Factory $release_tag"
git push origin "$release_tag"
```

Create the GitHub release:

```bash
gh release create "$release_tag" \
  --title "Factory $release_tag" \
  --notes-file /tmp/factory-release-notes.md
```

Release notes should include the source commit, what changed, validation
performed, and any deployment caveats.

## Deploy

Deploy from the exact release tag:

```bash
git fetch --tags origin
git switch --detach "$release_tag"
bash deploy/install-docker.sh --image-tag "$release_tag" --start
```

For release-shaped tags, the installer verifies that `HEAD` matches the tag and
that the checkout is clean before building. It then writes the systemd image
pin, reloads systemd, builds the selected image, restarts
`driftward-factory.service`, and runs `factory-docker-smoke`.

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

## Rollback

Rollback is a deployment of the previous known-good tag:

```bash
git fetch --tags origin
git switch --detach <previous-tag>
bash deploy/install-docker.sh --image-tag <previous-tag> --start
```

Preserve `/docker/driftward/factory/data/*` unless there is a separately
approved record migration or restore plan.

## Return To Development

After deployment, return the source checkout to `main`:

```bash
git switch main
git pull --ff-only
```
