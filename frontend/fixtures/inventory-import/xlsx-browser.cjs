// Exercise the real upload/validation path and browser ESM spreadsheet import.
// Start: npx vite --config fixtures/inventory-import/vite.config.ts
const { chromium } = require('playwright');
const XLSX = require('xlsx');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === 'http://127.0.0.1:4285' ? route.continue() : route.abort());
    await page.addInitScript(() => {
      const actual = window.fetch;
      window.fetch = (input, init) => String(input).includes('/api/gsc/status')
        ? Promise.resolve(Response.json({ success: true, configured: false, connected: false })) : actual(input, init);
    });
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('http://127.0.0.1:4285/fixtures/inventory-import/index.html');
      const book = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([
        ['URL', 'Notes'], ['/Case%2fPath?a=1&a=2', 'ignored'], ['https://fixture.invalid/second', 'ignored'],
      ]), 'URLs');
      XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([['/unused']]), 'Other');
      const buffer = XLSX.write(book, { type: 'buffer', bookType: 'xlsx' });
      await page.locator('input[type=file]').setInputFiles({ name: 'patched-engine.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer });
      await page.getByRole('button', { name: 'Remove patched-engine.xlsx' }).waitFor();
      assert.match(await page.locator('main').innerText(), /2 (URLs|rows)/i);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ xlsx: XLSX.version, widths: [1280, 390], uploaded_rows: 2, unused_sheet_excluded: true, browser_errors: errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
