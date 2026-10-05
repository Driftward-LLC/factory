import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash, randomBytes } from 'node:crypto';
import { ROOT_CONTEXT, SpanKind, SpanStatusCode, trace, type Span, type SpanContext } from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { AlwaysOnSampler, BasicTracerProvider, BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import type { BridgeJob } from './types';

const active = new AsyncLocalStorage<Span>();
const rootId = new AsyncLocalStorage<string>();
let provider: BasicTracerProvider | undefined;
let initialized = false;
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

// Same bytes as the deployed PostHog UUIDv5. Historical links remain unchanged.
export function executionTraceId(job: BridgeJob): string | undefined {
  const lineage = job.payload.factory_retry;
  const attempt = lineage && typeof lineage === 'object' && !Array.isArray(lineage) ? lineage.attempt_id : undefined;
  const id = typeof attempt === 'string' && uuid.test(attempt) ? `attempt:${attempt}`
    : uuid.test(job.job_id) ? `job:${job.job_id}` : undefined;
  if (!id) return undefined;
  const hash = createHash('sha1').update(Buffer.from('6ba7b8119dad11d180b400c04fd430c8', 'hex'))
    .update(`driftward-hermes:trace:${id}`).digest().subarray(0, 16);
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  return hash.toString('hex');
}

export function parseTraceparent(value: unknown): SpanContext | undefined {
  if (typeof value !== 'string') return undefined;
  const match = /^00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)$/.exec(value);
  if (!match || /^0+$/.test(match[1]) || /^0+$/.test(match[2])) return undefined;
  return { traceId: match[1], spanId: match[2], traceFlags: Number(match[3]), isRemote: true };
}

export function executionTraceparent(): string | undefined {
  const current = active.getStore()?.spanContext();
  return current ? `00-${current.traceId}-${current.spanId}-${current.traceFlags & 1 ? '01' : '00'}` : undefined;
}

export function telemetryEndpoint(): string | undefined {
  const value = process.env.FACTORY_OTEL_TRACES_ENDPOINT;
  // Only the private Collector receives worker telemetry, never vendor keys.
  if (!value || !/^http:\/\/(?:127\.0\.0\.1|localhost|otel-collector):[1-9][0-9]{0,4}\/v1\/traces$/.test(value)) return undefined;
  try { new URL(value); return value; } catch { return undefined; }
}

function tracer(service: string) {
  if (!initialized) {
    initialized = true;
    const endpoint = telemetryEndpoint();
    // The SDK merges generic exporter headers from env. Refuse rather than
    // accidentally forwarding inherited credentials to the private receiver.
    if (!endpoint || process.env.OTEL_EXPORTER_OTLP_HEADERS || process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS) return undefined;
    try {
      provider = new BasicTracerProvider({
        resource: resourceFromAttributes({ 'service.name': service }),
        sampler: new AlwaysOnSampler(),
        idGenerator: { generateTraceId: () => rootId.getStore() || randomBytes(16).toString('hex'),
          generateSpanId: () => randomBytes(8).toString('hex') },
        spanLimits: { attributeCountLimit: 16, attributeValueLengthLimit: 128, eventCountLimit: 0, linkCountLimit: 0 },
        spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: endpoint, timeoutMillis: 1000 }), {
          maxQueueSize: 256, maxExportBatchSize: 32, scheduledDelayMillis: 1000, exportTimeoutMillis: 1500,
        })],
      });
    } catch { provider = undefined; }
  }
  return provider?.getTracer('driftward.factory.execution', '1');
}

export function withExecutionSpan<T>(name: string, run: () => T, options: {
  job?: BridgeJob; parent?: unknown; service?: string; kind?: SpanKind;
} = {}): T {
  let span: Span | undefined;
  try {
    const jobTrace = options.job ? executionTraceId(options.job) : undefined;
    const inherited = options.parent === undefined ? active.getStore()?.spanContext() : parseTraceparent(options.parent);
    // Untrusted/mismatched context cannot join another attempt's trace.
    const parent = inherited && (!jobTrace || inherited.traceId === jobTrace) ? inherited : undefined;
    const attributes: Record<string, string> = {};
    if (options.job && uuid.test(options.job.job_id)) attributes['factory.job_id'] = options.job.job_id;
    const lineage = options.job?.payload.factory_retry;
    if (lineage && typeof lineage === 'object' && !Array.isArray(lineage)
        && typeof lineage.attempt_id === 'string' && uuid.test(lineage.attempt_id)) attributes['factory.attempt_id'] = lineage.attempt_id;
    span = rootId.run(jobTrace || randomBytes(16).toString('hex'), () => tracer(options.service || 'factory-executor-worker')?.startSpan(
      name, { attributes, kind: options.kind ?? SpanKind.INTERNAL }, parent ? trace.setSpanContext(ROOT_CONTEXT, parent) : ROOT_CONTEXT));
  } catch { /* Instrumentation must never prevent execution. */ }
  if (!span) return run();
  const finish = (failed: boolean) => {
    try { if (failed) span!.setStatus({ code: SpanStatusCode.ERROR }); span!.end(); } catch { /* fail open */ }
  };
  // Keep the business call outside instrumentation setup catches: never retry it.
  try {
    const result = active.run(span, run);
    if (result && typeof (result as unknown as PromiseLike<unknown>).then === 'function') return Promise.resolve(result).then(value => { finish(false); return value; }, error => { finish(true); throw error; }) as T;
    finish(false);
    return result;
  } catch (error) { finish(true); throw error; }
}

export function markExecutionTelemetryFailure(): void {
  try { active.getStore()?.setStatus({ code: SpanStatusCode.ERROR }); } catch { /* fail open */ }
}

export async function flushExecutionTelemetry(): Promise<void> {
  if (!provider) return;
  let timer: NodeJS.Timeout | undefined;
  try {
    await Promise.race([provider.forceFlush(), new Promise<void>(resolve => { timer = setTimeout(resolve, 1600); })]);
  } catch { /* Best effort, with a bounded shutdown delay. */ }
  finally { if (timer) clearTimeout(timer); }
}
