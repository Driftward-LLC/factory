import AgentTrace from '../src/AgentTrace.jsx';
import '../src/styles.css';

const attempt = { attempt_id: 'abcdef01-2345-6789-abcd-ef0123456789', work_item_key: 'ENG-123', generation: 2, mode: 'real' };
const agent_trace = { attempt_id: attempt.attempt_id, status: 'unverified', url: 'https://us.posthog.com/project/12345/ai-observability/traces/639a556e-8dfe-522c-8703-53cb2a68d2d5' };
export default {
  title: 'Factory/AgentTrace', component: AgentTrace,
  args: { record: { attempt, agent_trace } },
  parameters: { docs: { description: { component: 'Fictional data. Opens an attempt-scoped PostHog trace without asserting ingestion, completeness or safe takeover. Factory remains the operator surface; Hermes is an internal execution layer.' } } },
};
export const Configured = {};
export const Historical = { args: { record: { attempt, agent_trace: { ...agent_trace, status: 'unavailable', url: null } } } };
export const NotConfigured = { args: { record: { attempt, agent_trace: { ...agent_trace, status: 'not_configured', url: null } } } };
export const DryRun = { args: { record: { attempt: { ...attempt, mode: 'dry-run' }, agent_trace } } };
export const UnsafeUrl = { args: { record: { attempt, agent_trace: { ...agent_trace, url: 'javascript:alert(1)' } } } };
export const WrongAttempt = { args: { record: { attempt, agent_trace: { ...agent_trace, attempt_id: 'different-attempt' } } } };
