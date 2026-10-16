import { test, expect, type Page } from '@playwright/test';

async function fixture(page: Page, baseURL: string) {
  const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', full_name: 'Fixture User', plan: 'free' };
  const unexpected: string[] = [], blocked: string[] = [];
  let blockedChunk = '', release: (() => void) | undefined;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.addInitScript(user => {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const token = `${btoa(JSON.stringify({ alg: 'HS256' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture`;
    localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
  }, user);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== new URL(baseURL).origin) return route.abort('blockedbyclient');
    if (blockedChunk && url.pathname.includes(`/assets/${blockedChunk}-`)) {
      blocked.push(url.pathname);
      if (blockedChunk === 'SearchConsolePanel') await held;
      return route.abort('failed');
    }
    const json = (data: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    const envelope = (data: unknown) => ({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null, data });
    if (url.pathname === '/api/auth/me') return json({ success: true, user });
    if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
    if (url.pathname === '/api/billing/status') return json({ plan: 'free', manage_portal_available: false, agency: { has_subscription: false, usage_pages: 0 } });
    if (url.pathname === '/api/email/preferences') return json({ success: true, preferences: {} });
    if (url.pathname === '/api/keys') return json({ success: true, keys: [] });
    if (url.pathname === '/api/v2/migrations') return json(envelope({ items: [], next_cursor: null }));
    if (url.pathname.endsWith('/matches')) return json(envelope({ items: [{ mapping_id: 'mapping', old_url: 'https://old.example/source', new_url: 'https://new.example/saved', revision: 3, review_status: 'needs_review', traffic_observed: false, traffic_clicks: null }], selection_revision: 0, next_cursor: null }));
    if (url.pathname.startsWith('/api/v2/migrations/')) return json(envelope({ migration: { id: 'fixture', name: 'Fixture migration' }, run_id: 'run', run: { status: 'succeeded' }, ...(url.pathname.endsWith('/legacy') ? {} : { jev: { engine: 'jev-url-v1', model: 'fixture', pass: 1, seed_revision: 0, limits: { old_pages: 500, new_pages: 2000, passes: 3 } } }) }));
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture request' }); }
    return route.continue();
  });
  return { unexpected, blocked, fail: (name: string) => { blockedChunk = name; }, restore: () => { blockedChunk = ''; }, release: () => release!() };
}

for (const width of [1280, 390]) {
  test(`failed route chunk reloads the exact signed-in deep link at ${width}px`, async ({ page, baseURL }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const f = await fixture(page, baseURL!); f.fail('PivotMigrationDetail');
    await page.goto('/migrations/jev?return=fixture#review');
    await expect(page.getByRole('heading', { name: 'This page could not load' })).toBeVisible();
    await expect(page.getByRole('alert')).toContainText('Reload the page');
    await expect(page.getByText(/Reloading discards unsaved edits/)).toBeVisible();
    expect(f.blocked.length).toBeGreaterThan(0);
    await page.screenshot({ path: info.outputPath('route-failure.png') });
    f.restore();
    await page.getByRole('button', { name: 'Reload page' }).focus(); await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: 'Free URL mapping' })).toBeVisible();
    await expect(page).toHaveURL(/\/migrations\/jev\?return=fixture#review$/);
    expect(f.unexpected).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });

  test(`optional chunk failure preserves surrounding correction draft at ${width}px`, async ({ page, baseURL }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const f = await fixture(page, baseURL!); f.fail('SearchConsolePanel');
    await page.goto('/migrations/legacy');
    const target = page.getByRole('textbox', { name: 'Destination for https://old.example/source' });
    await target.fill('https://new.example/Unsaved%2FDraft');
    f.release();
    await expect(page.getByRole('heading', { name: 'Search Console could not load' })).toBeVisible();
    await expect(target).toHaveValue('https://new.example/Unsaved%2FDraft');
    await expect(page.getByRole('button', { name: 'Set destination', exact: true })).toBeEnabled();
    await expect(page.getByRole('heading', { name: 'This page could not load' })).toHaveCount(0);
    await page.screenshot({ path: info.outputPath('optional-failure.png') });
    f.restore(); await page.getByRole('button', { name: 'Reload page' }).click();
    await expect(page.getByRole('heading', { name: 'Search Console', exact: true })).toBeVisible();
    await expect(target).toHaveValue('');
    expect(f.unexpected).toEqual([]);
  });
}

test('API-key chunk failure leaves Settings navigable and reloads the selected tab', async ({ page, baseURL }) => {
  const f = await fixture(page, baseURL!); f.fail('ApiKeysPanel');
  await page.goto('/settings?tab=api-keys');
  await expect(page.getByRole('heading', { name: 'API keys could not load' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Profile', exact: true })).toBeEnabled();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  f.restore(); await page.getByRole('button', { name: 'Reload page' }).click();
  await expect(page.getByLabel('Name', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/settings\?tab=api-keys$/);
  expect(f.unexpected).toEqual([]);
});

test('a failed optional import arriving after navigation does not replace the new route', async ({ page, baseURL }) => {
  const f = await fixture(page, baseURL!); f.fail('SearchConsolePanel');
  await page.goto('/migrations/legacy');
  await expect(page.getByRole('textbox', { name: 'Destination for https://old.example/source' })).toBeVisible();
  await page.getByRole('link', { name: 'All migrations', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your migrations' })).toBeVisible();
  const failure = page.waitForEvent('requestfailed', { predicate: request => request.url().includes('SearchConsolePanel-') });
  f.release(); await failure;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByRole('heading', { name: 'Your migrations' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /could not load/ })).toHaveCount(0);
  expect(f.unexpected).toEqual([]);
});
