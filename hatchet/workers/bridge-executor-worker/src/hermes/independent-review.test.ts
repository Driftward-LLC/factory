import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import type { BridgeJob } from '@driftward/hatchet-worker-shared';
import { boundedIndependentReview, inconclusiveReview, IndependentReviewer, reviewIntent, type ReviewIntent } from './independent-review';

const requirements = { description: 'The counter must increase once per click.', title: 'Fix counter' };
const digest = (value: string) => createHash('sha256').update(value).digest('hex');
const intent: ReviewIntent = { review_id: 'review-1', repository: 'Driftward-LLC/demo',
  pr_url: 'https://github.com/Driftward-LLC/demo/pull/7', branch: 'codex/demo', head_sha: 'a'.repeat(40),
  expected_base: 'main', requirements, requirements_digest: digest(JSON.stringify(requirements)),
  generator_session_id: 'generator-1', pass_number: 1, policy_digest: 'b'.repeat(64) };
const job = (r = intent) => ({ requested_action: 'read_only_review', branch: r.branch,
  payload: { repo: r.repository, review: r, dry_run: false } }) as unknown as BridgeJob;

test('bounded review intent rejects path/command payloads, requirements tampering and wrong identity', () => {
  assert.deepEqual(reviewIntent(job()), intent);
  for (const r of [{ ...intent, repository: '../demo' }, { ...intent, review_id: '../../escape' },
    { ...intent, requirements_digest: '0'.repeat(64) }, { ...intent, pass_number: 3 },
    { ...intent, command: '/bin/sh' }, { ...intent, head_sha: 'main' }]) assert.throws(() => reviewIntent(job(r)), /Invalid/);
});

test('receipts cannot relabel model, head, isolation or pass malformed findings', () => {
  const receipt = inconclusiveReview(intent, 'review_backend_unavailable');
  assert.ok(boundedIndependentReview(intent, receipt));
  for (const change of [{ reviewer_model: 'gpt-6.1-sol' }, { head_sha: 'c'.repeat(40) },
    { reviewer_session_id: intent.generator_session_id }, { isolation: { write_credentials: true } },
    { verdict: 'passed' }, { verdict: 'changes_requested', reason: 'ok', findings: [] },
    { extra: true }]) assert.equal(boundedIndependentReview(intent, { ...receipt, ...change }), null);
});

test('default-off gate retains unavailable receipt and ambiguous reservations never call backend', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-review-journal-'));
  const before = process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
  delete process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
  try {
    let calls = 0;
    const adapter = new IndependentReviewer(directory, '/nonexistent', async () => { calls++; return ''; }, undefined, async () => true);
    const result = await adapter.run(job());
    assert.equal(result.reason, 'review_isolation_unavailable');
    assert.deepEqual(await adapter.run(job()), result);
    assert.equal(calls, 0);
    await assert.rejects(adapter.run(job({ ...intent, head_sha: 'd'.repeat(40) })), /different intent/);
    const reservation = path.join(directory, 'ambiguous');
    mkdirSync(reservation, { mode: 0o700 });
    assert.equal((await adapter.run(job({ ...intent, review_id: 'ambiguous' }))).reason, 'review_inconclusive');
    assert.equal(calls, 0);
  } finally {
    if (before === undefined) delete process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
    else process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED = before;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('unreserved review cannot invoke backend despite a valid-looking review intent', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-review-unreserved-'));
  const before = process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
  process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED = '1';
  try {
    let calls = 0;
    const adapter = new IndependentReviewer(directory, '/nonexistent', async () => { calls++; return ''; }, undefined, async () => false);
    assert.equal((await adapter.run(job())).reason, 'review_policy_changed');
    assert.equal((await adapter.run(job())).reason, 'review_policy_changed');
    assert.equal(calls, 0);
  } finally {
    if (before === undefined) delete process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
    else process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED = before;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('long review retains its receipt after launch inspection expires but still fences controls and changed heads', async () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-review-isolation-'));
  const before = process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
  process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED = '1';
  try {
    const provision = path.join(directory, 'provision');
    const repositories = path.join(directory, 'repositories');
    const mirror = path.join(repositories, `${intent.repository}.git`);
    mkdirSync(provision, { mode: 0o700 });
    mkdirSync(mirror, { recursive: true, mode: 0o700 });
    for (const name of ['auth.json', 'source-token', 'review-code.md', 'verify-app.md']) {
      writeFileSync(path.join(provision, name), name === 'source-token' ? 'synthetic-source-read-token' : 'synthetic', { mode: 0o600 });
    }
    const config = path.join(directory, 'config.json');
    writeFileSync(config, JSON.stringify({ schema: 'factory-isolated-reviewer-provisioning-v1',
      image: `reviewer@sha256:${'c'.repeat(64)}`, canary_digest: 'd'.repeat(64), policy_digest: intent.policy_digest,
      review_skill_digest: digest('synthetic'), verify_skill_digest: digest('synthetic'),
      repositories: { [intent.repository]: { base_ref: 'main' } } }), { mode: 0o600 });
    const commands: Array<{ file: string; args: string[] }> = [];
    const authorizationPhases: string[] = [];
    let inspectionAgeSeconds = 0;
    let completionAllowed = true;
    let headChanged = false;
    const run = async (file: string, args: string[], _timeout: number, env?: NodeJS.ProcessEnv) => {
      commands.push({ file, args });
      if (file.endsWith('/git')) {
        if (args.includes('fetch')) { assert.match(env?.GIT_CONFIG_VALUE_0 || '', /^Authorization: Basic /); return ''; }
        if (args.includes('bundle')) { writeFileSync(args[args.indexOf('create') + 1], 'synthetic bundle'); return ''; }
        return args.at(-1)?.startsWith('refs/heads/') ? 'e'.repeat(40)
          : headChanged && inspectionAgeSeconds > 0 ? 'f'.repeat(40) : intent.head_sha;
      }
      if (args[0] === 'network' && args[1] === 'inspect') return '[{"Internal":true,"EnableIPv6":false,"Options":{"com.docker.network.bridge.gateway_mode_ipv4":"isolated"}}]';
      if (args[0] === 'image') return '[{"Config":{"Labels":{"io.driftward.factory.independent-reviewer":"1"}}}]';
      if (args[0] === 'start') {
        inspectionAgeSeconds = 480;
        return JSON.stringify({ verdict: 'passed', findings: [], head_sha: intent.head_sha, reason: 'ok' });
      }
      if (args[0] === 'exec' && args.includes('rev-parse')) return intent.head_sha;
      return '';
    };
    const authorize = async (_job: BridgeJob, _intent: ReviewIntent, purpose = 'launch') => {
      authorizationPhases.push(purpose);
      return purpose === 'retrieve' || (purpose === 'complete' ? completionAllowed : inspectionAgeSeconds <= 120);
    };
    const adapter = new IndependentReviewer(path.join(directory, 'journal'), config, run, { provisioning: provision, repositories }, authorize);
    const receipt = await adapter.run(job());
    assert.equal(receipt.verdict, 'passed');
    assert.equal(receipt.reviewer_model, 'gpt-6-astra');
    assert.notEqual(receipt.reviewer_session_id, intent.generator_session_id);
    assert.equal(inspectionAgeSeconds, 480);
    assert.deepEqual(authorizationPhases, ['launch', 'launch', 'complete']);
    const source = commands.find((c) => c.args[0] === 'run')!.args.join(' ');
    const agent = commands.find((c) => c.args[0] === 'create')!.args.join(' ');
    assert.match(source, /source\.bundle/);
    assert.doesNotMatch(source, /auth\.json|source-token|factory-reviewer-egress|docker\.sock/);
    assert.match(agent, /reviewer-auth\.json/);
    assert.doesNotMatch(agent, /source\.bundle|repositories|docker\.sock/);
    const count = commands.length;
    assert.deepEqual(await new IndependentReviewer(path.join(directory, 'journal'), config, run, undefined, authorize).run(job()), receipt);
    assert.equal(commands.length, count);
    assert.equal(commands.filter((c) => c.args[0] === 'start').length, 1);
    assert.doesNotMatch(JSON.stringify(receipt), /synthetic-source-read-token/);
    assert.equal(JSON.parse(readFileSync(path.join(directory, 'journal', 'review-1', 'receipt.json'), 'utf8')).verdict, 'passed');
    const startCount = () => commands.filter((c) => c.args[0] === 'start').length;
    const alreadyStarted = startCount();
    assert.equal((await adapter.run(job({ ...intent, review_id: 'stale-launch' }))).reason, 'review_policy_changed');
    assert.equal(startCount(), alreadyStarted);
    for (const deniedControl of ['manual-denied', 'policy-denied']) {
      inspectionAgeSeconds = 0;
      completionAllowed = false;
      const denied = await adapter.run(job({ ...intent, review_id: deniedControl }));
      assert.equal(denied.verdict, 'inconclusive');
      assert.equal(denied.reason, 'review_policy_changed');
    }
    inspectionAgeSeconds = 0;
    completionAllowed = true;
    headChanged = true;
    const changed = await adapter.run(job({ ...intent, review_id: 'head-changed' }));
    assert.equal(changed.verdict, 'inconclusive');
    assert.equal(changed.reason, 'review_head_changed');
  } finally {
    if (before === undefined) delete process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED;
    else process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED = before;
    rmSync(directory, { recursive: true, force: true });
  }
});
