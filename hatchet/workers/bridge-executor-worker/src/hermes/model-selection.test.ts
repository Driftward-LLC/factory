import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { boundedModelSelection, HERMES_MODEL_SELECTION_QUERY, reportModelSelection } from './model-selection';

const selection = { model_source: 'executor_config', selected_model: 'gpt-6.1-sol', selected_provider: 'openai-codex' } as const;

test('model selection is bounded metadata, never arbitrary config or provider secrets', () => {
  assert.deepEqual(boundedModelSelection(selection), selection);
  assert.ok(boundedModelSelection({ ...selection, selected_model: 'anthropic/claude-sonnet-4.6' }));
  for (const value of [null, [], {}, { ...selection, api_key: 'private' }, { ...selection, model_source: 'model_output' }]) {
    assert.equal(boundedModelSelection(value), null);
  }
  for (const field of ['selected_model', 'selected_provider']) {
    for (const value of ['auto', 'unknown', '', 'gpt-6.1-sol\n', 'raw prompt text', '--help', 'https://private/path',
      'sk-proj-FAKE_TEST_VALUE', 'provider/github_pat_FAKE_TEST_VALUE', 'a'.repeat(121), 123]) {
      assert.equal(boundedModelSelection({ ...selection, [field]: value }), null);
    }
  }
});

test('native configuration query returns only model/provider and honors model env override', () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'factory-native-model-'));
  try {
    writeFileSync(path.join(directory, 'cli.py'), `CLI_CONFIG = {'model': {'default': 'gpt-6.1-sol', 'provider': 'openai-codex', 'api_key': 'PRIVATE_TEST_VALUE'}, 'agent': {'system_prompt': 'PRIVATE_PROMPT'}}\n`);
    const env = { ...process.env, PYTHONPATH: directory, HERMES_INFERENCE_MODEL: '', HERMES_INFERENCE_PROVIDER: 'unused-env-provider' };
    const query = (environment = env) => JSON.parse(execFileSync('python3', ['-c', HERMES_MODEL_SELECTION_QUERY], { env: environment, encoding: 'utf8' }));
    assert.deepEqual(query(), selection);
    assert.deepEqual(query({ ...env, HERMES_INFERENCE_MODEL: 'gpt-6-astra' }), { ...selection, selected_model: 'gpt-6-astra' });
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('telemetry observer failures do not fail execution or expose raw errors', async () => {
  const previous = console.warn;
  const warnings: unknown[] = [];
  console.warn = (...args) => { warnings.push(args); };
  try {
    await reportModelSelection(async () => { throw new Error('PRIVATE_TRANSPORT_ERROR'); }, selection);
    await reportModelSelection(undefined, selection);
    assert.equal(warnings.length, 1);
    assert.doesNotMatch(JSON.stringify(warnings), /PRIVATE/);
  } finally { console.warn = previous; }
});
