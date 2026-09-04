import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import type { JsonObject, JsonValue } from '@driftward/hatchet-worker-shared';

export type DraftPrVerifierPolicy = {
  catalog_version: number;
  digest: string;
  id: string;
  repo: string;
  expected_base: string;
  allowed_changed_paths: string[];
  forbidden_changed_paths: string[];
  required_local_commands: string[];
  secret_check_command: string;
};

export class DraftPrVerifierPolicyError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export function resolveDraftPrVerifierPolicy(repo: string, options: { policyPath?: string } = {}): DraftPrVerifierPolicy {
  const policyPath = options.policyPath || process.env.HATCHET_VERIFIER_POLICY_PATH || (process.env.NODE_ENV === 'test' ? path.resolve(process.cwd(), '..', 'verifier-policy.json') : undefined);
  if (!policyPath) throw new DraftPrVerifierPolicyError('verification_policy_unavailable', 'HATCHET_VERIFIER_POLICY_PATH is required outside test mode');
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(policyPath, 'utf8'));
  } catch (error) {
    throw new DraftPrVerifierPolicyError('verification_policy_unavailable', `draft PR verifier policy is unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', 'draft PR verifier policy must be an object');
  }
  const catalogVersion = (parsed as { version?: unknown }).version;
  const policies = (parsed as { policies?: unknown }).policies;
  if (!Number.isInteger(catalogVersion) || (catalogVersion as number) < 1) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', 'draft PR verifier policy catalog version is invalid');
  }
  if (!Array.isArray(policies)) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', 'draft PR verifier policy must contain policies');
  }
  const matches = policies.filter((policy) => isRecord(policy) && policy.repo === repo);
  if (matches.length !== 1) {
    throw new DraftPrVerifierPolicyError('verification_policy_missing', `expected exactly one verifier policy for ${repo}`);
  }
  return normalizePolicy(matches[0], catalogVersion as number);
}

export function verifierPolicyPayload(policy: DraftPrVerifierPolicy): JsonObject {
  return {
    catalog_version: policy.catalog_version,
    digest: policy.digest,
    id: policy.id,
    repo: policy.repo,
    expected_base: policy.expected_base,
    allowed_changed_paths: policy.allowed_changed_paths,
    forbidden_changed_paths: policy.forbidden_changed_paths,
    required_local_commands: policy.required_local_commands,
    secret_check_command: policy.secret_check_command
  };
}

function normalizePolicy(value: Record<string, unknown>, catalogVersion: number): DraftPrVerifierPolicy {
  const allowedFields = ['allowed_changed_paths', 'expected_base', 'forbidden_changed_paths', 'id', 'repo', 'required_local_commands', 'secret_check_command'];
  const fields = Object.keys(value).sort();
  if (fields.length !== allowedFields.length || fields.some((field, index) => field !== allowedFields[index])) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', `verifier policy ${String(value.id || '<unknown>')} has unexpected fields`);
  }
  const rawPolicy = {
    id: stringField(value, 'id'),
    repo: stringField(value, 'repo'),
    expected_base: stringField(value, 'expected_base'),
    allowed_changed_paths: stringArray(value, 'allowed_changed_paths'),
    forbidden_changed_paths: stringArray(value, 'forbidden_changed_paths'),
    required_local_commands: stringArray(value, 'required_local_commands'),
    secret_check_command: stringField(value, 'secret_check_command')
  };
  const policy: DraftPrVerifierPolicy = { ...rawPolicy, catalog_version: catalogVersion, digest: digest(rawPolicy) };
  if (!policy.expected_base || !policy.allowed_changed_paths.length || !policy.forbidden_changed_paths.length || !policy.required_local_commands.length || !policy.secret_check_command) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', `verifier policy ${policy.id || '<unknown>'} has empty required enforcement`);
  }
  if (new Set(policy.required_local_commands).size !== policy.required_local_commands.length) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', `verifier policy ${policy.id} has duplicate required entries`);
  }
  return policy;
}

function digest(value: unknown): string {
  return createHash('sha256').update(JSON.stringify(sort(value))).digest('hex');
}

function sort(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sort);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, sort(item)]));
  return value;
}

function stringField(value: Record<string, unknown>, field: string): string {
  const candidate = value[field];
  if (typeof candidate !== 'string' || !candidate.trim() || candidate.length > 300) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', `verifier policy field ${field} must be a bounded non-empty string`);
  }
  return candidate.trim();
}

function stringArray(value: Record<string, unknown>, field: string): string[] {
  const candidate = value[field];
  if (!Array.isArray(candidate) || candidate.length === 0 || candidate.length > 32 || candidate.some((item) => typeof item !== 'string' || !item.trim() || item.length > 300)) {
    throw new DraftPrVerifierPolicyError('verification_policy_invalid', `verifier policy field ${field} must be a non-empty bounded string array`);
  }
  return candidate.map((item) => (item as string).trim());
}

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
