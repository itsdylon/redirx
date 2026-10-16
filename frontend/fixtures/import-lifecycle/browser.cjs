/* Actual UploadPage + validators; auth/onboarding/layout are offline fixtures. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://127.0.0.1:4286';
(async () => {
  const output = path.resolve(process.argv[2] || '../docs/research/product-rd-20261015/import-responsiveness');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch(); const receipt = { widths: [], errors: [], unexpected: [] };
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      page.on('pageerror', error => receipt.errors.push(error.message));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) return route.abort();
        if (url.pathname === '/api/gsc/status') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, connected: false, configured: false }) });
        if (url.pathname.startsWith('/api/')) { receipt.unexpected.push(url.pathname); return route.abort(); }
        return route.continue();
      });
      await page.addInitScript(() => {
        const original = File.prototype.arrayBuffer;
        File.prototype.arrayBuffer = function () {
          const bytes = original.call(this);
          if (!this.name.startsWith('pending')) return bytes;
          return new Promise(resolve => { window.__releaseRead = async () => resolve(await bytes); });
        };
      });
      await page.goto(`${base}/fixtures/import-lifecycle/index.html`);
      await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
      const old = page.getByLabel('Old Site CSV', { exact: true });
      const csv = name => ({ name, mimeType: 'text/csv', buffer: Buffer.from('https://Example.com/Case%2FPath?x=A%20B\n/second') });
      const pending = name => ({ ...csv(name), mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
      async function selectPending(name) { await page.evaluate(() => { delete window.__releaseRead; }); await old.setInputFiles(pending(name)); await page.waitForFunction(() => typeof window.__releaseRead === 'function'); }
      await selectPending('pending.xlsx');
      await page.getByRole('status').filter({ hasText: 'Checking pending.xlsx' }).waitFor();
      await page.waitForFunction(() => typeof window.__releaseRead === 'function');
      await page.screenshot({ path: path.join(output, `pending-${width}.png`), fullPage: true });
      await page.getByRole('button', { name: 'Cancel checking pending.xlsx' }).focus(); await page.keyboard.press('Enter');
      await page.evaluate(() => window.__releaseRead());
      await page.waitForTimeout(30);
      assert.equal(await page.getByRole('status').count(), 0); assert.equal(await page.getByRole('alert').count(), 0);
      assert.equal(await page.getByRole('button', { name: /Begin Quick Match/ }).isDisabled(), true);
      await selectPending('pending-replaced.xlsx');
      await page.getByRole('status').waitFor(); await page.waitForFunction(() => typeof window.__releaseRead === 'function');
      await old.setInputFiles(csv('latest.csv'));
      await page.getByRole('button', { name: 'Remove latest.csv' }).waitFor();
      await page.evaluate(() => window.__releaseRead()); await page.waitForTimeout(30);
      assert.equal(await page.getByRole('button', { name: 'Remove latest.csv' }).count(), 1);
      assert.equal(await page.getByRole('alert').count(), 0);
      await old.setInputFiles({ name: 'broken.xml', mimeType: 'application/xml', buffer: Buffer.from('<urlset><url><loc>broken') });
      await page.getByRole('alert').filter({ hasText: 'File is not valid XML' }).waitFor();
      await old.setInputFiles({ name: 'corrupt.xlsx', mimeType: 'application/octet-stream', buffer: Buffer.from([0x50, 0x4b, 3, 4, 0, 0, 0, 0]) });
      await page.getByRole('alert').filter({ hasText: /Failed to parse Excel/ }).waitFor();
      await old.setInputFiles(csv('recovered.csv')); await page.getByRole('button', { name: 'Remove recovered.csv' }).waitFor();
      await selectPending('pending-owner.xlsx'); await page.getByRole('status').waitFor();
      await page.waitForFunction(() => typeof window.__releaseRead === 'function');
      await page.getByRole('button', { name: 'Switch fixture owner' }).click(); await page.evaluate(() => window.__releaseRead());
      await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
      assert.equal(await page.getByRole('status').count(), 0); assert.equal(await page.getByRole('alert').count(), 0);
      await selectPending('pending-unmount.xlsx'); await page.getByRole('status').waitFor();
      await page.waitForFunction(() => typeof window.__releaseRead === 'function');
      await page.getByRole('button', { name: 'Toggle fixture surface' }).click(); await page.evaluate(() => window.__releaseRead());
      await page.getByRole('button', { name: 'Toggle fixture surface' }).click();
      await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
      await old.setInputFiles(csv('final.csv')); await page.getByRole('button', { name: 'Remove final.csv' }).waitFor();
      assert.equal(await page.getByRole('alert').count(), 0);
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth); assert.equal(scrollWidth, width);
      await page.screenshot({ path: path.join(output, `recovered-${width}.png`), fullPage: true });
      receipt.widths.push({ width, scrollWidth, keyboardCancel: true, replacement: true, malformedRecovery: true, ownerIsolation: true, unmountIsolation: true });
      await page.close();
    }
    assert.deepEqual(receipt.errors, []); assert.deepEqual(receipt.unexpected, []);
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify(receipt, null, 2)); console.log(JSON.stringify(receipt));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
