import { test, expect } from '@playwright/test';

// No authenticated session, API fixtures, provider calls or production credentials.
// Authenticated interactions are covered by component and OAuth protocol suites.
test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === 'http://127.0.0.1:4173'
      ? route.continue() : route.abort('blockedbyclient');
  });
});

for (const path of ['/companion', '/migrations/10000000-0000-0000-0000-000000000001?tab=review',
  '/billing/current', '/billing/current/return?checkout_id=70000000-0000-0000-0000-000000000001']) {
  test(`protected deep link retains its destination: ${path}`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Login to Redirx' })).toBeVisible();
    const login = new URL(page.url());
    expect(login.pathname).toBe('/login');
    expect(login.searchParams.get('redirect')).toBe(path);
    await page.reload();
    await expect(page.getByRole('button', { name: 'Continue with Google' })).toBeVisible();
    expect(new URL(page.url()).searchParams.get('redirect')).toBe(path);
    expect(errors).toEqual([]);
  });
}

test('incomplete consent link fails visibly without approving access', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/oauth/consent');
  await expect(page.getByRole('heading', { name: 'Authorization link is incomplete' })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Approve/ })).toHaveCount(0);
  expect(errors).toEqual([]);
});
