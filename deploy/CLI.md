# Canonical CLI for a managed installation

The legacy `install-docker.sh` CLI assumes `/docker/driftward/factory`. For a
source-free production installation, publish `deploy/factory-cli.py` as the
canonical `factory` command after reviewing its explicit binding. This is a
host tooling installation, not an automatic deployment step.

Create `/etc/driftward/factory-cli.json` as a root-owned regular file, mode
`0600`, using the active installation's saved installation ID and its reviewed
production operations bundle:

```json
{
  "installation": "/srv/factory-candidate",
  "installation_id": "COPY THE installation_id FROM THE INSTALLATION MARKER",
  "production_bundle": "/srv/factory-bundles/REVIEWED-BUNDLE/deploy",
  "hermes_wrapper": "/usr/local/bin/driftward-hermes",
  "profile_runtime": "/usr/local/share/driftward-factory/profile-runtime/deploy/factory-profile-runtime.py",
  "console_url": "https://YOUR-FACTORY-CONSOLE"
}
```

Use the same `installation` and `production_bundle` paths as the approved
auto-deploy configuration. Do not point the bundle at an agent scratch checkout.
The production wrapper validates image provenance, installation ownership and
container inventory. Replacing an installation at the same path requires an
explicit binding update. The launcher never falls back to the legacy runtime.

After approval, publish from the reviewed checkout:

```bash
install -m 0755 deploy/factory-cli.py /usr/local/bin/factory
factory version
factory status
factory attempts list ENG-162 --json
factory help
```

The host needs Python 3, Docker, Bash, curl and jq. Operator commands use the CLI
from the exact active immutable image, extracted with no network, credentials
or mounts. This follows image-only deployments without copying a new host CLI.
Credentials are read as data from the installation's private generated env,
normalized into a private temporary file, and never printed. The existing CLI
retains its preview, digest and confirmation gates for recovery and mutations.

`factory status` reports the bound Compose service states; `logs` accepts the
existing named targets, `-f` and `-n N`. `console` prints the explicitly bound
console URL. `mode` reads the installation's configured intake mode; it does
not claim to verify live worker state. Mode changes and `doctor` are explicitly
unsupported by this launcher: they require a separately reviewed managed
installation operation. Operator commands (`attempts`, `retry`, `lifecycle`,
`analytics`, `review-repair`, `review-recovery`) retain the active image's
arguments. `factory version` shows the installation, project and active image.

`factory profiles status` (or `list`) reports whether the native Hermes profiles
`factory-executor` and `factory-independent-reviewer` exist. `factory profiles
create` creates missing profiles with native Hermes profile provisioning, without
aliases, bundled skills, cloning, or changing the active default. Existing
profiles are retained unchanged; a partial failure can be retried. These commands
privately inspect the native model/provider seed before any creation and refuse
nonempty inline credential fields, because native fresh profiles can inherit
that seed even without cloning. Status and an existing-profile create/no-op do
not inspect the source config. No source configuration is edited.
These commands
require the optional `hermes_wrapper` binding above, pointing to a root-owned
executable regular file whose path components are protected from group/world
writes. The bound Factory installation is validated and locked before calling
the wrapper. Ambient Hermes/Docker/Compose overrides are not forwarded.

Provisioning does not route Factory execution or independent reviews to these
profiles, migrate credentials, activate plugins, or establish credential
isolation: Hermes may still have default-profile authentication fallback until
separately configured. The existing reviewer auth and per-review isolation stay
in place. Only bounded profile inventory is returned; profile configs, native
command output, credentials, and error details are never displayed. The launcher
extension can be installed as reviewed host tooling without rebuilding Factory's
image; it does not change the production operations bundle.

For the existing shared-Hermes deployment, the optional root-protected
`profile_runtime` binding enables the reviewed role cutover helper. Install it
with its matching `factory-observers/posthog/{__init__.py,plugin.yaml}` and
`independent-reviewer/review_observer.py` public assets, preserving their paths
relative to the helper. Stage matching reviewed gateway/reviewer application
artifacts in this bundle before activation.
The matching compiled gateway artifact must also be staged beside the helper at
`gateway/hermes-gateway-server.js`. Activation publishes only this gateway file
and the three public reviewer files (`native_agent.py`, `runner.py`,
`review_observer.py`) after draining and stopping the gateway, retaining their
previous bytes for exact rollback.
The bundle also includes `engineering/review-code.md` and `engineering/verify-app.md`
from the pinned engineering archive. Activation verifies their fixed release
digests, refreshes only the existing private reviewer skill snapshots under
`/etc/driftward/factory-reviewer`, and updates both provisioning digests in the
same guarded cutover. Rollback restores their exact bytes and modes. Generic
reviewer plugins remain disabled.

```bash
factory profiles configure --plugin-source /path/to/reviewed/driftward-engineering
factory profiles status
factory profiles activate
factory profiles canary executor
factory profiles canary reviewer
```

Configure reads only Git's tracked archive at engineering commit
`db14524356cfebd132833ed06a7c8a097ef5c11c` (`0.1.0-rc.6`). It retains immutable
releases under the executor profile's own `factory-managed/releases` directory
and enables that package plus the metadata-only PostHog observer for the
executor. Existing default-profile files and shared release stores are not
modified. The named reviewer keeps generic plugins disabled and uses its native
review observer. Both observers use existing runtime PostHog configuration;
ingestion keys are not placed in profile files. Models are selected only in
these named role profiles (Sol executor, Astra reviewer).
Executor MCP/browser/terminal/tool configuration is preserved through an explicit
capability allowlist from the existing default config. Inline credential fields
are refused; only references to already available runtime environment variables
are accepted for those fields. Identity, authentication, channels, memories and
sessions are not migrated. Status reports capability parity and MCP server names
without config values. Activation refuses missing parity or blocked capability
settings. The reviewer does not inherit executor capability configuration.
Configure is an initial cutover operation; changing an already activated role
requires a separately reviewed upgrade.

Activate supports the fixed shared-runtime host paths documented here: the
gateway service `driftward-hermes-factory-gateway.service` and private reviewer
provisioning `/etc/driftward/factory-reviewer.json`. It requires no active or
unresolved Factory attempts/jobs/leases, pauses previously running execution
writers, stops the gateway, and checks native processes before moving the
existing dedicated review sign-in from `/opt/data/factory-reviewer-auth/auth.json`
to the named reviewer profile. It uses an atomic same-filesystem rename and
refuses an existing destination. Other reviewer state stays in place. A fixed
systemd drop-in selects the executor profile; reviewer provisioning selects the
named auth root. Previously exited workers remain exited. The private
`profile-cutover.json` receipt records completion, rollback, or required operator
recovery. Recovery restores only this cutover's exact files/auth location;
incomplete recovery leaves execution writers paused.

Canaries perform one bounded native SDK generation with no tools and a fixed
prompt. They return role/profile/session/trace identifiers, never prompt output
or credentials. The reviewer uses a fresh isolated child profile and its native
observer. A completed model run does not verify a complete repository review or
server ingestion: query PostHog for the returned trace identifiers to confirm
both roles' actual generation events arrived.

For recovery, use `factory review-recovery ENG-X preview --review REVIEW-ID
--source-attempt ATTEMPT-ID --repo OWNER/REPO --pr URL --branch BRANCH
--reviewed-head SHA --reason TEXT`. Confirm only after reviewing that preview,
adding `--request-id ID --snapshot-digest DIGEST --yes` and changing `preview`
to `confirm`. Inspection is separately gated by the image CLI's explicit `--yes`.
