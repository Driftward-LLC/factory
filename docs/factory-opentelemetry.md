# Factory execution tracing (first slice)

Status: opt-in execution tracing; disabled by default. Deployment and dashboard
acceptance are recorded separately from source availability. The operator has confirmed native canary readback in PostHog project
632215; readback of the new OpenTelemetry path remains an activation gate.
This is not an end-to-end intake/reconciliation/Linear-delivery trace yet.

Factory remains the operator surface. Hermes is an internal executor, not an
operator chat interface. Telemetry is diagnostic, never proof of containment,
effect absence, lease release, or permission to edit an issue manually. The
durable Factory ledger and invocation receipts retain those responsibilities.

## Coverage

The first slice starts after loading a durable job in the executor worker:

```text
attempt processing -> detached receipt dispatch -> receipt runner
                      -> gRPC gateway -> fenced Hermes invocation
                      -> bridge effect commit / settlement
```

The TypeScript and Python OpenTelemetry SDKs export OTLP/HTTP to an optional
private Collector. Context crosses the detached-process environment through
`FACTORY_TRACEPARENT`, gRPC metadata and authenticated bridge HTTP headers through
W3C `traceparent`. No baggage, prompts, responses, tool payloads, exception text,
URLs, fence tokens or credentials are exported. Recorded fields are fixed span
names, bounded job/attempt UUIDs, duration, status and bridge HTTP status codes.
Tracing does not modify sealed payloads, idempotency hashes, schemas or leases.

The worker's dispatch span ends after dispatch. It does not pretend to run for
the lifetime of the detached executor. Recovery/redelivery can create another
root span for the same attempt; missing roots after crashes are expected.
Cancellation authorization and receipt recovery semantics are unchanged.

## Compatibility with existing AI traces

The OTel trace ID is the same 16 bytes as the native observer's attempt UUIDv5,
represented as 32 lowercase hexadecimal characters. Generation retries have
different attempt IDs and therefore different traces. Legacy jobs use the
observer's job-ID fallback. SDK span IDs are new random IDs per operation.
Incoming context that disagrees with the loaded job's trace ID is discarded
at the execution scope. Trace headers never confer execution authority.

Existing PostHog AI events and Factory's **Find trace in PostHog** links retain their
dashed UUIDs. This avoids rewriting historical telemetry or changing console
destinations. Generation/tool events remain native PostHog events in this slice,
not OTel GenAI spans; correlation is at trace level, not native span parentage.

PostHog currently has no automatic UI link between Distributed Tracing and AI
Observability. Correlate using two queries, not a cross-cluster SQL join:

```sql
-- AI event trace ID normalization:
lower(replaceAll(toString(properties.$ai_trace_id), '-', ''))
-- Distributed Tracing trace ID normalization:
lower(hex(tryBase64Decode(trace_id)))
```

The existing synthetic canary maps to
`fb9fa7e140655818af723e41a35fe39a` (AI UUID:
`fb9fa7e1-4065-5818-af72-3e41a35fe39a`). It is a correlation golden fixture,
not evidence that an OTel trace has already been exported to production.

## Configuration and packaging

Set `FACTORY_OTEL_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces` only on
the bridge and executor worker. The detached runner inherits this nonsecret
setting plus its traceparent. The independently deployed host gateway needs
the new gateway bundle and the loopback endpoint `http://127.0.0.1:4318/v1/traces`.
Do not enable only one side and claim complete coverage.

The setting is absent by default. Only `http://otel-collector:<port>/v1/traces`,
`localhost` and `127.0.0.1` are accepted. SDKs use private providers, do not
replace Hatchet's global instrumentation, and do not enable auto-instrumentation.
Inherited generic OTLP authentication headers disable Factory tracing rather
than leaking exporter credentials from a worker. Invalid setup fails open.
Configuration changes require restarting the affected process.

`deploy/compose.telemetry.yml` and `deploy/otel-collector.yaml` are an opt-in
release-bundled overlay, with a pinned Collector image and bounded
memory/queues. This is part of one Compose application, not a separately
provisioned Hermes dependency. Only the Collector receives its dedicated
root-owned 0600 env file, named by `FACTORY_OTEL_COLLECTOR_ENV_FILE`:

```dotenv
POSTHOG_TRACES_ENDPOINT='https://us.i.posthog.com/i/v1/traces'
POSTHOG_PROJECT_TOKEN='<project ingestion token, provisioned privately>'
```

Never put this env file in source control, worker env files or release assets.
Do not automatically copy Hermes credentials. Do not use PostHog's AI-only
`/i/v0/ai/otel` endpoint: it drops ordinary application spans.

Use the `telemetry` command in the matching production or portable wrapper,
never ad-hoc Compose overrides. See [the installation procedure](../deploy/PORTABLE.md#optional-execution-telemetry).
The command requires a current cold backup and stopped services. It copies only
the two approved values into `env/factory-otel-collector.env`, stores the
Collector image ID and deployment hashes in the installation marker, and leaves
everything stopped. Source container fencing and image-upgrade history stay
unchanged. Configured installations include the optional service, secret file,
and deployment hashes in status, startup, backup and restore. Old installations
and snapshots remain unchanged. No extra durable volume is needed.

At current low volume all instrumented spans are sampled. SDK queues are bounded
to 256 spans, batches to 32, export timeout to about one second and runner flush
to 1.6 seconds. After business work settles and that flush returns, the detached
runner explicitly exits with its business exit code so a stalled exporter socket
cannot keep it alive. This does not terminate in-progress business work or the
long-lived gateway. Early setup failures mark the enclosing receipt span as an
error. Collector queues are also bounded. Outages can lose telemetry;
they must not retry business work or change its result. Disabling telemetry
requires removing the endpoint and recreating affected processes, not restoring
databases or replaying attempts.

## Acceptance and remaining gates

Local tests cover canonical ID parity, concurrent attempts, detached-process
propagation, real gRPC/HTTP transports, journal deduplication, malformed/mismatched
context, exporter outage, auth ordering and absence of secret-shaped fixtures in
exported spans. No real agent/model/GitHub effect is needed for these tests.

Before live activation:

1. Read back the existing native canary in PostHog project 632215 (confirmed by
   the operator's screenshot; not an OpenTelemetry acceptance result).
2. Run optional deployment-wrapper/package acceptance (including cold restore).
3. Verify the Distributed Tracing product and ingestion token belong to that
   same project; query/read credentials are not needed in runtime services.
4. Deploy the matching bridge, worker and gateway with tracing still off, then
   enable the private Collector during an approved rollout.
5. Run a reviewed synthetic canary and read back both trace formats. Prove
   exporter outage does not change the result, and retain historical AI links.

After this path is verified, extend coverage to intake, reconciliation and
Linear delivery; migrate native generation/tool events to OTel only with a
separate privacy and duplicate-counting check.

References: [OpenTelemetry context propagation](https://opentelemetry.io/docs/concepts/context-propagation/),
[PostHog distributed tracing installation](https://posthog.com/docs/distributed-tracing/installation/python),
[PostHog AI correlation and limitations](https://posthog.com/docs/distributed-tracing/link-ai-observability).
