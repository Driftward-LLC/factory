import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { organizationBinding, matchesOrganizationBinding, configuredRepositories, configuredLinearIssue, loadFactoryOrganization, validateFactoryOrganization } from './organization';

const organization = {
  schema: 'factory-organization-v1', github: { enabled: true },
  linear: { workspace_id: '11111111-1111-4111-8111-111111111111', team_id: '22222222-2222-4222-8222-222222222222',
    actor_id: '33333333-3333-4333-8333-333333333333', team_key: 'ACME', workspace_slug: 'example', delegate_name: 'Example Executor' }
};

test('organization config supports another team and disabled integrations without defaults', () => {
  const configured = validateFactoryOrganization(organization);
  assert.equal(configuredLinearIssue('ACME-42', configured), true);
  for (const value of ['ENG-42', 'ACME-0', 'ACME-42\n', undefined]) assert.equal(configuredLinearIssue(value, configured), false);
  assert.equal(configuredLinearIssue('ACME-1', validateFactoryOrganization({ ...organization, linear: null })), false);
  const previous = process.env.FACTORY_ORGANIZATION_CONFIG;
  const directory = mkdtempSync(path.join(tmpdir(), 'factory-org-'));
  try {
    delete process.env.FACTORY_ORGANIZATION_CONFIG;
    assert.deepEqual(loadFactoryOrganization(), { schema: 'factory-organization-v1', linear: null, github: { enabled: false } });
    process.env.FACTORY_ORGANIZATION_CONFIG = path.join(directory, 'organization.json');
    assert.throws(loadFactoryOrganization, /unavailable/);
    writeFileSync(process.env.FACTORY_ORGANIZATION_CONFIG, JSON.stringify(organization));
    assert.equal(loadFactoryOrganization().linear?.team_key, 'ACME');
    writeFileSync(process.env.FACTORY_ORGANIZATION_CONFIG, '{');
    assert.throws(loadFactoryOrganization, /unavailable/);
  } finally {
    if (previous === undefined) delete process.env.FACTORY_ORGANIZATION_CONFIG; else process.env.FACTORY_ORGANIZATION_CONFIG = previous;
    rmSync(directory, { recursive: true });
  }
});

test('malformed and incomplete organization identities fail closed', () => {
  for (const value of [null, {}, { ...organization, extra: true }, { ...organization, github: { enabled: 'true' } },
    { ...organization, linear: { ...organization.linear, actor_id: 'bad' } },
    { ...organization, linear: { ...organization.linear, team_key: 'ACME.*' } },
    { ...organization, linear: { ...organization.linear, workspace_slug: '../example' } },
    { ...organization, linear: { ...organization.linear, delegate_name: '' } }]) {
    assert.throws(() => validateFactoryOrganization(value), /Invalid Factory/);
  }
});

test('explicit repository allowlists default empty and reject malformed or duplicate entries', () => {
  const key = 'FACTORY_TEST_ALLOWED_REPOS';
  const previous = process.env[key];
  try {
    delete process.env[key];
    assert.deepEqual(configuredRepositories(key), []);
    process.env[key] = 'example/widgets, example/platform';
    assert.deepEqual(configuredRepositories(key), ['example/widgets', 'example/platform']);
    for (const value of ['example/widgets,bad', 'example/widgets,EXAMPLE/WIDGETS']) {
      process.env[key] = value;
      assert.throws(() => configuredRepositories(key), /unique owner\/repository/);
    }
  } finally {
    if (previous === undefined) delete process.env[key]; else process.env[key] = previous;
  }
});

test('organization binding requires the original exact workspace, team and key', () => {
  const configured = validateFactoryOrganization(organization);
  const binding = organizationBinding(configured)!;
  assert.equal(matchesOrganizationBinding(binding, configured), true);
  for (const value of [undefined, null, {}, { ...binding, extra: true }, { ...binding, team_key: 'ENG' },
    { ...binding, workspace_id: '44444444-4444-4444-8444-444444444444' },
    { ...binding, team_id: '44444444-4444-4444-8444-444444444444' }]) {
    assert.equal(matchesOrganizationBinding(value, configured), false);
  }
  const reconfigured = validateFactoryOrganization({ ...organization, linear: { ...organization.linear,
    workspace_id: '44444444-4444-4444-8444-444444444444', team_id: '55555555-5555-4555-8555-555555555555' } });
  assert.equal(reconfigured.linear!.team_key, configured.linear!.team_key);
  assert.equal(matchesOrganizationBinding(binding, reconfigured), false);
});
