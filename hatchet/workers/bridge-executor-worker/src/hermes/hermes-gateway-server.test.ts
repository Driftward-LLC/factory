import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import { resolveHermesGatewayProtoPath } from './hermes-gateway-client';

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

    await t.test('bundled executor rejects direct effect RPCs before parsing or execution', async () => {
      for (const enabled of [undefined, '0']) {
        await withGateway({ DRIFTWARD_HERMES_RUNTIME_MODE: 'local-executor', DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED: enabled }, async (target) => {
          await assert.rejects(request(target, grpc.credentials.createInsecure(), [], 'createDraftPullRequest'), (error: grpc.ServiceError) => {
            assert.equal(error.code, grpc.status.FAILED_PRECONDITION);
            assert.deepEqual(error.metadata.get('reason-code'), ['hermes_execution_disabled']);
            return true;
          });
        }, `unix://${file('gateway.sock')}`);
      }
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

async function request(target: string, credentials: grpc.ChannelCredentials, authorization: string[] = [], method = 'inspectPullRequestState'): Promise<void> {
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
      client[method]({ repository: 'example/repo', reference: 'invalid' }, metadata, { deadline: Date.now() + 3000 },
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
