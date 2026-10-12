#!/usr/bin/env node
// Disposable HTTP fixtures only: no operator, bridge or production runtime connection.
'use strict';
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { readFile } = require('node:fs/promises');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { chromium } = require('playwright');
const root = resolve(__dirname, '..');
const evidenceRoot = process.env.FACTORY_UI_EVIDENCE_DIR || tmpdir();
const stamp = new Date().toISOString();
const controls = new Map();
const confirms = [];
const previews = [];
const inspections = [];
let revisionBlocked = false;
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
    available_actions: [{ action: issue === 'ENG-906' ? 'revision' : 'takeover', label: issue === 'ENG-906' ? 'Revise existing draft PR' : 'Take over manually' }],
  });
  return { ...controls.get(issue), actions_enabled: !readOnly };
}
const assets = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'] };
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  res.setHeader('Cache-Control', 'no-store');
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
  if (path.endsWith('/revision/inspect')) {
    inspections.push(path); revisionBlocked = false;
    value = { created: true, reason: 'inspection_queued', attempt_id: 'fixture-source-attempt' };
  } else if (match) {
    const [, issue, action] = match;
    if (!action && failControl) { res.statusCode = 503; res.end('{}'); return; }
    value = control(issue);
    if (action === 'preview') {
      previews.push(body);
      if (previewError) { res.statusCode = 400; res.end(JSON.stringify({ error: previewError })); return; }
      if (delayPreview) { delayPreview(); delayPreview = null; await new Promise((done) => { releasePreview = done; }); }
      value = { eligible: body.action !== 'revision' || !revisionBlocked, work_item_key: issue, action: body.action, intent: body.intent, control: value,
        snapshot_digest: String(value.version).padStart(64, 'a'), preview_token: 'test-preview-token',
        expires_at: (Date.now() + (expirePreview ? 500 : 300000)) / 1000,
        effect_summary: body.action === 'revision' ? 'Test data: reserve one new immutable revision attempt on the same draft PR and branch at this commit. Earlier attempt history is preserved.' : 'Test data: fence future execution and request cancellation; do not edit until confirmed safe.',
        artifact: { pr_url: body.action === 'revision' ? 'https://github.com/example/project/pull/12' : 'javascript:alert(1)', repository: 'example/project', branch: 'factory/fixture', head_sha: 'a'.repeat(40) },
        revision: body.revision && { ...body.revision, reviewed_head_sha: 'a'.repeat(40), source_attempt_id: 'fixture-source-attempt', source_job_id: 'fixture-source-job' },
        blockers: body.action === 'revision' && revisionBlocked ? ['A fresh PR inspection is required. Request a PR observation, then preview again.'] : [] };
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
    const issue = path.split('/').pop();
    value = { work_item_key: issue, attempts: issue === 'ENG-906' ? [
      ...(!control(issue).available_actions.length ? [
      { attempt: { work_item_key: issue, attempt_id: 'fixture-revision-attempt', prior_attempt_id: 'fixture-source-attempt', generation: 2, status: 'accepted', mode: 'real' }, attempt_kind: 'revision',
        job: { job_id: 'fixture-revision-job', branch: 'factory/fixture', pr_url: 'https://github.com/example/project/pull/12', payload: { repo: 'example/project' } },
        revision: { ...confirms.filter((body) => body.action === 'revision').at(-1).revision, source_attempt_id: 'fixture-source-attempt', reviewed_head_sha: 'a'.repeat(40), approved_by: 'authenticated-operator@example.test' } },
      ] : []),
      { attempt: { work_item_key: issue, attempt_id: 'fixture-source-attempt', generation: 1, status: 'verified_ready', mode: 'real', created_at: stamp, updated_at: stamp }, attempt_kind: 'initial',
        job: { job_id: 'fixture-source-job', branch: 'factory/fixture', commit_sha: 'a'.repeat(40), pr_url: 'https://github.com/example/project/pull/12', payload: { repo: 'example/project' } } },
    ] : [], timeline: [] };
  } else { res.statusCode = 404; value = {}; }
  res.end(JSON.stringify(value));
});
async function main() {
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.FACTORY_CONSOLE_CHROMIUM });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = []; page.on('pageerror', (error) => errors.push(error.message));
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
    await reason.fill('Manual debugging <script>must remain text</script>');
    await preview.focus(); await page.keyboard.press('Enter');
    await confirm.waitFor();
    assert.equal(await panel.locator('h4').evaluate((el) => el === document.activeElement), true);
    assert.equal(await panel.locator('a[href^="javascript:"]').count(), 0);
    await page.screenshot({ path: join(tmpdir(), 'factory-control-preview-desktop.png'), fullPage: true });
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
    await page.screenshot({ path: join(tmpdir(), 'factory-control-recovery-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(tmpdir(), 'factory-control-recovery-mobile.png'), fullPage: true });
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
    for (const width of [320, 390, 700, 850, 1280]) {
      await page.setViewportSize({ width, height: 1080 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Overflow at ' + width);
      for (const button of await panel.locator('button:visible').all()) assert.ok((await button.boundingBox()).height >= 44);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(tmpdir(), 'factory-control-preview-mobile.png'), fullPage: true });
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });

    // Revision uses the same approval fencing and keeps optional attestations separate from proof.
    await open('ENG-906');
    const feedback = panel.getByLabel('Revision feedback');
    const revisionPreview = panel.getByRole('button', { name: 'Preview revision', exact: true });
    const revisionConfirm = panel.getByRole('button', { name: 'Confirm revision attempt' });
    assert.equal(await revisionPreview.isDisabled(), true);
    await feedback.fill('Apply review and approved design feedback. <script>plain text</script>');
    revisionBlocked = true;
    await revisionPreview.click();
    await panel.getByText('This action is blocked. No control change has been made.').waitFor();
    assert.equal(await revisionConfirm.count(), 0);
    const inspectionCount = inspections.length;
    await panel.getByRole('button', { name: 'Refresh control' }).click();
    assert.equal(inspections.length, inspectionCount, 'ordinary refresh does not request a PR observation');
    await panel.getByRole('button', { name: 'Refresh PR observation' }).click();
    await panel.getByText(/PR observation requested/).waitFor();
    assert.equal(inspections.length, inspectionCount + 1);
    previewError = 'revision_draft_pr_inspection_pending'; await revisionPreview.click();
    await panel.getByText(/A fresh PR inspection is required/).first().waitFor();
    assert.equal(await panel.getByRole('button', { name: 'Refresh PR observation' }).isEnabled(), true);
    previewError = ''; await panel.getByRole('button', { name: 'Refresh PR observation' }).click();
    await panel.getByText(/PR observation requested/).waitFor();
    await revisionPreview.focus(); await page.keyboard.press('Enter'); await revisionConfirm.waitFor();
    assert.equal(await panel.locator('h4').evaluate((el) => el === document.activeElement), true);
    assert.equal(previews.at(-1).revision.reviewed_head_sha, undefined, 'operator does not supply a trusted commit');
    assert.match(await panel.locator('.control-preview').textContent(), /fixture-source-attempt/);
    assert.match(await panel.locator('.control-preview').textContent(), /operator attestation/);
    await panel.getByLabel('Review reference (optional)').fill('https://github.com/example/project/pull/12#discussion_r1');
    assert.equal(await revisionConfirm.count(), 0, 'editing reference revokes approval');
    await revisionPreview.click(); await revisionConfirm.waitFor();
    for (const width of [320, 390, 700, 850, 1280]) {
      await page.setViewportSize({ width, height: 1080 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Revision overflow at ' + width);
    }
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.screenshot({ path: join(evidenceRoot, 'factory-revision-preview-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(evidenceRoot, 'factory-revision-preview-mobile.png'), fullPage: true });
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    conflict = true; await revisionConfirm.click();
    await panel.getByText(/The issue changed/).waitFor();
    assert.equal(await revisionConfirm.count(), 0);
    conflict = false; await panel.getByRole('button', { name: 'Refresh control' }).click();
    await revisionPreview.click(); await revisionConfirm.waitFor();
    expirePreview = true;
    await panel.getByRole('button', { name: 'Dismiss preview' }).click();
    await revisionPreview.click(); await panel.getByText(/The preview expired/).waitFor();
    expirePreview = false;
    await revisionPreview.click(); await revisionConfirm.waitFor();
    ambiguous = true; await revisionConfirm.evaluate((el) => { el.click(); el.click(); });
    await panel.getByRole('button', { name: 'Check previous request' }).waitFor();
    const previousRevision = confirms.at(-1);
    await page.locator('#close-issue').click();
    await page.goBack();
    await panel.getByRole('button', { name: 'Check previous request' }).waitFor();
    await page.reload();
    await panel.getByRole('button', { name: 'Check previous request' }).click();
    await panel.getByText(/Revision attempt recorded/).waitFor();
    assert.deepEqual(confirms.at(-1), previousRevision, 'revision replay reuses the same approval/request');
    await page.locator('#issue-attempts-mobile li').filter({ hasText: 'Revision' }).waitFor();
    assert.match(await page.locator('#issue-attempts-mobile').textContent(), /fixture-source-attempt/);
    assert.match(await page.getByRole('region', { name: 'Revision lineage' }).textContent(), /operator attestations/);
    assert.equal(await page.locator('script:not([src])').count(), 0);
    await page.screenshot({ path: join(evidenceRoot, 'factory-revision-history-mobile.png'), fullPage: true });

    readOnly = true; await page.reload();
    await panel.getByText(/Read-only access/).waitFor();
    assert.equal(await preview.count(), 0);
    assert.deepEqual(errors, []);
    console.log('Issue control browser checks passed: takeover/return/revision preview and confirmation, explicit PR observation, blocked/stale/expired previews, ambiguous replay across navigation/reload, lineage, keyboard and responsive layout.');
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
