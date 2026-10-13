#!/usr/bin/env node
// Isolated catalog and MCP checks. Uses only fictional stories; never calls runtime APIs.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { readFile, mkdtemp, open, mkdir } = require('node:fs/promises');
const { createServer } = require('node:net');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { setTimeout: delay } = require('node:timers/promises');
const { chromium } = require('playwright');

const consoleRoot = resolve(__dirname, '../factory/console');
const evidenceDir = process.env.FACTORY_CONSOLE_EVIDENCE_DIR || tmpdir();

(async () => {
  await mkdir(evidenceDir, { recursive: true });
  const socket = createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const temp = await mkdtemp(join(tmpdir(), 'factory-storybook-'));
  const logPath = join(temp, 'server.log');
  const log = await open(logPath, 'w');
  const server = spawn(process.execPath, ['node_modules/storybook/dist/bin/dispatcher.js', 'dev', '--host', '127.0.0.1', '--port', String(port), '--ci', '--disable-telemetry'], {
    cwd: consoleRoot, stdio: ['ignore', log.fd, log.fd], detached: true,
  });
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 120; i++) {
      if (server.exitCode !== null) throw new Error(`Storybook exited: ${server.exitCode}`);
      try { ready = (await fetch(base + '/index.json', { signal: AbortSignal.timeout(500) })).ok; } catch {}
      if (ready) break;
      await delay(500);
    }
    assert(ready, 'Storybook did not start');
    let requestId = 0;
    async function rpc(method, params) {
      const response = await fetch(base + '/mcp', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
        body: JSON.stringify({ jsonrpc: '2.0', id: ++requestId, method, params }),
        signal: AbortSignal.timeout(30000),
      });
      assert.equal(response.status, 200);
      const body = await response.text();
      const message = JSON.parse(body.startsWith('event:') ? body.split('\n').find(line => line.startsWith('data: ')).slice(6) : body);
      assert(!message.error, JSON.stringify(message.error));
      assert(!message.result.isError, JSON.stringify(message.result));
      return message.result;
    }
    await rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'factory-storybook-check', version: '1.0.0' } });
    const tools = await rpc('tools/list', {});
    for (const name of ['docs-list', 'docs-show', 'docs-show-story']) assert(tools.tools.some(tool => tool.name === name), `Missing MCP tool ${name}`);
    const docs = JSON.stringify(await rpc('tools/call', { name: 'docs-list', arguments: { withStoryIds: true } }));
    for (const component of ['button', 'notice', 'statusbadge', 'metric', 'summaryitem', 'externallink', 'brandmark']) assert(docs.includes(`components-${component}`));
    for (const component of ['textinput', 'metadata', 'surface']) assert(docs.includes(`components-${component}`));
    assert(docs.includes('foundations-factorytheme'));
    assert(docs.includes('foundations-start-here--docs'));
    const badge = JSON.stringify(await rpc('tools/call', { name: 'docs-show', arguments: { id: 'components-statusbadge' } }));
    for (const expected of ['@driftward/ui', 'tone', 'warning', 'children', 'Props']) assert(badge.includes(expected), `Missing MCP component contract: ${expected}`);
    const field = JSON.stringify(await rpc('tools/call', { name: 'docs-show', arguments: { id: 'components-textinput' } }));
    for (const expected of ['@driftward/ui', 'required', 'maxLength', 'pattern', 'isDisabled', 'onChange']) assert(field.includes(expected), `Missing MCP field contract: ${expected}`);
    const builtManifest = await readFile(join(consoleRoot, 'storybook-static/manifests/components.json'), 'utf8');
    assert(builtManifest.includes('components-statusbadge'));
    assert(builtManifest.includes('@driftward/ui'));
    assert(builtManifest.includes('Native browser pattern validation'), 'Static catalog documents native field validation');
    for (const component of Object.values(JSON.parse(builtManifest).components)) {
      assert(!component.error, `Invalid static component manifest: ${component.id}`);
    }

    browser = await chromium.launch({ headless: true, executablePath: process.env.FACTORY_CONSOLE_CHROMIUM || undefined });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    const apiRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) apiRequests.push(url.pathname);
      return url.origin === base ? route.continue() : route.abort();
    });
    const index = await (await fetch(base + '/index.json')).json();
    const stories = Object.values(index.entries).filter(entry => entry.type === 'story');
    assert(stories.length >= 20);
    async function visit(id) {
      await page.goto(base + '/iframe.html?id=' + id + '&viewMode=story');
      await page.waitForFunction(() => document.body.classList.contains('sb-show-main'));
      await page.locator('#storybook-root > *').first().waitFor({ state: 'attached' });
    }
    for (const story of stories) {
      await visit(story.id);
      assert(await page.locator('#storybook-root > *').count(), `Empty story ${story.id}`);
    }
    await visit('factory-beforeexecution--awaiting-intake');
    assert.match(await page.locator('.before-execution').textContent(), /not confirmation that work is queued or running/);
    await visit('factory-beforeexecution--unavailable');
    assert.match(await page.locator('.before-execution').textContent(), /does not mean nothing is waiting/);
    await visit('factory-beforeexecution--blocked');
    await page.getByText('Source and refresh limits', { exact: true }).focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.intake-scope').getAttribute('open'), '');
    await visit('factory-agenttrace--configured');
    assert.equal(await page.getByRole('link').getAttribute('target'), '_blank');
    assert.match(await page.locator('.agent-trace').textContent(), /Capture unconfirmed/);
    assert.match(await page.getByRole('link').textContent(), /Find trace in PostHog/);
    assert.equal(new URL(await page.getByRole('link').getAttribute('href')).searchParams.get('timestamp'), '2026-09-30T05:53:34.202Z');
    await visit('factory-agenttrace--historical');
    assert.match(await page.locator('.agent-trace').textContent(), /capture status unknown/);
    assert.doesNotMatch(await page.locator('.agent-trace').textContent(), /capture failed|not captured/i);
    await visit('factory-agenttrace--setup-failed');
    assert.match(await page.locator('.agent-trace').textContent(), /Telemetry setup failure recorded/);
    for (const state of ['historical', 'setup-failed', 'not-configured', 'dry-run', 'unsafe-url', 'wrong-attempt', 'missing-timestamp', 'invalid-timestamp', 'extra-query', 'fragment']) {
      await visit('factory-agenttrace--' + state);
      assert.equal(await page.locator('.agent-trace a').count(), 0, state + ' must not link');
    }
    await visit('components-button--default');
    assert.equal(await page.getByRole('button').getAttribute('type'), 'button');
    await page.keyboard.press('Tab');
    assert(await page.getByRole('button').evaluate(element => element === document.activeElement));
    assert(await page.getByRole('button').evaluate(element => {
      const style = getComputedStyle(element);
      return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0) || style.boxShadow !== 'none';
    }), 'Keyboard focus must be visible');
    await visit('components-button--loading');
    assert(await page.getByRole('button').isDisabled());
    assert.equal(await page.getByRole('button').getAttribute('aria-busy'), 'true');
    await visit('components-button--primary');
    assert.equal(await page.getByRole('button').getAttribute('type'), 'button');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await page.getByRole('button').evaluate(element => getComputedStyle(element).transitionDuration), '0s');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await visit('components-textinput--search');
    await page.getByRole('textbox', { name: 'Search' }).fill('ENG-901');
    assert.equal(await page.getByRole('textbox', { name: 'Search' }).inputValue(), 'ENG-901');
    await visit('components-textinput--required-reason');
    const reason = page.getByRole('textbox', { name: 'Reason for this handoff' });
    assert.equal(await reason.evaluate(element => element.checkValidity()), false);
    assert.equal(await reason.evaluate(element => element.maxLength), 320);
    await reason.fill('Fictional catalog validation');
    assert.equal(await reason.evaluate(element => element.checkValidity()), true);
    await visit('components-textinput--disabled');
    assert(await page.getByRole('textbox').isDisabled());
    await visit('components-textinput--invalid');
    assert.equal(await page.getByRole('textbox').getAttribute('aria-invalid'), 'true');
    assert.match(await page.locator('#storybook-root').textContent(), /Enter an Engineering issue/);
    await visit('components-metadata--recorded-evidence');
    assert.equal(await page.locator('dl dt').count(), 4);
    assert.equal(await page.locator('dl dd').count(), 4);
    assert.match(await page.locator('dl').textContent(), /Independent reviewUnavailable in this view/);
    await visit('components-surface--unavailable');
    assert.match(await page.getByRole('status').textContent(), /No successful snapshot has been retrieved/);
    await visit('components-surface--loading');
    assert.match(await page.getByRole('status').textContent(), /Retrieving evidence/);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert(await page.locator('circle').evaluateAll(elements => elements.some(element => getComputedStyle(element).animationDuration === '3s')), 'Loading feedback respects reduced motion');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await visit('foundations-factorytheme--operator-workbench');
    assert(await page.locator('[data-astryx-theme="neutral"]').count());
    assert.equal(await page.getByRole('heading', { name: 'Recorded execution' }).getAttribute('class').then(value => value.includes('astryx-heading')), true);
    await visit('components-notice--hidden');
    assert(await page.locator('.notice').isHidden());
    await visit('components-summaryitem--zero');
    assert.equal(await page.locator('.summary-item strong').textContent(), '0');
    await visit('components-externallink--default');
    assert.equal(await page.getByRole('link').getAttribute('rel'), 'noopener noreferrer');
    await visit('components-statusbadge--pending');
    assert.equal(await page.locator('.pill svg').getAttribute('aria-hidden'), 'true');
    assert.equal(await page.locator('.pill').evaluate(element => getComputedStyle(element).color), 'rgb(232, 192, 120)');
    await visit('factory-verificationevidence--head-changed');
    assert.match(await page.locator('.verification-evidence').textContent(), /Earlier verification does not cover that revision/);
    await page.getByText('Recorded check evidence', { exact: true }).focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.verification-evidence details').getAttribute('open'), '');
    await visit('factory-verificationevidence--stale-inspection');
    assert.match(await page.locator('.verification-evidence').textContent(), /outside its freshness window/);
    await visit('factory-verificationevidence--legacy-without-checkpoints');
    assert.match(await page.locator('.verification-evidence').textContent(), /No immutable checkpoints/);
    assert.equal(await page.locator('.verification-evidence .pill.success').count(), 0);
    await visit('factory-issuecontrol--factory-controlled');
    const control = page.locator('.issue-control');
    assert.equal(await control.getByRole('button', { name: 'Preview control change' }).isDisabled(), true);
    await control.getByLabel('Reason for this handoff').fill('Fictional catalog takeover');
    await control.getByRole('button', { name: 'Preview control change' }).focus();
    await page.keyboard.press('Enter');
    await control.getByRole('button', { name: 'Confirm manual takeover' }).waitFor();
    assert.equal(await control.locator('h4').evaluate(element => element === document.activeElement), true);
    await page.screenshot({ path: join(evidenceDir, 'factory-storybook-control-preview.png'), fullPage: true });
    await control.getByRole('button', { name: 'Confirm manual takeover' }).click();
    await control.getByText('Handoff pending', { exact: true }).waitFor();
    assert.equal(await control.getByText(/Manual control—safe to edit/).count(), 0);
    await visit('factory-issuecontrol--blocked-preview');
    await control.getByLabel('Reason for this handoff').fill('Fictional blocked catalog preview');
    await control.getByRole('button', { name: 'Preview control change' }).click();
    await control.getByText('This action is blocked. No control change has been made.').waitFor();
    assert.equal(await control.getByRole('button', { name: /Confirm/ }).count(), 0);
    await visit('factory-issuecontrol--manual-control');
    await control.getByText(/Manual control—safe to edit/).waitFor();
    await control.getByLabel('Reason for this handoff').fill('Fictional catalog handback');
    await control.getByRole('button', { name: 'Preview control change' }).click();
    await control.getByRole('button', { name: 'Confirm return to Factory' }).waitFor();
    assert.match(await control.textContent(), /does not continue or verify manual edits/);
    for (const width of [320, 390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Catalog ownership preview overflow at ' + width);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(evidenceDir, 'factory-storybook-control-mobile.png'), fullPage: true });
    await visit('factory-issuecontrol--read-only');
    await control.getByText(/Read-only access/).waitFor();
    assert.equal(await control.getByRole('button', { name: 'Preview control change' }).count(), 0);
    await visit('factory-issuecontrol--unconfirmed-manual');
    await control.getByText(/Safe handoff has not been confirmed/).waitFor();
    assert.equal(await control.getByText(/Manual control—safe to edit/).count(), 0);
    await page.setViewportSize({ width: 1280, height: 900 });
    await visit('foundations-catalog--tokens');
    await page.screenshot({ path: join(evidenceDir, 'factory-storybook-tokens.png') });
    await page.setViewportSize({ width: 375, height: 812 });
    await visit('foundations-catalog--icons');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: join(evidenceDir, 'factory-storybook-icons-mobile.png') });
    assert.deepEqual(errors, []);
    assert.deepEqual(apiRequests, []);
    console.log(`Passed: ${stories.length} stories, MCP discovery and typed docs, static manifest, keyboard/status/asset checks; no runtime API requests.`);
  } catch (error) {
    console.error((await readFile(logPath, 'utf8')).slice(-5000));
    throw error;
  } finally {
    await browser?.close();
    if (server.exitCode === null) process.kill(-server.pid, 'SIGTERM');
    await log.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
