import { test, expect } from '@playwright/test';

// Fixture-only browsers: every non-local request is blocked, including auth.
test.beforeEach(async ({ page, baseURL }) => {
  await page.route('**/*', route => new URL(route.request().url()).origin === new URL(baseURL!).origin
    ? route.continue() : route.abort('blockedbyclient'));
});

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`failed callback retains recovery destination and accessible forms at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const path = '/migrations/10000000-0000-0000-0000-000000000001?tab=review';
    await page.addInitScript(redirect => localStorage.setItem('auth_redirect', redirect), path);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('/auth/callback?error=access_denied&error_description=Sign-in+was+canceled.+Your+migration+is+saved.');
    await expect(page.getByRole('heading', { name: 'Sign-in Failed' })).toBeVisible();
    await expect(page.getByRole('alert')).toContainText('Your migration is saved');
    await page.screenshot({ path: testInfo.outputPath('callback.png'), fullPage: true });
    await page.getByRole('button', { name: 'Try Logging In Again' }).click();
    expect(new URL(page.url()).searchParams.get('redirect')).toBe(path);
    await page.getByLabel('Email', { exact: true }).fill('fixture@example.com');
    await page.getByLabel('Password', { exact: true }).fill('fixture-password');
    await page.screenshot({ path: testInfo.outputPath('login.png'), fullPage: true });
    await page.getByRole('link', { name: 'Sign up', exact: true }).click();
    expect(new URL(page.url()).searchParams.get('redirect')).toBe(path);
    await expect(page.getByLabel('Full Name', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Confirm Password', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('signup.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}
