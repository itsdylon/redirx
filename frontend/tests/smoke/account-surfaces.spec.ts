import { test, expect } from '@playwright/test';

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`free account key and settings recovery at ${viewport.width}px`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize(viewport);
    const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', full_name: 'Fixture User', plan: 'free' };
    const key = { id: '20000000-0000-4000-8000-000000000001', name: 'Fixture agent', key_prefix: 'rdx_fixture', created_at: '2026-10-16T00:00:00Z', revoked_at: null };
    let created = false;
    let createCalls = 0;
    let revokeCalls = 0;
    const unexpected: string[] = [];
    await page.addInitScript(user => {
      const expires = Math.floor(Date.now() / 1000) + 3600;
      const token = `${btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture-only`;
      localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture-refresh', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
    }, user);
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(baseURL!).origin) return route.abort('blockedbyclient');
      const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/auth/me') return json({ success: true, user });
      if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
      if (url.pathname === '/api/keys' && route.request().method() === 'GET') return json({ success: true, keys: created ? [key] : [] });
      if (url.pathname === '/api/keys' && route.request().method() === 'POST') {
        createCalls++; created = true;
        return json({ success: true, key: { ...key, key: 'rdx_fixture_only_not_a_real_credential' } }, 201);
      }
      if (url.pathname === `/api/keys/${key.id}` && route.request().method() === 'DELETE') {
        revokeCalls++; return json({ success: false, error: 'Fixture connection interrupted' }, 503);
      }
      if (url.pathname === '/api/email/preferences') return json({ error: 'Notification preferences unavailable' }, 503);
      if (url.pathname === '/api/billing/status') return json({ plan: 'free', manage_portal_available: false, agency: { has_subscription: false, usage_pages: 0 } });
      if (url.pathname === '/api/user/profile') return json({ success: true, profile: { ...user, company: 'Fixture company' } });
      if (url.pathname === '/api/user/sessions') return json({ success: true, pagination: 'cursor', sessions: [], total_count: 0, next_cursor: null, search: url.searchParams.get('q') || '' });
      if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture request' }, 500); }
      return route.continue();
    });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('/api-keys');
    await page.getByLabel('Name', { exact: true }).fill('Fixture agent');
    await page.getByRole('button', { name: 'Create key', exact: true }).click();
    await expect(page.getByText('rdx_fixture_only_not_a_real_credential', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Create key', exact: true })).toBeDisabled();
    expect(createCalls).toBe(1);
    await page.screenshot({ path: testInfo.outputPath('key-reveal.png'), fullPage: true });
    // Fixture-only Clipboard API: preserve two pending writes after the reveal
    // unmounts, then exercise both promise outcomes. No OS clipboard is touched.
    await page.evaluate(() => {
      const writes: Array<{ resolve: () => void; reject: (error: Error) => void }> = [];
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: () => new Promise<void>((resolve, reject) => writes.push({ resolve, reject })),
      } });
      (window as unknown as { finishRetiredCopies: () => Promise<void> }).finishRetiredCopies = async () => {
        if (writes.length !== 2) throw new Error('Expected two pending fixture clipboard writes');
        writes[0].resolve(); writes[1].reject(new Error('fixture clipboard unavailable'));
        await Promise.resolve();
      };
    });
    await page.getByRole('button', { name: 'Copy', exact: true }).focus();
    await page.keyboard.press('Enter');
    await page.getByRole('button', { name: 'Copy', exact: true }).click();
    await page.getByRole('button', { name: "I've saved it" }).click();
    await page.evaluate(() => (window as unknown as { finishRetiredCopies: () => Promise<void> }).finishRetiredCopies());
    await expect(page.getByText('Key copied', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Could not copy automatically — select the key and copy it', { exact: true })).toHaveCount(0);
    await expect(page.getByText('rdx_fixture_only_not_a_real_credential', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('button', { name: 'Delete key', exact: true }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await expect(page.getByRole('alertdialog').getByRole('alert')).toContainText('Fixture connection interrupted');
    expect(revokeCalls).toBe(1);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.goto('/settings?tab=notifications');
    await expect(page.getByRole('button', { name: 'Reload notification preferences' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save Notification Preferences' })).toHaveCount(0);
    await expect(page.getByRole('tab', { name: 'Notifications', exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath('settings-recovery.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.goto('/account');
    await expect(page.getByText('Fixture company', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('account.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    expect(unexpected).toEqual([]);
  });
}
