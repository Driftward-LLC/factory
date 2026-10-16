import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { assertHermesIdentityViaCommand, findAddedTempFileReference, inspectDraftPullRequestViaCommand } from './create-draft-pr';
import { NonRetryableBridgeExecutionError } from './errors';

test('pinned PR diff inspection uses complete bounded evidence and cleans up runtime source', async (t) => {
  const directory = mkdtempSync(path.join(tmpdir(), 'factory-pinned-diff-test-'));
  const source = path.join(directory, 'source');
  const temporary = path.join(directory, 'temporary');
  mkdirSync(source);
  mkdirSync(temporary);
  const git = (...args: string[]) => execFileSync('/usr/bin/git', args, { cwd: source,
    env: { PATH: process.env.PATH, HOME: '/nonexistent', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' },
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const commit = () => {
    git('add', '-A');
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture');
    return git('rev-parse', 'HEAD');
  };
  const names = ['PATH', 'DRIFTWARD_HERMES_RUNTIME_USER', 'TEST_DIFF_DIRECTORY', 'TEST_DIFF_MODE', 'TEST_DIFF_SCAN_PROBE', 'TMPDIR'] as const;
  const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  try {
    delete process.env.TEST_DIFF_SCAN_PROBE;
    git('init', '-q', '-b', 'main');
    writeFileSync(path.join(source, 'old name.txt'), 'a stable rename\n'.repeat(20));
    writeFileSync(path.join(source, 'README.md'), 'initial\n');
    const ancestor = commit();
    git('switch', '-qc', 'head');
    git('mv', 'old name.txt', 'new name.txt');
    writeFileSync(path.join(source, 'README.md'), 'initial\n@/tmp/not-file-content\n');
    for (let index = 0; index < 302; index += 1) {
      writeFileSync(path.join(source, `binary-${index}.dat`), Buffer.from([0, 1, 2, index % 256]));
    }
    const head = commit();
    git('switch', '-q', 'main');
    writeFileSync(path.join(source, 'base-only.txt'), 'this advanced only on base\n');
    const base = commit();
    assert.notEqual(base, ancestor);
    const files = git('diff', '--name-only', '-z', '--find-renames', `${base}...${head}`).split('\0').filter(Boolean).map((p) => ({ path: p }));
    assert.equal(files.length, 304);
    const view = { headRefName: 'head', headRefOid: head, baseRefName: 'main', baseRefOid: base,
      isDraft: true, changedFiles: files.length, url: 'https://github.com/Driftward-LLC/capykit/pull/82',
      author: { login: 'factory-hermes[bot]' }, body: 'Refs Linear ENG-161', statusCheckRollup: [] };
    writeFileSync(path.join(directory, 'view.json'), JSON.stringify(view));
    writeFileSync(path.join(directory, 'files.json'), JSON.stringify(files));
    const diskScanProbe = String.raw`
import os
from types import SimpleNamespace
original_walk, original_lstat = os.walk, os.lstat
def walk_with_removed_file(directory):
    for root, directories, files in original_walk(directory):
        filename = '.factory-disk-scan-probe.lock'
        candidate = os.path.join(root, filename)
        with open(candidate, 'w') as output:
            output.write('temporary lock')
        files.append(filename)
        os.unlink(candidate)
        yield root, directories, files
def probe_lstat(filename, *args, **kwargs):
    if filename.endswith('.factory-disk-scan-probe.lock'):
        with open(os.path.join(os.environ['TEST_DIFF_DIRECTORY'], 'disk-scan-probed'), 'w') as output:
            output.write('yes')
        mode = os.environ['TEST_DIFF_SCAN_PROBE']
        if mode == 'permission-error':
            raise PermissionError('PRIVATE_DISK_DIAGNOSTIC')
        if mode == 'disk-limit':
            return SimpleNamespace(st_size=256 * 1024 * 1024 + 1)
    return original_lstat(filename, *args, **kwargs)
os.walk, os.lstat = walk_with_removed_file, probe_lstat
`;
    writeFileSync(path.join(directory, 'docker'), `#!${process.execPath}
const fs = require('node:fs'), cp = require('node:child_process'), path = require('node:path');
const args = process.argv.slice(2), root = process.env.TEST_DIFF_DIRECTORY, mode = process.env.TEST_DIFF_MODE;
if (args.includes('python3')) {
  fs.writeFileSync(path.join(root, 'fallback-called'), 'yes');
  const readerArgs = args.slice(args.indexOf('python3') + 1);
  if (process.env.TEST_DIFF_SCAN_PROBE) readerArgs[1] = ${JSON.stringify(diskScanProbe)} + readerArgs[1];
  const child = cp.spawnSync('python3', readerArgs, {stdio: 'inherit'});
  process.exit(child.status ?? 1);
}
if (mode === 'identity-error') { process.stderr.write('PRIVATE_AUTH_DIAGNOSTIC'); process.exit(1); }
if (args.includes('view')) {
  const supportedFields = new Set('headRefName,headRefOid,baseRefName,isDraft,url,author,body,changedFiles,statusCheckRollup'.split(','));
  if (args[args.indexOf('--json') + 1].split(',').some(field => !supportedFields.has(field))) {
    process.stderr.write('Unknown JSON field'); process.exit(1);
  }
  const countPath = path.join(root, 'views');
  const count = fs.existsSync(countPath) ? Number(fs.readFileSync(countPath)) : 0;
  fs.writeFileSync(countPath, String(count + 1));
  const view = JSON.parse(fs.readFileSync(path.join(root, 'view.json')));
  if (count && mode === 'head-drift') view.headRefOid = 'a'.repeat(40);
  delete view.baseRefOid;
  process.stdout.write(JSON.stringify(view));
} else if (args.includes('repos/Driftward-LLC/capykit/pulls/82')) {
  const view = JSON.parse(fs.readFileSync(path.join(root, 'view.json')));
  const count = Number(fs.readFileSync(path.join(root, 'views')));
  if (count > 1 && mode === 'head-drift') view.headRefOid = 'a'.repeat(40);
  if (count > 1 && mode === 'base-drift') view.baseRefOid = 'b'.repeat(40);
  if (mode === 'missing-sha') delete view.baseRefOid;
  if (mode === 'snapshot-head-mismatch') view.headRefOid = 'c'.repeat(40);
  if (mode === 'snapshot-base-branch-mismatch') view.baseRefName = 'other-base';
  process.stdout.write(JSON.stringify(view));
} else if (args.some(a => a.includes('/compare/'))) {
  const view = JSON.parse(fs.readFileSync(path.join(root, 'view.json')));
  if (!args.includes('repos/Driftward-LLC/capykit/compare/' + view.baseRefOid + '...' + view.headRefOid)) process.exit(9);
  if (mode === 'api-success' || mode === 'api-partial' || mode === 'head-drift' || mode === 'base-drift') {
    process.stdout.write('diff --git a/README.md b/README.md\\n'.repeat(mode === 'api-partial' ? 1 : view.changedFiles) + '+@/tmp/not-file-content\\n');
  } else {
    process.stderr.write(mode === 'unrelated-error' ? 'HTTP 403 PRIVATE_AUTH_DIAGNOSTIC' : 'HTTP 406: Sorry, the diff exceeded the maximum number of files (300). Comparison.diff too_large PRIVATE_DIAGNOSTIC');
    process.exit(1);
  }
} else if (args.includes('api')) {
  let files = JSON.parse(fs.readFileSync(path.join(root, 'files.json')));
  if (mode === 'incomplete') files.pop();
  if (mode === 'wrong-paths') files[0] = {path: 'wrong-but-same-count.ts'};
  process.stdout.write(files.map(JSON.stringify).join('\\n') + '\\n');
} else process.exit(9);
`, { mode: 0o700 });
    writeFileSync(path.join(directory, 'git'), `#!/usr/bin/python3
import json, os, sys, time
args = sys.argv[1:]
root, mode = os.environ['TEST_DIFF_DIRECTORY'], os.environ['TEST_DIFF_MODE']
assert os.environ['GIT_CONFIG_GLOBAL'] == '/dev/null' and os.environ['GIT_CONFIG_NOSYSTEM'] == '1' and os.environ['GIT_TERMINAL_PROMPT'] == '0'
if 'fetch' in args:
    if mode == 'fetch-error':
        sys.stderr.write('PRIVATE_FETCH_DIAGNOSTIC')
        sys.exit(1)
    if mode == 'fetch-timeout':
        with open(os.path.join(root, 'fetch-pid'), 'w') as output:
            output.write(str(os.getpid()))
        time.sleep(60)
    index = args.index('origin')
    with open(os.path.join(root, 'view.json')) as source:
        view = json.load(source)
    assert args[index + 1:] == [view['baseRefOid'], view['headRefOid']]
    args[index] = os.path.join(root, 'source')
    args = [a.replace('protocol.file.allow=never', 'protocol.file.allow=always') for a in args]
os.execv('/usr/bin/git', ['git'] + args)
`, { mode: 0o700 });
    Object.assign(process.env, { PATH: `${directory}${path.delimiter}${previous.PATH || ''}`,
      DRIFTWARD_HERMES_RUNTIME_USER: '10000:10000', TEST_DIFF_DIRECTORY: directory, TMPDIR: temporary });
    const reset = (mode: string) => {
      process.env.TEST_DIFF_MODE = mode;
      for (const name of ['views', 'fallback-called', 'fetch-pid']) rmSync(path.join(directory, name), { force: true });
    };
    const failure = async (reason = 'verification_evidence_unavailable', timeout = 10_000, limit?: number) => {
      await assert.rejects(inspectDraftPullRequestViaCommand('Driftward-LLC/capykit', 82, timeout, limit), (error: unknown) => {
        assert.ok(error instanceof NonRetryableBridgeExecutionError);
        assert.equal(error.reasonCode, reason);
        assert.doesNotMatch(JSON.stringify(error) + error.message, /PRIVATE_|not-file-content/);
        return true;
      });
      assert.deepEqual(readdirSync(temporary), []);
    };
    await t.test('HTTP 406 falls back to a real merge-base diff including binary and renamed files', async () => {
      reset('too-large');
      const observed = await inspectDraftPullRequestViaCommand('Driftward-LLC/capykit', 82, 10_000);
      assert.equal(observed.headRefOid, head);
      assert.equal(observed.baseRefOid, base);
      assert.equal(observed.files?.length, 304);
      assert.ok(observed.files?.some((file) => file.path === 'new name.txt'));
      assert.match(observed.diffText, /rename from old name.txt/);
      assert.match(observed.diffText, /Binary files/);
      assert.doesNotMatch(observed.diffText, /base-only.txt/);
      assert.equal(findAddedTempFileReference(observed.diffText), '+@/tmp/not-file-content');
      assert.deepEqual(readdirSync(temporary), []);
    });
    await t.test('disk scan tolerates removed Git lock files without relaxing its other bounds', async () => {
      try {
        process.env.TEST_DIFF_SCAN_PROBE = 'removed-file';
        reset('too-large');
        const observed = await inspectDraftPullRequestViaCommand('Driftward-LLC/capykit', 82, 10_000);
        assert.equal(observed.files?.length, 304);
        assert.deepEqual(readdirSync(temporary), []);
        assert.equal(readFileSync(path.join(directory, 'disk-scan-probed'), 'utf8'), 'yes');
        await failure('hermes_process_output_limit_exceeded', 10_000, 16 * 1024);
        for (const probe of ['permission-error', 'disk-limit']) {
          rmSync(path.join(directory, 'disk-scan-probed'));
          process.env.TEST_DIFF_SCAN_PROBE = probe;
          await failure();
          assert.equal(readFileSync(path.join(directory, 'disk-scan-probed'), 'utf8'), 'yes');
        }
      } finally {
        delete process.env.TEST_DIFF_SCAN_PROBE;
      }
    });
    await t.test('supported CLI fields join exact REST revisions and API success needs no local source', async () => {
      reset('api-success');
      await inspectDraftPullRequestViaCommand('Driftward-LLC/capykit', 82, 5000);
      assert.equal(existsSync(path.join(directory, 'fallback-called')), false);
    });
    for (const mode of ['api-partial', 'head-drift', 'base-drift', 'missing-sha', 'snapshot-head-mismatch', 'snapshot-base-branch-mismatch', 'unrelated-error', 'incomplete', 'wrong-paths', 'fetch-error']) {
      await t.test(`fails closed for ${mode}`, async () => {
        reset(mode);
        await failure();
        if (['missing-sha', 'unrelated-error', 'incomplete'].includes(mode)) assert.equal(existsSync(path.join(directory, 'fallback-called')), false);
      });
    }
    await t.test('fallback output overflow remains bounded and cleans source', async () => {
      reset('too-large');
      await failure('hermes_process_output_limit_exceeded', 10_000, 16 * 1024);
    });
    await t.test('fallback deadline kills the fetch process and cleans source', async () => {
      reset('fetch-timeout');
      const started = Date.now();
      await failure('verification_evidence_unavailable', 4000);
      assert.ok(Date.now() - started < 4500);
      const pid = Number(readFileSync(path.join(directory, 'fetch-pid'), 'utf8'));
      assert.throws(() => process.kill(pid, 0), /ESRCH/);
    });
    await t.test('identity command failures keep their identity reason', async () => {
      reset('identity-error');
      await assert.rejects(assertHermesIdentityViaCommand('Driftward-LLC/capykit', {
        expectedGithubLogin: 'factory-hermes[bot]', expectedGitAuthorName: 'factory-hermes[bot]',
        expectedGitAuthorEmail: '123+factory-hermes[bot]@users.noreply.github.com',
        forbiddenGithubLogins: [], forbiddenGitAuthorEmails: [], timeout: 1000,
      }), (error: unknown) => error instanceof NonRetryableBridgeExecutionError
        && error.reasonCode === 'hermes_runtime_identity_check_failed' && !error.message.includes('PRIVATE_'));
    });
  } finally {
    for (const name of names) {
      if (previous[name] === undefined) delete process.env[name]; else process.env[name] = previous[name];
    }
    rmSync(directory, { recursive: true, force: true });
  }
});
