# Canonical CLI for a managed installation

## Reviewed baseline adoption

```bash
factory deploy baseline preview --run RUN_ID --json
factory deploy baseline apply --preview DIGEST --reason "Reviewed full protected baseline and helper equality" --yes --json
```

Review the entire protected diff, fingerprint, deployed current-main commit and
installed helper identities. All six installed helpers must match that commit
exactly, including source modes. Apply rechecks the snapshot under both deployment
locks, retains the completed release receipt, and durably records root's reason
before updating the baseline. It preserves services and execution/reviewer settings.
Interrupted adoption fences subsequent deployment for explicit operator recovery;
never repair its journals by hand.

## Audited deployment and execution

Use `factory deploy status --json`, `factory deploy preview --run RUN_ID --json`,
`factory deploy approve --preview DIGEST --reason TEXT --yes`, then
`factory deploy apply --preview DIGEST --yes`. Install matching root-protected
copies of `factory-auto-deploy`, `auto_deploy_artifact.py`,
`factory_deploy_operator.py`, and the launcher before using these commands.
Preview binds the installation/image, successful main CI attempt, exact format-2
artifact, installed helper bytes, configuration, and protected source changes.
Approval covers one release; it never adopts a new compatibility baseline.
Only the checked-in immutable activation blob map and byte-matched installed
helpers may cross the protected-change boundary; unknown changes stay refused.

Apply reruns one uniquely identified completed Factory Deploy workflow for the
approved head. Its existing fixed SSH callback supplies ephemeral package
credentials. Queueing does not consume approval or prove deployment. Under the
installation/tenant locks, the callback checks the exact approved producer run,
commit, artifact, and configuration before consuming approval. A duplicate or
uncertain dispatch stays latched for review through `factory deploy status --json`.
No workflow, SSH grammar, credential grants, or automatic compatibility policy
changes are involved. The cold backup and actual rollout receipt remain retained.

A stopped bridge executor stays stopped across automatic and audited deployment.
After the audited rollout completes, enable the independent reviewer with
`factory execution configure preview --json`, then the returned
`factory execution configure apply --snapshot-digest DIGEST --reason REASON --yes --json`.
This fixed operation changes only the reviewer flag in the executor worker's
private environment file. It requires the executor worker to remain stopped,
validates effective Compose configuration, and records a durable approval and
result. An interrupted or failed operation requires operator investigation;
it never starts services or repairs configuration automatically.

Use `factory execution preview --json`, then `factory execution resume
--snapshot-digest DIGEST --reason TEXT --yes --json`. The preview binds the image,
installation, successful trusted CI and rollout receipt, exact reviewed terminal
inspection guard source in the immutable image, quiescence, named role readiness,
reviewer sign-in, and gateway listener. Resume consumes a root receipt under the
same locks and a SQLite writer barrier, starts only the executor, and checks
health. Failure stops that executor and retains the failed receipt for review.
`factory execution status --json` is read-only. These commands do not migrate
state or change execution, repair, merge, or authentication policies.

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

An active executor profile accepts a separately reviewed observer-only upgrade.
Install the reviewed CLI, profile helper, and adjacent observer assets as one
versioned root-owned host tooling bundle. Retain all adjacent gateway, reviewer,
and engineering assets listed above for the existing commands. This installation
does not deploy an image, adopt a deployment fingerprint, or alter deployment
policy. Review and installation of unmerged source is a separate activation
decision; merging source alone does not select the observer in a running profile.

```bash
factory profiles upgrade-observer preview
factory profiles upgrade-observer apply \
  --snapshot-digest DIGEST_FROM_PREVIEW \
  --reason 'Reviewed observer-only upgrade' --request-id NEW_UUID --yes
factory profiles upgrade-observer status --request-id REQUEST_UUID
factory profiles canary github
```

Preview writes no runtime state. Its digest binds the exact selected and target
observer trees, engineering release, protected role files, host configuration,
and service states. Apply requires the executor already paused, bridge quiescence,
and native idle state. Under the installation lock it consumes a root-only audit
receipt, stops the running dispatcher and gateway, rechecks the inputs under the
bridge write lock, and atomically selects the immutable observer release. It
preserves role configuration, authentication, capabilities, reviewer policy and
the paused executor. Only the previously running dispatcher and gateway restart.
A consumed request cannot be replayed. Failure leaves stopped services stopped
and records `requires_operator` in the private receipt; inspect it before recovery.
Use `status --request-id` for read-only diagnosis through the canonical CLI, even
while the gateway is stopped. It returns only the validated request and digest
identities, recorded phase/stage, bounded stop-error names and currently selected
observer digest. If native inspection is unavailable, the receipt summary still
returns with `native_snapshot_status=unavailable`. Reasons, paths, exception
messages and native stderr are not returned. Status never retries an upgrade.

Readiness waits up to 30 seconds for the expected service states, active gateway
and listening socket. A timeout records a fixed `failed_predicate`, such as
`gateway_listener`; a changed host configuration fails immediately. Future
upgrade receipts also retain the prior service states.

Only an upgrade that selected the exact target and then failed at
`restored_health`, with no stop errors, supports this separate recovery path:

```bash
factory profiles recover-observer FAILED_UPGRADE_REQUEST_UUID preview
factory profiles recover-observer FAILED_UPGRADE_REQUEST_UUID apply \
  --snapshot-digest RECOVERY_DIGEST_FROM_PREVIEW \
  --reason 'Reviewed gateway recovery' --request-id NEW_RECOVERY_UUID --yes
factory profiles recover-observer FAILED_UPGRADE_REQUEST_UUID status \
  --request-id RECOVERY_UUID
```

Recovery requires the gateway, dispatcher and executor stopped, quiescent bridge,
idle native runtime and the selected observer equal to both the failed receipt's
target and installed observer assets. The preview binds the failed receipt and
current protected state. Apply consumes its own durable request, rechecks those
inputs under the bridge write barrier, starts the gateway, waits for its listener,
and verifies preserved profile state. The original failed receipt is retained
unchanged; recovery never selects or rewrites the observer. Execution stays paused.

By default the dispatcher stays stopped. Add `--restore-dispatcher` to both preview
and apply only when approving restoration of its previously running state. A
stored prior-service snapshot must support that choice when present. Older
receipts have no such evidence: the preview and recovery receipt explicitly label
the choice `operator_selection`, so the operator must use independently captured
prior Factory status. Changing the selection invalidates the preview. Recovery
status returns bounded phase, predicate and current execution/gateway states.
Readiness failure attempts to stop only the gateway and explicitly selected
dispatcher, retaining the consumed recovery receipt; other failure stages are
not eligible for this recovery command.

The GitHub canary requires the selected observer to match the installed bundle.
It invokes only `gh --version` through the native tool dispatcher, with native
approval and secret filtering. It makes no model/provider request. The two real
tool spans carry `factory.validation_only=true`; the receipt and CLI return exact
session, trace, tool-call, parent and child IDs, never command output or credentials.
The existing asynchronous sender is flushed boundedly without retrying execution.
`server_verification_pending` requires exact-ID server evidence before claiming
delivery. Existing model canaries remain separate commands.

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

## Legacy production artifact preservation

The optional `artifact_runtime` binding points to a reviewed executable copy of
`deploy/factory-artifact-runtime.py`, under root-owned directories protected from
group/world writes. Add this key without replacing existing installation,
profile runtime or Hermes bindings. The helper uses existing host `gh`
authentication with the fixed `Driftward-LLC/factory` repository on github.com.

```bash
factory artifacts preview --before 2026-10-20T22:35:06Z
factory artifacts preserve --before 2026-10-20T22:35:06Z --snapshot-digest PREVIEW_DIGEST --yes
factory artifacts prune --before 2026-10-20T22:35:06Z --snapshot-digest PREVIEW_DIGEST --yes
factory artifacts status
```

Preview is read-only. Its digest binds the cutoff and exact nonexpired legacy
`factory-production-<commit>` inventory. The cutoff cannot exceed the format-1
retirement boundary above. Only successful main-push runs of
`hatchet-quality.yml` in this repository qualify. Preservation downloads in
expiry order and verifies GitHub's ZIP digest, CRCs, the two exact regular ZIP
members, format-1 metadata and image archive content hash. It never extracts or
loads the image. Format-2 metadata and unrelated artifacts are refused.

Verified ZIPs and audit receipts remain under `/srv/factory-artifact-archive`
(root-owned directories `0700`, regular files `0600`). Preservation requires
12 GiB free headroom in addition to selected compressed bytes; downloads are
limited to 1 GiB each and image content to 4 GiB. Existing verified copies are
reused. There is no automatic expiry or local backup deletion.

Prune requires the same fresh snapshot digest and all verified local copies and
receipts before deleting any remote artifact. It rechecks remaining remote
inventory before each explicit artifact-ID deletion, and persists a private
intent and result per deletion. A failure stops the operation; retain receipts,
preview the remaining inventory again, and preserve it before using its new
digest. Never delete a remote archive to make room for its backup. These commands
lock the bound installation and shared archive, and never touch runtime
databases, Docker, services, workflow runs, logs or packages.
`status` verifies every retained local ZIP and receipt without network access,
reporting IDs, byte counts and deletion status even after remote pruning.
