import { isSecretShaped } from './bounded-evidence';

export const MODEL_SELECTION_HEADER = 'factory-model-selection-bin';
export type ExecutorModelSelection = {
  model_source: 'executor_config';
  selected_model: string;
  selected_provider: string;
};
export type ModelSelectionObserver = (selection: ExecutorModelSelection) => Promise<unknown> | void;

/** Configuration selected by the executor, not proof of a completed provider call. */
export function boundedModelSelection(value: unknown): ExecutorModelSelection | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  if (Object.keys(raw).sort().join(',') !== 'model_source,selected_model,selected_provider'
      || raw.model_source !== 'executor_config') return null;
  for (const key of ['selected_model', 'selected_provider']) {
    const item = raw[key];
    if (typeof item !== 'string' || item !== item.trim() || item.length > 120
        || !/^[A-Za-z0-9][A-Za-z0-9._+:/-]*$/.test(item) || item.includes('://')
        || isSecretShaped(item) || ['auto', 'unknown'].includes(item)) return null;
  }
  return { model_source: 'executor_config', selected_model: raw.selected_model as string,
    selected_provider: raw.selected_provider as string };
}

export async function reportModelSelection(observer: ModelSelectionObserver | undefined, selection: ExecutorModelSelection): Promise<void> {
  try { await observer?.(selection); }
  catch { console.warn('Factory model-selection telemetry unavailable; execution is unchanged'); }
}

// Runs only inside Hermes, through the executor's fixed command. Never return
// the config, environment, credentials, or exception text to the worker.
export const HERMES_MODEL_SELECTION_QUERY = `
import json, os, sys
sys.path.insert(0, '/opt/hermes')
from cli import CLI_CONFIG
config = CLI_CONFIG.get('model', {})
model = (config.get('default') or config.get('model')) if isinstance(config, dict) else config
provider = config.get('provider') if isinstance(config, dict) else None
print(json.dumps({'model_source': 'executor_config',
    'selected_model': os.environ.get('HERMES_INFERENCE_MODEL') or model,
    'selected_provider': provider or os.environ.get('HERMES_INFERENCE_PROVIDER')}))
`;
