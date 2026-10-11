// Only the trusted, image-owned checker writes machine-check evidence.
const fs = require('node:fs');
const { chromium } = require('playwright');
const crypto = require('node:crypto');

async function main() {
  const raw = fs.readFileSync('/request.json');
  const request = JSON.parse(raw);
  const result = { schema: 1, request_sha256: crypto.createHash('sha256').update(raw).digest('hex'),
    status: 'failed', completed_steps: 0, viewports: [], reason: 'browser_failure' };
  let browser;
  try {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport, serviceWorkers: 'block' });
      await context.route('**/*', route => new URL(route.request().url()).origin === 'http://preview:3000'
        ? route.continue() : route.abort());
      await context.tracing.start({ screenshots: true, snapshots: true });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      let pageError = false;
      page.on('pageerror', () => { pageError = true; });
      try {
        await page.goto('http://preview:3000' + request.path, { waitUntil: 'domcontentloaded', timeout: 15000 });
        for (const step of request.steps) {
          if (step.action === 'click') await page.getByRole(step.role, { name: step.name, exact: true }).click();
          else if (step.action === 'fill') await page.getByLabel(step.label, { exact: true }).fill(step.value.replaceAll('{viewport}', String(viewport.width)));
          else if (step.action === 'press') await page.keyboard.press(step.key);
          else if (step.action === 'expect') await page.getByText(step.text, { exact: true }).waitFor({ state: 'visible' });
          else throw new Error('unknown step');
          result.completed_steps++;
        }
        if (pageError) throw new Error('page error');
        if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('horizontal overflow');
        result.viewports.push(viewport.width);
      } finally {
        await page.screenshot({ path: `/evidence/viewport-${viewport.width}.png`, fullPage: true });
        await context.tracing.stop({ path: `/evidence/trace-${viewport.width}.zip` });
        await context.close();
      }
    }
    result.status = 'passed';
    result.reason = 'checks_completed';
  } catch {
    // Error text, page content and request data do not belong in control-plane logs.
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync('/evidence/checks.json', JSON.stringify(result) + '\n', { mode: 0o600 });
  }
}
main().catch(() => { process.exitCode = 1; });
