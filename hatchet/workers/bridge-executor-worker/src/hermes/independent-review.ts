import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { chmodSync, closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, rmdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { BridgeJob, JsonObject } from '@driftward/hatchet-worker-shared';
import { isSecretShaped } from './bounded-evidence';

export const REVIEW_REASONS = ['ok', 'review_backend_unavailable', 'review_isolation_unavailable', 'review_inconclusive',
  'review_malformed', 'review_head_changed', 'review_timeout', 'review_budget_exhausted', 'review_policy_changed'] as const;
type ReviewReason = typeof REVIEW_REASONS[number];
export type ReviewIntent = { review_id: string; repository: string; pr_url: string; branch: string; head_sha: string;
  expected_base: string; requirements: { title: string; description: string }; requirements_digest: string;
  generator_session_id: string; pass_number: number; policy_digest: string };
const INTENT_KEYS = 'branch,expected_base,generator_session_id,head_sha,pass_number,policy_digest,pr_url,repository,requirements,requirements_digest,review_id';

export function reviewIntent(job: BridgeJob): ReviewIntent {
  const r = job.payload.review as unknown as ReviewIntent;
  if (String(job.requested_action) !== 'read_only_review' || !r || typeof r !== 'object' || Array.isArray(r)
    || Object.keys(r).sort().join(',') !== INTENT_KEYS
    || typeof r.review_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(r.review_id)
    || typeof r.repository !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r.repository)
    || r.repository.split('/').some((s) => s === '.' || s === '..')
    || typeof r.pr_url !== 'string' || !r.pr_url.startsWith(`https://github.com/${r.repository}/pull/`)
    || !/^[1-9][0-9]*$/.test(r.pr_url.slice(`https://github.com/${r.repository}/pull/`.length))
    || typeof r.head_sha !== 'string' || !/^[a-f0-9]{40}$/.test(r.head_sha)
    || typeof r.expected_base !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/.test(r.expected_base)
    || !r.requirements || Object.keys(r.requirements).sort().join(',') !== 'description,title'
    || typeof r.requirements.title !== 'string' || r.requirements.title.length > 320
    || typeof r.requirements.description !== 'string' || r.requirements.description.length > 12000
    || isSecretShaped(r.requirements.title) || isSecretShaped(r.requirements.description)
    || createHash('sha256').update(JSON.stringify({ description: r.requirements.description, title: r.requirements.title })).digest('hex') !== r.requirements_digest
    || typeof r.policy_digest !== 'string' || !/^[a-f0-9]{64}$/.test(r.policy_digest)
    || typeof r.generator_session_id !== 'string' || !r.generator_session_id || r.generator_session_id.length > 200
    || typeof r.branch !== 'string' || !r.branch || r.branch.length > 200 || r.branch.startsWith('-')
    || !Number.isInteger(r.pass_number) || r.pass_number < 1 || r.pass_number > 2
    || job.payload.repo !== r.repository || job.payload.dry_run !== false) {
    throw new Error('Invalid bounded independent review intent');
  }
  return r;
}

export function inconclusiveReview(r: ReviewIntent, reason: ReviewReason, session = `unstarted:${r.review_id}`): JsonObject {
  return { schema: 'factory-independent-review-v1', review_id: r.review_id, repository: r.repository,
    pr_url: r.pr_url, head_sha: r.head_sha, verdict: 'inconclusive', reviewer_model: 'gpt-6-astra',
    reviewer_session_id: session, backend: 'isolated_hermes', isolation: { separate_session: true,
      isolated_checkout: true, isolated_browser: true, write_credentials: false }, findings: [], reason };
}

/** Validate receipts independently on both sides of the gateway. No model-authored isolation fields. */
export function boundedIndependentReview(r: ReviewIntent, value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value) || JSON.stringify(value).length > 20000) return null;
  const v = value as JsonObject;
  if (Object.keys(v).sort().join(',') !== 'backend,findings,head_sha,isolation,pr_url,reason,repository,review_id,reviewer_model,reviewer_session_id,schema,verdict'
    || v.schema !== 'factory-independent-review-v1' || v.review_id !== r.review_id || v.repository !== r.repository
    || v.pr_url !== r.pr_url || v.head_sha !== r.head_sha || v.reviewer_model !== 'gpt-6-astra'
    || !['isolated_codex', 'isolated_hermes'].includes(String(v.backend))
    || typeof v.reviewer_session_id !== 'string' || !v.reviewer_session_id || v.reviewer_session_id.length > 200
    || v.reviewer_session_id === r.generator_session_id || isSecretShaped(v.reviewer_session_id)
    || !['passed', 'changes_requested', 'inconclusive'].includes(String(v.verdict))
    || !REVIEW_REASONS.includes(v.reason as ReviewReason)
    || (v.verdict === 'inconclusive' ? v.reason === 'ok' : v.reason !== 'ok')) return null;
  const iso = v.isolation as JsonObject;
  if (!iso || Object.keys(iso).sort().join(',') !== 'isolated_browser,isolated_checkout,separate_session,write_credentials'
    || iso.separate_session !== true || iso.isolated_checkout !== true || iso.isolated_browser !== true || iso.write_credentials !== false) return null;
  if (!Array.isArray(v.findings) || v.findings.length > 20) return null;
  for (const item of v.findings) {
    const f = item as JsonObject;
    if (!f || typeof f !== 'object' || Array.isArray(f) || Object.keys(f).sort().join(',') !== 'body,file,line,priority,title'
      || !Number.isInteger(f.priority) || Number(f.priority) < 0 || Number(f.priority) > 3
      || !Number.isSafeInteger(f.line) || Number(f.line) < 1 || typeof f.file !== 'string' || !f.file || f.file.length > 300
      || f.file.startsWith('/') || f.file.includes('\\') || f.file.split('/').some((s) => s === '..' || !s)
      || typeof f.title !== 'string' || !f.title || f.title.length > 160
      || typeof f.body !== 'string' || !f.body || f.body.length > 1200
      || [f.file, f.title, f.body].some((s) => isSecretShaped(s))) return null;
  }
  if (v.verdict === 'passed' && v.findings.length || v.verdict === 'changes_requested' && !v.findings.length) return null;
  return v;
}

type Command = (file: string, args: string[], timeout: number, environment?: NodeJS.ProcessEnv) => Promise<string>;
export const reviewerCommand: Command = (file, args, timeout, environment = {}) => new Promise((resolve, reject) => {
  execFile(file, args, { timeout, maxBuffer: 256 * 1024, killSignal: 'SIGKILL',
    env: { PATH: '/usr/bin:/bin', HOME: '/nonexistent', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', ...environment } },
  (error, stdout) => error ? reject(error) : resolve(stdout));
});

type Provisioning = { schema: string; image: string; canary_digest: string; policy_digest: string;
  hermes_image: string;
  review_skill_digest: string; verify_skill_digest: string;
  repositories: Record<string, { base_ref: string }> };

function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}';
  return JSON.stringify(value);
}

type ReviewAuthorizationPurpose = 'launch' | 'complete' | 'retrieve';

export async function authorizeIndependentReview(job: BridgeJob, r: ReviewIntent, purpose: ReviewAuthorizationPurpose = 'launch'): Promise<boolean> {
  const base = process.env.HATCHET_HERMES_BRIDGE_URL;
  const token = process.env.HATCHET_HERMES_BRIDGE_TOKEN;
  const inspectionToken = process.env.HATCHET_HERMES_INSPECTION_TOKEN;
  if (!base || !token || !inspectionToken || !/^[a-f0-9-]{36}$/i.test(job.job_id)
    || typeof job.payload.review_capability !== 'string') return false;
  const location = new URL(base);
  if (!['http:', 'https:'].includes(location.protocol) || location.username || location.password) return false;
  try {
    const response = await fetch(`${base.replace(/\/+$/, '')}/jobs/${job.job_id}/independent-review/authorization`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { Authorization: `Bearer ${token}`, 'X-Hatchet-Inspection-Token': inspectionToken,
        'Content-Type': 'application/json' }, body: JSON.stringify({ review_capability: job.payload.review_capability }),
    });
    if (!response.ok) return false;
    const observed = await response.json() as { authorized?: boolean; invocation_allowed?: boolean; completion_allowed?: boolean; job_id?: string; review?: unknown };
    const allowed = purpose === 'retrieve' || (purpose === 'complete' ? observed.completion_allowed : observed.invocation_allowed) === true;
    return observed.authorized === true && allowed && observed.job_id === job.job_id
      && canonical(observed.review) === canonical(r);
  } catch { return false; }
}

function privatePath(location: string, directory: boolean): void {
  const info = lstatSync(location);
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile()) || (info.mode & 0o077)
    || info.uid !== (process.getuid?.() ?? 0)) throw new Error('Reviewer provisioning and journal must be private and owned by the gateway');
}

function persist(location: string, value: unknown): void {
  const temporary = `${location}.${randomUUID()}.tmp`;
  const fd = openSync(temporary, 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(temporary, location);
  const directory = openSync(path.dirname(location), 'r');
  try { fsyncSync(directory); } finally { closeSync(directory); }
}

/** mkdir reservation survives process crashes; an ambiguous outcome never starts a second model call. */
export class IndependentReviewer {
  private active = new Map<string, Promise<JsonObject>>();
  constructor(private readonly directory = '/var/lib/driftward-hermes-factory/reviews',
    private readonly configPath = '/etc/driftward/factory-reviewer.json', private readonly command = reviewerCommand,
    private readonly roots = { provisioning: '/etc/driftward/factory-reviewer', repositories: '/var/lib/driftward-factory-reviewer/repositories' },
    private readonly authorize = authorizeIndependentReview) {}

  async run(job: BridgeJob): Promise<JsonObject> {
    const r = reviewIntent(job);
    if (!path.isAbsolute(this.directory)) throw new Error('Reviewer journal requires an absolute path');
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    privatePath(this.directory, true);
    const location = path.join(this.directory, r.review_id);
    const fingerprint = createHash('sha256').update(canonical(r)).digest('hex');
    if (existsSync(location)) {
      privatePath(location, true);
      const intentPath = path.join(location, 'intent.json');
      if (!existsSync(intentPath)) return inconclusiveReview(r, 'review_inconclusive');
      privatePath(intentPath, false);
      if (JSON.parse(readFileSync(intentPath, 'utf8')).fingerprint !== fingerprint) throw new Error('Review identity already reserved for different intent');
      if (!await this.authorize(job, r, 'retrieve')) return inconclusiveReview(r, 'review_policy_changed');
      if (this.active.has(r.review_id)) return this.active.get(r.review_id)!;
      const receiptPath = path.join(location, 'receipt.json');
      if (!existsSync(receiptPath)) return inconclusiveReview(r, 'review_inconclusive');
      privatePath(receiptPath, false);
      return boundedIndependentReview(r, JSON.parse(readFileSync(receiptPath, 'utf8'))) ?? inconclusiveReview(r, 'review_malformed');
    }
    // Cross-process races must inspect the winner on retry, never continue launching.
    try { mkdirSync(location, { mode: 0o700 }); } catch { return this.run(job); }
    persist(path.join(location, 'intent.json'), { fingerprint });
    const pending = this.execute(job, r, location).catch(() => inconclusiveReview(r, 'review_backend_unavailable')).then((receipt) => {
      persist(path.join(location, 'receipt.json'), receipt);
      return receipt;
    });
    this.active.set(r.review_id, pending);
    try { return await pending; } finally { this.active.delete(r.review_id); }
  }

  private async execute(job: BridgeJob, r: ReviewIntent, location: string): Promise<JsonObject> {
    if (process.env.DRIFTWARD_FACTORY_REVIEWER_ENABLED !== '1') return inconclusiveReview(r, 'review_isolation_unavailable');
    if (!r.requirements.description) return inconclusiveReview(r, 'review_inconclusive');
    if (!await this.authorize(job, r)) return inconclusiveReview(r, 'review_policy_changed');
    let config: Provisioning;
    try {
      privatePath(this.configPath, false);
      config = JSON.parse(readFileSync(this.configPath, 'utf8'));
      if (config.schema !== 'factory-hermes-reviewer-provisioning-v2' || !/^.+@sha256:[a-f0-9]{64}$/.test(config.image)
        || !/^sha256:[a-f0-9]{64}$/.test(config.hermes_image)
        || !/^[a-f0-9]{64}$/.test(config.canary_digest) || !/^[a-f0-9]{64}$/.test(config.policy_digest)
        || !config.repositories?.[r.repository] || !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(config.repositories[r.repository].base_ref)) throw new Error();
      privatePath(path.join(this.roots.provisioning, 'source-token'), false);
      for (const [file, digest] of [['review-code', config.review_skill_digest], ['verify-app', config.verify_skill_digest]]) {
        const skill = path.join(this.roots.provisioning, `${file}.md`);
        privatePath(skill, false);
        if (!/^[a-f0-9]{64}$/.test(digest) || createHash('sha256').update(readFileSync(skill)).digest('hex') !== digest) throw new Error();
      }
    } catch { return inconclusiveReview(r, 'review_isolation_unavailable'); }
    if (config.policy_digest !== r.policy_digest) return inconclusiveReview(r, 'review_policy_changed');
    if (config.repositories[r.repository].base_ref !== r.expected_base) return inconclusiveReview(r, 'review_policy_changed');
    const session = `factory-reviewer:${randomUUID()}`;
    const fail = (reason: ReviewReason) => inconclusiveReview(r, reason, session);
    const mirror = path.join(this.roots.repositories, `${r.repository}.git`);
    const pull = r.pr_url.split('/').at(-1)!;
    const ref = `refs/pull/${pull}/head`;
    const baseRef = `refs/heads/${config.repositories[r.repository].base_ref}`;
    const deadline = Date.now() + 900000;
    const runCommand: Command = (file, args, timeout, env) => {
      const remaining = deadline - Date.now();
      if (remaining <= 0) return Promise.reject(Object.assign(new Error('Review budget expired'), { killed: true }));
      return this.command(file, args, Math.min(timeout, remaining), env);
    };
    const bundle = path.join(location, 'source.bundle');
    const snapshot = path.join(location, 'source.git');
    const container = `factory-reviewer-${randomUUID()}`;
    const source = `${container}-source`;
    const sourceNetwork = `${container}-net`;
    const token = createHash('sha256').update(randomUUID()).digest('hex');
    const requestPath = path.join(location, 'request.json');
    const tokenPath = path.join(location, 'mcp-token');
    let sourceLocked = false;
    try {
      privatePath(mirror, true);
      const git = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--git-dir', mirror];
      const sourceToken = readFileSync(path.join(this.roots.provisioning, 'source-token'), 'utf8').trim();
      if (!sourceToken || /[\r\n]/.test(sourceToken)) return fail('review_isolation_unavailable');
      const sourceEnv = { GIT_TERMINAL_PROMPT: '0', GIT_CONFIG_COUNT: '1', GIT_CONFIG_KEY_0: 'http.extraHeader',
        GIT_CONFIG_VALUE_0: `Authorization: Basic ${Buffer.from(`x-access-token:${sourceToken}`).toString('base64')}` };
      mkdirSync(`${mirror}.review-lock`, { mode: 0o700 });
      sourceLocked = true;
      const refresh = () => runCommand('/usr/bin/git', [...git, 'fetch', '--no-tags', '--no-recurse-submodules',
        `https://github.com/${r.repository}.git`, `+${ref}:${ref}`, `+${baseRef}:${baseRef}`], 60000, sourceEnv);
      await refresh();
      const sha = (await runCommand('/usr/bin/git', [...git, 'rev-parse', '--verify', `${ref}^{commit}`], 15000)).trim();
      if (sha !== r.head_sha) return fail('review_head_changed');
      const base = (await runCommand('/usr/bin/git', [...git, 'rev-parse', '--verify', `${baseRef}^{commit}`], 15000)).trim();
      if (!/^[a-f0-9]{40}$/.test(base)) return fail('review_malformed');
      await runCommand('/usr/bin/git', [...git, 'bundle', 'create', bundle, ref, baseRef], 60000);
      if (lstatSync(bundle).size > 256 * 1024 * 1024) return fail('review_budget_exhausted');
      chmodSync(bundle, 0o444);
      // Never expose a mirror's remotes/auth config. This exported Git database is immutable in source/tests.
      await runCommand('/usr/bin/git', ['clone', '--bare', '--no-hardlinks', bundle, snapshot], 60000);
      await runCommand('/usr/bin/git', ['--git-dir', snapshot, 'config', '--remove-section', 'remote.origin'], 15000);
      persist(requestPath, { head_sha: r.head_sha, expected_base: base, requirements: r.requirements });
      chmodSync(requestPath, 0o444);
      writeFileSync(tokenPath, token, { mode: 0o444, flag: 'wx' });
      const hermes = JSON.parse(await runCommand('/usr/bin/docker', ['inspect', 'hermes-driftward-hermes-1'], 15000));
      if (!Array.isArray(hermes) || hermes.length !== 1 || hermes[0].Image !== config.hermes_image
        || hermes[0].State?.Running !== true) return fail('review_isolation_unavailable');
      const image = JSON.parse(await runCommand('/usr/bin/docker', ['image', 'inspect', config.image], 15000))[0];
      if (image?.Config?.Volumes || image?.Config?.Labels?.['io.driftward.factory.independent-reviewer'] !== '1') return fail('review_isolation_unavailable');
      await runCommand('/usr/bin/docker', ['network', 'create', '--internal', '--opt',
        'com.docker.network.bridge.gateway_mode_ipv4=isolated', sourceNetwork], 15000);
      const common = ['--pull', 'never', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--pids-limit', '256', '--memory', '2g', '--cpus', '2', '--tmpfs', '/workspace:rw,nosuid,nodev,size=1g,mode=0777',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777'];
      await runCommand('/usr/bin/docker', ['run', '-d', '--name', source, ...common, '--network', sourceNetwork,
        '--network-alias', 'source', '--user', '10001:10001',
        '--mount', `type=bind,src=${bundle},dst=/run/source.bundle,readonly`,
        '--mount', `type=bind,src=${snapshot},dst=/run/source.git,readonly`,
        '--mount', `type=bind,src=${tokenPath},dst=/run/mcp-token,readonly`,
        '--env', 'HOME=/workspace/home', '--entrypoint', 'python3', config.image,
        '/opt/factory-reviewer/source_server.py', r.head_sha, base], 15000);
      if (!await this.authorize(job, r, 'launch')) return fail('review_policy_changed');
      const assets = '/usr/local/share/driftward-factory/independent-reviewer/deploy/independent-reviewer';
      const raw = await runCommand('/usr/bin/python3', [path.join(assets, 'runner.py'), requestPath,
        path.join(this.roots.provisioning, 'review-code.md'), path.join(this.roots.provisioning, 'verify-app.md'),
        source, 'hermes-driftward-hermes-1', session], 660000);
      const result = JSON.parse(raw);
      if (!result || Object.keys(result).sort().join(',') !== 'findings,head_sha,reason,verdict' || result.head_sha !== r.head_sha) return fail('review_malformed');
      const receipt = { ...fail('review_inconclusive'), verdict: result.verdict, findings: result.findings, reason: result.reason };
      const bounded = boundedIndependentReview(r, receipt);
      if (!bounded) return fail('review_malformed');
      if ((await runCommand('/usr/bin/docker', ['exec', source, '/usr/bin/git', '-C', '/workspace/repo', 'rev-parse', 'HEAD'], 15000)).trim() !== r.head_sha) return fail('review_head_changed');
      await runCommand('/usr/bin/docker', ['exec', source, '/usr/bin/git', '-C', '/workspace/repo', 'diff', '--quiet', r.head_sha], 15000);
      await runCommand('/usr/bin/docker', ['exec', source, 'python3', '/opt/factory-reviewer/source_server.py',
        '--validate-findings', r.head_sha, JSON.stringify(bounded.findings)], 15000);
      await refresh();
      if ((await runCommand('/usr/bin/git', [...git, 'rev-parse', '--verify', `${ref}^{commit}`], 15000)).trim() !== r.head_sha) return fail('review_head_changed');
      if (!await this.authorize(job, r, 'complete')) return fail('review_policy_changed');
      return bounded;
    } catch (error) {
      return fail(error && typeof error === 'object' && 'killed' in error && error.killed ? 'review_timeout' : 'review_backend_unavailable');
    } finally {
      // The one-shot host relay confirms exact native SDK termination; its own watchdog is independent.
      await this.command('/usr/bin/docker', ['rm', '-f', source], 15000).catch(() => undefined);
      await this.command('/usr/bin/docker', ['network', 'rm', sourceNetwork], 15000).catch(() => undefined);
      if (sourceLocked) { try { rmdirSync(`${mirror}.review-lock`); } catch { /* A retained lock fails closed. */ } }
      if (existsSync(bundle)) rmSync(bundle);
      if (existsSync(snapshot)) rmSync(snapshot, { recursive: true });
      if (existsSync(tokenPath)) rmSync(tokenPath);
      if (existsSync(requestPath)) rmSync(requestPath);
    }
  }
}
