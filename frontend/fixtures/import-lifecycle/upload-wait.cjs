/* Native held fetch; the late-response arm deliberately ignores AbortSignal. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://127.0.0.1:4286';
const baseline = process.argv.includes('--baseline');
const output = path.resolve(process.env.UPLOAD_WAIT_OUTPUT_DIR || '../docs/research/upload-wait');
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch();
  const receipt = { baseline, node: process.version, cases: [], externalRequests: [], pageErrors: [] };
  try {
    for (const width of [1280, 390]) for (const scenario of baseline ? ['held'] : ['stop-native', 'stop-late', 'owner-late']) {
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      page.setDefaultTimeout(5000);
      let posts = 0, polls = 0, historyReads = 0, held;
      page.on('pageerror', error => receipt.pageErrors.push(error.message));
      await page.addInitScript(ignoreAbort => {
        const nativeFetch = window.fetch;
        window.uploadFixture = { aborted: 0, resolved: 0, rejected: 0 };
        window.fetch = async (input, init) => {
          if (!String(input).endsWith('/api/process')) return nativeFetch(input, init);
          init?.signal?.addEventListener('abort', () => window.uploadFixture.aborted++, { once: true });
          try {
            const response = await nativeFetch(input, ignoreAbort ? { ...init, signal: undefined } : init);
            window.uploadFixture.resolved++; return response;
          } catch (error) { window.uploadFixture.rejected++; throw error; }
        };
      }, scenario.endsWith('late'));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) { receipt.externalRequests.push(url.origin); return route.abort(); }
        const json = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
        if (url.pathname === '/api/process') { posts++; held = route; return; }
        if (url.pathname === '/api/gsc/status') return json({ success: true, connected: false, configured: false });
        if (url.pathname.includes('/status')) { polls++; return json({ success: true, status: 'processing' }); }
        if (url.pathname === '/api/user/sessions') {
          historyReads++;
          return json({ success: true, pagination: 'cursor', search: '', total_count: 1, next_cursor: null, sessions: [{ id: 'late-session', project_name: 'Committed despite stopped wait', created_at: '2026-10-16T12:00:00Z', status: 'processing', pipeline_type: 'url_only', source_session_id: null, total_mappings: 0, approved_mappings: 0 }] });
        }
        if (url.pathname.startsWith('/api/')) throw new Error(`Unexpected API ${url.pathname}`);
        return route.continue();
      });
      await page.goto(`${base}/fixtures/import-lifecycle/${scenario === 'owner-late' ? 'index' : 'upload-response'}.html`);
      await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
      for (const [label, name] of [['Old Site CSV', 'old'], ['New Site CSV', 'new']]) {
        await page.getByLabel(label, { exact: true }).setInputFiles({ name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(`https://${name}.example/a\nhttps://${name}.example/b`) });
        await page.getByRole('button', { name: `Remove ${name}.csv` }).waitFor();
      }
      await page.getByRole('button', { name: /Begin Quick Match/ }).click();
      await page.waitForFunction(() => document.body.innerText.includes('Preparing') || document.body.innerText.includes('Waiting for upload confirmation'));
      assert.equal(posts, 1); assert.ok(held);
      if (baseline) {
        await page.getByText('Analyzing and matching your URLs.', { exact: false }).waitFor();
        assert.equal(await page.getByRole('button', { name: 'Stop waiting' }).count(), 0);
        assert.equal(await page.getByRole('button', { name: 'Continue in background' }).count(), 1);
        await page.screenshot({ path: path.join(output, `before-${width}.png`), fullPage: true });
      } else {
        await page.getByRole('heading', { name: 'Waiting for upload confirmation' }).waitFor();
        assert.equal(await page.getByRole('button', { name: 'Continue in background' }).count(), 0);
        if (scenario === 'stop-late') await page.screenshot({ path: path.join(output, `waiting-${width}.png`), fullPage: true });
        if (scenario === 'owner-late') await page.getByRole('button', { name: 'Switch fixture owner' }).click();
        else {
          await page.getByRole('button', { name: 'Stop waiting' }).focus();
          await page.keyboard.press('Enter');
          await page.getByRole('alert').filter({ hasText: 'The upload was not confirmed.' }).waitFor();
          for (const name of ['old', 'new']) assert.equal(await page.getByRole('button', { name: `Remove ${name}.csv` }).count(), 1);
        }
        await page.waitForFunction(() => window.uploadFixture.aborted === 1);
        if (scenario === 'stop-native') await page.waitForFunction(() => window.uploadFixture.rejected === 1);
        await held.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, session_id: 'late-session', is_duplicate: false }) });
        if (scenario.endsWith('late')) await page.waitForFunction(() => window.uploadFixture.resolved === 1);
        // Flush React work after the observable fetch settlement without timing a sleep.
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        if (scenario === 'owner-late') {
          await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
          assert.equal(await page.getByRole('button', { name: 'Remove old.csv' }).count(), 0);
          assert.equal(await page.getByRole('alert').count(), 0);
          assert.equal(await page.getByRole('button', { name: /Begin Quick Match/ }).isDisabled(), true);
        } else {
          await page.getByRole('alert').filter({ hasText: 'The upload was not confirmed.' }).waitFor();
          if (scenario === 'stop-late') await page.screenshot({ path: path.join(output, `after-${width}.png`), fullPage: true });
          await page.getByRole('link', { name: 'Check Project History' }).click();
          await page.getByText('Committed despite stopped wait', { exact: true }).waitFor();
          assert.equal(historyReads, 1);
        }
      }
      assert.equal(posts, 1); assert.equal(polls, 0);
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      assert.equal(scrollWidth, width);
      receipt.cases.push({ width, scenario, posts, polls, historyReads, scrollWidth, transport: await page.evaluate(() => window.uploadFixture) });
      await page.close();
    }
    assert.deepEqual(receipt.externalRequests, []); assert.deepEqual(receipt.pageErrors, []);
    fs.writeFileSync(path.join(output, `${baseline ? 'before' : 'after'}.json`), JSON.stringify(receipt, null, 2) + '\n');
    console.log(JSON.stringify(receipt));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
