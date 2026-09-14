#!/usr/bin/env node
// Browser checks against disposable API fixtures. See factory/README.md for setup.
'use strict';
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { readFile } = require('node:fs/promises');
const { resolve, join } = require('node:path');
const { tmpdir } = require('node:os');
const { chromium } = require('playwright');

const root = resolve(__dirname, '..');
const now = Date.now();
function record(issue, generation, status, mode = 'real', repo = 'Driftward-LLC/capykit') {
  const id = issue + '-' + generation;
  return {
    attempt: { attempt_id: id, work_item_key: issue, generation, status, mode, created_at: new Date(now - 3600000).toISOString(), updated_at: new Date(now - 60000).toISOString() },
    job: { job_id: 'job-' + id, state: status, branch: 'factory/' + id, payload: { repo },
      pr_url: 'https://github.com/' + repo + '/pull/' + generation, commit_sha: 'a'.repeat(40), reason_code: status === 'failed' ? 'verification_github_check_failed' : null },
    execution_lease: { status: status === 'running' ? 'active' : 'released' },
  };
}
const running = record('ENG-101', 2, 'running');
const failed = record('ENG-101', 1, 'failed');
const verified = record('ENG-102', 1, 'verified_ready');
const simulated = record('ENG-103', 1, 'verified_ready', 'dry-run', 'Driftward-LLC/driftward-host');
const unknown = record('ENG-104', 1, 'effect_outcome_unknown');
const unrecognized = record('ENG-105', 1, 'constructor');
unrecognized.job.pr_url = 'javascript:alert(1)';
const malicious = record('ENG-106', 1, 'failed');
malicious.job.payload.repo = '<img src=x onerror=alert(1)>';
malicious.job.pr_url = 'https://github.com/attacker/repo/pull/1';
const records = [running, failed, verified, simulated, unknown, unrecognized, malicious];
function overview() {
  return {
    generated_at: new Date().toISOString(), mode: 'real',
    active_attempts: [running, unknown],
    recent_attempts: records,
    status_counts: { running: 1, failed: 2, verified_ready: 2, effect_outcome_unknown: 1, constructor: 1 },
    reconciliation: { actionable: 2, actionable_severity: { critical: 0, warning: 2 }, scope: { terminal_work: 9 } },
    lifecycle_outbox: { available: false, dead_letters: 0 },
    links: { hatchet: 'https://attacker.example/' },
  };
}
function lifecycle(issue) {
  return {
    work_item_key: issue,
    attempts: records.filter((r) => r.attempt.work_item_key === issue).sort((a, b) => b.attempt.generation - a.attempt.generation),
    timeline: [{
      event_id: issue + '-event', event_type: 'execution_backend_invoked', generation: 1,
      created_at: new Date(now - 60000).toISOString(),
      details: { summary: 'Execution started for ' + issue + ' <script>alert(1)</script>' },
    }],
  };
}
let failOverview = false;
let failIssue = '';
let emptyOverview = false;
let overviewCalls = 0;
let delayedIssue = '';
let releaseDelayed;
let delayedStarted;
let onDelayed;
const methods = [];
const assets = { '/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/styles.css': ['styles.css', 'text/css'] };
const server = createServer(async (request, response) => {
  methods.push(request.method);
  const path = new URL(request.url, 'http://localhost').pathname;
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
  if (assets[path]) {
    const [name, type] = assets[path];
    response.setHeader('Content-Type', type);
    response.end(await readFile(join(root, 'factory/console', name)));
    return;
  }
  response.setHeader('Content-Type', 'application/json');
  if (path === '/api/overview') {
    overviewCalls++;
    response.statusCode = failOverview ? 503 : 200;
    const value = overview();
    if (emptyOverview) { value.active_attempts = []; value.recent_attempts = []; value.status_counts = {}; }
    response.end(JSON.stringify(failOverview ? { error: 'fixture failure' } : value));
  } else if (path.startsWith('/api/lifecycle/')) {
    const issue = decodeURIComponent(path.split('/').pop());
    if (issue === delayedIssue) {
      onDelayed();
      await new Promise((done) => { releaseDelayed = done; });
    }
    response.statusCode = issue === failIssue ? 503 : 200;
    response.end(JSON.stringify(issue === failIssue ? { error: 'fixture failure' } : lifecycle(issue)));
  } else { response.statusCode = 404; response.end('{}'); }
});
async function main() {
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.FACTORY_CONSOLE_CHROMIUM });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#metric-total').textContent === '7');
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('.skip-link').evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#main').evaluate((el) => el === document.activeElement), true);
    assert.equal(await page.locator('#signal-dead-letters').textContent(), 'Unavailable');
    assert.equal(await page.locator('#signal-terminal').textContent(), '9');
    assert.equal(await page.locator('#hatchet-link').isVisible(), false);
    assert.equal(await page.locator('#active-attempts tr').count(), 2);
    assert.equal(await page.locator('#recent-attempts img').count(), 0);
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-overview.png'), fullPage: true });

    await page.locator('[data-nav="work"]').click();
    await page.waitForURL('**/?view=work');
    assert.match(await page.locator('#work-count').textContent(), /^7 of 7/);
    assert.equal(await page.locator('#work-attempts tr').count(), 7);
    assert.equal(await page.locator('#work-attempts a[href^="javascript:"]').count(), 0);
    assert.equal(await page.locator('#work-attempts a[href*="attacker"]').count(), 0);
    assert.equal(await page.locator('#work-attempts img').count(), 0);
    await page.locator('#status-filter').selectOption('active');
    assert.equal(await page.locator('#work-attempts tr').count(), 2);
    await page.locator('#status-filter').selectOption('attention');
    assert.match(await page.locator('#work-attempts').textContent(), /ENG-104/);
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    await page.locator('#clear-filters').click();
    await page.locator('#work-query').fill('capykit');
    await page.locator('#mode-filter').selectOption('real');
    await page.locator('#status-filter').selectOption('verified');
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    assert.match(await page.locator('#work-attempts').textContent(), /ENG-102/);
    const filteredUrl = page.url();
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#work-count').textContent.startsWith('1 of 7'));
    assert.equal(page.url(), filteredUrl);
    assert.equal(await page.locator('#work-query').inputValue(), 'capykit');
    await page.locator('#work-attempts [data-issue="ENG-102"]').click();
    await page.waitForFunction(() => !document.querySelector('#issue-content').hidden);
    assert.match(await page.locator('#next-step').textContent(), /confirm its current head/);
    assert.match(await page.locator('#artifact-note').textContent(), /Current merge or review status is not available/);
    assert.equal(await page.locator('#github-link').getAttribute('href'), verified.job.pr_url);
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-work.png'), fullPage: true });
    await page.goBack();
    assert.equal(await page.locator('#issue-detail').isVisible(), false);
    await page.goForward();
    await page.waitForFunction(() => !document.querySelector('#issue-content').hidden);
    assert.equal(await page.locator('#issue-title').textContent(), 'ENG-102');

    failIssue = 'ENG-102';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#issue-freshness').textContent.startsWith('Stale issue'));
    assert.equal(await page.locator('#issue-content').isVisible(), true);
    failIssue = '';
    failOverview = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Stale snapshot');
    assert.match(await page.locator('#notice').textContent(), /last successful overview/);
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    failOverview = false;

    // A late response for the previous issue must not replace the selected issue.
    delayedIssue = 'ENG-101';
    delayedStarted = new Promise((done) => { onDelayed = done; });
    await page.locator('#issue-input').fill('ENG-101');
    await page.locator('#issue-form button').click();
    await delayedStarted;
    await page.locator('#issue-input').fill('ENG-103');
    await page.locator('#issue-form button').click();
    await page.waitForFunction(() => !document.querySelector('#issue-content').hidden && document.querySelector('#next-step').textContent.includes('dry-run attempt'));
    releaseDelayed();
    delayedIssue = '';
    await page.waitForFunction(() => document.querySelector('#issue-title').textContent === 'ENG-103');
    assert.match(await page.locator('#next-step').textContent(), /dry-run attempt/);
    assert.equal(await page.locator('#issue-timeline script').count(), 0);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-mobile.png'), fullPage: true });
    await page.locator('#close-issue').click();
    assert.equal(await page.locator('#issue-input').evaluate((el) => el === document.activeElement), true);
    await page.locator('#work-query').fill('nothing-matches');
    assert.match(await page.locator('#work-attempts').textContent(), /No attempts match/);

    // Hidden tabs do not refresh; becoming visible immediately requests a fresh snapshot.
    const before = overviewCalls;
    await page.evaluate(() => Object.defineProperty(document, 'hidden', { configurable: true, value: true }));
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    assert.equal(overviewCalls, before);
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, value: false }); document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Snapshot current');
    assert.ok(overviewCalls > before);

    emptyOverview = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#work-count').textContent.startsWith('0 of 0'));
    assert.match(await page.locator('#work-attempts').textContent(), /No active or recent/);
    await page.goto(base + '/?issue=not-an-issue');
    await page.waitForFunction(() => document.querySelector('#issue-notice').textContent.includes('Engineering issue'));
    assert.equal(await page.locator('#linear-link').isVisible(), false);
    assert.equal(await page.locator('#issue-content').isVisible(), false);
    failOverview = true;
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Unavailable');
    assert.match(await page.locator('#notice').textContent(), /data is unavailable/);
    assert.equal(await page.locator('#metric-active').textContent(), '—');
    assert.deepEqual(errors, []);
    assert.ok(methods.every((method) => method === 'GET'));
    console.log('PASS: navigation, deep links, filters, deduplication, safe links/text, unknown states, stale/empty/error states, issue races, visibility refresh, mobile layout and read-only requests.');
    console.log('Fixture screenshots: ' + join(tmpdir(), 'factory-dashboard-{overview,work,mobile}.png'));
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => {
  releaseDelayed?.();
  server.closeAllConnections();
  server.close();
});
