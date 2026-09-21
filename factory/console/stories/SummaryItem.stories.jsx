import { SummaryItem } from '@driftward/ui';

export default {
  title: 'Components/SummaryItem', component: SummaryItem, tags: ['autodocs'],
  args: { title: 'Repository', children: 'example/project' },
};
export const Available = {};
export const Missing = { args: { children: null } };
export const Zero = { args: { title: 'Generation', children: 0 } };
