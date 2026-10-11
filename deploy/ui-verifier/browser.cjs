// The app can reach this container: authenticate before forwarding any MCP traffic.
const http = require('node:http');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const token = fs.readFileSync('/run/secrets/browser-token', 'utf8').trim();
const expected = Buffer.from('Bearer ' + token);
fs.mkdirSync('/tmp/browser-work', { recursive: true });
const child = spawn(process.execPath, ['/opt/verifier/node_modules/@playwright/mcp/cli.js',
  '--headless', '--browser', 'chromium', '--no-sandbox', '--isolated', '--no-webmcp',
  '--block-service-workers', '--host', '127.0.0.1', '--port', '8932',
  '--allowed-hosts', '127.0.0.1:8932', '--allowed-origins', 'http://preview:3000',
  '--output-dir', '/tmp/browser-work', '--timeout-navigation', '15000'],
{ cwd: '/tmp/browser-work', stdio: 'ignore' });
const server = http.createServer((req, res) => {
  const actual = Buffer.from(req.headers.authorization || '');
  if (actual.length !== expected.length || !crypto.timingSafeEqual(actual, expected)) {
    res.writeHead(401).end(); return;
  }
  if (req.url !== '/mcp' || !['GET', 'POST', 'DELETE'].includes(req.method)) {
    res.writeHead(404).end(); return;
  }
  const headers = { ...req.headers, host: '127.0.0.1:8932' };
  delete headers.authorization;
  const upstream = http.request({ hostname: '127.0.0.1', port: 8932, path: '/mcp', method: req.method, headers }, reply => {
    res.writeHead(reply.statusCode, reply.headers); reply.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) res.writeHead(503); res.end(); });
  req.on('aborted', () => upstream.destroy());
  res.on('close', () => upstream.destroy());
  req.pipe(upstream);
});
server.listen(8931, '0.0.0.0');
child.on('exit', () => { server.close(); process.exitCode = 1; });
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => { child.kill(sig); server.close(); });
