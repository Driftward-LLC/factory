import '@driftward/ui/styles.css';
import { themes } from 'storybook/theming';

export default {
  parameters: {
    layout: 'padded',
    docs: { theme: themes.dark },
    controls: { expanded: true },
    options: { storySort: { order: ['Foundations', 'Components'] } },
  },
};
