# Containerized Service Runbook

Use this when adding or materially changing a Driftward VPS service that runs
under Docker or Docker Compose. The goal is a service that is reproducible,
observable, capped, restartable, and clear about ownership.

## Scope Decision

Classify the service before creating files:

| Scope | Use when | Runtime root |
| --- | --- | --- |
| `host-level` | The service supports the VPS or shared tooling across repos | `/docker/driftward/<service>` |
| `service-scoped` | The service is one Driftward application or workflow | `/docker/driftward/<service>` |
| `personal` | The service belongs to Derek's personal runtime | `/docker/personal/<service>` |

Do not put Driftward company services under `/docker/personal`, and do not copy
company credentials into a personal runtime. If the service is a scheduled job,
MCP integration, browser workflow, chat gateway, memory, or agent automation,
check whether it belongs in Driftward Hermes before creating a new runtime.

## Required Artifacts

Every containerized service should have:

- Source or configuration in an owned repo.
- Runtime directory under `/docker/driftward/<service>` or
  `/docker/personal/<service>`.
- `docker-compose.yml` with explicit image/build source, volumes, env files,
  restart policy, health checks when available, and resource caps.
- Protected env or secret file outside git.
- Status, logs, restart, deploy, rollback, and smoke-test commands.
- A `host/registry.json` entry when the service becomes a durable host runtime.
- A short service doc or README naming owner, scope, paths, credentials, and
  alerting behavior.

## Filesystem Layout

Use separate paths for source, runtime state, and secrets:

```text
/root/driftward/repos/<owner>/<repo>/     # source checkout, if repo-owned
/docker/driftward/<service>/              # compose project and runtime files
/docker/driftward/<service>/data/         # durable service data
/docker/driftward/<service>/logs/         # local logs, only if needed
/etc/driftward/<service>.env              # protected host env file
/usr/local/bin/<service>-smoke            # optional host smoke test
```

For personal services, replace `/docker/driftward` with `/docker/personal` and
do not use `/etc/driftward` for personal-only secrets.

Keep generated caches out of source repos unless they are intentionally tracked.
Do not relocate or delete runtime state until the owning service is stopped or
migrated intentionally.

## Compose Template

Start from this shape and tighten it for the actual service:

```yaml
services:
  app:
    image: ghcr.io/driftward-llc/example-service:2026-08-09
    restart: unless-stopped
    env_file:
      - /etc/driftward/example-service.env
    volumes:
      - ./data:/app/data
    ports:
      - "127.0.0.1:8080:8080"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    cpus: "0.50"
    mem_limit: 256m
```

Default to localhost-bound ports. User-facing web URLs must be exposed through
Tailscale Serve or another approved Tailscale-only path. Expose public ports
only after the ingress, TLS, authentication, and alerting model is explicit.

Pin images by immutable tag or digest for production services. For source-built
services, persist the selected tag or image reference in the installed runtime
configuration, such as a systemd drop-in, and verify the source checkout matches
the release tag before building. Floating tags such as `latest` are acceptable
only for disposable experiments.

## Systemd Template

Use systemd when the service should start on boot or be operated like a host
service. Keep the unit in repo-managed source when practical, then install it
to `/etc/systemd/system`.

```ini
[Unit]
Description=Driftward Example Service
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
Documentation=file:/root/driftward/repos/factory/docs/containerized-service-runbook.md

[Service]
Type=simple
WorkingDirectory=/docker/driftward/example-service
Environment=COMPOSE_PROJECT_NAME=driftward-example-service
ExecStart=/usr/bin/docker compose up --remove-orphans
ExecStop=/usr/bin/docker compose down --remove-orphans
Restart=always
RestartSec=10
TimeoutStartSec=120
TimeoutStopSec=45
CPUQuota=50%
MemoryMax=384M

[Install]
WantedBy=multi-user.target
```

Validate and install:

```bash
systemd-analyze verify /etc/systemd/system/driftward-example-service.service
systemctl daemon-reload
systemctl enable --now driftward-example-service.service
systemctl status driftward-example-service.service --no-pager
```

## Secrets

Secrets must stay outside git. Store only references to their paths in docs,
issues, PRs, and logs.

Recommended Driftward host locations:

```text
/etc/driftward/<service>.env
/root/.config/<service>/
/docker/driftward/<service>/data/.env
```

Set directories to `0700` and secret files to `0600` unless the service user
requires narrower ownership. Never paste credential values into GitHub issues,
Linear, Codex messages, commit messages, docs, or smoke-test output.

## Smoke Test

File state is not implementation evidence. A service is not deployed until the
live container passes a smoke test.

Minimum smoke test:

```bash
systemctl is-active --quiet driftward-example-service.service
docker compose --project-directory /docker/driftward/example-service ps
curl -fsS http://127.0.0.1:8080/health
```

For durable services, add a script such as `/usr/local/bin/<service>-smoke`
that checks:

- Systemd unit is active.
- Expected container is running.
- Health endpoint or equivalent command succeeds.
- User-facing web endpoints are reachable through Tailscale when they are meant
  for human access.
- Required env is present without printing secret values.
- Required mounts are present and writable only where intended.
- External dependency probes are narrow and read-only unless mutation is the
  point of the service.

## Registry Entry

Add a `host/registry.json` entry after the runtime path is stable:

```json
"example_service": {
  "kind": "runtime",
  "scope": "service-scoped",
  "current": {
    "host_path": "/docker/driftward/example-service"
  },
  "target": {
    "host_path": "/docker/driftward/example-service"
  },
  "notes": "Short owner, credential, or migration note."
}
```

Use a stable snake_case registry key. Do not use registry entries to store
secrets or volatile runtime state.

## Deploy Flow

1. Confirm owner, scope, runtime root, source repo, and secret locations.
2. Create the runtime directory and protected env files.
3. Add `docker-compose.yml` and any service-local README.
4. Start manually with `docker compose --project-directory <root> up`.
5. Run the smoke test against the live container.
6. Add and verify a systemd unit if the service should persist.
7. Add or update `host/registry.json`.
8. Document status, logs, restart, deploy, rollback, and smoke commands.
9. Commit source changes without secrets.
10. If credentials, Compose wiring, or Driftward Hermes behavior changed, run
    the relevant runtime smoke test before marking the work complete.

## Operations

Use these command shapes in the service doc:

```bash
systemctl status driftward-example-service.service --no-pager
journalctl -u driftward-example-service.service -n 100 --no-pager
docker compose --project-directory /docker/driftward/example-service ps
docker compose --project-directory /docker/driftward/example-service logs --tail=100
systemctl restart driftward-example-service.service
```

Rollback should be explicit. For image-based services, record the previous
image tag or digest before updating. For source-built services, record the
release tag and commit SHA that produced the running image.

## Stop Or Retire

Before removing a service:

1. Confirm no active owner or dependency still needs it.
2. Stop and disable the systemd unit.
3. Preserve or archive durable data if needed.
4. Remove installed units, wrappers, and bootstrap references.
5. Update `host/registry.json`, docs, and any issue handoffs.
6. Verify no live container, timer, open port, or credential loader remains.

Use issues for uncertain ownership rather than deleting runtime state on a
guess.
