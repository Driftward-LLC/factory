import { Button } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';

export default {
  title: 'Components/Button', component: Button, tags: ['autodocs'],
  args: { children: 'Refresh', type: 'button', disabled: false },
  argTypes: {
    type: { control: 'select', options: ['button', 'submit', 'reset'] },
    disabled: { control: 'boolean' },
  },
  parameters: { docs: { description: { component: 'Native button attributes and events are forwarded. Keep a visible action label; set type="submit" explicitly for forms. Native focus and disabled behavior apply.' } } },
};
export const Default = {};
export const WithIcon = { render: (args) => <Button {...args}><ReloadIcon aria-hidden="true" />Refresh</Button> };
export const Loading = { args: { disabled: true, children: 'Refreshing…' } };
