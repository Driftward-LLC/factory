import { readFileSync } from 'node:fs';

export type FactoryOrganization = {
  schema: 'factory-organization-v1';
  linear: null | {
    workspace_id: string; team_id: string; team_key: string; workspace_slug: string;
    actor_id: string; delegate_name: string;
  };
  github: { enabled: boolean };
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const disabled: FactoryOrganization = { schema: 'factory-organization-v1', linear: null, github: { enabled: false } };

export function loadFactoryOrganization(): FactoryOrganization {
  const location = process.env.FACTORY_ORGANIZATION_CONFIG;
  if (!location) return disabled;
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(location, 'utf8'));
  } catch {
    throw new Error('Factory organization configuration is unavailable');
  }
  return validateFactoryOrganization(value);
}

export function validateFactoryOrganization(value: unknown): FactoryOrganization {
  if (!record(value) || !keys(value, ['schema', 'linear', 'github']) || value.schema !== 'factory-organization-v1'
    || !record(value.github) || !keys(value.github, ['enabled']) || typeof value.github.enabled !== 'boolean') {
    throw new Error('Invalid Factory organization configuration');
  }
  if (value.linear !== null) {
    const linear = value.linear;
    if (!record(linear) || !keys(linear, ['workspace_id', 'team_id', 'team_key', 'workspace_slug', 'actor_id', 'delegate_name'])
      || !['workspace_id', 'team_id', 'actor_id'].every((field) => typeof linear[field] === 'string' && (linear[field] as string).length === 36 && UUID.test(linear[field] as string))
      || typeof linear.team_key !== 'string' || linear.team_key !== linear.team_key.trim() || !/^[A-Z][A-Z0-9]{0,15}$/.test(linear.team_key)
      || typeof linear.workspace_slug !== 'string' || linear.workspace_slug !== linear.workspace_slug.trim() || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(linear.workspace_slug)
      || typeof linear.delegate_name !== 'string' || !linear.delegate_name.trim() || linear.delegate_name.length > 100 || linear.delegate_name.trim() !== linear.delegate_name || /[\x00-\x1f]/.test(linear.delegate_name)) {
      throw new Error('Invalid Factory Linear organization configuration');
    }
  }
  return value as FactoryOrganization;
}

export function configuredLinearIssue(identifier: unknown, organization = loadFactoryOrganization()): boolean {
  return typeof identifier === 'string' && identifier === identifier.trim() && organization.linear !== null
    && new RegExp(`^${organization.linear.team_key}-[1-9][0-9]{0,8}$`).test(identifier);
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function keys(value: Record<string, unknown>, expected: string[]): boolean {
  return Object.keys(value).length === expected.length && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

export function configuredRepositories(environmentKey: string): string[] {
  const repositories = (process.env[environmentKey] || '').split(',').map((repo) => repo.trim()).filter(Boolean);
  if (repositories.some((repo) => repo !== repo.trim() || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo))
    || new Set(repositories.map((repo) => repo.toLowerCase())).size !== repositories.length) {
    throw new Error(`${environmentKey} must contain unique owner/repository entries`);
  }
  return repositories;
}

export function requireGithubOrganization(): FactoryOrganization {
  const organization = loadFactoryOrganization();
  if (!organization.github.enabled) throw new Error('Factory GitHub integration is disabled');
  return organization;
}

export type OrganizationBinding = { workspace_id: string; team_id: string; team_key: string };

export function organizationBinding(organization = loadFactoryOrganization()): OrganizationBinding | null {
  const linear = organization.linear;
  return linear ? { workspace_id: linear.workspace_id, team_id: linear.team_id, team_key: linear.team_key } : null;
}

export function matchesOrganizationBinding(value: unknown, organization = loadFactoryOrganization()): boolean {
  const expected = organizationBinding(organization);
  return expected !== null && record(value) && keys(value, ['workspace_id', 'team_id', 'team_key'])
    && value.workspace_id === expected.workspace_id && value.team_id === expected.team_id && value.team_key === expected.team_key;
}
