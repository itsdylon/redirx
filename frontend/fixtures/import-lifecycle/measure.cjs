/* Offline, warmed parser comparison. Serve this fixture with its Vite config.
 * node fixtures/import-lifecycle/measure.cjs /tmp/import-comparison.json [base-ref]
 * Captures wall time and event-loop timer gap, not field INP or peak memory. */
const { chromium } = require('playwright');
const XLSX = require('xlsx');
const fs = require('node:fs');
const cp = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const base = 'http://127.0.0.1:4286';
const baselinePath = path.join(__dirname, '.baseline-parser.ts');
function workbook(data) {
  const book = XLSX.utils.book_new(); XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet(data), 'URLs');
  return XLSX.write(book, { type: 'buffer', bookType: 'xlsx', compression: true });
}
function sparse(bytes) {
  const zip = XLSX.CFB.read(bytes, { type: 'buffer' });
  const name = zip.FullPaths.find(value => value.endsWith('/xl/worksheets/sheet1.xml'));
  const xml = Buffer.from(XLSX.CFB.find(zip, name).content).toString().replace('ref="A1:A2"', 'ref="A1:A1048576"');
  XLSX.CFB.utils.cfb_add(zip, name, Buffer.from(xml));
  return XLSX.CFB.write(zip, { type: 'buffer', fileType: 'zip', compression: true });
}
(async () => {
  const baselineRef = process.argv[3] || '01824ce';
  fs.writeFileSync(baselinePath, cp.execFileSync('git', ['show', `${baselineRef}:frontend/src/utils/fileParsers.ts`], { cwd: path.join(__dirname, '../../..') }));
  const warm = workbook([['url'], ['https://Example.com/Case%2FPath?x=A%20B']]);
  const fixtures = [
    { name: 'sparse-dimension', bytes: sparse(warm), count: 1 },
    { name: 'dense-single-column', bytes: workbook([['url'], ...Array.from({ length: 10000 }, (_, i) => [`https://Example.com/Page%2F${i}?x=A%20B`])]), count: 10000 },
    { name: 'wide-first-sheet', bytes: workbook([['url', ...Array(39).fill('ignored')], ...Array.from({ length: 5000 }, (_, i) => [`https://Example.com/Page%2F${i}?x=A%20B`, ...Array.from({ length: 39 }, (_, c) => `ignored-${i}-${c}`)])]), count: 5000 },
  ];
  const browser = await chromium.launch(); const rows = []; const expected = new Map();
  try {
    for (let repeat = 0; repeat < 6; repeat++) for (const variant of repeat % 2 ? ['candidate', 'baseline'] : ['baseline', 'candidate']) for (const fixture of fixtures) {
      assert.ok(fixture.bytes.length < 10 * 1024 * 1024);
      const page = await browser.newPage(); const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) return route.abort();
        if (url.pathname.startsWith('/api/')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, connected: false, configured: false, properties: [] }) });
        return route.continue();
      });
      await page.goto(`${base}/fixtures/import-lifecycle/index.html`);
      const result = await page.evaluate(async ({ variant, bytes, warm }) => {
        const module = await import(variant === 'baseline' ? '/fixtures/import-lifecycle/.baseline-parser.ts' : '/src/utils/fileParsers.ts');
        await module.parseXlsx(new File([new Uint8Array(warm)], 'warm.xlsx'));
        const file = new File([new Uint8Array(bytes)], 'measured.xlsx');
        let last = performance.now(), maxGap = 0;
        const timer = setInterval(() => { const now = performance.now(); maxGap = Math.max(maxGap, now - last); last = now; }, 10);
        await new Promise(resolve => setTimeout(resolve, 30));
        const start = performance.now(); const parsed = await module.parseXlsx(file); const elapsed = performance.now() - start;
        await new Promise(resolve => setTimeout(resolve, 30)); clearInterval(timer);
        return { elapsed_ms: elapsed, max_timer_gap_ms: maxGap, parsed };
      }, { variant, bytes: Array.from(fixture.bytes), warm: Array.from(warm) });
      assert.deepEqual(errors, []); assert.equal(result.parsed.urls.length, fixture.count);
      if (!expected.has(fixture.name)) expected.set(fixture.name, result.parsed);
      assert.deepEqual(result.parsed, expected.get(fixture.name));
      const { parsed, ...metrics } = result;
      rows.push({ variant, repeat, workload: fixture.name, bytes: fixture.bytes.length, urls: parsed.urls.length, ...metrics });
      await page.close();
    }
    const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    const receipt = { node: process.version, chromium: browser.version(), baseline: baselineRef, source_sha256: { baseline: hash(baselinePath), candidate: hash(path.join(__dirname, '../../src/utils/fileParsers.ts')) }, repeats: 6, exact_complete_result_parity: true, rows };
    fs.writeFileSync(process.argv[2], JSON.stringify(receipt, null, 2)); console.log(JSON.stringify(receipt));
  } finally { await browser.close(); fs.unlinkSync(baselinePath); }
})().catch(error => { console.error(error); process.exitCode = 1; });
