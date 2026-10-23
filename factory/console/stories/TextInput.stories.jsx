import { useState } from 'react';
import { TextInput, Button } from '@driftward/ui';
import { MagnifyingGlassIcon } from '@driftward/ui/icons';

function ControlledInput(args) {
  const [value, setValue] = useState(args.value || '');
  return <TextInput {...args} value={value} onChange={setValue} />;
}
export default {
  title: 'Components/TextInput', component: TextInput, tags: ['autodocs'],
  args: { label: 'Search', value: '', placeholder: 'Issue, repository, branch' },
  argTypes: {
    required: { control: 'boolean', description: 'Native browser required validation, forwarded to the input.' },
    maxLength: { control: 'number', description: 'Native maximum number of characters accepted by the input.' },
    pattern: { control: 'text', description: 'Native browser pattern validation, checked when a form submits.' },
    spellCheck: { control: 'boolean', description: 'Native browser spelling hint for the input.' },
    isDisabled: { control: 'boolean', description: 'Disables editing and native focus/activation.' },
    onChange: { action: 'changed', description: 'Called with the new string value and the native change event.' },
  },
  render: ControlledInput,
  parameters: { docs: { description: { component: 'Astryx controlled field: onChange receives the new string value. containerClassName styles the complete field and controlClassName styles the control box. Native required, maxLength, pattern and spellCheck are retained by the Driftward adapter for browser validation. Use isDisabled for native disabled behavior. Avoid width/style props in the strict-CSP console.' } } },
};
export const Search = { args: { startIcon: <MagnifyingGlassIcon aria-hidden="true" /> } };
export const RequiredReason = {
  args: { label: 'Reason for this handoff', required: true, maxLength: 320, placeholder: 'What needs operator attention?' },
  render: (args) => <form onSubmit={event => event.preventDefault()}><ControlledInput {...args} /><Button type="submit">Preview control change</Button></form>,
};
export const Disabled = { args: { isDisabled: true, value: 'Preview in progress', label: 'Reason for this handoff' } };
export const Invalid = { args: { label: 'Go to issue', value: 'Invalid identifier', status: { type: 'error', message: 'Enter an Engineering issue such as ENG-901.' } } };
