# Hatchet Event Ingress

Local-only event ingress for external provider events that should enter Hatchet
without giving Hatchet workers provider credentials.

## Why Not Direct Hatchet Webhooks

Hatchet webhooks are acceptable for trusted internal triggers, manual smoke
checks, or simple events that can safely start a workflow as received. Provider
webhooks from Linear, GitHub, or similar external systems must enter through
Driftward event ingress first.

Driftward ingress is the provider boundary:

```text
external provider webhook
  -> verify provider signature and timestamp
  -> normalize payload into Driftward event shape
  -> record a stable provider event key
  -> apply observation and execution gates
  -> emit Driftward-owned Hatchet event
```

Do not wire Linear or GitHub provider webhooks directly to Hatchet workflow
webhook URLs. Direct Hatchet webhooks skip the Driftward checks that keep
provider events observational until they are explicitly eligible for execution.

## Current Slices

### Linear Issue Ingress

Linear is the work source of truth. Issue webhooks enter Hatchet here before
any worker is allowed to dispatch Hermes.

```text
Linear Issue create/update/remove
  -> normalize issue snapshot
  -> record in SQLite with a stable event key
  -> publish driftward.linear.issue.observed
  -> publish driftward.linear.issue.ready_for_implementation only if gates pass
  -> do not dispatch Hermes from ingress
```

Implementation readiness gates:

```text
team = Engineering
status = Ready
no active blockedBy relation
status is not Done, Canceled, In Review, Needs Operator, or Blocked
delegate is not Driftward Factory
project execution policy is enabled for the immutable project ID
```

For eligible and observation events alike, ingress preserves the immutable
Linear `projectId` and diagnostic `projectName` from the webhook. Intake uses
only `projectId` with its reviewed routing registry; the name cannot select a
repository.

Ingress reads the same non-secret `HATCHET_INTAKE_ROUTING_REGISTRY_PATH` used
by intake. A disabled project, or a missing or malformed policy, remains a
persisted `driftward.linear.issue.observed` event with `dispatchEligible=false`;
it never emits `driftward.linear.issue.ready_for_implementation`. Intake repeats
the policy check before bridge lookup as defense in depth.

### GitHub PR Ingress

GitHub PR events are implementation and evidence signals. Supported pull
request actions are `opened`, `synchronize`, and `closed`:

```text
GitHub pull_request.opened
  -> normalize as driftward.github.pr.created
  -> record in SQLite with a stable event key
  -> publish the Hatchet observation event once
  -> do not dispatch Hermes

GitHub pull_request.synchronize
  -> normalize as driftward.github.pr.synchronized
  -> record in SQLite with a stable event key
  -> publish the Hatchet observation event once
  -> do not dispatch Hermes

GitHub pull_request.closed
  -> normalize as driftward.github.pr.closed_unmerged or
     driftward.github.pr.closed_merged
  -> record in SQLite with a stable event key
  -> publish the Hatchet observation event once
  -> do not dispatch Hermes
```

These are intentionally observation-first. PR events can help the factory
detect new work artifacts, stale evidence, abandoned work, or completed work,
but they are not execution requests.

## Runtime

- Service: `driftward-hatchet-event-ingress.service`
- Local API: `http://127.0.0.1:19878`
- Database: `/var/lib/driftward-hatchet-event-ingress/ingress.db`
- Optional env: `/etc/driftward/hatchet-event-ingress.env`
- Hatchet token: inherited from `/etc/driftward/hatchet-workers.env`
- Public proxy service: `driftward-hatchet-linear-webhook-proxy.service`
- Public Linear webhook URL: `https://srv1379035.tail5c6a78.ts.net/events/linear/issue`

The full ingress service binds to `127.0.0.1` only. The public webhook URL is a
Tailscale Funnel route to a narrow proxy on `127.0.0.1:19879`; that proxy only
forwards `POST /events/linear/issue` and refuses payloads until the Linear
webhook signing secret is configured.

## API

Health:

```bash
curl -fsS http://127.0.0.1:19878/health
```

Public Linear webhook proxy readiness:

```bash
curl -fsS http://127.0.0.1:19879/ready
```

Ingest a GitHub PR webhook payload:

```bash
curl -fsS \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Delivery: example-delivery-id' \
  --data @payload.json \
  http://127.0.0.1:19878/events/github/pull_request
```

Ingest a Linear issue webhook payload:

```bash
curl -fsS \
  -H 'Content-Type: application/json' \
  -H 'Linear-Delivery: example-delivery-id' \
  -H 'Linear-Event: Issue' \
  --data @payload.json \
  http://127.0.0.1:19878/events/linear/issue
```

List recent events:

```bash
curl -fsS http://127.0.0.1:19878/events?limit=20
```

Reconciliation may read only the sanitized local projection below. It never
receives webhook raw payloads, descriptions, or provider credentials:

```bash
curl -fsS 'http://127.0.0.1:19878/reconciliation/observations?limit=100' | jq
```

The projection stores provider `updatedAt` as a canonical UTC timestamp plus
an indexed epoch-microsecond ordering value, with an event-ID tie-break. Legacy
rows are backfilled during ingress initialization, so a delayed older webhook
cannot replace a newer provider observation even when providers use different
UTC offsets. Ingress reads this projection through indexed, sanitized columns
only; raw webhook bodies are never deserialized for reconciliation. A loopback
caller may cancel a bounded read at any time. Ingress treats that as a normal
disconnect, closes the response quietly, and does not emit a `500` or stack
trace.

## Optional GitHub Signature

Set this in `/etc/driftward/hatchet-event-ingress.env` to require
`X-Hub-Signature-256`:

```text
HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET=...
```

Do not commit or paste the secret.

## Optional Linear Signature

Linear signs webhook payloads with a hex HMAC-SHA256 in `Linear-Signature`.
Set this in `/etc/driftward/hatchet-event-ingress.env` before enabling the
Linear webhook. The public proxy refuses webhook payloads until this key is
present, and the private ingress service uses it for signature and timestamp
verification:

```text
HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET=...
```

The service reads `Linear-Timestamp` or the payload `webhookTimestamp` and
rejects signed deliveries outside the default 60-second tolerance. Do not
commit or paste the secret.

Install or refresh the public webhook endpoint:

```bash
bash hatchet/event-ingress/install-public-webhook.sh
```

Check the Funnel route:

```bash
tailscale funnel status
```
