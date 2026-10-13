# @driftward/ui

Shared React components, dark theme, and icons consumed by Factory and its
Storybook. Version 0.2.0; private package, not published to a registry.

```jsx
import { FactoryTheme, Button, StatusBadge } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';
import '@driftward/ui/styles.css';

<FactoryTheme>
  <Button onClick={refresh}><ReloadIcon aria-hidden="true" />Refresh</Button>
  <StatusBadge tone="warning">Pending verification</StatusBadge>
</FactoryTheme>
```

Exports: `Button`, `Notice`, `StatusBadge`, `ExternalLink`, `Metric`,
`SummaryItem`, `BrandMark`, and `FactoryTheme`. Astryx 0.7.0 supplies `Card`,
`Heading`, `MetadataList`, `MetadataListItem`, `Link`, `EmptyState` and `Spinner`
directly. `TextInput` retains Astryx's field properties and adds typed native
`required`, `maxLength`, `pattern` and `spellCheck` attributes used by Factory.
Component properties are typed in `index.tsx`.
`Button` requires `aria-label` or `aria-labelledby` for composed/nested children
when `loading` can become true. TypeScript enforces this contract; JavaScript
misuse throws a clear error. Plain text/number labels name themselves. Supply
the explicit name before loading begins so it stays stable through the action.
Native `aria-label` and `aria-labelledby` overrides remain available;
`aria-labelledby` retains its normal precedence when both are supplied.
Icons are named React SVG exports in `icons.ts` (Radix Icons, MIT).

The stylesheet applies global element styles and the shared dark theme. Use
it once per application. The package supplies TypeScript/JSX source and requires
a React-capable bundler and React 19. TypeScript consumers use
`moduleResolution: "bundler"`; Astryx's declarations do not support NodeNext.
The stylesheet bundles precompiled Astryx rules and the neutral dark theme;
consumers need no StyleX compiler or runtime style injection.
Factory-specific layouts, routing, data
fetching, and status mappings stay in the consuming application.

Run `npm run storybook --prefix factory/console` from the Factory repository.
See `docs/design-system.md` there for agent access, checks, and distribution.
