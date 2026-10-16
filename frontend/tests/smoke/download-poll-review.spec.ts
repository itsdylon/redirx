import { test, expect, type Page } from '@playwright/test';

async function fixture(page: Page, baseURL: string) {
  const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', plan: 'free' };
  const errors: string[] = [], unexpected: string[] = [];
  await page.addInitScript(user => {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const token = `${btoa(JSON.stringify({ alg: 'HS256' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture`;
    localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
  }, user);
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== new URL(baseURL).origin) return route.abort();
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (url.pathname === '/api/auth/me') return json({ success: true, user });
    if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({}, 500); }
    return route.continue();
  });
  return { errors, unexpected };
}

for (const [width, denied] of [[1280, false], [390, false], [1280, true]] as const) test(`download completion with overlapping poll at ${width}px denied=${denied}`, async ({ page, baseURL }, info) => {
  await page.setViewportSize({ width, height: 900 });
  await page.clock.install();
  const { errors, unexpected } = await fixture(page, baseURL!);
  let summaryReads = 0;
  let heldPoll: import('@playwright/test').Route | undefined;
  let heldDownload: import('@playwright/test').Route | undefined;
  const summary = (poll: boolean) => ({ contract_version: '1.0.0', status: 'running', next_action: poll ? 'poll' : 'none', retry_after_seconds: 10, error: null,
    data: { migration: { id: 'stale', name: 'Refining migration with prior export' }, run_id: 'run', run: { status: 'running' },
      artifact: { artifact_id: 'immutable-snapshot', format: 'csv', included_count: 1, excluded_count: 0 } } });
  await page.route('**/api/v2/migrations/stale', async route => {
    summaryReads++;
    if (summaryReads === 1) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(summary(true)) });
    heldPoll = route;
  });
  await page.route('**/artifacts/immutable-snapshot', route => { heldDownload = route; });
  await page.goto('/migrations/stale');
  const button = page.getByRole('button', { name: 'Download redirects' });
  await expect(button).toBeEnabled();
  await button.focus(); await page.keyboard.press('Enter');
  await expect.poll(() => !!heldDownload).toBe(true);
  await page.clock.fastForward(10000);
  await expect.poll(() => !!heldPoll).toBe(true);
  await expect(page.getByText('Loading migration status…')).toBeVisible();
  const file = page.waitForEvent('download');
  await heldDownload!.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null,
    data: { artifact_id: 'immutable-snapshot', format: 'csv', content: 'old,new\n/a,/b\n' } }) });
  await file;
  await expect(page.getByText('Download prepared. Check your browser’s downloads.')).toBeVisible();
  await expect(button).toBeDisabled();
  await heldPoll!.fulfill({ status: denied ? 401 : 200, contentType: 'application/json', body: JSON.stringify(denied ? { ...summary(false), error: { code: 'reconnect_required', message: 'Sign in again before refreshing status.' } } : summary(false)) });
  if (denied) {
    await expect(page.getByRole('alert')).toContainText('Sign in again');
    await expect(button).toBeDisabled();
    await expect(button).not.toBeFocused();
  } else {
    await expect(button).toBeEnabled();
    await expect(button).toBeFocused({ timeout: 1000 });
  }
  expect(errors).toEqual([]); expect(unexpected).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('after.png') });
  await info.attach('overlapping-poll-focus', { body: JSON.stringify(await page.evaluate(() => ({ focus: document.activeElement?.tagName }))), contentType: 'application/json' });
});
