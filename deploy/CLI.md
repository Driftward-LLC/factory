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

An old, accepted read-only PR inspection can block profile activation even when
its 120-second inspection capability has expired. The root-only CLI supports a
bounded recovery for one untouched inspection with exactly one pending linked
request and one outbox row, no execution/workflow/artifact/result evidence, no
live delivery lease, no other unresolved execution, and a terminal prior attempt
when linked. This is not recovery for an executing or effect-producing job.
Existing `stale_active_bridge_job` reconciliation annotations are retained;
their stable finding key and kind are bound to consent, while harmless warning
counter/timestamp refreshes do not invalidate it. Other finding kinds are refused.

```bash
factory profiles recover-inspection JOB_UUID preview
factory profiles recover-inspection JOB_UUID apply \
  --snapshot-digest DIGEST_FROM_PREVIEW \
  --reason 'Expire abandoned inspection before profile activation' \
  --request-id NEW_UUID --yes
factory profiles recover-inspection JOB_UUID status
```

Preview and status are read-only and return bounded metadata. Apply revalidates
the digest under an immediate SQLite transaction after stopping/rechecking the
writers and gateway and checking native execution is idle. It expires the
request and unacknowledged delivery authority, retains an acknowledged outbox
unchanged, marks the job failed with `inspection_capability_expired`, and appends
an `inspection_expired` bridge audit event with root identity, operator reason,
request UUID and digests. It never deletes history or creates an inspection
result, success or cancellation receipt. Repeating the exact apply is idempotent;
changing its intent or recovered state is refused.

The recovery requires `bridge-executor-worker` already exited and keeps it
exited: legacy images could still perform a read-only GitHub inspection from an
already queued event after expiry. Deploy the source terminal-inspection guard
before enabling that consumer again. Other originally running writers are
restored with no dependencies started. A pre-existing failing gateway may still
fail restoration after the ledger transaction commits. `status` reports that
committed outcome and fixed restoration-error tokens; inspect it before retrying
or proceeding with the already-reviewed profile activation. No output claims the
baseline gateway is healthy. `profiles status` additionally reports
`gateway_socket_listening`, a bounded local Unix socket connection probe, not a
gRPC or model readiness check.

Use bridge ledger/audit records as recovery evidence. PostHog absence does not
establish that a job never ran. Do not export capability, payload, prompts,
outputs or credentials; observability checks use exact bounded job/attempt/session
or trace identifiers only.

Configure reads only Git's tracked archive at engineering commit
`db14524356cfebd132833ed06a7c8a097ef5c11c` (`0.1.0-rc.6`). It retains immutable
releases under the executor profile's own `factory-managed/releases` directory
and enables that package plus the metadata-only PostHog observer for the
executor. Existing default-profile files and shared release stores are not
modified. The named reviewer keeps generic plugins disabled and uses its native
review observer. Both observers use existing runtime PostHog configuration;
ingestion keys are not placed in profile files. Models are selected only in
these named role profiles (Sol executor, Astra reviewer).

The existing Hermes wrapper runs native operations as container root. The helper
validates the runtime-owned default config and executor directory against the
owner of `/opt/data`, while Factory-managed configs and releases retain the
calling identity's strict ownership checks. Configure changes only the named
reviewer directory to root ownership and mode 0700, preparing its private auth
parent; it does not recursively change runtime files or change any secret's
bytes, owner, mode or inode. Status remains read-only. Native reviewer execution
uses a fresh child home so Hermes resolves the named reviewer's own auth domain.
These profiles do not provide Unix-user isolation.

Profile status also reports bounded gateway service health and the last cutover
phase, failed stage and rollback stage. Receipts use fixed stage names and never
retain subprocess output or exception text. A rollback is marked `requires_operator`
if the gateway health check fails. The managed gateway unit and activation
drop-in ask systemd to create its runtime directory before applying filesystem
namespaces, preserving that directory across stops/restarts so existing worker
socket-directory bind mounts retain the same inode.

The credential-free mixed-UID native regression can be run with
`python3 tools/check_factory_profile_runtime_native.py --plugin-source REVIEWED_GIT_REPO`.
It uses the pinned Hermes image, disposable tmpfs data, no network and no host
runtime mounts. It verifies status, configure/idempotence, auth inode preservation,
native reviewer auth boundaries and executor skill preload/zero-tool construction;
it does not prove provider calls or PostHog ingestion.
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
