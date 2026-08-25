import { readFileSync } from 'node:fs';
import path from 'node:path';

const PROJECT_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

export const ROUTING_REGISTRY_PATH_ENV = 'HATCHET_INTAKE_ROUTING_REGISTRY_PATH';

export type LinearProjectRoute = {
  project_id: string;
  repo: string;
  execution_enabled: boolean;
};

export type LinearProjectRoutingRegistry = {
  version: number;
  routes: LinearProjectRoute[];
};

export type ResolvedLinearProjectRoute = {
  projectId: string;
  repo: string;
  executionEnabled: true;
  registryVersion: number;
};

export class LinearProjectRoutingError extends Error {
  constructor(
    public readonly code: string,
    message: string
  ) {
    super(message);
    this.name = 'LinearProjectRoutingError';
  }
}

export function resolveLinearProjectRoute(
  projectId: string | undefined,
  options: { registryPath?: string } = {}
): ResolvedLinearProjectRoute {
  if (!projectId) {
    throw new LinearProjectRoutingError('project_id_missing', 'Linear project ID is required for repository routing');
  }
  if (!PROJECT_ID_PATTERN.test(projectId)) {
    throw new LinearProjectRoutingError('project_id_malformed', 'Linear project ID must be a canonical UUID');
  }

  const registry = loadLinearProjectRoutingRegistry(options.registryPath);
  const matchingRoutes = registry.routes.filter((route) => route.project_id === projectId);
  if (matchingRoutes.length === 0) {
    throw new LinearProjectRoutingError('project_id_unmapped', `Linear project ${projectId} has no repository route`);
  }
  if (matchingRoutes.length !== 1) {
    throw new LinearProjectRoutingError('project_id_ambiguous', `Linear project ${projectId} has ambiguous repository routes`);
  }
  if (!matchingRoutes[0].execution_enabled) {
    throw new LinearProjectRoutingError(
      'linear_issue_project_execution_disabled',
      `Linear project ${projectId} is disabled for factory execution`
    );
  }

  return {
    projectId,
    repo: matchingRoutes[0].repo,
    executionEnabled: true,
    registryVersion: registry.version
  };
}

export function loadLinearProjectRoutingRegistry(registryPath = routingRegistryPath()): LinearProjectRoutingRegistry {
  let raw: string;
  try {
    raw = readFileSync(registryPath, 'utf8');
  } catch (error) {
    throw new LinearProjectRoutingError(
      'routing_registry_read_failed',
      `Unable to read Linear project routing registry at ${registryPath}: ${errorMessage(error)}`
    );
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new LinearProjectRoutingError(
      'routing_registry_parse_failed',
      `Unable to parse Linear project routing registry at ${registryPath}: ${errorMessage(error)}`
    );
  }

  return validateLinearProjectRoutingRegistry(parsed);
}

export function validateLinearProjectRoutingRegistry(value: unknown): LinearProjectRoutingRegistry {
  if (!isRecord(value)) {
    throw new LinearProjectRoutingError('routing_registry_malformed', 'Linear project routing registry must be a JSON object');
  }
  if (value.version !== 2) {
    throw new LinearProjectRoutingError('routing_registry_version_invalid', 'Linear project routing registry version must be 2');
  }
  if (!Array.isArray(value.routes) || value.routes.length === 0) {
    throw new LinearProjectRoutingError('routing_registry_routes_invalid', 'Linear project routing registry must contain routes');
  }

  const routes: LinearProjectRoute[] = [];
  const projectIds = new Set<string>();
  for (const [index, route] of value.routes.entries()) {
    if (!isRecord(route) || typeof route.project_id !== 'string' || typeof route.repo !== 'string') {
      throw new LinearProjectRoutingError('routing_registry_route_malformed', `Linear project route ${index} is malformed`);
    }
    if (!PROJECT_ID_PATTERN.test(route.project_id)) {
      throw new LinearProjectRoutingError('routing_registry_project_id_malformed', `Linear project route ${index} has an invalid project_id`);
    }
    if (!REPOSITORY_PATTERN.test(route.repo)) {
      throw new LinearProjectRoutingError('routing_registry_repo_malformed', `Linear project route ${index} has an invalid repo`);
    }
    if (typeof route.execution_enabled !== 'boolean') {
      throw new LinearProjectRoutingError(
        'routing_registry_execution_policy_malformed',
        `Linear project route ${index} must define execution_enabled as a boolean`
      );
    }
    if (projectIds.has(route.project_id)) {
      throw new LinearProjectRoutingError('routing_registry_project_id_duplicate', `Linear project route ${index} duplicates ${route.project_id}`);
    }
    projectIds.add(route.project_id);
    routes.push({
      project_id: route.project_id,
      repo: route.repo,
      execution_enabled: route.execution_enabled
    });
  }

  return { version: 2, routes };
}

function routingRegistryPath(): string {
  return process.env[ROUTING_REGISTRY_PATH_ENV] || path.resolve(__dirname, '../../../routing-registry.json');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
