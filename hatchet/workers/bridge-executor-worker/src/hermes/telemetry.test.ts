import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import * as grpc from '@grpc/grpc-js';
import { test } from 'node:test';
import { BridgeClient, executionTraceId, executionTraceparent, flushExecutionTelemetry, parseTraceparent,
  telemetryEndpoint, withExecutionSpan, type BridgeJob } from '@driftward/hatchet-worker-shared';
import { selectedReceiptEnvironment } from './create-draft-pr';
import { createServer as createGateway } from './hermes-gateway-server';
import { prepareHermesInvocation, runHermesCreateDraftPrViaGrpc } from './hermes-gateway-client';

const job = { job_id: '11111111-1111-4111-8111-111111111111', payload: {
  factory_retry: { attempt_id: 'bdb1edb7-459e-4c1c-a30f-b657c1c4833d' }, implementation_prompt: 'SECRET_PROMPT',
} } as unknown as BridgeJob;

test('canonical trace identity matches deployed observer canary; malformed context is ignored', () => {
  assert.equal(executionTraceId(job), 'fb9fa7e140655818af723e41a35fe39a');
  assert.notEqual(executionTraceId({ ...job, payload: {} }), executionTraceId(job));
  for (const parent of ['', '00-' + '0'.repeat(32) + '-123456789abcdef0-01', '00-' + '1'.repeat(32) + '-' + '0'.repeat(16) + '-01', 'garbage\nsecret']) {
    assert.equal(parseTraceparent(parent), undefined);
  }
  for (const endpoint of ['https://evil.test/v1/traces', 'http://user:secret@localhost:4318/v1/traces', 'http://localhost:65536/v1/traces']) {
    process.env.FACTORY_OTEL_TRACES_ENDPOINT = endpoint;
    assert.equal(telemetryEndpoint(), undefined);
  }
  delete process.env.FACTORY_OTEL_TRACES_ENDPOINT;
});

test('actual receipt setup failure marks its outer span and exits even if OTLP never finishes', async () => {
  for (const stalled of [false, true]) {
    const batches: any[] = [];
    const receiver = createServer((req, res) => {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        if (req.url === '/v1/traces') {
          batches.push(JSON.parse(body));
          if (stalled) {
            res.writeHead(200); res.write(' ');
            const drip = setInterval(() => res.write(' '), 100);
            res.on('close', () => clearInterval(drip));
          } else res.end('{}');
        } else if (req.method === 'GET') {
          res.writeHead(404); res.end('{}');
        } else res.end('{}'); // Synthetic durable failure settlement.
      });
    }).listen(0, '127.0.0.1');
    await once(receiver, 'listening');
    const base = `http://127.0.0.1:${(receiver.address() as any).port}`;
    const child = spawn(process.execPath, [path.join(__dirname, 'create-draft-pr-receipt-runner.js'), job.job_id], {
      env: {
        PATH: process.env.PATH,
        HATCHET_HERMES_BRIDGE_URL: base,
        HATCHET_FACTORY_EXECUTION_LEASE: JSON.stringify({ lease_id: 'test', owner: 'test', epoch: 1, fence_token: 'SECRET_FENCE' }),
        FACTORY_OTEL_TRACES_ENDPOINT: `${base}/v1/traces`,
        FACTORY_TRACEPARENT: '00-fb9fa7e140655818af723e41a35fe39a-123456789abcdef0-01',
      }, stdio: 'ignore',
    });
    // The fake bridge cannot authorize execution; no provider/Hatchet credentials
    // or failure-event key are supplied to this real entrypoint.
    const watchdog = setTimeout(() => child.kill('SIGKILL'), 4000);
    try {
      const [code, signal] = await once(child, 'exit');
      assert.equal(signal, null, 'receipt exceeded the bounded export shutdown');
      assert.equal(code, 1, 'preserve the original business failure exit code');
      const spans = batches.flatMap(b => b.resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
      assert.equal(spans.find(s => s.name === 'factory.receipt.start')?.status.code, 2);
      assert(!JSON.stringify(batches).includes('SECRET_'));
    } finally {
      clearTimeout(watchdog);
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      receiver.closeAllConnections(); receiver.close();
    }
  }
});

test('SDK exports bounded metadata across process and HTTP boundaries; concurrent attempts stay separate', async () => {
  const batches: any[] = [];
  const receiver = createServer((req, res) => {
    let body = ''; req.on('data', chunk => { body += chunk; });
    req.on('end', () => { batches.push(JSON.parse(body)); res.end('{}'); });
  }).listen(0, '127.0.0.1');
  await once(receiver, 'listening');
  process.env.FACTORY_OTEL_TRACES_ENDPOINT = `http://127.0.0.1:${(receiver.address() as any).port}/v1/traces`;
  let httpParent: unknown;
  const bridge = createServer((req, res) => { httpParent = req.headers.traceparent; res.end('{}'); }).listen(0, '127.0.0.1');
  await once(bridge, 'listening');
  try {
    const parent = await withExecutionSpan('factory.execution.dispatch', async () => {
      const value = executionTraceparent();
      assert.equal(parseTraceparent(value)?.traceId, executionTraceId(job));
      const env = Object.fromEntries(selectedReceiptEnvironment(job, { lease_id: 'lease', owner: 'owner', epoch: 1, fence_token: 'SECRET_FENCE' }, 'dispatch'));
      assert.equal(env.FACTORY_TRACEPARENT, value);
      assert.equal(env.FACTORY_OTEL_TRACES_ENDPOINT, process.env.FACTORY_OTEL_TRACES_ENDPOINT);
      await new BridgeClient({ baseUrl: `http://127.0.0.1:${(bridge.address() as any).port}`, token: 'SECRET_TOKEN' }).getJob('SECRET_PATH');
      assert.equal(parseTraceparent(httpParent)?.traceId, executionTraceId(job));
      // A separate process, using the same allowlisted context carrier as the real runner.
      const child = spawn(process.execPath, ['-e', `const t=require('@driftward/hatchet-worker-shared'); t.withExecutionSpan('factory.execution.receipt',()=>{}, {parent:process.env.FACTORY_TRACEPARENT,service:'factory-receipt-runner'});t.flushExecutionTelemetry();`], {
        cwd: __dirname, env: { PATH: process.env.PATH, FACTORY_TRACEPARENT: value, FACTORY_OTEL_TRACES_ENDPOINT: process.env.FACTORY_OTEL_TRACES_ENDPOINT }, stdio: 'pipe',
      });
      const [code] = await once(child, 'exit'); assert.equal(code, 0);
      return value;
    }, { job });
    const second = { ...job, payload: { factory_retry: { attempt_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' } } } as BridgeJob;
    const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-otel-gateway-'));
    const target = `unix://${path.join(directory, 'gateway.sock')}`;
    const previous = { target: process.env.DRIFTWARD_HERMES_GRPC_TARGET, journal: process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY };
    process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
    process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY = path.join(directory, 'journal');
    const gatewayJob = { ...job, idempotency_key: 'test:otel', requested_action: 'create_draft_pr', branch: 'codex/otel-test', state: 'running',
      payload: { ...job.payload, repo: 'example/project', dry_run: false } } as BridgeJob;
    let invocations = 0;
    const runtime = { schema: 1 as const, backend: 'hermes', kind: 'docker' as const, instance_id: 'a'.repeat(64), image_digest: 'sha256:' + 'b'.repeat(64), incarnation: 'sha256:' + 'c'.repeat(64) };
    const gateway = createGateway(target, async (_job, _command, _timeout, _buffer, _selection, context) => {
      await context!.bindRuntime!(runtime);
      invocations++;
      return { status: 'created', prUrl: 'https://github.com/example/project/pull/1', prNumber: 1, commitSha: 'a'.repeat(40),
        branch: gatewayJob.branch!, hermesSessionId: null, summary: 'draft_pr_created', validation: null };
    });
    try {
      await new Promise<void>((resolve, reject) => gateway.bindAsync(target, grpc.ServerCredentials.createInsecure(), error => error ? reject(error) : resolve()));
      await withExecutionSpan('factory.rpc.test', async () => {
        const lease = { lease_id: '22222222-2222-4222-8222-222222222222', owner: 'test-executor', epoch: 1, fence_token: 'SECRET_FENCE' };
        gatewayJob.execution_runtime = { lease_id: lease.lease_id, epoch: lease.epoch, runtime: await prepareHermesInvocation(gatewayJob, lease) };
        await runHermesCreateDraftPrViaGrpc(gatewayJob, 5000, lease);
        await runHermesCreateDraftPrViaGrpc(gatewayJob, 5000, lease);
      }, { job });
      assert.equal(invocations, 1, 'tracing must not change journal deduplication');
    } finally {
      gateway.forceShutdown();
      if (previous.target === undefined) delete process.env.DRIFTWARD_HERMES_GRPC_TARGET; else process.env.DRIFTWARD_HERMES_GRPC_TARGET = previous.target;
      if (previous.journal === undefined) delete process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY; else process.env.DRIFTWARD_HERMES_INVOCATION_DIRECTORY = previous.journal;
      rmSync(directory, { recursive: true, force: true });
    }
    await Promise.all([job, second].map(item => withExecutionSpan('factory.concurrent', async () => {
      await new Promise(resolve => setTimeout(resolve, 5));
      assert.equal(parseTraceparent(executionTraceparent())?.traceId, executionTraceId(item));
    }, { job: item })));
    let calls = 0;
    const failure = new Error('SECRET_ERROR');
    await assert.rejects(withExecutionSpan('factory.failed', async () => { calls++; throw failure; }, { job }), error => error === failure);
    assert.equal(calls, 1);
    withExecutionSpan('factory.mismatch', () => assert.equal(parseTraceparent(executionTraceparent())?.traceId, executionTraceId(second)), { job: second, parent });
    await flushExecutionTelemetry();
    const spans = batches.flatMap(b => b.resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
    const dispatch = spans.find(s => s.name === 'factory.execution.dispatch');
    const receipt = spans.find(s => s.name === 'factory.execution.receipt');
    assert.equal(receipt.traceId, dispatch.traceId);
    assert.equal(receipt.parentSpanId, dispatch.spanId);
    assert.equal(spans.find(s => s.name === 'factory.mismatch').parentSpanId, undefined);
    assert.equal(spans.find(s => s.name === 'factory.failed').status.code, 2);
    const gatewaySpan = spans.find(s => s.name === 'factory.gateway.handle');
    assert(spans.some(s => s.name === 'factory.gateway.call' && s.spanId === gatewaySpan.parentSpanId));
    assert.equal(gatewaySpan.traceId, dispatch.traceId);
    assert.equal(spans.filter(s => s.name === 'factory.hermes.execute').length, 1);
    assert(!JSON.stringify(batches).includes('SECRET_'));
    assert.equal(executionTraceparent(), undefined);
    // Exporter outage cannot rerun or reject successful business work.
    receiver.close(); await once(receiver, 'close');
    const result = withExecutionSpan('factory.outage', () => { calls++; return 42; }, { job });
    await flushExecutionTelemetry();
    assert.equal(result, 42); assert.equal(calls, 2);
  } finally {
    bridge.close(); receiver.close(); delete process.env.FACTORY_OTEL_TRACES_ENDPOINT;
  }
});
