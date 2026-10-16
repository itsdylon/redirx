import { test, expect, type Page } from '@playwright/test';
import { captureAccessibilityAudit } from './accessibilityAudit';

async function fixtures(page: Page, baseURL: string) {
  const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', full_name: 'Fixture User', plan: 'free' };
  const key = { id: 'key-fixture', name: 'Fixture agent', key_prefix: 'rdx_fixture', created_at: '2026-10-16T00:00:00Z', revoked_at: null as string | null };
  const errors: string[] = [];
  const unexpected: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(user => {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const token = `${btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture-only`;
    localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture-refresh', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw new Error('Fixture clipboard denied'); } } });
  }, user);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.origin === 'http://127.0.0.1:54321' && url.pathname === '/auth/v1/oauth/authorizations/fixture') return json({ authorization_id: 'fixture', client: { id: 'fixture-client', name: 'Fixture MCP client', uri: 'https://client.example' }, user, scope: 'openid profile email' });
    if (url.origin === 'http://127.0.0.1:54321' && url.pathname.endsWith('/fixture/consent')) return json({ message: 'Fixture authorization could not complete', error_code: 'invalid_request' }, 400);
    if (url.origin !== new URL(baseURL).origin) return route.abort('blockedbyclient');
    if (url.pathname === '/api/auth/me') return json({ success: true, user });
    if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
    if (url.pathname === '/api/keys' && route.request().method() === 'GET') return json({ success: true, keys: [key] });
    if (url.pathname === '/api/keys' && route.request().method() === 'POST') return json({ success: true, key: { ...key, key: 'rdx_fixture_keyboard_only_not_a_real_credential' } }, 201);
    if (url.pathname === `/api/keys/${key.id}`) { key.revoked_at = '2026-10-16T00:00:00Z'; return json({ success: true }); }
    if (url.pathname === '/api/email/preferences') return json({ error: 'Fixture preferences unavailable' }, 503);
    if (url.pathname === '/api/billing/status') return json({ plan: 'free', manage_portal_available: false, agency: { has_subscription: false, usage_pages: 0 } });
    if (url.pathname === '/api/v2/migrations') return json({ contract_version: '1.0.0', error: null, data: { items: [], next_cursor: null } });
    if (url.pathname === '/api/v2/migrations/fixture') return json({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null, data: { migration: { id: 'fixture', name: 'Fixture migration' }, jev: { engine: 'jev-url-v1', model: 'jev', pass: 1, seed_revision: 0, limits: { old_pages: 500, new_pages: 2000, inventory_bytes: 2097152, passes: 3, new_runs_per_24h: 5 } } } });
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture request' }, 500); }
    return route.continue();
  });
  return { errors, unexpected };
}

for (const width of [1280, 390]) {
  test(`API key reveal and dialog retain keyboard access at ${width}px`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const observed = await fixtures(page, baseURL!);
    await page.goto('/api-keys');
    const remove = page.getByRole('button', { name: 'Delete', exact: true });
    await remove.focus(); await page.keyboard.press('Enter');
    const dialog = page.getByRole('alertdialog');
    await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(dialog).toHaveCount(0);
    await expect(remove).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(remove).toBeFocused();
    await page.getByLabel('Name', { exact: true }).fill('Fixture agent');
    await page.getByRole('button', { name: 'Create key', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: /Copy your key now/ })).toBeFocused();
    await page.keyboard.press('Tab');
    const secret = page.getByRole('textbox', { name: 'New API key', exact: true });
    await expect(secret).toBeFocused();
    const mod = await page.evaluate(() => /Mac/.test(navigator.platform)) ? 'Meta' : 'Control';
    await page.keyboard.press(`${mod}+a`);
    expect(await secret.evaluate(element => { const field = element as HTMLTextAreaElement; return field.value.slice(field.selectionStart, field.selectionEnd); })).toBe('rdx_fixture_keyboard_only_not_a_real_credential');
    await page.keyboard.press('Tab'); await page.keyboard.press('Enter');
    await expect(page.getByText(/Could not copy automatically/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('key-reveal.png'), fullPage: true });
    await page.getByRole('button', { name: "I've saved it" }).focus(); await page.keyboard.press('Enter');
    await expect(secret).toHaveCount(0);
    await expect(page.getByLabel('Name', { exact: true })).toBeFocused();
    await remove.focus(); await page.keyboard.press('Enter');
    for (let i = 0; i < 5; i++) { await page.keyboard.press('Tab'); expect(await page.evaluate(() => !!document.activeElement?.closest('[role="alertdialog"]'))).toBe(true); }
    await dialog.getByRole('button', { name: 'Delete key', exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(dialog).toHaveCount(0);
    await expect(page.getByLabel('Name', { exact: true })).toBeFocused();
    expect(observed).toEqual({ errors: [], unexpected: [] });
  });

  test(`consent recovery is announced and remains keyboard reachable at ${width}px`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const observed = await fixtures(page, baseURL!);
    await page.goto('/oauth/consent?authorization_id=fixture');
    await expect(page.getByRole('heading', { name: 'Connect Fixture MCP client' })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Decline', exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Approve connection', exact: true })).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: 'Unable to load authorization' })).toBeFocused();
    await expect(page.getByRole('alert')).toContainText('Fixture authorization could not complete');
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Reload authorization request' })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath('consent-recovery.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(observed).toEqual({ errors: [], unexpected: [] });
  });
}

test('optional automated audit records primary route findings', async ({ page, baseURL }, testInfo) => {
  test.skip(!process.env.REDIRX_AXE_PATH, 'Provide an isolated axe-core script for supplemental audit evidence.');
  const observed = await fixtures(page, baseURL!);
  const routes = [
    ['/companion', 'Your migrations'], ['/migrations/fixture', 'Free URL mapping'],
    ['/settings?tab=notifications', 'Settings'], ['/api-keys', 'API keys'],
    ['/oauth/consent?authorization_id=fixture', 'Connect Fixture MCP client'],
  ];
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, heading] of routes) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading, exact: true }).first()).toBeVisible();
      const name = `${width}-${path.split('?')[0].replaceAll('/', '-')}`;
      await captureAccessibilityAudit(page, testInfo, name);
      await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
    }
  }
  expect(observed).toEqual({ errors: [], unexpected: [] });
});
