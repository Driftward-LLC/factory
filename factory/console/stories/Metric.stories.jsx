import { Metric } from '@driftward/ui';

export default {
  title: 'Components/Metric', component: Metric, tags: ['autodocs'],
  args: { name: 'Recorded attempts', value: 12, children: 'All modes · fictional example' },
  parameters: { docs: { description: { component: 'Show a count with its scope. Missing evidence must stay unavailable; do not turn it into a zero. Supply id only when the value needs a stable DOM identifier.' } } },
};
export const Available = {};
export const Unavailable = { args: { value: 'Unavailable', children: 'No snapshot loaded' } };
export const Zero = { args: { value: 0, children: 'No attempts in this returned set' } };
