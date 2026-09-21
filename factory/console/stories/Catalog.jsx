import * as icons from '@driftward/ui/icons';

export function TokenCatalog() {
  return <section>
    <h1>Theme tokens</h1>
    <p>Live values from @driftward/ui/styles.css. Use these variables in application styles.</p>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 16 }}>
      {['canvas', 'surface', 'sidebar', 'hover', 'selected', 'ink', 'muted', 'line', 'control-line', 'blue', 'green', 'amber', 'red', 'red-soft'].map(name =>
        <div key={name}><div style={{ height: 64, border: '1px solid var(--control-line)', borderRadius: 6, background: `var(--${name})` }} /><code>--{name}</code></div>
      )}
    </div>
    <h2 style={{ marginTop: 32 }}>Typography</h2>
    <p>Platform sans-serif. Page titles 24px, section titles 14px, controls 13px; monospace for technical references. No remote font downloads.</p>
    <h1>Page heading</h1><h2>Section heading</h2><p>Body text and <span className="muted">supporting information</span>.</p>
  </section>;
}

export function IconCatalog() {
  return <section>
    <h1>Approved icons</h1>
    <p>Import named SVG components from @driftward/ui/icons. Pair with visible labels and hide decorative SVGs from assistive technology. Radix Icons, MIT licensed.</p>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 24 }}>
      {Object.entries(icons).map(([name, Icon]) => <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 12 }}><Icon aria-hidden="true" /><code>{name}</code></div>)}
    </div>
  </section>;
}
