import { Notice } from '@driftward/ui';

export default {
  title: 'Components/Notice', component: Notice, tags: ['autodocs'],
  args: { children: 'Refresh failed. Showing the last successful snapshot.' },
  parameters: { docs: { description: { component: 'Announces status politely. Preserve stale-data warnings until a refresh succeeds. Pass no children to hide the notice. These examples use fictional data.' } } },
};
export const Stale = {};
export const Unavailable = { args: { children: 'Status delivery information is unavailable.' } };
export const Hidden = { args: { children: null } };
