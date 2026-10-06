# One Factory bundle, isolated executor

New releases can include both digest-pinned Factory and executor images. The
portable wrapper selects the bundled executor automatically from release
metadata. Both images run in one Compose project and share one lifecycle,
without `driftward-host`, host Node, systemd, a host Hermes installation,
or a Docker socket mount. Docker/Compose and the Python installation helper
remain the deployment prerequisites. Linux/amd64 is the supported platform.

The executor image contains pinned Hermes, the typed Factory gateway, GitHub
CLI/App-token wrapper, and the metadata-only PostHog observer. Generations/tool
spans correlate with Factory attempts; PostHog is optional and uses your own
key, only in the executor. Existing external-executor installations remain
unchanged; this profile does not migrate shared Driftward Hermes state.

The image also packages a pinned Capykit remote stdio MCP client for optional
connected-app tooling. See [Capykit setup and read-only acceptance](CAPYKIT.md).
It is not automatically enabled and does not replace the executor or Factory's
job lifecycle, effect fences, GitHub App authentication, or receipts.

The default installation remains dry-run. Packaging a runtime is not approval
to create PRs or process real issues. This milestone makes the executor
installable and proves its private transport; portable real-execution
activation remains a separate reviewed gate. Do not bypass the wrapper's
dry-run guard by editing environment files.

## Install and configure

Use the normal release installation commands in [PORTABLE.md](PORTABLE.md).
For explicit development images:

```bash
docker build -f deploy/Dockerfile -t driftward/factory:test .
docker build -f deploy/executor/Dockerfile \
  --build-arg FACTORY_IMAGE=driftward/factory:test \
  -t driftward/factory-executor:test .
python3 deploy/factory-portable.py --directory /srv/factory init \
  --project factory --image driftward/factory:test \
  --executor-image driftward/factory-executor:test --apply
python3 deploy/factory-portable.py --directory /srv/factory up
python3 deploy/factory-portable.py --directory /srv/factory executor setup
```

`executor` runs native Hermes commands inside this installation's container.
Provider configuration/auth, MCP configuration, browser state, sessions and
worktrees persist in its dedicated `PROJECT-executor-data` volume.
No credentials or memories are imported from root, Driftward or personal Hermes.

Private `env/executor.env` contains executor-only environment settings, such as
your provider keys, `POSTHOG_API_KEY` and `POSTHOG_HOST`. Use the generated
single-quoted dotenv format and retain mode 0600. The native `executor setup`
flow may instead store provider state within the executor's own volume. Never
print secret-bearing config or raw Compose output.

Private `env/executor-identity.env` contains exactly four public settings:

```dotenv
DRIFTWARD_HERMES_ALLOWED_REPOS='your-org/your-repo'
DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN='your-factory-app[bot]'
DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME='your-factory-app[bot]'
DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL='123456+your-factory-app[bot]@users.noreply.github.com'
```

These are shared with the bridge executor worker for preflight validation.
Extra keys are rejected so credentials cannot accidentally enter that envelope.
The gateway uses only typed Git/GitHub operations; it has no generic shell RPC.
The executor owns its Git config and sets the configured bot signature on start.
Changing these environment files requires recreating this installation's
containers; run `up` only while it remains dry-run and idle.

GitHub effects require your own dedicated GitHub App, not a personal token.
The bundled `gh` wrapper expects `/opt/data/github-apps/factory-hermes/app-id`,
`installation-id` and `private-key.pem`, or explicit executor-only
`DRIFTWARD_HERMES_GITHUB_APP_*_FILE` path settings. Provision them using a secure
operator session into this installation's executor volume. It mints expiring
installation tokens on demand and refuses fallback authentication when those
files are absent. Never copy our production App key or root GitHub auth.
Repository routing and verification policies remain Factory's existing
checked-in allowlists; packaging does not make arbitrary repositories eligible.

## Isolation, recovery and acceptance

Workers mount only `PROJECT-executor-socket` read-only. The executor
has a separate network for its outbound tool traffic, publishes no port,
runs as UID/GID 10000, and receives no bridge/Hatchet credentials. Only its
own executor data volume is writable; it sees neither host repositories nor
shared Hermes volumes. A healthy socket proves transport availability, not
provider/GitHub authentication or successful execution.

Portable backup/restore includes the executor image identity, private config,
and executor data. Socket state is recreated, not backed up. Existing strict
archive validation rejects links/special files; a workspace containing symlinks
must be resolved before this snapshot mechanism can archive it. Never silently
delete worktree or session material to make a backup succeed.

Disposable acceptance (no real provider or GitHub credentials/effects):

```bash
python3 tools/check_factory_portable.py --image driftward/factory:test \
  --executor-image driftward/factory-executor:test
```

It checks the bundled native gateway with a fixture-backed GitHub command,
worker credential/mount isolation, dry-run intake, restart and cold restore.
The fixture is installed only in the newly created disposable executor.

The release artifact follows Docker's
[one-project multi-container deployment model](https://docs.docker.com/compose/intro/compose-application-model/).
