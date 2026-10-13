import { Button } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';
import { useState } from 'react';

function EvidenceLabel() {
  return <><ReloadIcon aria-hidden="true" /><span>Refresh <strong>evidence</strong><span aria-hidden="true"> · decorative hint</span></span></>;
}
function ComposedLoadingButtons(args) {
  const [loading, setLoading] = useState(false);
  return <>
    <Button {...args} id="composed-action" aria-label="Refresh evidence" loading={loading} onClick={() => setLoading(true)}><EvidenceLabel /></Button>
    <Button id="other-composed-action" loading aria-label="Refresh activity">Refresh <span>activity</span></Button>
  </>;
}

export default {
  title: 'Components/Button', component: Button, tags: ['autodocs'],
  args: { children: 'Refresh', type: 'button', disabled: false, loading: false, variant: 'secondary' },
  argTypes: {
    type: { control: 'select', options: ['button', 'submit', 'reset'] },
    disabled: { control: 'boolean' },
    loading: { control: 'boolean' },
    variant: { control: 'select', options: ['secondary', 'primary', 'ghost', 'destructive'] },
  },
  parameters: { docs: { description: { component: 'Astryx button preserving native button attributes and events. Keep a visible action label; set type="submit" explicitly for forms. loading adds busy feedback and blocks activation. Composed/nested children require an explicit aria-label or aria-labelledby when loading can become true; TypeScript enforces this and JavaScript misuse throws a clear contract error. Plain text/number labels name themselves. Native naming overrides remain caller-owned. Primary is reserved for an operator next step; a preview is still required before confirmation.' } } },
};
export const Default = {};
export const WithIcon = { render: (args) => <Button {...args} aria-label="Refresh"><ReloadIcon aria-hidden="true" />Refresh</Button> };
export const Loading = { args: { loading: true, children: 'Refreshing…' } };
export const ComposedLoading = { render: ComposedLoadingButtons };
export const LoadingNameOverride = {
  render: () => <>
    <Button loading aria-label="Inspect artifact"><EvidenceLabel /></Button>
    <span id="provided-action-name">Inspect recorded outcome</span>
    <Button loading aria-labelledby="provided-action-name" aria-label="Ignored lower-priority label"><EvidenceLabel /></Button>
  </>,
};
export const Primary = { args: { variant: 'primary', children: 'Preview control change' } };
export const Ghost = { args: { variant: 'ghost', children: 'Back to work' } };
export const Destructive = { args: { variant: 'destructive', children: 'Confirm cancellation' } };
