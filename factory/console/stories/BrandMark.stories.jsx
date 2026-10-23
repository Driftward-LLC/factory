import { BrandMark } from '@driftward/ui';

export default {
  title: 'Components/BrandMark', component: BrandMark, tags: ['autodocs'],
  parameters: { docs: { description: { component: 'Factory’s decorative F mark. Pair with the visible Factory name. This is a product mark, not a generic logo for every Driftward application.' } } },
};
export const WithName = { render: () => <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><BrandMark /><strong>Factory</strong></div> };
