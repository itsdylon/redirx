import { test, expect } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`loads optional panels only when needed at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', full_name: 'Fixture User', plan: 'free' };
    await page.addInitScript(user => {
      const expires = Math.floor(Date.now() / 1000) + 3600;
      const token = `${btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture-only`;
      localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture-refresh', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
    }, user);
    const assets: string[] = [];
    const unexpected: string[] = [];
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(baseURL!).origin) return route.abort('blockedbyclient');
      const json = (body: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname.startsWith('/assets/')) assets.push(url.pathname);
      if (url.pathname === '/api/auth/me') return json({ success: true, user });
      if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
      if (url.pathname === '/api/billing/status') return json({ plan: 'free', manage_portal_available: false, agency: { has_subscription: false, usage_pages: 0 } });
      if (url.pathname === '/api/keys') return json({ success: true, keys: [] });
      if (url.pathname === '/api/email/preferences') return json({ success: true, preferences: {} });
      if (url.pathname.startsWith('/api/v2/migrations/')) {
        const legacy = url.pathname.endsWith('/legacy');
        return json({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null, data: {
          migration: { id: legacy ? 'legacy' : 'jev', name: 'Fixture migration' },
          ...(!legacy ? { jev: { engine: 'jev-url-v1', model: 'jev', pass: 1, seed_revision: 0, limits: { old_pages: 500, new_pages: 2000, inventory_bytes: 2097152, passes: 3, new_runs_per_24h: 5 } } } : {}),
        } });
      }
      if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture request' }); }
      return route.continue();
    });
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
    expect(assets.some(path => /DashboardLayout-|ApiKeysPanel-/.test(path))).toBe(false);
    const keysTab = page.getByRole('tab', { name: 'API keys', exact: true });
    await keysTab.focus();
    await keysTab.press('Enter');
    await expect(page.getByLabel('Name', { exact: true })).toBeVisible();
    expect(assets.some(path => path.includes('ApiKeysPanel-'))).toBe(true);
    await expect(keysTab).toBeFocused();
    assets.length = 0;
    await page.goto('/migrations/jev');
    await expect(page.getByRole('heading', { name: 'Free URL mapping', exact: true })).toBeVisible();
    expect(assets.some(path => path.includes('SearchConsolePanel-'))).toBe(false);
    await expect(page.getByRole('heading', { name: 'Search Console', exact: true })).toHaveCount(0);
    await page.goto('/migrations/legacy');
    await expect(page.getByRole('heading', { name: 'Search Console', exact: true })).toBeVisible();
    expect(assets.some(path => path.includes('SearchConsolePanel-'))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    expect(unexpected).toEqual([]);
  });
}
