import { ExternalLink } from '@driftward/ui';
import { ArrowTopRightIcon } from '@driftward/ui/icons';

export default function AgentTrace({ record }) {
  const trace = record?.agent_trace;
  const attempt = record?.attempt;
  const bound = Boolean(attempt?.attempt_id) && trace?.attempt_id === attempt.attempt_id;
  const url = trace?.url;
  const safe = typeof url === 'string' && url === url.trim()
    && /^https:\/\/(us|eu|app)\.posthog\.com\/project\/[1-9][0-9]*\/ai-observability\/traces\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(url);
  if (attempt?.mode === 'real' && bound && trace?.status === 'unverified' && safe) {
    return <div className="agent-trace">
      <ExternalLink className="artifact-link" href={url}
        aria-label={`Open agent trace for ${attempt.work_item_key}, generation ${attempt.generation}`}
        title="Opens PostHog. Events may be missing, especially for older attempts. Telemetry does not confirm execution or safe manual takeover.">
        Open agent trace <ArrowTopRightIcon aria-hidden="true" />
      </ExternalLink>
      <small className="muted">Telemetry only · availability unverified</small>
    </div>;
  }
  return <div className="agent-trace muted"><small>{attempt?.mode === 'dry-run'
    ? 'Agent trace: not applicable (dry run)'
    : bound && trace?.status === 'not_configured'
      ? 'Agent trace: PostHog not configured'
      : 'Agent trace unavailable'}</small></div>;
}
