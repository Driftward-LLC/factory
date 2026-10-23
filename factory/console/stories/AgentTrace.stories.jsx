import AgentTrace from '../src/AgentTrace.jsx';
import '../src/styles.css';

const attempt = { attempt_id: 'abcdef01-2345-6789-abcd-ef0123456789', work_item_key: 'ENG-123', generation: 2, mode: 'real' };
const traceBase = 'https://us.posthog.com/project/12345/ai-observability/traces/639a556e-8dfe-522c-8703-53cb2a68d2d5';
const agent_trace = { attempt_id: attempt.attempt_id, status: 'unverified', url: traceBase + '?timestamp=2026-09-30T05%3A53%3A34.202Z' };
export default {
  title: 'Factory/AgentTrace', component: AgentTrace,
  args: { record: { attempt, agent_trace } },
  parameters: { docs: { description: { component: 'Fictional data. Find trace is a dated, attempt-scoped lookup, never proof of ingestion. Unknown capture, missing correlation, navigation not configured, dry runs and recorded telemetry setup failures remain distinct. A missing trace does not prove capture failed. Factory remains the operator surface.' } } },
};
export const Configured = {};
export const Historical = { args: { record: { attempt, agent_trace: { ...agent_trace, status: 'unavailable', url: null } } } };
export const SetupFailed = { args: { record: { attempt, agent_trace: { ...agent_trace, status: 'setup_failed', url: null } } } };
export const NotConfigured = { args: { record: { attempt, agent_trace: { ...agent_trace, status: 'not_configured', url: null } } } };
export const DryRun = { args: { record: { attempt: { ...attempt, mode: 'dry-run' }, agent_trace } } };
export const UnsafeUrl = { args: { record: { attempt, agent_trace: { ...agent_trace, url: 'javascript:alert(1)' } } } };
export const WrongAttempt = { args: { record: { attempt, agent_trace: { ...agent_trace, attempt_id: 'different-attempt' } } } };
export const MissingTimestamp = { args: { record: { attempt, agent_trace: { ...agent_trace, url: traceBase } } } };
export const InvalidTimestamp = { args: { record: { attempt, agent_trace: { ...agent_trace, url: traceBase + '?timestamp=2026-02-30T00%3A00%3A00.000Z' } } } };
export const ExtraQuery = { args: { record: { attempt, agent_trace: { ...agent_trace, url: agent_trace.url + '&redirect=https://attacker.example' } } } };
export const Fragment = { args: { record: { attempt, agent_trace: { ...agent_trace, url: agent_trace.url + '#other-trace' } } } };
