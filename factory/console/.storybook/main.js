export default {
  framework: '@storybook/react-vite',
  stories: ['../stories/*.mdx', '../stories/*.stories.jsx'],
  addons: ['@storybook/addon-docs', '@storybook/addon-mcp'],
  features: { componentsManifest: true },
  core: { disableTelemetry: true },
};
