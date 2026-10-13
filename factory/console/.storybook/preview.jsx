import '@driftward/ui/styles.css';
import { themes } from 'storybook/theming';
import { FactoryTheme } from '@driftward/ui';

export default {
  decorators: [(Story) => <FactoryTheme><Story /></FactoryTheme>],
  parameters: {
    layout: 'padded',
    docs: { theme: themes.dark },
    controls: { expanded: true },
    options: { storySort: { order: ['Foundations', 'Components'] } },
  },
};
