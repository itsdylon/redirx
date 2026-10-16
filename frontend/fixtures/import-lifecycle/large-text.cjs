/* Bounded large CSV/XML diagnostic. No comparative performance claim. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch(); const page = await browser.newPage();
  try {
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'http://127.0.0.1:4286') return route.abort();
      if (url.pathname.startsWith('/api/')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, connected: false, configured: false }) });
      return route.continue();
    });
    await page.goto('http://127.0.0.1:4286/fixtures/import-lifecycle/index.html');
    const result = await page.evaluate(async () => {
      const { validateFile } = await import('/src/utils/validation.ts');
      const urls = Array.from({ length: 5000 }, (_, i) => `https://Example.com/Case%2F${i}?x=${'A'.repeat(1700)}`);
      const rows = [];
      for (const ext of ['csv', 'xml']) {
        const content = ext === 'csv' ? urls.join('\n') : `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls.map(url => `<url><loc>${url}</loc></url>`).join('')}</urlset>`;
        const file = new File([content], `large.${ext}`);
        const start = performance.now(); const parsed = await validateFile(file); const elapsed = performance.now() - start;
        rows.push({ ext, bytes: file.size, elapsed_ms: elapsed, valid: parsed.valid, rowCount: parsed.rowCount, warnings: parsed.warnings, errors: parsed.errors, exactRawOutput: ext === 'csv' || await parsed.convertedFile.text() === urls.join('\n') });
      }
      return rows;
    });
    for (const row of result) { assert.ok(row.bytes < 10 * 1024 * 1024); assert.equal(row.rowCount, 5000); assert.equal(row.valid, true); assert.equal(row.exactRawOutput, true); }
    fs.writeFileSync(process.argv[2], JSON.stringify({ node: process.version, chromium: browser.version(), diagnosticOnly: true, rows: result }, null, 2)); console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
