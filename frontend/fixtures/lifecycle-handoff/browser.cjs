const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = path.resolve(process.argv[2]);
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [], views = [], externalRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    if (new URL(route.request().url()).origin === 'http://127.0.0.1:4287') return route.continue();
    externalRequests.push(route.request().url()); return route.abort();
  });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const state of ['export', 'installed', 'passed', 'partial', 'stale']) {
      await page.goto(`http://127.0.0.1:4287/fixtures/lifecycle-handoff/index.html?state=${state}`);
      await page.locator('h3').waitFor();
      for (const summary of await page.locator('summary').all()) { await summary.focus(); await page.keyboard.press('Enter'); }
      const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, openDetails: document.querySelectorAll('details[open]').length, heading: document.querySelector('h3').textContent }));
      assert.equal(metrics.scrollWidth, width);
      assert.equal(metrics.openDetails, state === 'stale' ? 2 : 3);
      views.push({ state, ...metrics });
      if (state === 'export' || state === 'partial') await page.screenshot({ path: path.join(output, `handoff-${state}-${width}.png`), fullPage: true });
    }
  }
  assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ views, keyboardDisclosure: true, errors, externalRequests }, null, 2));
  console.log(JSON.stringify({ views, errors, externalRequests }));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
