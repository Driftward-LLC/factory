# Installation-local Hermes gateway and reviewed activation

Fresh installations remain dry-run. `compose.local-executor.yml` wires the
existing dedicated Hermes executor/reviewer profiles to a private gateway;
`compose.local-execution.yml` enables effects only through the portable
wrapper's explicit approval receipt. No same-host cutover history is invented.

Workers receive public GitHub identity and the private gateway socket. Provider,
Composio and browser credentials remain in this installation's own Hermes data.
No Docker socket or host root is mounted into workers or Hermes.

## Prepare one project and repository

Provision the existing `factory-executor` (SOL) and
`factory-independent-reviewer` (Astra) native profiles in this installation,
including its existing native Composio session. Do not copy shared Hermes data.
Source/review transport uses that authenticated session for private repositories.
Authentication alone does not prove that the implementation and verifier checks
can execute successfully; keep the first task bounded and inspect its receipts.

Prepare the four existing JSON policies in the private installation `config/`:
configure organization identity and integrations, exactly one enabled project
route, its repository verifier policy, and independent review enabled for only
that repository. Use a dedicated project containing only the first task to scope
the canary. A host-service issue must route to its owning host repository, never
to the Factory repository merely to reuse its project. Keep the issue outside
Ready until the approved runtime has started.

Create private `factory-hermes-execution.env` with exactly these four public
identity settings in the generated single-quoted dotenv format:

```dotenv
FACTORY_HERMES_ALLOWED_REPOS='your-org/your-repo'
FACTORY_HERMES_EXPECTED_GITHUB_LOGIN='your-connected-github-principal'
FACTORY_HERMES_EXPECTED_GIT_AUTHOR_NAME='Your configured Git author'
FACTORY_HERMES_EXPECTED_GIT_AUTHOR_EMAIL='your-configured-author@example.com'
```

No key, token or provider credential belongs in this file: workers also receive
these public identity settings. The routed repository must match exactly.

Create a private `reviewer/` directory with the checked-in `review-code.md` and
`verify-app.md` instructions used by the reviewer and `provisioning.json`:

```json
{
  "schema": "factory-hermes-source-reviewer-v1",
  "auth_root": "/opt/data/profiles/factory-independent-reviewer",
  "repositories": {"your-org/your-repo": {"base_ref": "main"}},
  "policy_digest": "<SHA256 of canonical sorted compact independent-review-policy.json>",
  "review_skill_digest": "<SHA256 of review-code.md bytes>",
  "verify_skill_digest": "<SHA256 of verify-app.md bytes>"
}
```

Keep directories mode 0700 and files 0600. Activation gives only these public
configuration and reviewer files to UID 10000 so the gateway can read them;
contents and private modes are preserved. Provider credentials are not touched.

## Native engineering plugin readiness

Provision the executor's already maintained native engineering plugin separately
from the public Factory image. Use its operator-reviewed immutable archive and
existing native adapter, not another set of skill copies. Private package source
or download credentials must not be embedded in this repository or public image.
Extract only into the dedicated executor profile's `plugins/NAMESPACE/` directory,
refuse conflicting existing files, and enable that package with the native Hermes
plugin command scoped to `HERMES_HOME=/opt/data/profiles/factory-executor`. Preserve
provider auth, model settings and all other plugin settings.

Install private-mode public metadata `executor-plugin.json` in the installation
root, readable by UID 10000. See `examples/executor-plugin.json` for its schema.
The source commit/version and archive SHA identify the reviewed artifact;
`tree_sha256` is SHA256 of sorted compact JSON mapping every archive-relative
regular file path to its SHA256. The package must contain no links or special
files. Store the same metadata as `.factory-source.json` at the installed plugin
root, excluding only that root receipt from the tree digest. No other files,
including stale bytecode, are silently excluded. The native loader registers the
maintained package; Factory explicitly preloads only `implement-change`,
`verify-app` and `prepare-pr`, including the linked PR template.

The wrapper derives the explicit `FACTORY_HERMES_SKILL_NAMESPACE` from this public
pin. Its activation preview and gateway startup run the actual compiled profile
argument builder, native profile resolver, CLI argparse and native plugin/skill
preload without invoking a command handler, model, inline skill preprocessing or
tool. All selected skills and the PR template must resolve from the exact enabled
pinned native package. Missing, disabled, shadowed or changed instructions refuse
readiness before effect admission. Recheck also occurs during authenticated
executor identity preflight, so a later configuration change cannot rely on a
stale startup check. The bounded readiness receipt identifies source commit and
version; it is not proof of model execution or task completion.

## Review and apply

Use the same image-pinned release bundle that owns this installation. The
installation must already be initialized and running dry-run for the initial
preview. Its bridge must be idle and its native journal free of unresolved jobs
and admission holds.

```sh
python3 deploy/factory-portable.py --directory /srv/factory execution
# Independently review the displayed installation, runtime, route and hashes.
python3 deploy/factory-portable.py --directory /srv/factory execution \
  --approve-preview <reviewed-preview-sha256>
```

The preview contains only public identity and hashes, including configuration,
role envs, deployment files and the observed full container/image/incarnation.
Apply revalidates its hash under the installation lock, stops writers, checks
idle state again, records approval, starts the local gateway and observes its
new actual Docker identity before starting workers. Configuration/source changes
require a new reviewed preview. No environment edit alone enables real execution.

Ordinary `status`, `stop`, `up`, `logs` and cold `backup` continue through the
portable wrapper. `up` validates the saved approval and refuses unresolved native
or bridge work before starting anything. It then refreshes the public runtime
binding from the actual owned container. A restored backup retains executor and
reviewer configuration/state but drops execution approval: the new installation
starts dry-run and requires a fresh preview and approval before real admission.
The optional Linear lifecycle writer is not enabled by this command.

## Evidence and uncertainty

Private source review exposes immutable source operations only, with no shell,
checkout execution or write credentials. Receipts explicitly say tests and
browser behavior were not independently executed. Verifier policy commands
still require their real evidence; source review cannot manufacture it.

Local cancellation records an unknown outcome and retains a durable admission
hold on failure or uncertainty. A cancellation marker, CLI exit or missing PID
is not proof of descendant cleanup or settled external effects. Do not delete
holds, claims or checkpoints to make startup succeed. Stop this installation and
perform reviewed operator recovery against the original evidence; automatic
retry/reuse is intentionally refused.

The experimental native owner is a separate inactive prototype and is not part
of this activation path. Host deployment or external-service registration is an
operator action; a worker task may prepare a draft PR without obtaining host
root or Docker privileges.

After an existing root-attested interrupted-execution resolution, startup may
recognize the retained unfinished native journal only when its exact version-2
intent fingerprint, lease ID/owner/epoch/fence hash and complete runtime binding
match the committed resolution and audit digests. The root wrapper also proves
the exact old Docker object has been removed through a successful full inventory.
A stopped object, ordinary failed state, revoked write authority alone, missing
process or uncommitted attestation is insufficient. This read-only recognition
neither deletes the old journal nor writes a result, reopens old fences or clears
an admission hold. Nonempty native admission holds still require separate review.

The preflight permits the reviewed engineering plugin and the existing native
PostHog observer, whose complete package must match the image bundled observer.
Other enabled adapters are a conflict requiring operator review, so readiness
cannot execute unrelated plugin registration code. It uses native safe YAML,
forces bytecode writes off and checks the complete pinned tree. At reservation,
the existing invocation intent retains the successful preflight's namespace,
package version, source commit and archive/tree hashes before model dispatch.
Historical unknown intents are never backfilled from the current installation.
The independent reviewer consumes its existing verified instruction snapshots
and suppresses native plugin discovery; it does not install this executor plugin.

The existing portable executor binds the native packaged tool store through
`HERMES_RUNTIME_DIR=/opt/hermes/tools`. Operator plugin admission must use that
same binding and complete Hermes' locked dependency transaction in dedicated
executor state before activation. A cold profile may need public dependency
downloads or a reviewed native cache; copying an enabled setting does not admit
the plugin. Readiness uses Hermes' read-only `venv_is_current()` probe and refuses
a cold or stale dependency selection before registering any plugin.
