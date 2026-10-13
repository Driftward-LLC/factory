#!/usr/bin/env node
// Exercise the published file boundary in an isolated consumer, including CSS typing.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { resolve, join } = require('node:path');

const root = resolve(__dirname, '../factory/console');
const project = JSON.parse(readFileSync(join(root, 'package.json')));
const temp = mkdtempSync(join(tmpdir(), 'factory-ui-consumer-'));
try {
  const packed = JSON.parse(execFileSync('npm', ['pack', '--workspace', '@driftward/ui', '--pack-destination', temp, '--json'], { cwd: root, encoding: 'utf8' }));
  assert.equal(packed[0].name, '@driftward/ui');
  assert.equal(packed[0].version, '0.2.0');
  assert(!packed[0].files.some(file => /(?:storybook|stories|node_modules)\//.test(file.path)), 'Catalog and installed dependencies must not ship inside the package');
  const dependencies = { '@driftward/ui': `file:${join(temp, packed[0].filename)}` };
  for (const name of ['react', 'react-dom']) dependencies[name] = project.dependencies[name];
  for (const name of ['@types/react', '@types/react-dom']) dependencies[name] = project.devDependencies[name];
  writeFileSync(join(temp, 'package.json'), JSON.stringify({ private: true, type: 'module', dependencies }));
  writeFileSync(join(temp, 'consumer.tsx'), `
import { FactoryTheme, Button, Notice, StatusBadge, Metric, SummaryItem, ExternalLink, Link, BrandMark, Card, Heading, MetadataList, MetadataListItem, TextInput, EmptyState, Spinner } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';
import '@driftward/ui/styles.css';
import { renderToStaticMarkup } from 'react-dom/server';
const html = renderToStaticMarkup(<FactoryTheme>
  <BrandMark /><Card><Heading level={2}>Disposable package consumer</Heading>
    <Button><ReloadIcon aria-hidden="true" />Refresh</Button>
    <Button variant="primary" loading>Refreshing…</Button>
    <Button id="composed-action" loading aria-label="Refresh evidence"><ReloadIcon aria-hidden="true" /><span>Refresh <strong>evidence</strong><span aria-hidden="true"> decorative hint</span></span></Button>
    <Button loading aria-label="Inspect artifact"><ReloadIcon aria-hidden="true" /><span>Refresh evidence</span></Button>
    <span id="provided-action-name">Inspect recorded outcome</span>
    <Button loading aria-labelledby="provided-action-name" aria-label="Ignored lower-priority label"><span>Refresh evidence</span></Button>
    <StatusBadge tone="warning">Pending</StatusBadge><Metric name="Attempts" value={0}>Recorded examples</Metric>
    <SummaryItem title="Missing evidence" /><Notice>Recorded service failure</Notice>
    <ExternalLink href="https://example.test/record">Fictional record</ExternalLink>
    <Link href="#consumer">Local inspection</Link>
    <TextInput label="Fixture reason" value="" isDisabled isRequired required onChange={() => {}} />
    <MetadataList><MetadataListItem label="Commit">Unavailable</MetadataListItem></MetadataList>
    <EmptyState title="No recorded attempts" description="Unavailable is distinct from zero." />
    <Spinner label="Loading recorded evidence" />
  </Card>
</FactoryTheme>);
for (const expected of ['type="button"', 'disabled=""', 'pill warning', '<strong>0</strong>', 'Fixture reason', 'required=""', '<dt', '<dd', 'No recorded attempts', 'role="status"', 'noopener noreferrer']) {
  if (!html.includes(expected)) throw new Error('Packed component render missing ' + expected);
}
if (html.includes('<style')) throw new Error('Packed components must use the static stylesheet');
const buttons = [...html.matchAll(/<button([^>]*)>([\\s\\S]*?)<\\/button>/g)];
const composed = buttons.find(([, , content]) => content.includes('Refresh <strong>evidence</strong>'));
if (!composed?.[1].includes('aria-label="Refresh evidence"') || /aria-labelledby=/.test(composed[1])) throw new Error('Packed composed loading action must retain its explicit name');
const nativeIds = buttons.map(([, attrs]) => attrs.match(/(?:^|\\s)id="([^"]+)"/)?.[1]).filter(Boolean);
if (new Set(nativeIds).size !== nativeIds.length) throw new Error('Packed buttons must retain distinct native IDs');
const explicit = buttons.find(([, attrs]) => attrs.includes('aria-label="Inspect artifact"'));
if (!explicit || /aria-labelledby=/.test(explicit[1])) throw new Error('Packed explicit aria-label must retain native naming priority');
if (!buttons.some(([, attrs]) => attrs.includes('aria-labelledby="provided-action-name"') && attrs.includes('aria-label="Ignored lower-priority label"'))) throw new Error('Packed explicit aria-labelledby must be retained');
// @ts-expect-error Composed loading labels need an explicit accessible name.
const unnamedLoading = <Button loading><ReloadIcon aria-hidden="true" /><span>Refresh evidence</span></Button>;
let rejectedUnnamedLoading = false;
try { renderToStaticMarkup(unnamedLoading); }
catch (error) { rejectedUnnamedLoading = error instanceof Error && error.message === 'Loading buttons with composed children require aria-label or aria-labelledby.'; }
if (!rejectedUnnamedLoading) throw new Error('JavaScript consumers must reject unsupported unnamed composed loading actions');
`);
  execFileSync('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: temp, stdio: 'pipe' });
  const installed = JSON.parse(readFileSync(join(temp, 'node_modules/@driftward/ui/package.json')));
  assert.equal(installed.dependencies['@astryxdesign/core'], '0.7.0');
  assert.equal(installed.dependencies['@astryxdesign/theme-neutral'], '0.7.0');
  assert(!installed.dependencies.storybook && !installed.dependencies['@astryxdesign/cli'], 'Build and catalog tools must not be runtime dependencies');
  const run = (name, args) => execFileSync(join(root, 'node_modules/.bin', name), args, { cwd: temp, stdio: 'inherit' });
  // Astryx's source-oriented declarations use extensionless imports, resolved by React bundlers.
  run('tsc', ['--noEmit', '--strict', '--jsx', 'react-jsx', '--module', 'esnext', '--moduleResolution', 'bundler', '--target', 'es2022', 'consumer.tsx']);
  run('esbuild', ['consumer.tsx', '--bundle', '--jsx=automatic', '--platform=node', '--format=cjs', '--outfile=consumer.cjs']);
  execFileSync(process.execPath, ['consumer.cjs'], { cwd: temp, stdio: 'inherit' });
  const css = readFileSync(join(temp, 'consumer.css'), 'utf8');
  assert(css.includes('--canvas:'));
  assert(css.includes('--color-text-primary'), 'Packed styles must carry the Astryx theme');
  assert(/\.x[\da-z_-]+/.test(css), 'Packed styles must carry compiled Astryx component rules');
  assert(!/@import\s+(?:url\()?['"]?https?:/.test(css), 'Packed UI must not fetch remote styles');
  console.log('Passed: isolated package consumer types, theme, controls, metadata, states, rendering, icons, and static Astryx CSS without catalog/build runtime dependencies.');
} finally {
  rmSync(temp, { recursive: true, force: true });
}
