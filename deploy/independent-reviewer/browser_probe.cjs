// One fresh browser context per call; no shared browser state or credentials.
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    const url = process.argv[2] || 'data:text/html,<button>Factory reviewer isolation probe</button>';
    await page.goto(url, { timeout: 15000 });
    if (!process.argv[2] && await page.getByRole('button').innerText() !== 'Factory reviewer isolation probe') throw new Error();
    const text = (await page.locator('body').innerText()).slice(0, 16000);
    const screenshot = (await page.screenshot()).toString('base64');
    await context.close();
    process.stdout.write(JSON.stringify({ text, screenshot }));
  } finally { await browser.close(); }
})().catch(() => { process.stderr.write('Isolated browser probe failed'); process.exitCode = 1; });
