#!/usr/bin/env node
// Disposable HTTP fixtures only: no operator, bridge or production runtime connection.
'use strict';
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { readFile, mkdir } = require('node:fs/promises');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { chromium } = require('playwright');
const root = resolve(__dirname, '..');
const evidenceDir = process.env.FACTORY_CONSOLE_EVIDENCE_DIR || tmpdir();
const stamp = new Date().toISOString();
const controls = new Map();
const confirms = [];
const previews = [];
let conflict = false;
let ambiguous = false;
let expirePreview = false;
let delayPreview;
let releasePreview;
let readOnly = false;
let failControl = false;
let previewError = '';
function control(issue) {
  if (!controls.has(issue)) controls.set(issue, {
    schema: 'factory-console-issue-control-v1', work_item_key: issue, status: 'factory',
    version: 0, safe_to_edit: false, updated_at: stamp, actor: null, reason: null,
    pending_attempts: [], history: [], actions_enabled: true,
    available_actions: [{ action: 'takeover', label: 'Take over manually' }],
  });
  return { ...controls.get(issue), actions_enabled: !readOnly };
}
const assets = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'] };
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
  if (assets[path]) {
    res.setHeader('Content-Type', assets[path][1]);
    res.end(await readFile(join(root, 'factory/console', assets[path][0]))); return;
  }
  res.setHeader('Content-Type', 'application/json');
  let body;
  if (req.method === 'POST') {
    assert.equal(req.headers['x-factory-action'], 'issue-control');
    let data = ''; for await (const part of req) data += part;
    body = JSON.parse(data);
    assert.equal(body.actor, undefined, 'actor must come from the authenticated backend');
  }
  const match = path.match(/^\/api\/issues\/(ENG-\d+)\/control(?:\/(preview|confirm))?$/);
  let value;
  if (match) {
    const [, issue, action] = match;
    if (!action && failControl) { res.statusCode = 503; res.end('{}'); return; }
    value = control(issue);
    if (action === 'preview') {
      previews.push(body);
      if (previewError) { res.statusCode = 400; res.end(JSON.stringify({ error: previewError })); return; }
      if (delayPreview) { delayPreview(); delayPreview = null; await new Promise((done) => { releasePreview = done; }); }
      value = { eligible: true, work_item_key: issue, action: body.action, intent: body.intent, control: value,
        snapshot_digest: String(value.version).padStart(64, 'a'), preview_token: 'test-preview-token',
        expires_at: (Date.now() + (expirePreview ? 500 : 300000)) / 1000,
        effect_summary: 'Test data: fence future execution and request cancellation; do not edit until confirmed safe.',
        artifact: { pr_url: 'javascript:alert(1)', branch: 'factory/fixture', head_sha: 'a'.repeat(40) } };
    } else if (action === 'confirm') {
      confirms.push(body);
      assert.equal(body.preview_token, 'test-preview-token');
      if (conflict) { res.statusCode = 409; res.end('{}'); return; }
      if (ambiguous) { ambiguous = false; res.statusCode = 503; res.end('{}'); return; }
      value = { ...value, status: body.action === 'takeover' ? 'handoff_pending' : 'factory', version: value.version + 1,
        safe_to_edit: false, automatic_intake_blocked: true, actor: 'authenticated-operator@example.test', reason: body.reason,
        available_actions: [], pending_attempts: body.action === 'takeover' ? [{ attempt_id: 'fixture-attempt', kind: 'cancellation_requested' }] : [],
        history: [{ request_id: body.request_id, action: body.action, actor: 'authenticated-operator@example.test', reason: body.reason, created_at: stamp }] };
      controls.set(issue, value);
      value = { control: value, request_id: body.request_id };
    }
  } else if (path === '/api/overview') {
    value = { generated_at: stamp, active_attempts: [], recent_attempts: [], status_counts: {} };
  } else if (path.startsWith('/api/lifecycle/')) {
    value = { work_item_key: path.split('/').pop(), attempts: [], timeline: [] };
  } else { res.statusCode = 404; value = {}; }
  res.end(JSON.stringify(value));
});
async function main() {
  await mkdir(evidenceDir, { recursive: true });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.FACTORY_CONSOLE_CHROMIUM });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = []; page.on('pageerror', (error) => errors.push(error.message));
    const cspViolations = [];
    await page.exposeFunction('recordFactoryCspViolation', violation => cspViolations.push(violation));
    await page.addInitScript(() => document.addEventListener('securitypolicyviolation', event => window.recordFactoryCspViolation({ directive: event.effectiveDirective, blocked: event.blockedURI })));
    const panel = page.locator('.issue-control');
    const reason = panel.getByLabel('Reason for this handoff');
    const preview = panel.getByRole('button', { name: 'Preview control change' });
    const confirm = panel.getByRole('button', { name: 'Confirm manual takeover' });
    async function open(issue) {
      await page.goto(base + '/?view=work&issue=' + issue);
      await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    }
    await open('ENG-901');
    assert.equal(await preview.isDisabled(), true);
    assert.equal(await reason.evaluate(element => element.required), true, 'Ownership reason retains native required validation');
    assert.equal(await reason.evaluate(element => element.maxLength), 320);
    await reason.fill('   ');
    assert.equal(await preview.isDisabled(), true, 'Whitespace cannot authorize a preview');
    assert.equal(previews.length, 0);
    await reason.fill('Manual debugging <script>must remain text</script>');
    await preview.focus(); await page.keyboard.press('Enter');
    await confirm.waitFor();
    assert.equal(await panel.locator('h4').evaluate((el) => el === document.activeElement), true);
    assert.equal(await panel.locator('a[href^="javascript:"]').count(), 0);
    await page.screenshot({ path: join(evidenceDir, 'factory-control-preview-desktop.png'), fullPage: true });
    // Two synchronous clicks must produce one durable request, then pending (never safe-to-edit).
    await confirm.evaluate((el) => { el.click(); el.click(); });
    await panel.getByText('Handoff pending', { exact: true }).waitFor();
    assert.equal(confirms.length, 1);
    assert.equal(await panel.getByText(/safe to edit/).count(), 0);
    await panel.locator('summary').click();
    assert.match(await panel.locator('.control-audit').textContent(), /authenticated-operator@example.test/);
    assert.equal(await panel.locator('script').count(), 0);

    controls.set('ENG-901', { ...control('ENG-901'), pending_attempts: [{ attempt_id: 'fixture-attempt', kind: 'effect_in_flight',
      recovery: { state: 'not_found', observed_at: stamp } }] });
    await panel.getByRole('button', { name: 'Refresh control' }).click();
    await panel.getByText(/No durable executor receipt was found/).waitFor();
    assert.equal(await panel.getByText(/Manual control—safe to edit/).count(), 0);
    await page.screenshot({ path: join(evidenceDir, 'factory-control-recovery-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(evidenceDir, 'factory-control-recovery-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 960 });

    // Only authoritative safe_to_edit makes the manual editing label available.
    controls.set('ENG-901', { ...control('ENG-901'), status: 'manual', safe_to_edit: true,
      pending_attempts: [], available_actions: [{ action: 'return', intent: 'fresh_attempt', label: 'Return to Factory: fresh attempt' }] });
    await panel.getByRole('button', { name: 'Refresh control' }).click();
    await panel.getByText(/Manual control—safe to edit/).waitFor();
    failControl = true; await panel.getByRole('button', { name: 'Refresh control' }).click();
    await panel.getByText(/Control state is unavailable or stale/).waitFor();
    assert.equal(await panel.getByText(/Manual control—safe to edit/).count(), 0);
    failControl = false; await panel.getByRole('button', { name: 'Refresh control' }).click();
    await panel.getByText(/Manual control—safe to edit/).waitFor();
    assert.equal(await panel.locator('option').count(), 1);
    assert.match(await panel.locator('option').textContent(), /fresh attempt/);
    await reason.fill('Handing back completed investigation'); await preview.click();
    await panel.getByRole('button', { name: 'Confirm return to Factory' }).waitFor();
    assert.equal(previews.at(-1).intent, 'fresh_attempt');
    await panel.getByRole('button', { name: 'Confirm return to Factory' }).click();
    await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    await panel.getByText(/Only explicit future attempts/).waitFor();
    assert.equal(await panel.getByText(/Manual control—safe to edit/).count(), 0);

    // Return blockers are allowlisted copy; unknown server diagnostics stay private.
    await open('ENG-905'); await reason.fill('Return eligibility fixture');
    for (const [code, expected] of [
      ['trusted_retry_eligibility_failed', /current Linear issue must be Ready and unblocked/],
      ['prior_draft_pr_blocks_retry', /open PR requires a continuation path/],
      ['prior_draft_pr_inspection_pending', /fresh PR inspection is required/],
      ['prior_draft_pr_merged_create_new_issue', /previous PR is merged/],
      ['private fixture diagnostic', /Control request failed \(HTTP 400\)/],
    ]) {
      previewError = code; await preview.click(); await panel.getByText(expected).waitFor();
      assert.equal(await panel.getByText('private fixture diagnostic', { exact: false }).count(), 0);
      previewError = ''; await panel.getByRole('button', { name: 'Refresh control' }).click();
      await page.waitForFunction(() => !document.querySelector('.control-form button').disabled);
    }

    // A stale snapshot invalidates approval; no confirmation can be blindly replayed.
    await open('ENG-902'); conflict = true;
    await reason.fill('Take over'); await preview.click(); await confirm.click();
    await panel.getByText(/The issue changed/).waitFor();
    assert.equal(await confirm.count(), 0);
    assert.equal(await panel.getByRole('button', { name: 'Check previous request' }).count(), 0);
    conflict = false;
    await panel.getByRole('button', { name: 'Refresh control' }).click();
    await preview.waitFor({ state: 'visible' });

    // An ambiguous response survives reload and retries exactly the same approved request.
    ambiguous = true; await preview.click(); await confirm.click();
    await panel.getByRole('button', { name: 'Check previous request' }).waitFor();
    const previous = confirms.at(-1);
    await page.reload();
    await panel.getByRole('button', { name: 'Check previous request' }).click();
    await panel.getByText('Handoff pending', { exact: true }).waitFor();
    assert.deepEqual(confirms.at(-1), previous);
    assert.equal(await panel.getByRole('button', { name: 'Check previous request' }).count(), 0);

    // Form changes revoke the preview; expiry does too.
    await open('ENG-903'); await reason.fill('Take over'); await preview.click(); await confirm.waitFor();
    await reason.fill('Changed reason'); assert.equal(await confirm.count(), 0);
    expirePreview = true; await preview.click();
    await panel.getByText(/The preview expired/).waitFor();
    assert.equal(await confirm.count(), 0); expirePreview = false;

    // Switching issues fences a late preview and does not alter history/focus handling.
    let started; const delayed = new Promise((done) => { started = done; }); delayPreview = started;
    await preview.click(); await delayed;
    assert.equal(await reason.isDisabled(), true, 'A pending preview disables the reason');
    assert.equal(await panel.getByLabel('Control action').isDisabled(), true, 'A pending preview disables the action');
    assert.equal(await panel.getByRole('button', { name: 'Preparing preview…' }).isDisabled(), true);
    assert.equal(await panel.getByRole('button', { name: 'Refresh control' }).isDisabled(), true);
    await page.locator('#issue-input').fill('ENG-904'); await page.locator('#issue-form').evaluate((el) => el.requestSubmit());
    await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    releasePreview();
    assert.equal(await panel.locator('.control-preview').count(), 0);
    assert.equal(await reason.inputValue(), '');
    assert.equal(await page.locator('#issue-title').evaluate((el) => el === document.activeElement), true);
    await page.goBack(); await panel.getByText('Factory-controlled', { exact: true }).waitFor();
    assert.match(page.url(), /ENG-903/);

    // Phone/intermediate layouts, keyboard and doubled text retain usable controls.
    await reason.fill('Responsive control example'); await preview.click(); await confirm.waitFor();
    for (const width of [320, 390, 700, 768, 850, 1024, 1280, 1440]) {
      await page.setViewportSize({ width, height: 1080 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Overflow at ' + width);
      for (const button of await panel.locator('button:visible').all()) assert.ok((await button.boundingBox()).height >= 44);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(evidenceDir, 'factory-control-preview-mobile.png'), fullPage: true });
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    readOnly = true; await page.reload();
    await panel.getByText(/Read-only access/).waitFor();
    assert.equal(await preview.count(), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(cspViolations, [], 'Ownership controls must work with the production CSP');
    console.log('Issue control browser checks passed: preview/confirm, fencing, idempotency, authorization states, responsive layout.');
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => {
  releasePreview?.();
  server.closeAllConnections();
  server.close();
});
