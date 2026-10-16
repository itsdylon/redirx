const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const out = path.resolve('../docs/research/product-rd-20261015/notifications');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = []; const writes = []; const views = [];
  page.on('pageerror', error => errors.push(error.message));
  let preferences = ['mapping_complete', 'mapping_failed', 'watch_alert', 'onboard_nudge'].map(email_type => ({ email_type, opted_out: false }));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4286') return route.abort();
    if (url.pathname === '/api/email/preferences') {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON(); writes.push(body);
        preferences = preferences.map(p => p.email_type === body.email_type ? body : p);
        return route.fulfill({ json: { success: true, ...body } });
      }
      return route.fulfill({ json: { preferences } });
    }
    if (url.pathname.startsWith('/api/billing/')) return route.fulfill({ json: { plan: 'free', manage_portal_available: false, agency: { has_subscription: false } } });
    return route.continue();
  });
  const templates = fs.readdirSync(out).filter(name => name.endsWith('.html'));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const name of templates) {
      await page.setContent(fs.readFileSync(path.join(out, name), 'utf8'));
      const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
      assert.equal(metrics.scrollWidth, width, name);
      views.push({ name, ...metrics });
      if (['watch-traffic.html', 'unsubscribe-confirm.html'].includes(name)) await page.screenshot({ path: path.join(out, `${name.replace('.html', '')}-${width}.png`), fullPage: true });
    }
    await page.goto('http://127.0.0.1:4286/fixtures/notifications/index.html?tab=notifications');
    await page.getByRole('switch', { name: 'Monitoring Alert Emails' }).waitFor();
    await page.screenshot({ path: path.join(out, `settings-${width}.png`), fullPage: true });
    const metrics = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    assert.equal(metrics.scrollWidth, width, 'settings');
    views.push({ name: 'settings', ...metrics });
  }
  await page.getByRole('switch', { name: 'Monitoring Alert Emails' }).focus(); await page.keyboard.press('Space');
  await page.getByRole('switch', { name: 'Onboarding Tip Emails' }).focus(); await page.keyboard.press('Space');
  await page.getByRole('button', { name: 'Save Notification Preferences' }).click();
  await page.waitForFunction(() => document.querySelector('button') !== null);
  await page.waitForTimeout(100);
  assert.equal(writes.length, 4);
  assert.ok(writes.some(p => p.email_type === 'watch_alert' && p.opted_out === true));
  assert.ok(writes.some(p => p.email_type === 'onboard_nudge' && p.opted_out === true));
  assert.deepEqual(errors, []);
  const receipt = { views, keyboardOptionalOptOuts: true, writes, pageErrors: errors, realProviderCalls: 0 };
  fs.writeFileSync(path.join(out, 'browser.json'), JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify({ rendered: views.length, keyboardOptionalOptOuts: true, writes: writes.length, pageErrors: errors }));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
