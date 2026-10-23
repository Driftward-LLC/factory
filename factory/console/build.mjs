import { build, context } from 'esbuild';

await Promise.all([
  { entryPoints: ['src/main.jsx'], outfile: 'app.js', minify: true, jsx: 'automatic', target: 'es2022', legalComments: 'inline' },
  { entryPoints: ['src/styles.css'], outfile: 'styles.css' },
].map(async (options) => {
  const config = { bundle: true, logLevel: 'info', ...options };
  if (process.argv.includes('--watch')) await (await context(config)).watch();
  else await build(config);
}));
