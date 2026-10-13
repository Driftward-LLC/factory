import { Button } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';

export default {
  title: 'Components/Button', component: Button, tags: ['autodocs'],
  args: { children: 'Refresh', type: 'button', disabled: false, loading: false, variant: 'secondary' },
  argTypes: {
    type: { control: 'select', options: ['button', 'submit', 'reset'] },
    disabled: { control: 'boolean' },
    loading: { control: 'boolean' },
    variant: { control: 'select', options: ['secondary', 'primary', 'ghost', 'destructive'] },
  },
  parameters: { docs: { description: { component: 'Astryx button preserving native button attributes and events. Keep a visible action label; set type="submit" explicitly for forms. loading adds busy feedback and blocks activation. Primary is reserved for an operator next step; a preview is still required before confirmation.' } } },
};
export const Default = {};
export const WithIcon = { render: (args) => <Button {...args}><ReloadIcon aria-hidden="true" />Refresh</Button> };
export const Loading = { args: { loading: true, children: 'Refreshing…' } };
export const Primary = { args: { variant: 'primary', children: 'Preview control change' } };
export const Ghost = { args: { variant: 'ghost', children: 'Back to work' } };
export const Destructive = { args: { variant: 'destructive', children: 'Confirm cancellation' } };
