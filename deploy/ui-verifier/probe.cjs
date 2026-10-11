// Synthetic acceptance only: exercises real MCP + Chromium, never calls a model.
const fs = require('node:fs');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
async function main() {
  assert.equal(fs.readFileSync('/run/secrets/openai-api-key', 'utf8'), '');
  const token = fs.readFileSync('/run/secrets/browser-token', 'utf8').trim();
  const raw = fs.readFileSync('/request.json');
  let session, id = 0;
  const unauthenticated = await fetch('http://browser:8931/mcp');
  assert.equal(unauthenticated.status, 401);
  async function rpc(method, params) {
    const response = await fetch('http://browser:8931/mcp', { method: 'POST',
      headers: { authorization: 'Bearer ' + token, 'content-type': 'application/json',
        accept: 'application/json, text/event-stream', ...(session ? { 'mcp-session-id': session } : {}) },
      body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }), signal: AbortSignal.timeout(25000) });
    assert.equal(response.status, 200);
    session = response.headers.get('mcp-session-id') || session;
    const body = await response.text();
    const value = response.headers.get('content-type')?.includes('text/event-stream')
      ? JSON.parse(body.split('\n').find(line => line.startsWith('data: ')).slice(6)) : JSON.parse(body);
    assert.ok(!value.error && !value.result?.isError, JSON.stringify(value));
    return value.result;
  }
  for (let n = 0; ; n++) {
    try { await rpc('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'factory-acceptance', version: '1' } }); break; }
    catch (error) { if (n >= 15) throw error; await new Promise(r => setTimeout(r, 500)); }
  }
  const navigation = await rpc('tools/call', { name: 'browser_navigate', arguments: { url: 'http://preview:3000' + JSON.parse(raw).path } });
  assert.ok(navigation.content.some(c => c.type === 'text' && c.text.includes('Create agent')));
  await rpc('tools/call', { name: 'browser_resize', arguments: { width: 390, height: 844 } });
  await rpc('tools/call', { name: 'browser_press_key', arguments: { key: 'Tab' } });
  await rpc('tools/call', { name: 'browser_close', arguments: {} });
  fs.writeFileSync('/evidence/agent.json', JSON.stringify({ schema: 1,
    request_sha256: crypto.createHash('sha256').update(raw).digest('hex'),
    status: 'completed', reason: 'fixture_browser_transport', browser_calls: 4,
    assessment: 'not_a_model_assessment' }) + '\n', { mode: 0o600 });
}
main().catch(error => {
  // This entry point refuses real credentials; diagnostic text is synthetic only.
  fs.writeFileSync('/evidence/probe-error.json', JSON.stringify({ error: String(error.message).slice(0, 3000) }));
  process.exitCode = 1;
});
