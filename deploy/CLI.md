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

For recovery, use `factory review-recovery ENG-X preview --review REVIEW-ID
--source-attempt ATTEMPT-ID --repo OWNER/REPO --pr URL --branch BRANCH
--reviewed-head SHA --reason TEXT`. Confirm only after reviewing that preview,
adding `--request-id ID --snapshot-digest DIGEST --yes` and changing `preview`
to `confirm`. Inspection is separately gated by the image CLI's explicit `--yes`.

## Audited deployment commands

Use the installed `factory` command for Factory operations. A missing operation
requires a product change; do not bypass its binding with a checkout script,
direct database edits, or a private transport command.

```bash
factory deploy status
factory deploy preview --run 123456 --json
factory deploy approve --preview EXACT_DIGEST --reason "Reviewed the listed CLI release changes" --yes
factory deploy apply --preview EXACT_DIGEST --yes
factory deploy preview --help
```

Each subcommand supports `--json` and its own help/examples. Help works without
root, configuration, Docker, or network access; operations require root. Normal
output shows the bound installation, current image, exact target, restart impact,
and next command. JSON results use stdout; sanitized refusal codes use stderr
with a nonzero exit status. A preview with protected changes that cannot be
approved returns status 2 and lists those paths.

Preview verifies the successful current-main CI run and immutable artifact
metadata and saves a private snapshot. It does not download/import an image,
change runtime configuration, stop services, or dispatch work. Approval records
the reason and root identity for that exact snapshot. Apply rechecks current
main, CI attempt, artifact identity/digest, installation/image, baseline, installed
helpers, production bundle, and installation provenance under the existing
installation and tenant locks. Changed evidence invalidates approval.

Approval is consumed durably before image import or rollout effects. Apply then
uses the existing image archive/provenance checks, quiescence writer barrier,
cold backup, image pin update, and health verification. Interrupted or failed
manual deployments also stop automatic deployment. No approval replay, blind
retry, migration, executor identity change, or automatic restore is provided.
Keep the private audit records and cold backup for an explicit recovery review.

The closed review scope is `factory/factory`, this guide, and
`.github/workflows/hatchet-quality.yml`, plus the existing root-installed routine
policy. Known host helper source files are eligible only when the target Git blob
exactly matches the separately reviewed, already installed helper bytes and
expected source-file mode. Deleted files, unknown protected paths, schema,
executor, Compose, and dependency changes remain blocked. An artifact cannot
install or approve its own host helper. Both the retained-baseline comparison
and the actual current-image-to-target transition must satisfy that policy;
an old baseline cannot authorize a schema or executor downgrade.

This version approves one release only: it never changes the root compatibility
baseline or disables the compatibility guard. Future automatic releases remain
blocked while their protected fingerprint differs. A private verified baseline
commit anchor is retained so another explicit manual preview can still compare
against the original baseline after a successful one-release deployment.

### Reviewed host tooling installation

The deployment group requires the reviewed canonical launcher, the updated
`factory-auto-deploy` helper, `auto_deploy_artifact.py`, and
`factory_deploy_operator.py` installed together as root-owned regular files in
their documented locations. Publish the launcher at `/usr/local/bin/factory` and
the other three files at `/usr/local/libexec/` (the backend executable is named
`factory-auto-deploy`, without `.py`). Helpers and all parent directories must be
root-owned, free of symlinks, and not writable by group/others. The launcher and
backend may be executable; policy/operator modules may use mode 0644. Keep the
existing reviewed production operations bundle and explicit installation binding.

This is a separate reviewed host tooling update, never a downloaded artifact or
an automatic deployment action. Older installed helpers refuse the new workflow
until updated. No path/config/environment override is accepted. The restricted
SSH helper still accepts only its existing status/deploy request syntax; its
deployment account gains no approve/apply command.

The command structure borrows the installed Driftward Hermes CLI's noun/verb
groups, per-command help, examples, and plan-before-mutation approach. These were
observed through help only; a running Hermes container is not a full health
check. Factory keeps its own installation scope, approval ledger, and safety
checks, with no force flags, new dependencies, or interactive terminal required.
