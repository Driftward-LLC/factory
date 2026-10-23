import BeforeExecution from '../src/BeforeExecution.jsx';
import '../src/styles.css';

const item = { issue: 'ENG-999', linear_status: 'Blocked', blocked_by: ['ENG-998'], blocked_by_count: 1, dispatch_eligible: false, observed_at: '2026-10-06T18:00:00Z', provider_updated_at: '2026-10-06T17:59:00Z' };
const intake = { available: true, items: [item], limit: 100, scan_limit: 500, truncated: false };
export default {
  title: 'Factory/BeforeExecution', component: BeforeExecution, args: { intake },
  parameters: { docs: { description: { component: 'Fictional observed Linear issues with no Factory attempt. Read-only: eligibility is not a queue reservation. Source timestamps and bounded coverage stay explicit; unavailable evidence never means an empty backlog.' } } },
};
export const Blocked = {};
export const AwaitingIntake = { args: { intake: { ...intake, items: [{ ...item, linear_status: 'Ready', blocked_by: [], blocked_by_count: 0, dispatch_eligible: true }] } } };
export const Empty = { args: { intake: { ...intake, items: [] } } };
export const Unavailable = { args: { intake: { ...intake, available: false, items: [] } } };
export const Stale = { args: { stale: true } };
export const Partial = { args: { intake: { ...intake, truncated: true } } };
export const IssueWithoutObservation = { args: { intake: { ...intake, items: [] }, issue: true } };
