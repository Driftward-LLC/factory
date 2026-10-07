import { ExternalLink } from '@driftward/ui';
import { ArrowTopRightIcon } from '@driftward/ui/icons';

function safeTraceUrl(url) {
  if (typeof url !== 'string' || url !== url.trim()) return false;
  const match = url.match(/^https:\/\/(us|eu|app)\.posthog\.com\/project\/[1-9][0-9]*\/ai-observability\/traces\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\?timestamp=([^&#?]+)$/);
  if (!match) return false;
  try {
    const timestamp = decodeURIComponent(match[2]);
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(timestamp)
      && Number.isFinite(Date.parse(timestamp)) && new Date(timestamp).toISOString() === timestamp;
  } catch { return false; }
}

export default function AgentTrace({ record }) {
  const trace = record?.agent_trace;
  const attempt = record?.attempt;
  const bound = Boolean(attempt?.attempt_id) && trace?.attempt_id === attempt.attempt_id;
  const url = trace?.url;
  if (attempt?.mode === 'real' && bound && trace?.status === 'unverified' && safeTraceUrl(url)) {
    return <div className="agent-trace">
      <small className="muted">Capture unconfirmed · trace may not exist for this run</small>
      <ExternalLink className="artifact-link" href={url}
        aria-label={`Find trace in PostHog for ${attempt.work_item_key}, generation ${attempt.generation}`}
        title="Searches the recorded run time in PostHog. Capture and availability have not been verified. Telemetry cannot confirm completion or safe manual takeover.">
        Find trace in PostHog <ArrowTopRightIcon aria-hidden="true" />
      </ExternalLink>
    </div>;
  }
  return <div className="agent-trace muted"><small>{attempt?.mode === 'dry-run'
    ? 'Agent trace: not applicable (dry run)'
    : attempt?.mode === 'real' && bound && trace?.status === 'setup_failed'
      ? 'Telemetry setup failure recorded · agent capture not confirmed'
    : bound && trace?.status === 'not_configured'
      ? 'Trace link not configured · capture status unknown'
      : 'Trace unavailable: no correlated, dated link · capture status unknown'}</small></div>;
}
