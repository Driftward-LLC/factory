# Factory Docker Deployment

For a **new Linux/amd64 dry-run installation**, start with
[the portable release guide](PORTABLE.md). That path uses published images,
private per-installation configuration and namespaced state. The installer and
cutover procedure below document the legacy managed-host migration and external
execution integration. They are not the default installation or upgrade path.

To prepare a move from that existing Compose app to a new installation, follow
[same-host cutover preparation](CUTOVER.md). The new helper previews first and
can only copy an already stopped, quiescent source; it does not start the
candidate or switch production routing. The legacy host-to-Docker gate below
is a different migration and should not be used to rename the live deployment.

Factory should deploy as a Docker Compose application owned by this repository.
The host keeps Docker, the Compose wrapper, systemd supervision, and protected
env files; the Factory Compose app owns Hatchet Lite/Postgres plus the CLI,
Hatchet workers, bridge, ingress, lifecycle writer, Linear token renewer,
manifests, and checked-in runtime code.

## Legacy managed-host defaults

- Scope: one Factory installation.
- Source: `<source-checkout>`.
- Runtime root: `/docker/driftward/factory`.
- Compose file: `/docker/driftward/factory/docker-compose.yml`.
- Systemd wrapper: `driftward-factory.service`, ordered after
  `driftward-hermes.service` and `driftward-hermes-factory-gateway.service`.
- Hermes gateway: `driftward-hermes-factory-gateway.service`, listening on
  `unix:///run/driftward-hermes-factory/hermes.sock`.
- Durable data lives in Docker named volumes:
  - `driftward-factory-hatchet-postgres-data`
  - `driftward-factory-hatchet-config-data`
  - `driftward-factory-bridge-data`
  - `driftward-factory-ingress-data`
  - `driftward-factory-lifecycle-data`
  - `driftward-factory-linear-token-data`
- Hatchet key material: `/docker/driftward/factory/keys/hatchet`
- Local operator socket: `/run/driftward-factory-operator/operator.sock`.
- Local Factory Console: `http://127.0.0.1:19881/`.
- Local Hermes gateway socket: `/run/driftward-hermes-factory/hermes.sock`.

## Boundaries

Factory containers must not mount Hermes data, provider credentials,
browser state, root GitHub auth, the Docker socket, or the host root filesystem.
Hatchet Lite and its Postgres database are part of the Factory Compose app.
Real agent execution still belongs to Hermes. The host systemd unit
orders Factory after Hermes, but Dockerized Factory records and fences work
through the bridge, then reaches Hermes only through an explicit approved
gRPC gateway. The Factory Compose app mounts only
`/run/driftward-hermes-factory`; it must not mount the Docker socket, Hermes
data, provider credentials, browser state, or host root filesystem.
The retry operator also mounts Factory ingress state read-only so it can fence
manual retry eligibility against the latest trusted Linear observation.

The Docker worker defaults are safe for source deployment and dry-run intake
when protected env files are absent:

```text
HATCHET_INTAKE_DRY_RUN defaults to true
DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED defaults to disabled
```

Real Docker effects use the host-local Hermes Factory gateway. The installer
seeds `DRIFTWARD_HERMES_GRPC_TARGET` in
`/etc/driftward/hatchet-worker-bridge-executor.env` when absent:

```text
DRIFTWARD_HERMES_GRPC_TARGET=unix:///run/driftward-hermes-factory/hermes.sock
DRIFTWARD_HERMES_AUTH_MODE=unknown
```

The gateway may bind TCP later by setting `DRIFTWARD_HERMES_GRPC_TARGET` or
`DRIFTWARD_HERMES_GRPC_BIND_TARGET` plus `DRIFTWARD_HERMES_GRPC_ALLOW_TCP=1`
and TLS plus `DRIFTWARD_HERMES_GRPC_AUTH_TOKEN`, or TLS with enforced client
certificates (`DRIFTWARD_HERMES_GRPC_CA_PATH`). Server-only TLS does not
authenticate callers. Explicit insecure development TCP is limited to literal
loopback addresses and still requires a token. Do not solve Factory to
Hermes access by copying Hermes credentials or session state into Factory.
The gateway is bundled during install and requires a host Node.js 18+ binary;
override the default `/usr/bin/node` with `DRIFTWARD_FACTORY_NODE_BIN` if the
host keeps Node elsewhere.

Set the non-secret `DRIFTWARD_HERMES_AUTH_MODE` deployment label to
`chatgpt_codex_oauth` or `api_key` only when that route is explicitly
configured. It defaults to `unknown`; the label supports analytics and never
copies provider credentials into Factory.

Set optional `DRIFTWARD_HERMES_VERSION` from reviewed deployment configuration
when known. Do not configure provider/model labels in Factory: the executor
reads Hermes's native configuration and pins the selected model/provider on
the invocation. It reports only those bounded identifiers to the worker, not
credentials or session state. The retired `DRIFTWARD_HERMES_MODEL` and
`DRIFTWARD_HERMES_PROVIDER` variables are ignored and removed by the installers.
The current worker always records the truthful
route `factory-default-v1` / `hermes` / `static`. It deliberately ignores
experiment-label environment variables: a future comparison must first add a
real, reviewed router with stable assignment and lineage stickiness.

## Required Env Files

The Compose app reads existing protected env files under `/etc/driftward`:

```text
/etc/driftward/hatchet-workers.env
/etc/driftward/hatchet.env
/etc/driftward/hatchet-bridge.env
/etc/driftward/hatchet-verifier.env
/etc/driftward/hatchet-worker-bridge-executor.env
/etc/driftward/hatchet-worker-reconciliation.env
/etc/driftward/hatchet-event-ingress.env
/etc/driftward/factory-operator.env
/etc/driftward/factory-lifecycle-writer.env
/etc/driftward/factory-linear-token.env
```

Keep those files mode `0600`. Do not commit them or paste their values.
`/etc/driftward/factory-linear-token.env` should contain the Factory-owned
Linear OAuth client values as `FACTORY_LINEAR_CLIENT_ID` and
`FACTORY_LINEAR_CLIENT_SECRET`. Do not mount root's `/root/.config/linear`
credential directory into Factory containers.

The Compose app also needs migrated Hatchet key material under
`/docker/driftward/factory/keys/hatchet`. The private and master keys are
runtime secrets and must not be checked into this repository.

## Cutover Gate

Installing release files does not migrate historical records. Before starting the
Factory runtime, migrate existing Hatchet state into Docker named volumes and
key material into the runtime root:

```text
driftward-factory-hatchet-postgres-data
driftward-factory-hatchet-config-data
driftward-factory-bridge-data
driftward-factory-ingress-data
driftward-factory-lifecycle-data
driftward-factory-linear-token-data
/docker/driftward/factory/keys/hatchet
```

Legacy host-bind state under `/docker/driftward/factory/data/*`, if present,
is a migration source or archive only. It is not the live Docker runtime store.

Then stop the legacy host-managed Hatchet unit:

```bash
systemctl disable --now driftward-hatchet.service
```

`deploy/install-docker.sh --start` fails closed while
`driftward-hatchet.service` is active or the migrated Hatchet DB/key material is
missing. If legacy bridge, ingress, or lifecycle SQLite files exist under the
old host-bind path, start also fails until matching files exist inside the
Docker volumes. That prevents accidentally starting an empty Factory control
plane on the same ports and ledgers.

## Release deployment

Use published release images and the matching bundle for normal installation
and upgrades. See the [release runbook](../docs/factory-release-runbook.md) and
[portable guide](PORTABLE.md). Optional [deployment automation](../docs/automatic-deployment.md)
pulls the same published image after verifying its provenance and compatibility.

The source-building `install-docker.sh` helper below is retained for migration
and recovery of the legacy managed-host layout. It is not the release consumer
path and must not be used to bypass a managed installation's compatibility gates.

## Legacy host installer

Install the Compose wrapper without starting it:

```bash
bash deploy/install-docker.sh
```

Install and start the current checkout in one step:

```bash
bash deploy/install-docker.sh --start
```

By default this pins and builds `driftward/factory:local`. Use
`--image-tag TAG` for a named Factory image tag or `--image IMAGE` for a full
image reference.

## Operations

The Factory operator container also serves the Factory Console (read-only by default) on
loopback port `19881`. Publish it privately through Tailscale Serve, never
through Funnel or a public listener:

```bash
tailscale serve --bg --https=19881 http://127.0.0.1:19881
factory console
```

The console requires the `Tailscale-User-Login` identity header on every page
and API request; `/health` is the only unauthenticated endpoint. For local
development only, set `FACTORY_CONSOLE_ALLOW_UNAUTHENTICATED=1`. This bypass
**always disables ownership commands**, including requests carrying an identity.

### Per-issue manual ownership (opt-in)

After a reviewed rollout and bridge schema-8 migration, an operator may enable
the narrow console approval API in the protected `factory-operator.env` file:

```dotenv
FACTORY_CONSOLE_PUBLIC_ORIGIN=https://your-host.your-tailnet.ts.net:19881
FACTORY_CONSOLE_OPERATOR_LOGINS=operator@example.com
```

Use the exact HTTPS origin, without a trailing slash, path, query, or credentials.
The login list is comma-separated and exact-match. Missing/invalid configuration
leaves commands disabled. This change does not enable them automatically.
Deploy all bridge/operator/dispatcher processes from the same schema-8 release;
do not run mixed versions against the shared database. Use the existing reviewed
backup/quiesce/migration procedure, not console-triggered migration.

Tailscale identity headers are **not cryptographic authentication on their own**.
Only the trusted Tailscale Serve proxy and trusted local/runtime processes may
reach port 19881. Verify host firewall, loopback publication, container-network
access, and tailnet ACLs before enabling commands. Do not put an untrusted proxy
or public listener in front of it. POSTs additionally require exact Origin,
JSON content type, and the console's action header; forwarded host or browser
supplied actor fields never confer authority. There is no generic root-operator
proxy or browser dead-letter archive action.

In an issue's **Execution control** panel:

1. Choose **Take manual control**, enter a reason, preview, then confirm. The
   durable hold fences new intake, queued execution, verification, and repairs.
2. Wait for **Manual control—safe to edit**. Handoff pending means execution is
   still unsettled. An already-authorized external effect can finish and record
   its receipt; cancellation and lease expiry do not prove it has stopped. An
   unknown outcome requires the existing reviewed operator recovery procedure.
3. To start over, choose **Return for a fresh attempt**, preview, and confirm.
   The latest stored Linear observation must be Ready, unblocked, and routed to
   an enabled repository. A previous PR must be closed/unmerged with a current
   worker inspection and exact artifact fencing. Missing/stale inspection or an
   open/merged PR blocks handback. The preview uses cached provider observations,
   not a synchronous live Linear query, and displays the recorded update time.

Handback atomically clears the hold and reserves one new generation/outbox event;
normal configured verification/repair policy then applies. Failed reservation
retains manual control. Once controlled, this issue requires explicit future
reservations; ordinary webhook intake stays fenced so delayed pre-hold events
cannot regain authority. Linear workflow status is not changed by these commands.
Fresh handback checks recorded Factory artifacts; it does not discover or adopt
a separate PR or branch created manually. Reconcile those artifacts deliberately
before approving a new attempt.
Verification or continuation of manually edited **open PRs is not implemented**;
it needs a new commit-bound validation/execution contract, not an old-job resume.

If confirmation times out, use **Check previous request**. Its durable command ID
recovers the committed result even after token expiry or a server restart, without
duplicating execution. Ownership audit history retains actor, time, and reason.

```bash
systemctl status driftward-factory.service --no-pager
systemctl status driftward-hermes-factory-gateway.service --no-pager
journalctl -u driftward-factory.service -n 100 --no-pager
journalctl -u driftward-hermes-factory-gateway.service -n 100 --no-pager
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory ps
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory logs --tail=100
systemctl reload driftward-factory.service
systemctl restart driftward-hermes-factory-gateway.service
factory-docker-smoke
```

Rollback to the previous release by checking out the prior tag and running
`bash deploy/install-docker.sh --image-tag <tag> --start`. Preserve the
`driftward-factory-*` Docker volumes and `/docker/driftward/factory/keys/*`
unless there is a separately approved record migration or restore plan.

`factory status` treats container OOM state, unexpected restarts, failed Docker
health checks, Hatchet readiness, desired-state drift, release provenance, and
actionable reconciliation findings for active work or system inventory as
first-class signals. Findings attached to terminal jobs are reported
separately as nonfatal reconciliation debt. The ingress health
endpoint is unhealthy while persisted events await Hatchet publication; the
shared outbox dispatcher retries those rows with a lease every 30 seconds.
The same cycle advances bounded recovery for observed effect receipts whose
read-only PR inspection worker never returned; it does not reinvoke Hermes.
Lifecycle status reports permanent dead letters by bounded reason code. The
operator mounts only the lifecycle named volume needed for root-only,
snapshot-fenced dead-letter archival; that action remains unavailable to the
browser, even when per-issue ownership approvals are enabled.
Reconciliation runs natively in Hatchet every 15 minutes.
