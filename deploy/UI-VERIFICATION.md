# Isolated Codex UI-verification pilot

Status: opt-in operator pilot, not automatic attempt verification. Hermes remains
the implementation executor. Production startup, routing, ownership, the Factory
ledger and Linear state are unchanged. No model calls happen on install/startup.

This is part of the Factory release, not a separately installed desktop app.
The release includes a pinned verifier image reference and the host launcher.
The launcher needs Linux/amd64, Docker Compose, Python 3 and the root operator
session (to create private files owned by the non-root container user). It uses
an existing private Factory installation only for identity and evidence storage;
it does not start, stop or mount its control-plane services or databases.

## Roles and boundaries

- Preview: an **operator-reviewed synthetic preview image**, no credentials,
  read-only root filesystem, writable disposable `/data` and `/tmp`, UID 1000.
- Checker: trusted image-owned Playwright steps on desktop and mobile-sized
  viewports. Produces assertions, screenshots and browser traces independently
  of the model. Runs before exploration, then the preview is recreated.
- Browser: official Playwright MCP, isolated profile, no provider credentials,
  authenticated private MCP transport. No published host port. Browser origins
  are restricted as a guardrail; Playwright's origin settings are not a sandbox.
- Codex: pinned official CLI, no shell tools, repository checkout, GitHub write
  credentials, ambient plugins or operator auth. Only it receives the dedicated
  OpenAI API credential. Its explicit MCP tool allowlist excludes arbitrary code,
  file upload, storage-state import and browser developer tools.

Preview/checker/browser use internal Docker networks without Internet routing.
Only Codex has an outbound provider network. Containers receive neither the
Docker socket nor host repositories. Internal Docker networking is not a VM or a
host firewall: use only reviewed preview images, not hostile arbitrary code.
Chromium's inner sandbox is disabled for this constrained container pilot; the
non-root, capability-dropped container is the process boundary. Do not weaken
these controls or publish browser ports to troubleshoot it.

The host launcher owns container lifecycle, bounded timeouts and cleanup. One
run per installation is allowed. A crashed launcher leaves a nonterminal private
report; a subsequent run refuses to start while prior run containers remain.
No "agent says done" response can create a Factory success receipt. The launcher
marks cleanup failures `containment_unconfirmed`, not success.

## Preview contract

Build the intended app revision **before** injecting any model credential. The
image must work offline with synthetic data/providers, bind `0.0.0.0:3000`, return
200 from `/health`, run as UID 1000, and keep mutable state under `/data` or `/tmp`.
It must not declare durable Docker volumes or require production secrets. Label
it `io.driftward.factory.ui-preview=1` and set
`org.opencontainers.image.revision` to its exact 40-character source commit.
The launcher checks those labels and resolves an immutable local image ID.
Labels are operator provenance assertions, not a substitute for trusted builds.
Use a separate Factory task to add a Capykit preview recipe; this pilot does not
modify Capykit, emulate its OAuth providers, or reuse your owner session.

An operator-approved JSON request contains exactly these fields:

```json
{
  "source_commit": "0123456789012345678901234567890123456789",
  "preview_image": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "path": "/",
  "objective": "Create an agent, grant its action, and verify that it can use it. Use synthetic data only.",
  "steps": [
    {"action": "fill", "label": "Agent name", "value": "Factory test {viewport}"},
    {"action": "press", "key": "Tab"},
    {"action": "press", "key": "Enter"},
    {"action": "expect", "text": "Agent created"}
  ],
  "model": "OPERATOR_SELECTED_MODEL",
  "timeout_seconds": 300
}
```

Choose a model available to the dedicated API project; the runner never borrows
the interactive session's selection/auth or silently falls back. `steps` supports
`fill` by label, `click` by role/name, `press` of an allowlisted key, and `expect`
exact visible text. At least one assertion is required. `{viewport}` in fill
values expands to 1280 or 390 to avoid accidental duplicate records between
checks. Those contexts have separate browser storage but the same synthetic app
database. Emulated mobile width is not a real-device/Safari verification.

## Run

Select the reviewed verifier digest from `FACTORY_UI_VERIFIER_IMAGE` in the
release's `factory.env` (do not source arbitrary environment files). Pull it and
the approved preview image explicitly. Nothing is pulled or built during a run.
Choose an installed, reviewed Driftward Engineering plugin directory. The launcher
snapshots **only** its `verify-app` instructions into the prompt and records the
plugin version and instruction digest. It does not install/activate a plugin,
copy its tools/hooks, or update the shared policy installation.

Provision a dedicated API-project key in a private, root-owned file using the
host's secret-placement workflow. Never paste credentials into chat, requests,
PRs or command arguments; do not copy `/root/.codex` or Hermes auth. OpenAI
recommends API auth for automation; usage is separate from ChatGPT subscription
credits. See [non-interactive Codex](https://learn.chatgpt.com/docs/non-interactive-mode).

Preview first; append `--apply` for the approved run:

```bash
python3 deploy/factory-ui-verify.py \
  --directory /srv/factory \
  --request /srv/factory-verifier/request.json \
  --runner-image sha256:REPLACE_WITH_VERIFIER_IMAGE_ID \
  --plugin-directory /srv/factory-verifier/driftward-engineering \
  --api-key-file /srv/factory-verifier/openai-api-key
```

The launcher copies the key only into a per-run private secret file, mounts it
only in Codex, and removes its copy after cleanup. It does not rotate or delete
the operator's original secret. A Docker bind mount remains accessible to a
running container even after unlink: `containment_unconfirmed` requires operator
containment, not an assumption that file removal revoked access.

## Evidence and recovery

Evidence is under `INSTALLATION/ui-verification/RUN_ID/`. `report.json` identifies
the source revision, immutable image IDs, request digest, instruction digest,
execution mode, outcome, artifact hashes and containment result. Machine checks
and agent observations are separate. `observed` is **not verified**;
`factory_verified` is always false in this pilot. A fixture can only return
`fixture_passed`. No browser invocation, missing artifacts, timeout or invalid
response may be silently accepted.

Reports contain bounded metadata. Screenshots, Playwright traces and narrative
are private synthetic-test artifacts, **not automatically redacted**. Never run
with real customer data/credentials. Raw model streams and Docker logs are not
retained. Nothing is exported to PostHog or the Factory ledger in this milestone.
The normal Factory cold backup does not include this new pilot directory; retain
needed private evidence separately before removing the installation. Set a
retention policy before enabling automatic execution.

If a run is interrupted, inspect containers with both
`io.driftward.factory.installation` and `io.driftward.factory.ui-run` labels. Only
remove that exact run's containers/networks after confirming ownership; never
prune Docker or stop the production Compose project. Retain the incomplete report
and record manual containment separately. A subsequent run is a new run, never
a rewrite of historical evidence.

## Reproducible, credential-free acceptance

```bash
docker build --build-arg FACTORY_RELEASE_COMMIT="$(git rev-parse HEAD)" \
  -f deploy/ui-verifier/Dockerfile -t factory-ui-verifier:local .
docker build --build-arg FACTORY_RELEASE_COMMIT="$(git rev-parse HEAD)" \
  -f tools/fixtures/ui-verifier/Dockerfile -t factory-ui-preview:local .
sudo python3 tools/check_factory_ui_verifier.py \
  --runner-image factory-ui-verifier:local --preview-image factory-ui-preview:local
python3 -m unittest discover -s tools -p test_factory_ui_verifier.py
```

Acceptance drives a real synthetic form and backend, tests private MCP transport,
retains desktop/mobile screenshots and traces, rejects an intentionally failing
assertion, cancels a running check, and confirms cleanup. It does **not** prove model quality, live API
authentication, provider OAuth, Capykit behavior, or automatic dispatch.

Before real use: independent review, dedicated credential provisioning, an
approved app preview recipe and one supervised Codex canary. Before automatic
use: connect the pilot to the existing attempt lease/cancellation protocol and
evidence ledger, expose artifacts in the console, add governed retention and
evaluate the results. Do not add a competing queue or silently promote this
operator pilot into a production verification gate.
