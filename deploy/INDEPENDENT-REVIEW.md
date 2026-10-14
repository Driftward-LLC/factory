# Isolated independent reviewer

This is a dormant execution path behind the existing **host** Hermes gateway,
not a production enablement or a new always-on reviewer service. The typed
`ReadOnlyReview` RPC takes a reserved bridge job. Its native wrapper fixes
`gpt-6-astra`, records a separate reviewer invocation ID, and returns the bounded
`factory-independent-review-v1` receipt. The model supplies verdict/findings only.

Each invocation starts two disposable containers. The credential role runs the
official Codex CLI with fresh home, no repository, no shell, no apps/plugins,
and only the required source MCP endpoint. The source role has an immutable
Git bundle, independent checkout, test tools and fresh Playwright browser.
It has no provider/platform credentials, no outbound network, and no generator
session/browser/home. Neither role gets Docker sockets, host root, shared Hermes
data or GitHub/Linear write credentials. Tests execute only in the source role.
Both containers have read-only images, bounded tmpfs/process/CPU/memory, dropped
capabilities, and no-new-privileges. The host gateway retains Docker authority;
workers retain only the private gateway socket.

The implementation follows the split credential/browser pattern in unmerged
[PR 65](https://github.com/Driftward-LLC/factory/pull/65), revision
`d2a00f5681c1dce6aa3efe9f5572c221a86e1ec7`, including its explicit Codex shell,
app, and plugin disabling. It does not merge that pilot or claim its canary.
Managed Driftward Engineering `review-code` and `verify-app` instructions are
provisioned as verified digest snapshots, explicitly read into the prompt;
installed plugin presence alone is insufficient.

Activation requires a reviewed operator change. Do not copy root/shared Hermes
auth. Provision a new reviewer-only Codex auth file and separate source-only
GitHub read credential. Their root-owned mode-0600 files are respectively
`/etc/driftward/factory-reviewer/auth.json` and `source-token`. Source credentials
are used only by host Git fetch subprocesses, never mounted into either role.
Provision root-owned private bare mirrors at
`/var/lib/driftward-factory-reviewer/repositories/OWNER/REPO.git`; they contain
no shared auth. Native fixed HTTPS fetch updates the PR and configured base
under a per-repository lock, snapshots them to a bundle, and checks exact PR
SHA again before returning. The base ref is resolved once and its immutable
SHA is used throughout inspection. New commits require a new review ID.

Build `deploy/independent-reviewer/Dockerfile`: it pins the reviewed Node base
digest and the PR 65 dependency lock for Codex 0.160.1 and Playwright MCP 0.0.83,
installs Git/Python and Playwright Chromium, and contains no auth, ambient Codex
config/hooks/plugins or stored browser data. Project-specific offline test
dependencies may require a separately reviewed derived image. Pin the final image
by digest. A missing browser/dependency/check blocks a meaningful review and must
yield an inconclusive result.
The host installer packages these public assets under
`/usr/local/share/driftward-factory/independent-reviewer/`, preserving the
`deploy/independent-reviewer` build-context paths. The gateway's existing esbuild
step includes the adapter; its existing proto install includes the new RPC.
An operator can build from the packaged public directory with
`docker build -f deploy/independent-reviewer/Dockerfile .`.
The portable release is not a host gateway installer and does not enable this path.

Provision the internal `factory-reviewer` Docker network and a separately
reviewed `factory-reviewer-egress:3128` HTTP CONNECT proxy on it. Its allowlist
must permit only the required official provider/auth endpoints, never host/VPS
services or arbitrary destinations. The source network is freshly created per
invocation and has no proxy. The agent has both networks; source has only the
fresh internal network. Internal network metadata alone is **not** a complete
egress canary. No proxy is installed or enabled by this change.
Both networks must use Docker 28+ IPv4 bridge gateway mode `isolated`, with IPv6
disabled. The adapter checks this for the provisioned network and creates the
fresh source network with that option. This avoids internal networks retaining
a reachable host bridge address; see [Docker's gateway-mode documentation](https://docs.docker.com/engine/network/port-publishing/).

Root-owned mode-0600 `/etc/driftward/factory-reviewer.json` must contain:

```json
{
  "schema": "factory-isolated-reviewer-provisioning-v1",
  "image": "reviewer@sha256:REVIEWED_IMAGE_DIGEST",
  "canary_digest": "DIGEST_OF_RETAINED_OPERATOR_CANARY_EVIDENCE",
  "policy_digest": "CURRENT_FACTORY_REVIEW_POLICY_DIGEST",
  "review_skill_digest": "SHA256_OF_REVIEW_CODE_SNAPSHOT",
  "verify_skill_digest": "SHA256_OF_VERIFY_APP_SNAPSHOT",
  "repositories": {"Driftward-LLC/factory": {"base_ref": "main"}}
}
```

The two skill snapshots live at `review-code.md` and `verify-app.md` beside auth,
also root-owned mode 0600. A real operator canary must independently demonstrate
exact Astra access, source refresh with a read-only identity, source/browser/test
functionality, no provider/platform auth inside source role, no inherited
generator home/MCP/browser/session, forbidden egress denial, native SHA/finding
binding, and one model invocation across response-loss retries. Record raw
non-secret evidence and its digest before setting
`DRIFTWARD_FACTORY_REVIEWER_ENABLED=1` on the host gateway. The bridge's own
review/repair policy gate must also be deliberately configured. This task does
not provision credentials, build/enable the production image, or run that canary.

The private gateway journal reserves each exact review ID before launch and
uses the authenticated read-only bridge authorization endpoint before any new
provider call. The host needs `HATCHET_HERMES_BRIDGE_URL`,
`HATCHET_HERMES_BRIDGE_TOKEN` and `HATCHET_HERMES_INSPECTION_TOKEN`; these stay on
the gateway and never enter either container. A claimed current review with
matching capability, policy, original source and live head is required. Receipt
retrieval validates identity against the same endpoint without authorizing a
new invocation. Missing setup and fabricated or unreserved jobs fail closed.

The journal
atomically persists bounded receipts. Same-ID intent drift conflicts. An
ambiguous reservation returns inconclusive; it never re-executes. Do not delete
reservations to retry model calls. A crash can retain a repository lock or
container: inspect the exact native journal/container first and perform reviewed
cleanup; never treat missing output as permission to rerun a reserved review.
Only bounded receipts persist; source bundles/request/token files are removed
after execution. Model streams, prompts, source and tool logs are not retained.
An inconclusive receipt's isolation object denotes the required configured
boundary, not proof that an unavailable backend ran or completed containment.
