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
