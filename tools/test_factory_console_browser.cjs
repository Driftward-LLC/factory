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
const verifying = record('ENG-107', 1, 'verification_pending');
const traceBase = 'https://us.posthog.com/project/12345/ai-observability/traces/';
for (const [index, item] of [running, failed, verified].entries()) {
  item.agent_trace = { attempt_id: item.attempt.attempt_id, status: 'unverified', url: traceBase + `fb9fa7e1-4065-5818-af72-3e41a35fe39${index}?timestamp=${encodeURIComponent(item.attempt.created_at)}` };
}
simulated.agent_trace = { ...verified.agent_trace, attempt_id: simulated.attempt.attempt_id };
unknown.agent_trace = { ...verified.agent_trace }; // Wrong-attempt projection must not link.
verifying.agent_trace = { attempt_id: verifying.attempt.attempt_id, status: 'not_configured', url: null };
const unrecognized = record('ENG-105', 1, 'constructor');
unrecognized.job.pr_url = 'javascript:alert(1)';
const malicious = record('ENG-106', 1, 'failed');
malicious.job.payload.repo = '<img src=x onerror=alert(1)>';
malicious.job.pr_url = 'https://github.com/attacker/repo/pull/1';
malicious.agent_trace = { attempt_id: malicious.attempt.attempt_id, status: 'unverified', url: 'https://us.posthog.com.attacker.example/project/12345/ai-observability/traces/fb9fa7e1-4065-5818-af72-3e41a35fe39a' };
const records = [running, failed, verified, simulated, unknown, unrecognized, malicious, verifying];
let intakeState = 'blocked';
function intake() {
  return { available: intakeState !== 'unavailable', truncated: intakeState === 'partial', scan_limit: 500, limit: 100,
    items: ['empty', 'unavailable'].includes(intakeState) ? [] : [{ issue: 'ENG-154', linear_status: 'Blocked', blocked_by: ['ENG-151'], blocked_by_count: 1, dispatch_eligible: false, observed_at: new Date(now - 60000).toISOString(), provider_updated_at: new Date(now - 120000).toISOString() }] };
}
function overview() {
  return {
    generated_at: new Date().toISOString(), mode: 'real',
    intake: intake(),
    active_attempts: [running, unknown, verifying],
    recent_attempts: records,
    status_counts: { running: 1, failed: 2, verified_ready: 2, effect_outcome_unknown: 1, constructor: 1, verification_pending: 1 },
    reconciliation: { actionable: 2, actionable_severity: { critical: 0, warning: 2 }, scope: { terminal_work: 9 } },
    lifecycle_outbox: { available: false, dead_letters: 0 },
    links: { hatchet: 'https://attacker.example/' },
  };
}
let evidenceState = 'same_head';
function verificationEvidence(issue) {
  const item = records.find(record => record.attempt.work_item_key === issue);
  if (!item || evidenceState === 'missing') return null;
  return {
    schema: 'factory-console-verification-v1', attempt_id: evidenceState === 'wrong_attempt' ? 'another-attempt' : item.attempt.attempt_id,
    mode: item.attempt.mode, available: true,
    specification: { result: 'recorded', recorded_at: new Date(now - 3600000).toISOString() },
    implementation: { result: item.attempt.mode === 'dry-run' ? 'not_applicable' : 'recorded', recorded_at: new Date(now - 600000).toISOString() },
    verification: { result: item.attempt.mode === 'dry-run' ? 'not_applicable' : 'passed', recorded_at: new Date(now - 300000).toISOString(), commit_sha: 'a'.repeat(40), checks_passed: 3, local_commands_passed: 2, policy_digest: 'c'.repeat(64), receipt_digest: 'd'.repeat(64), local_evidence_source: 'executor_reported' },
    inspection: { status: evidenceState === 'stale' ? 'stale' : 'recorded', observed_at: new Date(now - 60000).toISOString(), head_sha: (evidenceState === 'head_changed' ? 'b' : 'a').repeat(40), pr_state: 'open', merged: false, draft: true },
    head_comparison: evidenceState === 'stale' ? 'same_head' : evidenceState, independent_review: 'unavailable',
  };
}
function lifecycle(issue) {
  return {
    work_item_key: issue,
    intake: issue === 'ENG-154' ? intake() : { ...intake(), items: [] },
    verification_evidence: verificationEvidence(issue),
    attempts: records.filter((r) => r.attempt.work_item_key === issue).sort((a, b) => b.attempt.generation - a.attempt.generation),
    timeline: [{
      event_id: issue + '-event', event_type: 'execution_backend_invoked', generation: 1,
      created_at: new Date(now - 60000).toISOString(),
      details: { summary: 'Execution started for ' + issue + ' <script>alert(1)</script>' },
    }],
  };
}
let analyticsState = 'missing';
let failAnalytics = false;
const analyticsCalls = [];
let delayedAnalyticsDays;
let releaseAnalytics;
let onAnalyticsDelayed;
function analytics(days) {
  const empty = analyticsState === 'empty';
  const partial = analyticsState === 'partial';
  const missingIngress = analyticsState === 'no-ingress';
  const items = empty ? 0 : ({ 7: 7, 30: 13, 90: 22 })[days];
  const prs = empty ? 0 : 18;
  const coverage = (observed) => missingIngress ? null : ({ observed, eligible: prs, rate: prs ? observed / prs : null, complete: observed === prs });
  const points = Array.from({ length: days + 1 }, (_, index) => ({
    date: new Date(Math.floor(now / 86400000) * 86400000 - (days - index) * 86400000).toISOString().slice(0, 10),
    partial: index === 0 || index === days,
    prs_recorded: empty ? 0 : index === days - 3 ? 3 : index === days - 1 ? 1 : 0,
    verified_attempts: empty ? 0 : index === days - 2 ? 2 : 0,
    repair_attempts: analyticsState === 'missing-series' ? null : empty ? 0 : index === days - 1 ? 1 : 0,
    failed_attempts: partial && index === days - 2 ? null : empty ? 0 : index === days - 1 ? 1 : 0,
  }));
  const seriesCoverage = Object.fromEntries(['prs_recorded', 'verified_attempts', 'repair_attempts', 'failed_attempts'].map((key) => {
    const observed = points.reduce((sum, point) => sum + (point[key] || 0), 0);
    const eligible = observed + (points.some((point) => point[key] === null) ? 1 : 0);
    return [key, { observed, eligible, rate: eligible ? observed / eligible : null, complete: observed === eligible }];
  }));
  return {
    schema: 'factory-console-analytics-v1', generated_at: new Date(now).toISOString(),
    window: { days, started_at: new Date(now - days * 86400000).toISOString(), ended_at: new Date(now).toISOString() },
    coverage: { cohort_attempts: empty ? 0 : 20, cohort_work_items: items, dry_run_attempts_excluded: 2, legacy_attempts_excluded: 1, bridge_available: true, ingress_available: !missingIngress },
    funnel: { stages: Object.fromEntries(Object.entries({ attempted: items, executor_started: items, effect_committed: empty ? 0 : 6, draft_pr_available: empty ? 0 : 6, verified_ready: empty ? 0 : 4 }).map(([key, value]) => [key, { count: value, rate_from_attempted: items ? value / items : null }])) },
    github: {
      available: !missingIngress, coverage: coverage(partial ? 6 : 0),
      merged_prs: missingIngress ? null : partial ? 2 : 0, closed_unmerged_prs: missingIngress ? null : partial ? 1 : 0, open_prs: missingIngress ? null : partial ? 3 : 0, unobserved_prs: missingIngress ? null : partial ? 12 : prs,
      check_suite_observations: { available: !missingIngress, coverage: coverage(partial ? 4 : 0), observations: partial ? 8 : 0, successful_observations: partial ? 6 : 0 },
      reviews: { available: !missingIngress, coverage: coverage(partial ? 5 : 0), observations: partial ? 7 : 0, changes_requested_prs: partial ? 2 : 0, changes_requested_rate: partial ? .4 : null },
      first_pass_checks: { available: false, success_rate: null },
    },
    recovery: { by_attempt_kind: { initial: { attempts: empty ? 0 : 13, verified: empty ? 0 : 1 }, repair: { attempts: empty ? 0 : 7, verified: empty ? 0 : 3 } }, repair_budgets_exhausted: empty ? 0 : 1 },
    failures: { currently_failed_attempts: empty ? 0 : 3, by_classification: empty ? [] : [{ failure_domain: 'verification', failure_stage: 'checks', reason_code: 'verification_github_check_failed', count: 3 }] },
    latency_ms: { effect_committed_to_verified_ready: { samples: empty ? 0 : 4, eligible: empty ? 0 : 6, p50: empty ? null : 60000, p90: empty ? null : 120000, p95: empty ? null : 180000, coverage_rate: empty ? null : 4 / 6 } },
    time_series: { timezone: 'UTC', basis: 'event_time', started_at: new Date(now - days * 86400000).toISOString(), ended_at: new Date(now).toISOString(), points, coverage: seriesCoverage },
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
async function contrastFailures(page, selector, property = 'color', minimum = 4.5) {
  return page.evaluate(({ selector, property, minimum }) => {
    const rgb = (value) => value.match(/[\d.]+/g).map(Number);
    const luminance = (color) => color.slice(0, 3).map((n) => { const v = n / 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
    return [...document.querySelectorAll(selector)].filter((el) => el.getClientRects().length).flatMap((el) => {
      let background = [255, 255, 255];
      for (let node = el; node; node = node.parentElement) {
        const color = rgb(getComputedStyle(node).backgroundColor);
        if (color.length === 3 || color[3] === 1) { background = color; break; }
      }
      const foreground = luminance(rgb(getComputedStyle(el)[property]));
      const back = luminance(background);
      const ratio = (Math.max(foreground, back) + .05) / (Math.min(foreground, back) + .05);
      return ratio < minimum ? [{ text: el.textContent || el.dataset.series, ratio }] : [];
    });
  }, { selector, property, minimum });
}
const server = createServer(async (request, response) => {
  methods.push(request.method);
  const url = new URL(request.url, 'http://localhost');
  const path = url.pathname;
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
  } else if (path === '/api/analytics') {
    const days = Number(url.searchParams.get('days'));
    analyticsCalls.push(days);
    const value = analytics(days);
    if (days === delayedAnalyticsDays) {
      onAnalyticsDelayed();
      await new Promise((done) => { releaseAnalytics = done; });
    }
    response.statusCode = failAnalytics ? 503 : 200;
    response.end(JSON.stringify(failAnalytics ? { error: 'fixture failure' } : value));
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
    await page.waitForFunction(() => document.querySelector('#metric-total').textContent === '8');
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('.skip-link').evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#main').evaluate((el) => el === document.activeElement), true);
    const intakePanel = page.locator('[data-view="overview"] .before-execution');
    assert.match(await intakePanel.textContent(), /ENG-154/);
    assert.match(await intakePanel.textContent(), /Linear: Blocked \(observed\)/);
    assert.equal(await page.locator('#nav-work-count').textContent(), '8', 'Pre-execution issues do not inflate attempt counts');
    await intakePanel.getByRole('button', { name: 'ENG-154', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('#issue-content').hidden);
    assert.match(await page.locator('#issue-content').textContent(), /No attempt recorded for this issue/);
    assert.equal(await page.locator('#issue-summary').count(), 0, 'No invented attempt stage');
    await page.locator('#close-issue').click();
    for (const [state, message] of [['unavailable', 'Linear observations unavailable'], ['empty', 'No pre-execution issues found'], ['partial', 'Partial list:']]) {
      intakeState = state;
      await page.locator('#refresh-button').click();
      await page.waitForFunction((message) => document.querySelector('[data-view="overview"] .before-execution').textContent.includes(message), message);
    }
    intakeState = 'blocked';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('[data-view="overview"] .intake-list') !== null);
    failOverview = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('[data-view="overview"] .before-execution').textContent.includes('Stale dashboard snapshot'));
    failOverview = false;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => !document.querySelector('#notice').textContent);
    for (const width of [320, 768, 1440]) {
      await page.setViewportSize({ width, height: 1080 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Intake layout fits viewport');
      if (width === 320) await page.screenshot({ path: join(tmpdir(), 'factory-before-execution-mobile.png'), fullPage: true });
    }
    assert.deepEqual(await contrastFailures(page, '.before-execution .muted, .before-execution .pill'), []);
    assert.equal(await page.locator('#signal-dead-letters').textContent(), 'Unavailable');
    assert.equal(await page.locator('#signal-terminal').textContent(), '9');
    assert.equal(await page.locator('#hatchet-link').isVisible(), false);
    assert.equal(await page.locator('#active-attempts tr').count(), 3);
    assert.match(await page.locator('#active-attempts tr').first().textContent(), /ENG-104/);
    assert.equal(await page.locator('.ledger-summary').getAttribute('open'), null);
    await page.locator('.ledger-summary summary').click();
    assert.equal(await page.locator('#metric-total').isVisible(), true);
    await page.locator('.ledger-summary summary').click();
    for (const [queue, total, issue] of [['attention', '1', 'ENG-104'], ['verifying', '1', 'ENG-107'], ['failed', '2', 'ENG-101']]) {
      assert.equal(await page.locator('#queue-' + queue).textContent(), total);
      await page.locator('[data-queue="' + queue + '"]').click();
      assert.equal(await page.locator('#status-filter').inputValue(), queue);
      assert.match(await page.locator('#work-attempts').textContent(), new RegExp(issue));
      if (queue === 'verifying') {
        await page.reload();
        await page.waitForFunction(() => document.querySelector('#work-count').textContent.startsWith('1 of 8'));
        assert.equal(await page.locator('#status-filter').inputValue(), 'verifying');
      }
      await page.locator('[data-nav="overview"]').click();
    }
    await page.locator('[data-sidebar-queue="verifying"]').click();
    assert.equal(await page.locator('#status-filter').inputValue(), 'verifying');
    assert.match(await page.locator('#work-attempts').textContent(), /ENG-107/);
    await page.locator('[data-nav="overview"]').click();
    assert.equal(await page.locator('#recent-attempts img').count(), 0);
    await page.setViewportSize({ width: 1280, height: 960 });
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-overview.png'), fullPage: true });
    assert.deepEqual(await contrastFailures(page, '.muted, .pill, .review-queues small, nav a, .signal p'), []);
    for (const width of [320, 700, 850, 1024, 1280]) {
      await page.setViewportSize({ width, height: 960 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Overview overflow at ' + width);
    }
    await page.setViewportSize({ width: 1440, height: 1080 });

    await page.locator('[data-nav="work"]').click();
    await page.locator('#clear-filters').click();
    await page.waitForURL('**/?view=work');
    assert.match(await page.locator('#work-count').textContent(), /^8 of 8/);
    assert.equal(await page.locator('#work-attempts tr').count(), 8);
    assert.equal(await page.locator('#work-attempts a[href^="javascript:"]').count(), 0);
    assert.equal(await page.locator('#work-attempts a[href*="attacker"]').count(), 0);
    assert.equal(await page.locator('#work-attempts img').count(), 0);
    const traces = page.locator('#work-attempts .agent-trace a');
    assert.equal(await traces.count(), 3);
    assert.equal(await traces.first().getAttribute('target'), '_blank');
    assert.equal(await traces.first().getAttribute('rel'), 'noopener noreferrer');
    assert.equal(new Set(await traces.evaluateAll(links => links.map(link => link.href))).size, 3);
    assert.match(await traces.first().textContent(), /Find trace in PostHog/);
    assert.match(await page.locator('#work-attempts').textContent(), /Capture unconfirmed/);
    assert.equal(await page.getByRole('link', { name: /Open agent trace/ }).count(), 0);
    assert.equal(new URL(await traces.first().getAttribute('href')).searchParams.get('timestamp'), running.attempt.created_at);
    assert.match(await page.locator('#work-attempts').textContent(), /Trace link not configured/);
    assert.match(await page.locator('#work-attempts').textContent(), /not applicable \(dry run\)/);
    await page.context().route('https://us.posthog.com/**', route => route.fulfill({ body: 'Fictional trace destination' }));
    await traces.first().focus();
    const popupPromise = page.waitForEvent('popup');
    await page.keyboard.press('Enter');
    const popup = await popupPromise;
    await popup.waitForLoadState();
    assert(popup.url().startsWith(traceBase));
    await popup.close();
    await page.locator('[data-work-scope="active"]').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#status-filter').inputValue(), 'active');
    assert.equal(await page.locator('[data-work-scope="active"]').getAttribute('aria-current'), 'page');
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-work-list.png'), fullPage: true });
    assert.equal(await page.locator('#work-attempts tr').count(), 3);
    await page.locator('#status-filter').selectOption('attention');
    assert.match(await page.locator('#work-attempts').textContent(), /ENG-104/);
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    await page.locator('#clear-filters').click();
    await page.locator('#work-query').fill('capykit');
    await page.locator('#mode-filter').selectOption('real');
    await page.locator('[data-work-scope="failed"]').click();
    assert.equal(await page.locator('#work-query').inputValue(), 'capykit');
    assert.equal(await page.locator('#mode-filter').inputValue(), 'real');
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    await page.locator('#status-filter').selectOption('verified');
    assert.equal(await page.locator('#work-attempts tr').count(), 1);
    assert.match(await page.locator('#work-attempts').textContent(), /ENG-102/);
    const filteredUrl = page.url();
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#work-count').textContent.startsWith('1 of 8'));
    assert.equal(page.url(), filteredUrl);
    assert.equal(await page.locator('#work-query').inputValue(), 'capykit');
    await page.locator('#work-attempts [data-issue="ENG-102"]').click();
    await page.waitForFunction(() => !document.querySelector('#issue-content').hidden);
    assert.equal(await page.locator('[data-view="work"]').isVisible(), false);
    assert.match(await page.locator('#next-step').textContent(), /confirm its current head/);
    assert.match(await page.locator('#artifact-note').textContent(), /not live GitHub state or review approval/);
    assert.equal(await page.locator('#issue-attempts .agent-trace a').getAttribute('href'), verified.agent_trace.url);
    const evidencePanel = page.locator('.verification-evidence');
    assert.match(await evidencePanel.textContent(), /matches the verified SHA/);
    assert.match(await evidencePanel.textContent(), /Independent reviewUnavailable in this view/);
    await evidencePanel.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await evidencePanel.locator('details').getAttribute('open'), '');
    assert.match(await evidencePanel.textContent(), /executor-reported, not independent test execution/);
    await page.keyboard.press('Enter');
    for (const [state, message] of [
      ['head_changed', 'Earlier verification does not cover that revision'],
      ['inspection_predates_verification', 'The PR inspection predates verification'],
      ['stale', 'outside its freshness window'],
      ['missing', 'Verification evidence is unavailable'],
      ['wrong_attempt', 'Verification evidence is unavailable'],
      ['same_head', 'matches the verified SHA'],
    ]) {
      evidenceState = state;
      await Promise.all([
        page.waitForResponse(response => response.url().includes('/api/lifecycle/ENG-102')),
        page.locator('#refresh-button').click(),
      ]);
      await page.waitForFunction(text => document.querySelector('.verification-evidence').textContent.includes(text), message);
      await page.waitForFunction(() => document.querySelector('#issue-freshness').textContent.startsWith('Evidence retrieved'));
    }
    evidenceState = 'head_changed';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('[data-comparison="head_changed"]'));
    for (const width of [320, 700, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1080 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Evidence overflow at ' + width);
      if (width === 320) {
        const trace = page.locator('#issue-attempts-mobile .agent-trace a');
        assert.equal(await trace.isVisible(), true);
        assert.equal(await trace.getAttribute('href'), verified.agent_trace.url);
        assert((await trace.boundingBox()).height >= 44, 'Mobile trace link touch target');
      }
      if (width === 320) await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-evidence-mobile.png'), fullPage: true });
    }
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Evidence overflow at 200% text');
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    assert.deepEqual(await contrastFailures(page, '.verification-evidence .muted, .verification-evidence .pill, .verification-evidence small'), []);
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-evidence.png'), fullPage: true });
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
    // Retrying must keep the old issue evidence visibly stale until success.
    delayedIssue = 'ENG-102';
    delayedStarted = new Promise((done) => { onDelayed = done; });
    await page.locator('#refresh-button').click();
    await delayedStarted;
    assert.match(await page.locator('#issue-freshness').textContent(), /^Stale issue/);
    assert.equal(await page.locator('#issue-content').isVisible(), true);
    releaseDelayed();
    delayedIssue = '';
    await page.waitForFunction(() => document.querySelector('#issue-freshness').textContent.startsWith('Evidence retrieved'));
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
    assert.match(await page.locator('.verification-evidence').textContent(), /Dry-run evidence does not establish a real artifact/);
    assert.equal(await page.locator('#issue-timeline script').count(), 0);

    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Snapshot current');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('#issue-attempts-mobile').isVisible(), true);
    await page.locator('#issue-attempts-mobile summary').first().click();
    assert.equal(await page.locator('#issue-attempts-mobile details a').first().isVisible(), true);
    await page.locator('#issue-attempts-mobile summary').first().click();
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-mobile.png'), fullPage: true });
    await page.locator('#close-issue').click();
    assert.equal(await page.locator('#issue-input').evaluate((el) => el === document.activeElement), true);
    assert.equal(await page.locator('#work-attempts-mobile').isVisible(), true);
    assert.match(await page.locator('#work-attempts-mobile').textContent(), /Confirm current PR checks/);
    assert.equal(await page.locator('#status-filter').inputValue(), 'verified');
    assert.equal(await page.locator('#status-filter').isVisible(), false);
    assert.match(await page.locator('.filter-toggle').textContent(), /2 active/);
    await page.locator('.filter-toggle').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#status-filter').isVisible(), true);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#status-filter').isVisible(), false);
    await page.locator('#work-attempts-mobile .inspect-link').click();
    await page.waitForFunction(() => document.querySelector('#issue-title').textContent === 'ENG-102' && !document.querySelector('#issue-content').hidden);
    await page.locator('#close-issue').click();
    assert.ok(await page.locator('#work-attempts-mobile .inspect-link').evaluate((el) => el.getBoundingClientRect().height >= 44));
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-mobile-work.png'), fullPage: true });
    for (const width of [320, 560, 700, 850, 1024, 1280]) {
      await page.setViewportSize({ width, height: 960 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Work overflow at ' + width);
    }
    await page.setViewportSize({ width: 390, height: 844 });
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

    // Analytics loads only when selected, and missing GitHub evidence never becomes 0% success.
    assert.deepEqual(analyticsCalls, []);
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.locator('[data-nav="analytics"]').focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '13');
    assert.equal(new URL(page.url()).searchParams.get('view'), 'analytics');
    assert.equal(new URL(page.url()).searchParams.get('days'), null);
    assert.equal(await page.locator('#analytics-days').inputValue(), '30');
    assert.equal(await page.locator('[data-nav="analytics"]').getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('[data-view="work"]').isVisible(), false);
    assert.equal(await page.locator('#analytics-verified').textContent(), '4');
    assert.equal(await page.locator('#analytics-coverage-warning').isVisible(), true);
    assert.match(await page.locator('#analytics-content').textContent(), /0 of 18 PRs observed/);
    for (const id of ['analytics-first-pass', 'analytics-review-rate', 'analytics-merged']) {
      assert.match(await page.locator('#' + id).textContent(), /Unavailable/);
    }
    const trend = page.locator('#analytics-trend-chart');
    const trendDetail = page.locator('#analytics-trend-detail');
    assert.equal(await trend.getAttribute('role'), 'img');
    assert.equal(await trend.locator('title').count(), 1);
    assert.equal(await trend.locator('desc').count(), 1);
    assert.equal(await trend.locator('path[data-series]').count(), 4);
    assert.equal(await page.locator('#analytics-trend-table tbody tr').count(), 31);
    await trend.focus();
    assert.equal(await trend.evaluate((el) => document.activeElement === el), true);
    await page.keyboard.press('Home');
    assert.equal(await trendDetail.getAttribute('data-date'), analytics(30).time_series.points[0].date);
    assert.match(await trendDetail.textContent(), /partial/i);
    await page.keyboard.press('ArrowRight');
    assert.equal(await trendDetail.getAttribute('data-date'), analytics(30).time_series.points[1].date);
    await page.keyboard.press('End');
    assert.equal(await trendDetail.getAttribute('data-date'), analytics(30).time_series.points[30].date);
    await page.keyboard.press('ArrowLeft');
    assert.equal(await trendDetail.getAttribute('data-date'), analytics(30).time_series.points[29].date);

    await page.locator('#analytics-days').selectOption('7');
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '7');
    assert.equal(new URL(page.url()).searchParams.get('days'), '7');
    assert.equal(await page.locator('#analytics-trend-table tbody tr').count(), 8);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '7');
    assert.equal(await page.locator('#analytics-days').inputValue(), '7');
    await page.locator('#analytics-days').selectOption('90');
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '22');
    assert.equal(await page.locator('#analytics-trend-table tbody tr').count(), 91);
    await page.goBack();
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '7');
    assert.equal(await page.locator('#analytics-days').inputValue(), '7');
    await page.goForward();
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '22');

    failAnalytics = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Stale snapshot');
    assert.equal(await page.locator('#analytics-work-items').textContent(), '22');
    assert.match(await page.locator('#notice').textContent(), /last successful analytics/i);
    assert.match(await page.locator('#updated-at').textContent(), /Last successful/);
    failAnalytics = false;
    delayedAnalyticsDays = 90;
    let analyticsStarted = new Promise((done) => { onAnalyticsDelayed = done; });
    await page.locator('#refresh-button').click();
    await analyticsStarted;
    assert.equal(await page.locator('#connection-state').textContent(), 'Stale snapshot');
    assert.match(await page.locator('#notice').textContent(), /last successful analytics/i);
    assert.equal(await page.locator('#analytics-work-items').textContent(), '22');
    releaseAnalytics();
    delayedAnalyticsDays = undefined;
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Snapshot current');

    // Selecting a new period hides the old cohort; late results cannot replace the current period.
    delayedAnalyticsDays = 30;
    analyticsStarted = new Promise((done) => { onAnalyticsDelayed = done; });
    const oldAnalyticsRequestEnded = new Promise((done) => {
      const onEnd = (request) => {
        if (!request.url().endsWith('/api/analytics?days=30')) return;
        page.off('requestfinished', onEnd);
        page.off('requestfailed', onEnd);
        done();
      };
      page.on('requestfinished', onEnd);
      page.on('requestfailed', onEnd);
    });
    await page.locator('#analytics-days').selectOption('30');
    await analyticsStarted;
    assert.equal(await page.locator('#analytics-content').isVisible(), false);
    assert.equal(await page.locator('#analytics-work-items').isVisible(), false);
    await page.locator('#analytics-days').selectOption('7');
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '7');
    releaseAnalytics();
    delayedAnalyticsDays = undefined;
    await oldAnalyticsRequestEnded;
    await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    assert.equal(await page.locator('#analytics-work-items').textContent(), '7');
    assert.equal(await page.locator('#analytics-days').inputValue(), '7');

    analyticsState = 'partial';
    failOverview = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#analytics-review-rate')?.textContent.includes('40%'));
    assert.equal(await page.locator('#connection-state').textContent(), 'Snapshot current');
    assert.equal(await page.locator('#notice').isVisible(), false);
    failOverview = false;
    assert.equal(await page.locator('#analytics-coverage-warning').isVisible(), true);
    assert.match(await page.locator('#analytics-content').textContent(), /6 of 18 PRs observed/);
    assert.match(await page.locator('#analytics-content').textContent(), /2\s*(?:of|\/)\s*5/);
    assert.equal(await page.locator('#analytics-merged').textContent(), '2');
    assert.match(await page.locator('#analytics-first-pass').textContent(), /Unavailable/);
    const trendPoints = analytics(7).time_series.points;
    assert.equal(await trend.locator('path[data-series]').count(), 4);
    const prCoordinates = [...(await trend.locator('path[data-series="prs_recorded"]').getAttribute('d')).matchAll(/[ML]\s*([\d.-]+)[,\s]+([\d.-]+)/g)].map((match) => [Number(match[1]), Number(match[2])]);
    assert.equal(prCoordinates.length, 8);
    assert.ok(prCoordinates[4][1] < prCoordinates[6][1] && prCoordinates[6][1] < prCoordinates[0][1], 'PR line must plot 3 above 1 above zero');
    assert.equal((await trend.locator('path[data-series="failed_attempts"]').getAttribute('d')).match(/M/g).length, 2, 'Unknown day must break the failure line');
    assert.doesNotMatch(await trend.innerHTML(), /NaN|Infinity/);
    assert.equal(await page.locator('#analytics-trend-unavailable').isVisible(), true);
    await page.locator('#analytics-trend-day').selectOption(trendPoints[4].date);
    assert.equal(await trendDetail.getAttribute('data-date'), trendPoints[4].date);
    assert.match(await trendDetail.textContent(), /PRs recorded\s*3/);
    await page.locator('#analytics-trend-day').selectOption(trendPoints[5].date);
    assert.match(await trendDetail.textContent(), /Failed attempts\s*Unavailable/);
    const beforePointer = await trendDetail.getAttribute('data-date');
    const chartBox = await trend.boundingBox();
    await trend.hover({ position: { x: chartBox.width / 3, y: chartBox.height / 2 } });
    assert.notEqual(await trendDetail.getAttribute('data-date'), beforePointer);
    const pointerDate = await trendDetail.getAttribute('data-date');
    assert.ok(trendPoints.some((point) => point.date === pointerDate));
    const dailyDataToggle = page.locator('details:has(#analytics-trend-table) summary');
    await dailyDataToggle.focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#analytics-trend-table').isVisible(), true);
    assert.equal(await page.locator('#analytics-trend-table tbody tr').count(), 8);
    assert.match(await page.locator('#analytics-trend-table').textContent(), /Unavailable/);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#analytics-trend-table').isVisible(), false);
    assert.deepEqual(await contrastFailures(page, '#analytics-trend .muted, #analytics-trend label, #analytics-trend-detail'), []);
    assert.deepEqual(await contrastFailures(page, '#analytics-trend-chart text', 'fill'), []);
    assert.deepEqual(await contrastFailures(page, '#analytics-trend-chart path[data-series]', 'stroke', 3), []);
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-analytics.png'), fullPage: true });
    for (const width of [320, 560, 700, 850, 1024, 1280, 1440]) {
      await page.setViewportSize({ width, height: 960 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Analytics overflow at ' + width);
      assert.doesNotMatch(await trend.innerHTML(), /NaN|Infinity/);
    }
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Analytics overflow at 200% text size');
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.locator('#analytics-days').evaluate((el) => el.getBoundingClientRect().height >= 44));
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-analytics-mobile.png'), fullPage: true });

    analyticsState = 'missing-series';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => !document.querySelector('#analytics-trend-chart path[data-series="repair_attempts"]'));
    assert.equal(await trend.locator('path[data-series]').count(), 3);
    assert.equal(await page.locator('#analytics-trend-unavailable').isVisible(), true);
    assert.match(await trendDetail.textContent(), /Repair attempts\s*Unavailable/);
    assert.doesNotMatch(await trend.innerHTML(), /NaN|Infinity/);

    failAnalytics = true;
    await page.goto(base + '/?view=analytics&days=30');
    await page.waitForFunction(() => document.querySelector('#connection-state').textContent === 'Unavailable');
    assert.equal(await page.locator('#analytics-content').isVisible(), false);
    assert.match(await page.locator('#notice').textContent(), /analytics.*unavailable/i);
    failAnalytics = false;
    analyticsState = 'no-ingress';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#analytics-work-items')?.textContent === '13');
    assert.equal(await page.locator('#analytics-coverage-warning').isVisible(), true);
    assert.match(await page.locator('#analytics-content').textContent(), /Coverage unavailable/);
    assert.equal(await page.locator('#analytics-review-rate').textContent(), 'Unavailable');
    assert.equal(await page.locator('#analytics-merged').textContent(), 'Unavailable');
    analyticsState = 'empty';
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.querySelector('#analytics-empty')?.getClientRects().length > 0);
    assert.equal(await page.locator('#analytics-work-items').textContent(), '0');
    assert.match(await page.locator('#analytics-first-pass').textContent(), /Unavailable/);
    assert.equal(await page.locator('#analytics-review-rate').textContent(), 'Unavailable');
    assert.equal(await page.locator('#analytics-merged').textContent(), '0');
    assert.equal(await page.locator('#analytics-closed').textContent(), '0');
    assert.equal(await page.locator('#analytics-trend-empty').isVisible(), true);
    assert.equal(await page.locator('#analytics-trend-unavailable').isVisible(), false);
    assert.ok(analyticsCalls.includes(7) && analyticsCalls.includes(30) && analyticsCalls.includes(90));

    await page.locator('[data-nav="overview"]').click();
    await page.evaluate(() => { document.activeElement.blur(); window.scrollTo(0, 0); });
    await page.screenshot({ path: join(tmpdir(), 'factory-dashboard-mobile-overview.png'), fullPage: true });
    await page.locator('[data-nav="work"]').click();
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
    assert.equal(await page.locator('#queue-attention').textContent(), 'Unavailable');
    assert.deepEqual(errors, []);
    assert.ok(methods.every((method) => method === 'GET'));
    console.log('PASS: review queues, navigation, sidebar queues, keyboard scope links, deep links, filters, deduplication, safe links/text, unknown states, analytics evidence coverage, time-series rendering and gaps, pointer/keyboard day inspection, daily data table, period history and races, stale/empty/error states, issue races, visibility refresh, responsive details, keyboard controls, text/chart contrast and read-only requests.');
    console.log('Fixture screenshots: ' + join(tmpdir(), 'factory-dashboard-{overview,work,mobile,mobile-work,analytics,analytics-mobile}.png'));
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => {
  releaseDelayed?.();
  releaseAnalytics?.();
  server.closeAllConnections();
  server.close();
});
