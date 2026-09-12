# Driftward Hatchet Lite

This is the Factory-owned Hatchet Lite control plane for Driftward
bridge-backed intake. It is deployed with the standalone Factory Docker
Compose application and remains intentionally local-only and low-volume. See
`docs/factory-standalone-app-boundary.md` for the ownership split and
`hatchet/desired-state/factory-hatchet.json` for checked-in desired state.

## Scope

- Runtime root: `/docker/driftward/factory`
- Systemd unit: `driftward-factory.service`
- Compose project: `driftward-factory`
- Local dashboard/API: `http://127.0.0.1:8888`
- Private dashboard/API URL: `https://srv1379035.tail5c6a78.ts.net:8888/`
- Local Factory Console: `http://127.0.0.1:19881`
- Worker gRPC: `127.0.0.1:7077`
- Health port: `127.0.0.1:8733`
- Secret env: `/etc/driftward/hatchet.env`
- Smoke test: `factory-docker-smoke`

Hatchet remains a control plane only. Do not mount Driftward Hermes provider
credentials, browser credentials, GitHub credentials, Linear credentials, MCP
credentials, or profile/session state into this runtime.

The dashboard is user-facing and must remain exposed through Tailscale Serve,
not a public listener. The local container binding stays on `127.0.0.1`.

## Hermes Bridge

Bridge source lives under `hatchet/bridge`. It is the local-only execution
ledger for Hatchet-to-Hermes jobs: Hatchet workers submit idempotent, bounded
requests; the bridge records a durable job row plus append-only events; Hermes
execution happens behind allowlisted actions.

Runtime:

- Compose service: `bridge`
- Database: `/var/lib/driftward-hatchet-bridge/bridge.db`
- Local API: `http://127.0.0.1:19877`

The bridge binds to `127.0.0.1` only and should never expose a generic shell or
Hermes credential mount to Hatchet workers. The managed reconciliation path is
the `reconciliation-worker`, which records bounded bridge, Hatchet, ingress,
GitHub, and Linear drift findings without direct provider credentials. The
legacy bridge reconciler timer and service are retired from the automatic
runtime and should remain disabled; its `reconcile` CLI is break-glass recovery
only from the immutable bridge release.

## Worker Source

Worker plans, event names, and source live under `hatchet/workers`. The first
workers are `intake-worker`, `execution-dispatch-worker`,
`bridge-executor-worker`, `pr-event-worker`, and `reconciliation-worker`. They
run as Factory Compose services and prove the bridge-backed intake paths:
dispatch a `create_draft_pr` job manually or from a gated Linear ready event,
fence it through the bridge ledger, emit Hatchet events, observe the synthetic
PR-created event, and inspect bridge ledger consistency.

Merge, deploy, and release workers remain out of scope until real draft PR
creation, checks, reviews, idempotency, and reconciliation are proven.

`hatchet/workers/catalog.json` is the operator-facing catalog for worker and
workflow purpose. Every registered Hatchet workflow should have a catalog entry
that states what it does, what it explicitly does not do, which events it
consumes and produces, and which workflow normally follows it. Workflow names
must use the responsibility-first shape documented in `hatchet/workers/README.md`
so dashboard rows can be understood without reading source.

## Event Ingress

Event ingress source lives under `hatchet/event-ingress`. It is a local-only
HTTP service that accepts webhook-shaped provider events, normalizes them,
dedupes them in SQLite, and publishes Hatchet events without giving Hatchet
workers provider credentials.

Runtime:

- Compose service: `event-ingress`
- Public Linear webhook proxy: `linear-webhook-proxy`
- Database: `/var/lib/driftward-hatchet-event-ingress/ingress.db`
- Local API: `http://127.0.0.1:19878`
- Public Linear webhook URL: `https://srv1379035.tail5c6a78.ts.net/events/linear/issue`

Supported events:

- `driftward.linear.issue.observed`, produced from Linear Issue webhooks that
  do not pass implementation gates.
- `driftward.linear.issue.ready_for_implementation`, produced from Linear
  Engineering Issue webhooks that pass implementation gates.
- `driftward.github.pr.created`, produced from GitHub `pull_request.opened`
  payloads.
- `driftward.github.pr.synchronized`, produced from GitHub
  `pull_request.synchronize` payloads.
- `driftward.github.pr.closed_unmerged`, produced from GitHub
  `pull_request.closed` payloads where `merged=false`.
- `driftward.github.pr.closed_merged`, produced from GitHub
  `pull_request.closed` payloads where `merged=true`.

Canonical event IDs and wire names live in `hatchet/contracts/events.json`.
Run `python3 tools/hatchet-contracts.py check` before committing contract,
catalog, ingress, or worker event changes.

Ingress is not an execution trigger by itself. It records and publishes control
plane events; Hermes dispatch still belongs behind workers and the bridge. The
public URL is a Tailscale Funnel route to a narrow proxy and must not expose
the full local ingress API.

## Operations

Use `factory` as the main operator entry point for the current factory path.
It is the front door over Hatchet intake, the bridge ledger, Driftward Hermes,
the lifecycle writer, retry operator, and recovery components:

```bash
factory status
factory doctor hatchet --profile prod
factory mode
factory mode dry-run
factory mode real --yes
factory logs linear -f
factory logs all
factory attempts list ENG-62
```

Reach for direct `systemctl`, `curl`, Docker, or Tailscale commands only when
installing, debugging a specific component named by `factory status`, or doing
break-glass recovery.

Install or refresh the complete Factory Compose runtime and its host smoke
helper together:

```bash
bash deploy/install-docker.sh
cmp deploy/factory-docker-smoke /usr/local/bin/factory-docker-smoke
```

Use the Factory installer so the runtime Compose file, CLI, desired state, and
`/usr/local/bin/factory-docker-smoke` cannot lag the source under review.

Check factory path health:

```bash
factory status
factory-docker-smoke
```

Read recent logs:

```bash
factory logs all
factory logs ingress
factory logs intake
factory logs executor
factory logs pr
factory logs reconciliation
factory logs bridge
factory logs lifecycle
```

Follow the Linear intake path live:

```bash
factory logs linear -f
```

Use these in separate terminals while changing a Linear Engineering issue to
`Ready`. The expected path is:

```text
event-ingress
  receives and verifies the Linear webhook
  emits driftward.linear.issue.ready_for_implementation only when immutable project execution policy is enabled

intake-worker
  consumes the ready event
  reserves or reuses one active factory-attempt-backed create_draft_pr job for the issue

factory outbox dispatcher
  emits driftward.hermes.job.accepted for the reserved bridge job

bridge-executor-worker
  consumes driftward.hermes.job.accepted
  records the draft artifact, then verifies it before human-review readiness
```

Check or change Hatchet intake mode:

```bash
factory mode
factory mode dry-run
factory mode real --yes
```

`dry-run` is the default. `real` allows Ready Linear issues to create real
bridge jobs for Driftward Hermes draft PR execution. This mode toggle is not
the retired unattended factory/controller stack.

Use `factory doctor hatchet` for read-only drift checks against the desired
state manifest before reviewing any workflow pause/remove action. The doctor
command reports stale quickstart/smoke workflows and task actions with no
active repo-managed worker, but it never mutates Hatchet. Use `--profile` to
collect live Hatchet state through the CLI, or `--live-json` to compare a saved
sanitized inventory snapshot.

The reviewed `workers/routing-registry.json` also controls whether a Linear
project may execute at all. Disabled projects remain observable at ingress but
never create bridge jobs, regardless of dry-run or real mode.

The root-owned Factory lifecycle writer polls only safe local ingress
projections and the authenticated bridge ledger. It is the only Shipwright
Linear client; workers, bridge, ingress, and Hermes do not receive its token.
It permanently records dry-run rows as suppressed and comments only for real
started, verified-ready success, terminal failure, and routing setup failure.
Check its local-only health with `curl -fsS http://127.0.0.1:19880/status`.

Inspect stored state for focused debugging:

```bash
curl -fsS 'http://127.0.0.1:19878/events?limit=10' | jq
curl -fsS -H "Authorization: Bearer $(awk -F= '$1 == \"HATCHET_HERMES_BRIDGE_TOKEN\" { print $2; exit }' /etc/driftward/hatchet-bridge.env)" 'http://127.0.0.1:19877/jobs?limit=10' | jq
```

The bridge event history endpoint is bounded and returns `{events,total}`. Use
its `limit` only within `1..250`; reconciliation treats `total > events.length`
as incomplete coverage.

Use the ingress events endpoint to confirm the webhook was normalized and
deduped. Use the bridge jobs endpoint to confirm the intake worker created the
expected idempotent job. Ingress is per event; bridge execution is per active
Linear issue/work item.

Check Docker and Tailscale only when debugging the Hatchet control plane or
exposure layer directly:

```bash
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory ps
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory logs --tail=100
tailscale serve status
tailscale funnel status
factory-docker-smoke
```

Restart individual services only for focused remediation after the CLI or smoke
test identifies a component:

```bash
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory up -d --no-deps <service>
factory-docker-smoke
```

### Post-Restart Validation And Rollback

Reloading `driftward-factory.service` converges the Compose application without
stopping unchanged services or PostgreSQL. Workers recreated by an image
change can record transient `UNAVAILABLE` listener and heartbeat errors while
they reconnect. Treat those records as expected only inside that deploy
window; wait for health, run the smoke test, then observe two clean minutes.

```bash
for unit in intake execution-dispatch bridge-executor pr-event reconciliation; do
  docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory logs --since 2m "${unit}-worker" \
    | rg -i 'failed to send heartbeat|heartbeat interval delay|listener encountered an error|unavailable|unauthenticated' \
    || true
done

postgres_pid="$(docker inspect -f '{{.State.Pid}}' driftward-factory-postgres-1)"
postgres_cgroup="/sys/fs/cgroup$(awk -F: '$1 == \"0\" { print $3 }' "/proc/${postgres_pid}/cgroup")"
cat "${postgres_cgroup}/memory.current"
cat "${postgres_cgroup}/memory.peak"
cat "${postgres_cgroup}/memory.swap.current"
cat "${postgres_cgroup}/memory.events"
```

For the PostgreSQL capacity guardrail, require a healthy container with the
configured 2 GiB memory cap and 256 MiB cgroup swap allowance (`memswap_limit:
2304m`). Require no increase in `memory.events` `oom` or `oom_kill`, and keep
`memory.peak` below 90% of the 2 GiB cap during the observation window.
Small, bounded cgroup swap use is permitted, but investigate sustained growth or
material host `pswpin`/`pswpout` churn. Memory and CPU PSI `full` must remain
zero throughout the window.

The installer automatically restores the previously running image if a new
image fails smoke validation. To roll back later, deploy a known-good Factory
release tag and repeat the smoke and clean-window checks:

```bash
git switch --detach <known-good-release-tag>
bash deploy/install-docker.sh --image-tag <known-good-release-tag> --start
```

Manual start without systemd:

```bash
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory up -d
```

Manual stop:

```bash
docker compose --env-file /etc/driftward/hatchet.env --project-directory /docker/driftward/factory stop
```

Restore the Tailscale dashboard route:

```bash
tailscale serve --bg --https=8888 http://127.0.0.1:8888
```

## Secrets

`/etc/driftward/hatchet.env` contains the Postgres password, Hatchet admin
password, cookie secrets, Hatchet server URL, and cookie domain. Keep it mode
`0600`; do not commit or paste its contents into Linear, GitHub, Codex, or
logs.

The key files under `/docker/driftward/factory/keys/hatchet` are generated by
the Hatchet admin image and mounted read-only into `hatchet-lite`.

`/etc/driftward/hatchet-bridge.env` contains the bridge-local bearer token.
`hatchet/bridge/install.sh` generates it when absent and keeps the file mode
`0600`; it is delivered only to the protected worker environment.

`/etc/driftward/hatchet-workers.env` contains the Hatchet worker client token
and connection settings. `/etc/driftward/hatchet-worker-bridge-executor.env`
contains the explicit Driftward Hermes execution gate and expected GitHub App
identity. Keep both files mode `0600`.

`/etc/driftward/hatchet-event-ingress.env` is optional and holds ingress-local
settings such as the GitHub webhook signature secret. Keep it mode `0600` if it
exists.

## Upgrade Path

This pilot uses Hatchet Lite with Postgres as the datastore and message queue.
The next production step is the full Hatchet Docker Compose topology with
separate API, engine, dashboard, migration/setup jobs, Postgres, and RabbitMQ.
Before switching, back up Postgres, preserve key material, and verify worker
tokens and gRPC broadcast addresses.
