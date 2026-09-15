import { StatusBadge } from '@driftward/ui';

export default {
  title: 'Components/StatusBadge', component: StatusBadge, tags: ['autodocs'],
  args: { tone: 'warning', children: 'Pending verification' },
  argTypes: { tone: { control: 'select', options: ['', 'info', 'success', 'warning', 'danger'] } },
  parameters: { docs: { description: { component: 'Use a visible evidence label with the status icon. A success tone indicates recorded verification, not current PR acceptance. Factory owns stage-to-label mapping in src/model.js; the shared component never infers runtime state.' } } },
};
export const Pending = {};
export const Verified = { args: { tone: 'success', children: 'Verification recorded' } };
export const Failed = { args: { tone: 'danger', children: 'Failed attempt' } };
export const Informational = { args: { tone: 'info', children: 'Queued' } };
export const Unknown = { args: { tone: '', children: 'Unknown stage' } };
