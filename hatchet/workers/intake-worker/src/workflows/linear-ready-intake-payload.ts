import type { BridgeJob, JsonObject, JsonValue } from '@driftward/hatchet-worker-shared';
import {
  resolveLinearProjectRoute,
  type ResolvedLinearProjectRoute
} from './linear-project-routing';
import { resolveDraftPrVerifierPolicy, verifierPolicyPayload } from './draft-pr-verifier-policy';

export type LinearReadyIssuePayloadInput = {
  eventKey?: string;
  deliveryId?: string;
  issueId?: string;
  identifier?: string;
  issueUrl?: string;
  title?: string;
  description?: string;
  projectId?: string;
  projectName?: string;
  status?: string;
};

export type LinearReadyIssueEligibilityInput = LinearReadyIssuePayloadInput & {
  eventKey?: string;
  team?: string;
  teamKey?: string;
  blockedBy?: JsonValue[];
  dispatchEligible?: boolean;
  decision?: string;
  reasonCode?: string;
};

export function assertReadyLinearIssueDispatch(input: LinearReadyIssueEligibilityInput): ResolvedLinearProjectRoute {
  const failures: string[] = [];
  if (!input.eventKey) failures.push('eventKey_missing');
  if (!input.identifier) failures.push('identifier_missing');
  if (input.dispatchEligible !== true) failures.push('dispatch_not_eligible');
  if (input.decision !== 'ready_for_implementation') failures.push('decision_not_ready_for_implementation');
  if (input.reasonCode !== 'linear_issue_ready_for_implementation') failures.push('reason_code_not_ready');
  if (input.team !== 'Engineering' && input.teamKey !== 'ENG') failures.push('team_not_engineering');
  if (input.status !== 'Ready') failures.push('status_not_ready');
  if (Array.isArray(input.blockedBy) && input.blockedBy.length > 0) failures.push('blocked_by_not_empty');

  if (failures.length > 0) {
    throw new Error(`ready Linear issue failed intake validation: ${failures.join(', ')}`);
  }

  return resolveLinearProjectRoute(input.projectId);
}

export function assertRepoAllowed(repo: string): void {
  const allowed = (process.env.HATCHET_INTAKE_ALLOWED_REPOS || 'Driftward-LLC/driftward-host,Driftward-LLC/capykit')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  if (!allowed.includes(repo)) {
    throw new Error(`repo ${repo} is not in HATCHET_INTAKE_ALLOWED_REPOS`);
  }
}

export function assertExistingJobRouting(job: Pick<BridgeJob, 'payload'>, route: ResolvedLinearProjectRoute): void {
  if (job.payload.repo !== route.repo) {
    throw new Error(`existing bridge job repo ${String(job.payload.repo)} conflicts with resolved repo ${route.repo}`);
  }

  const routing = job.payload.routing;
  if (routing !== undefined) {
    if (!isJsonObject(routing)
      || routing.source !== 'linear_project'
      || routing.project_id !== route.projectId
      || routing.registry_version !== route.registryVersion) {
      throw new Error('existing bridge job routing evidence conflicts with the resolved Linear project route');
    }
  }
  if (job.payload.dry_run === true) return;
  const verifier = job.payload.verification_policy;
  if (!isJsonObject(verifier) || verifier.repo !== route.repo || typeof verifier.id !== 'string' || !verifier.id.trim()) {
    throw new Error('existing bridge job verifier policy conflicts with the resolved Linear project route');
  }
}

export function buildBridgePayload(
  input: LinearReadyIssuePayloadInput,
  route: ResolvedLinearProjectRoute,
  dryRun: boolean
): JsonObject {
  const verifierPolicy = resolveDraftPrVerifierPolicy(route.repo);
  return {
    work_item_id: input.identifier || '',
    source_event_key: input.eventKey || null,
    source_delivery_id: input.deliveryId || null,
    repo: route.repo,
    linear_issue_id: input.issueId || null,
    linear_issue: input.identifier || null,
    linear_issue_url: input.issueUrl || null,
    routing: {
      source: 'linear_project',
      project_id: route.projectId,
      project_name: input.projectName || null,
      registry_version: route.registryVersion
    },
    expected_head_sha: null,
    expected_base: verifierPolicy.expected_base,
    allowed_changed_paths: verifierPolicy.allowed_changed_paths,
    forbidden_changed_paths: verifierPolicy.forbidden_changed_paths,
    required_local_commands: verifierPolicy.required_local_commands,
    secret_check_command: verifierPolicy.secret_check_command,
    verification_policy: verifierPolicyPayload(verifierPolicy),
    implementation_prompt: buildImplementationPrompt(input),
    instructions: dryRun
      ? 'Dry-run intake dispatch from a ready Linear issue. Do not create external GitHub, Linear, or repo effects.'
      : 'Real intake dispatch from a ready Linear issue. Create or reuse one draft GitHub PR through Driftward Hermes and the bridge ledger.',
    repo_path: null,
    title: input.title || null,
    body: input.identifier ? `Refs Linear ${input.identifier}` : null,
    dry_run: dryRun
  };
}

function buildImplementationPrompt(input: LinearReadyIssuePayloadInput): string {
  const lines = [
    `Linear issue ${input.identifier}: ${input.title || 'Untitled'}`,
    input.issueUrl ? `URL: ${input.issueUrl}` : null,
    input.status ? `Status: ${input.status}` : null,
    '',
    input.description || ''
  ];
  return lines.filter((line): line is string => line !== null).join('\n').trim();
}

function isJsonObject(value: JsonValue): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
