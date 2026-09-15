# @driftward/ui

Shared React components, dark theme, and icons consumed by Factory and its
Storybook. Version 0.1.0; private package, not published to a registry.

```jsx
import { Button, StatusBadge } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';
import '@driftward/ui/styles.css';
```

Exports: `Button`, `Notice`, `StatusBadge`, `ExternalLink`, `Metric`,
`SummaryItem`, and `BrandMark`. Component properties are typed in `index.tsx`.
Icons are named React SVG exports in `icons.ts` (Radix Icons, MIT).

The stylesheet applies global element styles and the shared dark theme. Use
it once per application. The package supplies TypeScript/JSX source and requires
a React-capable bundler and React 19. Factory-specific layouts, routing, data
fetching, and status mappings stay in the consuming application.

Run `npm run storybook --prefix factory/console` from the Factory repository.
See `docs/design-system.md` there for agent access, checks, and distribution.
