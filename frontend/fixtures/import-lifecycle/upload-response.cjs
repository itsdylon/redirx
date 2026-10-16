/* Actual UploadPage, native fetch/Response, validation and loading screen; fixture auth/layout only. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://127.0.0.1:4286';
const baseline = process.argv.includes('--baseline');
const output = path.resolve(process.env.UPLOAD_RESPONSE_OUTPUT_DIR || '../docs/research/import-response');
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch();
  const receipt = { baseline, externalRequests: [], pageErrors: [], cases: [] };
  try {
    for (const width of [1280, 390]) {
      for (const scenario of (baseline ? ['validation', 'unconfirmed', 'queued', 'duplicate'] : ['validation', 'unconfirmed', 'invalid-json', 'network', 'queued', 'duplicate'])) {
        const page = await browser.newPage({ viewport: { width, height: 1000 } });
        page.setDefaultTimeout(5000);
        let posts = 0, polls = 0, historyReads = 0;
        page.on('pageerror', error => receipt.pageErrors.push(error.message));
        await page.route('**/*', route => {
          const url = new URL(route.request().url());
          if (url.origin !== base) { receipt.externalRequests.push(url.origin); return route.abort(); }
          const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
          if (url.pathname === '/api/gsc/status') return json({ success: true, connected: false, configured: false });
          if (url.pathname === '/api/process') {
            posts++;
            assert.equal(route.request().method(), 'POST');
            if (scenario === 'validation') return json({ success: false, error: 'No valid URLs found in the CSV file.' }, 400);
            if (scenario === 'unconfirmed') return json({ success: true });
            if (scenario === 'invalid-json') return route.fulfill({ contentType: 'application/json', body: '{"success":true,' });
            if (scenario === 'network') return route.abort('connectionreset');
            return json({ success: true, session_id: 'fixture-session', is_duplicate: scenario === 'duplicate', pipeline_type: 'url_only' });
          }
          if (url.pathname === '/api/user/sessions/fixture-session/status') {
            polls++;
            return json({ success: true, session_id: 'fixture-session', status: 'processing', project_name: 'Fixture', total_mappings: 0, current_stage: 1, stage_name: 'Fixture job accepted', total_stages: 3 });
          }
          if (url.pathname === '/api/user/sessions') {
            historyReads++;
            return json({ success: true, pagination: 'cursor', search: '', total_count: 1, next_cursor: null, sessions: [{ id: 'fixture-session', project_name: 'Committed fixture project', created_at: '2026-10-16T12:00:00Z', status: 'processing', pipeline_type: 'url_only', source_session_id: null, total_mappings: 0, approved_mappings: 0 }] });
          }
          if (url.pathname.startsWith('/api/')) throw new Error(`Unexpected API request: ${url.pathname}`);
          return route.continue();
        });
        await page.goto(`${base}/fixtures/import-lifecycle/${baseline ? 'index' : 'upload-response'}.html`);
        await page.getByRole('button', { name: 'Import a CSV or sitemap file' }).click();
        for (const [label, name] of [['Old Site CSV', 'old'], ['New Site CSV', 'new']]) {
          await page.getByLabel(label, { exact: true }).setInputFiles({ name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(`https://${name}.example/a\nhttps://${name}.example/b`) });
          await page.getByRole('button', { name: `Remove ${name}.csv` }).waitFor();
        }
        await page.getByRole('button', { name: /Begin Quick Match/ }).click();
        if (scenario === 'validation') {
          await page.getByText(baseline ? 'Please check your request and try again.' : 'No valid URLs found in the CSV file.', { exact: true }).waitFor();
          assert.equal(await page.getByRole('button', { name: /Begin Quick Match/ }).isEnabled(), true);
        } else if (['unconfirmed', 'invalid-json', 'network'].includes(scenario)) {
          if (baseline) {
            await page.getByText('Analyzing and matching your URLs.', { exact: false }).waitFor();
            assert.equal(await page.getByRole('button', { name: /Begin Quick Match/ }).count(), 0);
          } else {
            await page.getByText(/not confirmed.*Project History/i).waitFor();
            assert.equal(await page.getByRole('button', { name: /Begin Quick Match/ }).isEnabled(), true);
            assert.equal(await page.getByRole('button', { name: 'Remove old.csv' }).count(), 1);
            assert.equal(await page.getByRole('button', { name: 'Remove new.csv' }).count(), 1);
          }
          if (scenario === 'unconfirmed') await page.screenshot({ path: path.join(output, `${baseline ? 'before' : 'after'}-${width}.png`), fullPage: true });
          if (!baseline) {
            await page.getByRole('link', { name: 'Check Project History' }).focus();
            await page.keyboard.press('Enter');
            await page.waitForURL('**/projects');
            await page.getByText('Committed fixture project', { exact: true }).waitFor();
            assert.equal(historyReads, 1);
          }
        } else if (scenario === 'queued') {
          await page.getByText('Fixture job accepted...', { exact: true }).waitFor();
          assert.ok(polls >= 1);
        } else {
          await page.getByText('Duplicate Request Detected').waitFor();
          await page.getByRole('button', { name: 'View Existing Results' }).click();
          await page.waitForURL('**/review/fixture-session');
        }
        assert.equal(posts, 1);
        if (scenario !== 'queued') assert.equal(polls, 0);
        const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
        assert.equal(scrollWidth, width);
        receipt.cases.push({ width, scenario, posts, polls, historyReads, scrollWidth });
        await page.close();
      }
    }
    assert.deepEqual(receipt.externalRequests, []); assert.deepEqual(receipt.pageErrors, []);
    fs.writeFileSync(path.join(output, `${baseline ? 'before' : 'after'}.json`), JSON.stringify(receipt, null, 2) + '\n');
    console.log(JSON.stringify(receipt));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
