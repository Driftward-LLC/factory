import { ExternalLink } from '@driftward/ui';
import { ArrowTopRightIcon } from '@driftward/ui/icons';

export default {
  title: 'Components/ExternalLink', component: ExternalLink, tags: ['autodocs'],
  args: { href: 'https://storybook.js.org/', children: 'Storybook documentation' },
  parameters: { docs: { description: { component: 'Opens a new tab with noopener/noreferrer. Callers must validate dynamic URLs and apply their own destination allowlist. This component is not a URL sanitizer. Use an explicit destination label.' } } },
};
export const Default = {};
export const WithIcon = { render: (args) => <ExternalLink {...args}>{args.children} <ArrowTopRightIcon aria-hidden="true" /></ExternalLink> };
