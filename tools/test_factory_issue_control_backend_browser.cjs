#!/usr/bin/env node
// Full browser -> trusted test proxy -> real console API -> disposable SQLite stores.
'use strict';
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const { createServer } = require('node:https');
const { request } = require('node:http');
const { mkdtemp, readFile, rm } = require('node:fs/promises');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { createInterface } = require('node:readline');
const { once } = require('node:events');
const { chromium } = require('playwright');
const root = resolve(__dirname, '..');

async function main() {
  const temp = await mkdtemp(join(tmpdir(), 'factory-control-browser-tls-'));
  let proxy, fixture, browser, lines;
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-subj', '/CN=127.0.0.1', '-keyout', join(temp, 'key.pem'), '-out', join(temp, 'cert.pem')], { stdio: 'ignore' });
    let upstreamPort;
    proxy = createServer({ key: await readFile(join(temp, 'key.pem')), cert: await readFile(join(temp, 'cert.pem')) }, (req, res) => {
      // This loopback-only test proxy is the trusted identity boundary, like Tailscale Serve.
      const upstream = request({ hostname: '127.0.0.1', port: upstreamPort, path: req.url, method: req.method,
        headers: { ...req.headers, host: '127.0.0.1:' + upstreamPort, 'tailscale-user-login': 'operator@driftward.test' } }, (reply) => {
        res.writeHead(reply.statusCode, reply.headers); reply.pipe(res);
      });
      upstream.on('error', () => { res.statusCode = 502; res.end('Test upstream unavailable'); });
      req.pipe(upstream);
    });
    await new Promise((done) => proxy.listen(0, '127.0.0.1', done));
    const origin = 'https://127.0.0.1:' + proxy.address().port;
    fixture = spawn(process.env.FACTORY_CONSOLE_TEST_PYTHON || 'python3', [join(root, 'tools/factory_issue_control_browser_fixture.py'), origin], {
      cwd: root, env: { ...process.env, PYTHONPATH: [join(root, 'hatchet/bridge'), join(root, 'hatchet/bridge/tests'), join(root, 'hatchet/factory-lifecycle-writer')].join(':') },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    lines = createInterface({ input: fixture.stdout });
    const nextLine = () => Promise.race([
      once(lines, 'line').then(([line]) => JSON.parse(line)),
      once(fixture, 'exit').then(([code]) => { throw new Error('Fixture exited before replying: ' + code); }),
      new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('Fixture response timed out')), 20000); timer.unref(); }),
    ]);
    const ready = await nextLine(); upstreamPort = ready.port;
    assert.equal(ready.actor, 'operator@driftward.test');
    browser = await chromium.launch({ headless: true, executablePath: process.env.FACTORY_CONSOLE_CHROMIUM });
    const page = await browser.newPage({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 960 } });
    const errors = []; page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(origin + '/?view=work&issue=ENG-8');
    const panel = page.locator('.issue-control');
    await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    await panel.getByLabel('Reason for this handoff').fill('Disposable real-backend manual takeover');
    await panel.getByRole('button', { name: 'Preview control change' }).click();
    await panel.getByRole('button', { name: 'Confirm manual takeover' }).click();
    await panel.getByText(/Manual control—safe to edit/).waitFor();
    assert.match(await panel.textContent(), /operator@driftward.test/);
    await panel.getByLabel('Reason for this handoff').fill('Disposable real-backend authorized handback');
    await panel.getByRole('button', { name: 'Preview control change' }).click();
    await panel.getByText(/does not continue or verify manual edits/).first().waitFor();
    const confirm = panel.getByRole('button', { name: 'Confirm return to Factory' });
    await confirm.evaluate((el) => { el.click(); el.click(); });
    await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    await panel.getByText(/Only explicit future attempts/).waitFor();
    await page.locator('#issue-attempts tr').filter({ hasText: 'Accepted' }).waitFor();
    assert.equal(await panel.getByText(/Manual control—safe to edit/).count(), 0);
    await panel.locator('summary').click();
    assert.match(await panel.locator('.control-audit').textContent(), /Manual takeover/);
    assert.match(await panel.locator('.control-audit').textContent(), /Return to Factory/);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: join(tmpdir(), 'factory-control-real-backend.png'), fullPage: true });
    const result = nextLine(); fixture.stdin.end('verify\n');
    assert.deepEqual(await result, { verified: true, attempts: 1, outbox: 1, history: 2 });
    const code = fixture.exitCode ?? (await once(fixture, 'exit'))[0]; assert.equal(code, 0);
    console.log('Real-backend browser check passed: authenticated preview/confirm takeover, safe handoff, return, one attempt/outbox, actor audit.');
  } finally {
    if (browser) await browser.close();
    if (fixture && fixture.exitCode === null) { fixture.stdin.end(); fixture.kill(); }
    lines?.close();
    if (proxy) await new Promise((done) => proxy.close(done));
    await rm(temp, { recursive: true, force: true });
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
