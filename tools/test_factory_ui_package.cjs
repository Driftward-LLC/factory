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
  const dependencies = { '@driftward/ui': `file:${join(temp, packed[0].filename)}` };
  for (const name of ['react', 'react-dom']) dependencies[name] = project.dependencies[name];
  for (const name of ['@types/react', '@types/react-dom']) dependencies[name] = project.devDependencies[name];
  writeFileSync(join(temp, 'package.json'), JSON.stringify({ private: true, type: 'module', dependencies }));
  writeFileSync(join(temp, 'consumer.tsx'), `
import { Button, StatusBadge, Metric } from '@driftward/ui';
import { ReloadIcon } from '@driftward/ui/icons';
import '@driftward/ui/styles.css';
import { renderToStaticMarkup } from 'react-dom/server';
const html = renderToStaticMarkup(<><Button><ReloadIcon aria-hidden="true" />Refresh</Button><StatusBadge tone="warning">Pending</StatusBadge><Metric name="Attempts" value={0}>Example</Metric></>);
if (!html.includes('type="button"') || !html.includes('pill warning') || !html.includes('<strong>0</strong>')) throw new Error('Packed component render failed');
`);
  execFileSync('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: temp, stdio: 'pipe' });
  const run = (name, args) => execFileSync(join(root, 'node_modules/.bin', name), args, { cwd: temp, stdio: 'inherit' });
  run('tsc', ['--noEmit', '--strict', '--jsx', 'react-jsx', '--module', 'nodenext', '--target', 'es2022', 'consumer.tsx']);
  run('esbuild', ['consumer.tsx', '--bundle', '--jsx=automatic', '--platform=node', '--format=cjs', '--outfile=consumer.cjs']);
  execFileSync(process.execPath, ['consumer.cjs'], { cwd: temp, stdio: 'inherit' });
  assert(readFileSync(join(temp, 'consumer.css'), 'utf8').includes('--canvas:'));
  console.log('Passed: isolated package consumer types, React rendering, icons, and CSS bundle.');
} finally {
  rmSync(temp, { recursive: true, force: true });
}
