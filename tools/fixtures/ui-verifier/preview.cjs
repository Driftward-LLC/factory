const http = require('node:http');
const fs = require('node:fs');
if (Object.keys(process.env).some(key => /OPENAI|CODEX_API|GITHUB_TOKEN|HATCHET.*TOKEN/.test(key))) process.exit(1);
const page = `<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Synthetic Factory verifier fixture</title>
<style>body{font:18px system-ui;margin:20px;max-width:600px}input,button{padding:12px;max-width:90%}</style>
<h1>Create agent</h1><form><label>Agent name <input name="agent" required></label>
<button>Create agent</button></form><p role="status"></p><script>
document.querySelector('form').onsubmit=async e=>{e.preventDefault();
const result=await fetch('/agents',{method:'POST',body:document.querySelector('input').value});
document.querySelector('[role=status]').textContent=await result.text();};</script>`;
http.createServer((req, res) => {
  if (req.url === '/health') return res.end('ready');
  if (req.url === '/agents' && req.method === 'POST') {
    let body = ''; req.on('data', chunk => { body += chunk; });
    req.on('end', () => { fs.appendFileSync('/data/agents', body + '\n'); res.end('Agent created'); });
    return;
  }
  res.setHeader('content-type', 'text/html'); res.end(page);
}).listen(3000, '0.0.0.0');
