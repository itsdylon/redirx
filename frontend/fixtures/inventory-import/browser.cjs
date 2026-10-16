const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = path.resolve(process.argv[2] || '../docs/research/product-rd-20261015/inventory-import');
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => new URL(route.request().url()).origin === 'http://127.0.0.1:4285' ? route.continue() : route.abort());
  await page.addInitScript(() => {
    const actual = window.fetch;
    window.fetch = async (input, init) => {
      const url = String(input);
      if (url.includes('/api/gsc/status')) return Response.json({ success: true, connected: true, configured: true });
      if (url.includes('/api/gsc/properties')) return Response.json({ success: true, properties: [{ site_url: 'sc-domain:example.com', permission_level: 'siteOwner' }] });
      if (url.includes('/api/discovery/discover')) {
        window.__request = JSON.parse(init.body);
        window.__signal = init.signal;
        // Deliberately resolve after cancellation: exercises the revision guard too.
        return new Promise(resolve => { window.__finish = () => resolve(Response.json({ success: true, root_url: 'https://example.com', urls: ['https://example.com/Page'], count: 1, total_found: 1, truncated: false, max_urls: 1000, method: 'sitemap', generator: null, plan: 'free' })); });
      }
      return actual(input, init);
    };
  });
  const receipt = { viewports: [], keyboardReplace: false, keyboardRemove: false, invalidDropShown: false, optOut: false, cancelledLateIgnored: false, errors };
  const longName = 'Migration-export-with-a-very-long-unbroken-URL-inventory-name-and-Case%2FPath-20261016.csv';
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('http://127.0.0.1:4285/fixtures/inventory-import/index.html');
    await page.locator('input[type=file]').setInputFiles({ name: longName, mimeType: 'text/csv', buffer: Buffer.from('https://example.com/Case%2FPath?q=A&b=1\n/legacy-relative-path') });
    await page.getByRole('button', { name: 'Replace file' }).waitFor();
    await page.getByRole('textbox', { name: 'Old site domain' }).fill('example.com');
    await page.getByRole('button', { name: 'Change', exact: true }).click();
    await page.getByLabel('Search Console property').selectOption('');
    await page.getByRole('button', { name: 'Find Pages' }).click();
    await page.getByRole('button', { name: 'Cancel scan' }).waitFor();
    await page.screenshot({ path: path.join(output, `inventory-${width}.png`), fullPage: true });
    const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, inputFont: getComputedStyle(document.querySelector('input[aria-label="Old site domain"]')).fontSize, selectFont: getComputedStyle(document.querySelector('select')).fontSize }));
    assert.equal(metrics.scrollWidth, width);
    if (width === 390) { assert.equal(metrics.inputFont, '16px'); assert.equal(metrics.selectFont, '16px'); }
    receipt.viewports.push(metrics);
    assert.equal(await page.evaluate(() => window.__request.use_gsc), false);
    receipt.optOut = true;
    await page.getByRole('button', { name: 'Cancel scan' }).click();
    assert.equal(await page.evaluate(() => window.__signal.aborted), true);
    await page.evaluate(() => window.__finish());
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => window.__discovered), null);
    assert.equal(await page.getByRole('button', { name: 'Find Pages' }).isEnabled(), true);
    receipt.cancelledLateIgnored = true;
  }
  const replacement = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Replace file' }).focus();
  await page.keyboard.press('Enter');
  await (await replacement).setFiles({ name: 'replacement.txt', mimeType: 'text/plain', buffer: Buffer.from('/replacement') });
  await page.getByRole('button', { name: 'Remove replacement.txt' }).waitFor();
  receipt.keyboardReplace = true;
  await page.getByRole('button', { name: 'Remove replacement.txt' }).focus();
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: 'Browse Files' }).waitFor();
  receipt.keyboardRemove = true;
  const data = await page.evaluateHandle(() => { const transfer = new DataTransfer(); transfer.items.add(new File(['wrong format'], 'urls.pdf', { type: 'application/pdf' })); return transfer; });
  await page.locator('div.border-dashed').filter({ has: page.locator('input[type=file]') }).dispatchEvent('drop', { dataTransfer: data });
  await page.getByRole('alert').waitFor();
  receipt.invalidDropShown = true;
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify(receipt));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
