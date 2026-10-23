import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import { inspectDraftPullRequestViaGrpc, resolveHermesGatewayProtoPath } from './hermes-gateway-client';
import { NonRetryableBridgeExecutionError } from './errors';

test('real gateway uses the shared bounded PR inspection default', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-inspection-'));
  const previousTarget = process.env.DRIFTWARD_HERMES_GRPC_TARGET;
  try {
    writeFileSync(path.join(directory, 'docker'), `#!${process.execPath}
const args = process.argv.slice(2);
if (args.includes('python3')) { process.stderr.write('PRIVATE_FALLBACK_CONTENT'); process.exit(Number(process.env.TEST_FALLBACK_CODE)); }
else if (args.includes('view')) process.stdout.write(${JSON.stringify(JSON.stringify({changedFiles: 1, headRefOid: 'a'.repeat(40)}))});
else if (args.includes('repos/Driftward-LLC/capykit/pulls/77')) process.stdout.write(${JSON.stringify(JSON.stringify({baseRefOid: 'b'.repeat(40), headRefOid: 'a'.repeat(40)}))});
else if (args.some(a => a.includes('/compare/'))) {
  if (process.env.TEST_DIFF_FAILURE) {
    process.stderr.write('HTTP ' + process.env.TEST_DIFF_FAILURE + ' Comparison.diff too_large PRIVATE_CONTENT');
    process.exit(1);
  }
  process.stdout.write('diff --git a/README.md b/README.md\\nPRIVATE_DIFF_CONTENT'.padEnd(Number(process.env.TEST_DIFF_BYTES), '+'));
}
else if (args.includes('api')) process.stdout.write(JSON.stringify({path:'README.md'}) + '\\n');
else process.exit(1);
`, { mode: 0o700 });
    for (const diffBytes of [1536 * 1024, 3 * 1024 * 1024]) {
      await withGateway({ PATH: `${directory}${path.delimiter}${process.env.PATH || ''}`, DRIFTWARD_HERMES_RUNTIME_USER: '10000:10000', TEST_DIFF_BYTES: String(diffBytes) }, async (target) => {
        process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
        if (diffBytes < 2 * 1024 * 1024) {
          const observation = await inspectDraftPullRequestViaGrpc('Driftward-LLC/capykit', 77, 5000);
          assert.equal(Buffer.byteLength(observation.diffText), diffBytes);
        } else {
          await assert.rejects(inspectDraftPullRequestViaGrpc('Driftward-LLC/capykit', 77, 5000), (error: unknown) => {
            assert.ok(error instanceof NonRetryableBridgeExecutionError);
            assert.equal(error.reasonCode, 'hermes_process_output_limit_exceeded');
            assert.equal(error.bufferExceeded, true);
            assert.doesNotMatch(error.message, /PRIVATE_DIFF_CONTENT/);
            return true;
          });
        }
      }, `unix://${path.join(directory, 'gateway.sock')}`);
    }
    for (const [status, fallbackCode] of [[403, 75], [406, 75], [406, 73], [406, 74]]) {
      await withGateway({ PATH: `${directory}${path.delimiter}${process.env.PATH || ''}`, DRIFTWARD_HERMES_RUNTIME_USER: '10000:10000',
        TEST_DIFF_FAILURE: String(status), TEST_FALLBACK_CODE: String(fallbackCode) }, async (target) => {
        process.env.DRIFTWARD_HERMES_GRPC_TARGET = target;
        await assert.rejects(inspectDraftPullRequestViaGrpc('Driftward-LLC/capykit', 77, 5000), (error: unknown) => {
          assert.ok(error instanceof NonRetryableBridgeExecutionError);
          assert.equal(error.reasonCode, status === 406 && fallbackCode === 73 ? 'hermes_process_output_limit_exceeded' : 'verification_evidence_unavailable');
          assert.equal(error.failureDomain, 'verification');
          assert.equal(error.failureStage, 'verification_checks');
          assert.equal(error.bufferExceeded, status === 406 && fallbackCode === 73);
          assert.equal(error.timedOut, status === 406 && fallbackCode === 74);
          assert.doesNotMatch(JSON.stringify(error) + error.message, /PRIVATE_/);
          return true;
        });
      }, `unix://${path.join(directory, 'gateway.sock')}`);
    }
  } finally {
    if (previousTarget === undefined) delete process.env.DRIFTWARD_HERMES_GRPC_TARGET;
    else process.env.DRIFTWARD_HERMES_GRPC_TARGET = previousTarget;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('gateway transport authenticates TCP callers before handling requests', async (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'hermes-gateway-auth-'));
  const file = (name: string) => path.join(directory, name);
  const openssl = (...args: string[]) => execFileSync('openssl', args, { cwd: directory, stdio: 'ignore' });
  const token = 'test-gateway-caller-secret';
  try {
    // Disposable test CA and separate server/client identities, never live credentials.
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Gateway test CA', '-keyout', 'ca.key', '-out', 'ca.crt');
    for (const name of ['server', 'client']) {
      openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', `/CN=${name}`, '-keyout', `${name}.key`, '-out', `${name}.csr`);
      writeFileSync(file(`${name}.ext`), name === 'server'
        ? 'subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n'
        : 'extendedKeyUsage=clientAuth\n');
      openssl('x509', '-req', '-in', `${name}.csr`, '-CA', 'ca.crt', '-CAkey', 'ca.key', '-set_serial', name === 'server' ? '2' : '3', '-days', '1', '-extfile', `${name}.ext`, '-out', `${name}.crt`);
    }
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Untrusted caller', '-keyout', 'untrusted.key', '-out', 'untrusted.crt');
    const tls = {
      DRIFTWARD_HERMES_GRPC_TLS: '1',
      DRIFTWARD_HERMES_GRPC_SERVER_KEY_PATH: file('server.key'),
      DRIFTWARD_HERMES_GRPC_SERVER_CERT_PATH: file('server.crt'),
    };
    const credentials = (identity?: string) => grpc.credentials.createSsl(
      readFileSync(file('ca.crt')),
      identity ? readFileSync(file(`${identity}.key`)) : undefined,
      identity ? readFileSync(file(`${identity}.crt`)) : undefined,
    );

    await t.test('server-only TLS fails closed at startup', async () => {
      await assert.rejects(withGateway(tls, async () => assert.fail('unauthenticated server started')),
        /require an auth token or a CA/);
    });

    await t.test('TLS bearer authentication rejects missing, wrong, and duplicate credentials without leaking them', async () => {
      const logs = await withGateway({ ...tls, DRIFTWARD_HERMES_GRPC_AUTH_TOKEN: token }, async (target) => {
        await request(target, credentials(), [`Bearer ${token}`]);
        for (const values of [[], ['Bearer wrong-test-token'], [`Bearer ${token}`, `Bearer ${token}`]]) {
          await assert.rejects(request(target, credentials(), values), (error: grpc.ServiceError) => {
            // HTTP/2 can reject duplicate authorization headers before the handler.
            assert.ok(error.code === grpc.status.UNAUTHENTICATED || (values.length > 1 && error.code === grpc.status.INTERNAL));
            assert.doesNotMatch(error.message, /test-gateway-caller-secret|wrong-test-token/);
            return true;
          });
        }
      });
      assert.doesNotMatch(logs, /test-gateway-caller-secret|wrong-test-token/);
    });

    await t.test('mutual TLS requires a certificate signed by the dedicated client CA', async () => {
      await withGateway({ ...tls, DRIFTWARD_HERMES_GRPC_CA_PATH: file('ca.crt') }, async (target) => {
        await request(target, credentials('client'));
        for (const identity of [undefined, 'untrusted']) {
          await assert.rejects(request(target, credentials(identity)), (error: grpc.ServiceError) => {
            assert.ok(error.code === grpc.status.UNAVAILABLE || error.code === grpc.status.DEADLINE_EXCEEDED);
            return true;
          });
        }
      });
    });

    await t.test('when both are configured, a client certificate does not bypass the token', async () => {
      await withGateway({ ...tls, DRIFTWARD_HERMES_GRPC_CA_PATH: file('ca.crt'), DRIFTWARD_HERMES_GRPC_AUTH_TOKEN: token }, async (target) => {
        await assert.rejects(request(target, credentials('client')), (error: grpc.ServiceError) => error.code === grpc.status.UNAUTHENTICATED);
        await request(target, credentials('client'), [`Bearer ${token}`]);
      });
    });

    await t.test('explicit insecure development transport is loopback-only and still authenticated', async () => {
      const environment = { DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP: '1', DRIFTWARD_HERMES_GRPC_AUTH_TOKEN: token };
      await assert.rejects(withGateway(environment, async () => assert.fail('non-loopback server started'), '0.0.0.0:0'), /requires TLS/);
      await assert.rejects(withGateway({ DRIFTWARD_HERMES_GRPC_ALLOW_INSECURE_TCP: '1' }, async () => assert.fail('unauthenticated server started')), /require an auth token or a CA/);
      await withGateway(environment, async (target) => {
        await request(target, grpc.credentials.createInsecure(), [`Bearer ${token}`]);
        await assert.rejects(request(target, grpc.credentials.createInsecure()), (error: grpc.ServiceError) => error.code === grpc.status.UNAUTHENTICATED);
      });
    });

    await t.test('Unix sockets retain their local permission boundary without TCP credentials', async () => {
      await withGateway({}, async (target) => {
        await request(target, grpc.credentials.createInsecure());
      }, `unix://${file('gateway.sock')}`);
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

async function withGateway(
  environment: NodeJS.ProcessEnv,
  run: (target: string) => Promise<void>,
  target = '127.0.0.1:0',
): Promise<string> {
  const child = spawn(process.execPath, [path.join(__dirname, 'hermes-gateway-server.js'), '--target', target], {
    env: { PATH: process.env.PATH, DRIFTWARD_HERMES_COMMAND: '/nonexistent/test-hermes', DRIFTWARD_HERMES_GRPC_ALLOW_TCP: '1', ...environment },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  const exited = new Promise<void>((resolve) => child.once('close', () => resolve()));
  try {
    const address = await new Promise<string>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('test gateway startup timed out')), 10_000);
      child.once('error', (error) => { clearTimeout(timeout); reject(error); });
      child.once('close', () => { clearTimeout(timeout); reject(new Error(output)); });
      child.stderr.on('data', (chunk) => { output += chunk.toString(); });
      child.stdout.on('data', (chunk) => {
        output += chunk.toString();
        if (output.includes('gateway listening on ')) {
          clearTimeout(timeout);
          const port = output.match(/tcp:(\d+)/)?.[1];
          resolve(port ? `127.0.0.1:${port}` : target);
        }
      });
    });
    await run(address);
  } finally {
    child.kill('SIGKILL');
    await exited;
  }
  return output;
}

async function request(target: string, credentials: grpc.ChannelCredentials, authorization: string[] = []): Promise<void> {
  const definition = protoLoader.loadSync(resolveHermesGatewayProtoPath(), { keepCase: false, longs: Number, defaults: true });
  const loaded = grpc.loadPackageDefinition(definition);
  const service = (((loaded.driftward as grpc.GrpcObject).factory as grpc.GrpcObject).hermes as grpc.GrpcObject).v1 as grpc.GrpcObject;
  const Client = service.HermesFactoryGateway as grpc.ServiceClientConstructor;
  const client = new Client(target, credentials);
  const metadata = new grpc.Metadata();
  for (const value of authorization) metadata.add('authorization', value);
  try {
    await new Promise<void>((resolve, reject) => {
      // This handler validates the reference locally; it never calls Hermes or GitHub.
      client.inspectPullRequestState({ repository: 'example/repo', reference: 'invalid' }, metadata, { deadline: Date.now() + 3000 },
        (error: grpc.ServiceError | null, response: { reason: string }) => {
          if (error) reject(error);
          else {
            try { assert.equal(response.reason, 'invalid_reference'); resolve(); }
            catch (failure) { reject(failure); }
          }
        });
    });
  } finally {
    client.close();
  }
}
