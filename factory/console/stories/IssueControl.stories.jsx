import IssueControl from '../src/IssueControl.jsx';
import '../src/styles.css';

const base = {
  schema: 'factory-console-issue-control-v1', work_item_key: 'ENG-901', status: 'factory',
  version: 1, safe_to_edit: false, updated_at: '2026-10-01T10:00:00Z', actor: 'operator@example.test',
  reason: 'Fictional ownership example', pending_attempts: [], history: [], actions_enabled: true,
  available_actions: [{ action: 'takeover', label: 'Take over manually' }],
};
function fixture(options = {}) {
  let control = { ...base, ...options.control };
  return async (path, body) => {
    if (options.unavailable) throw new Error('Fictional control service unavailable.');
    if (!body) return control;
    if (path.endsWith('/preview')) return {
      eligible: !options.blocked, work_item_key: control.work_item_key, action: body.action, intent: body.intent,
      snapshot_digest: 'a'.repeat(64), preview_token: 'fictional-preview', expires_at: new Date(Date.now() + 300000).toISOString(), control,
      effect_summary: body.action === 'takeover'
        ? 'Hold this issue, fence new execution and request cancellation. Manual editing remains unsafe until the authority confirms handoff.'
        : 'Validate current evidence and authorize one fresh attempt. Old queued events remain fenced.',
      blockers: options.blocked ? ['An earlier execution has an unresolved external effect.'] : [],
      artifact: { repository: 'example/project', pr_url: 'https://github.com/example/project/pull/12', branch: 'factory/ENG-901', head_sha: 'b'.repeat(40) },
    };
    if (options.conflict) { const error = new Error('The issue changed. Preview again.'); error.status = 409; throw error; }
    control = { ...control, status: body.action === 'takeover' ? 'handoff_pending' : 'factory', version: control.version + 1,
      safe_to_edit: false, reason: body.reason, available_actions: [],
      history: [{ action: body.action, actor: 'operator@example.test', reason: body.reason, created_at: new Date().toISOString(), request_id: body.request_id }] };
    return { control, request_id: body.request_id, message: 'Fictional action recorded. No runtime is connected.' };
  };
}
export default {
  title: 'Factory/IssueControl', component: IssueControl,
  args: { issue: 'ENG-901', api: fixture() },
  parameters: { docs: { description: { component: 'Authenticated, previewed per-issue ownership controls. All examples are disposable fictional data; no runtime is connected. Only server-confirmed safe_to_edit permits the safe handoff label. Ambiguous confirmation results retain the same request ID across navigation/reload. Only server-advertised actions are offered.' } } },
};
export const FactoryControlled = {};
export const ManualControl = { args: { api: fixture({ control: { status: 'manual', safe_to_edit: true, available_actions: [{ action: 'return', intent: 'fresh_attempt', label: 'Return to Factory: fresh attempt' }] } }) } };
export const HandoffPending = { args: { api: fixture({ control: { status: 'handoff_pending', available_actions: [], pending_attempts: [{ attempt_id: 'fictional-active-attempt', kind: 'cancellation_requested' }] } }) } };
export const ReceiptUnavailable = { args: { api: fixture({ control: { status: 'handoff_pending', available_actions: [], pending_attempts: [{ attempt_id: 'fictional-active-attempt', kind: 'effect_in_flight', recovery: { state: 'unavailable', observed_at: '2026-10-01T10:00:00Z' } }] } }) } };
export const LegacyReceiptMissing = { args: { api: fixture({ control: { status: 'handoff_pending', available_actions: [], pending_attempts: [{ attempt_id: 'fictional-active-attempt', kind: 'effect_in_flight', recovery: { state: 'not_found', observed_at: '2026-10-01T10:00:00Z' } }] } }) } };
export const UnconfirmedManual = { args: { api: fixture({ control: { status: 'manual', safe_to_edit: false, available_actions: [] } }) } };
export const ReadOnly = { args: { api: fixture({ control: { actions_enabled: false, available_actions: [] } }) } };
export const BlockedPreview = { args: { api: fixture({ blocked: true }) } };
export const ConcurrentChange = { args: { api: fixture({ conflict: true }) } };
export const Unavailable = { args: { api: fixture({ unavailable: true }) } };
