import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

async function fixture(page: Page, baseURL: string) {
  const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', plan: 'free' };
  const state = { revision: 0, access: 'ok', downloadFailure: false, writes: [] as Record<string, unknown>[], reads: 0, downloads: 0, errors: [] as string[], unexpected: [] as string[] };
  const target = 'https://new.example/Typed%2FCase?x=A%20B';
  await page.addInitScript(user => {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const token = `${btoa(JSON.stringify({ alg: 'HS256' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture`;
    localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
  }, user);
  page.on('pageerror', error => state.errors.push(error.message));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== new URL(baseURL).origin) return route.abort();
    const envelope = (data: unknown) => ({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null, data });
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    const denied = () => json({ ...envelope({}), error: { code: state.access === 'expired' ? 'reconnect_required' : 'not_found', message: state.access === 'expired' ? 'Sign in again to access this migration.' : 'This migration was not found.' } }, state.access === 'expired' ? 401 : 404);
    if (url.pathname === '/api/auth/me') return json({ success: true, user });
    if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
    if (url.pathname.startsWith('/api/v2/') && state.access !== 'ok') return denied();
    if (url.pathname === '/api/v2/migrations/stale') return json(envelope({ migration: { id: 'stale', name: 'Long-open review' }, run_id: 'run', run: { status: 'succeeded' },
      artifact: { artifact_id: 'immutable-snapshot', format: 'csv', included_count: 500, excluded_count: 0 },
      jev: { engine: 'jev-url-v1', model: 'fixture', pass: 1, seed_revision: state.revision, limits: { old_pages: 500, new_pages: 2000, passes: 3 } } }));
    if (url.pathname.endsWith('/matches')) {
      if (route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON(); state.writes.push(body);
        expect(body.decisions[0]).toEqual({ mapping_id: 'row-499', expected_revision: state.revision, action: 'set_target', target_url: target });
        if (state.revision === 0) { state.revision = 1; return json({ ...envelope({ outcomes: [{ code: 'revision_conflict' }] }), status: 'partial' }); }
        state.revision++; return json(envelope({ outcomes: [{ code: 'ok' }] }));
      }
      state.reads++;
      const count = Number(url.searchParams.get('limit'));
      return json(envelope({ items: Array.from({ length: count }, (_, i) => ({ mapping_id: `row-${i}`, old_url: `https://old.example/page-${i}`, new_url: `https://new.example/original-${i}`,
        revision: i === 499 ? state.revision : 0, review_status: i === 499 && state.revision === 1 ? 'reject' : 'approve', traffic_observed: false, traffic_clicks: null,
        decision_target: i === 499 && state.revision === 2 ? target : undefined })), selection_revision: state.revision, next_cursor: count < 500 ? 'opaque' : null }));
    }
    if (url.pathname.endsWith('/artifacts/immutable-snapshot')) {
      if (state.downloadFailure) return json({ ...envelope({}), error: { code: 'temporarily_unavailable', message: 'Download temporarily unavailable. Try again.' } }, 503);
      state.downloads++; return json(envelope({ artifact_id: 'immutable-snapshot', format: 'csv', content: 'source,target\nhttps://old.example/page-499,https://new.example/original-499\n' }));
    }
    if (url.pathname.startsWith('/api/')) { state.unexpected.push(url.pathname); return json({}, 500); }
    return route.continue();
  });
  return { state, target };
}

for (const width of [1280, 390, 320]) test(`stale review recovery preserves drafts and immutable exports at ${width}px`, async ({ page, baseURL }, info) => {
  await page.setViewportSize({ width, height: 900 });
  const { state, target } = await fixture(page, baseURL!);
  await page.goto('/migrations/stale');
  await page.getByRole('button', { name: 'Load up to 500 mappings for this filter' }).click();
  const list = page.getByRole('list', { name: 'Mapping decisions' }); await expect(list.getByRole('listitem')).toHaveCount(500);
  await page.getByRole('searchbox', { name: 'Find in loaded mappings' }).fill('page-499');
  const row = list.getByRole('listitem'), input = row.getByRole('textbox');
  await input.fill(target); await input.press('Enter');
  await expect(row.getByRole('alert')).toContainText('This mapping changed'); await expect(row.getByRole('alert')).toBeFocused();
  await expect(row.getByRole('button', { name: 'Set destination' })).toBeDisabled(); await expect(input).toHaveValue(target);
  await expect(page.getByRole('button', { name: 'Download redirects' })).toBeEnabled();
  await page.screenshot({ path: info.outputPath('conflict.png'), fullPage: false });
  await row.getByRole('button', { name: 'Refresh mappings' }).focus(); await page.keyboard.press('Enter');
  await expect(row.getByRole('status')).toContainText('Current mappings loaded'); await expect(row.getByRole('status')).toBeFocused();
  await expect(row).toContainText('Rejected.'); await expect(input).toHaveValue(target);
  expect(state.writes).toHaveLength(1);
  await row.getByRole('button', { name: 'Set destination' }).click(); await expect(row.getByRole('status')).toContainText('Destination approved');
  expect(state.writes).toHaveLength(2); expect(state.writes[0].idempotency_key).not.toBe(state.writes[1].idempotency_key);
  const download = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download redirects' }).click();
  const file = await download; expect(file.suggestedFilename()).toBe('redirects-immutable-snapshot.csv');
  expect(await readFile((await file.path())!, 'utf8')).toBe('source,target\nhttps://old.example/page-499,https://new.example/original-499\n');
  expect(state.downloads).toBe(1);
  await input.fill('https://new.example/keep-this-draft');
  state.access = 'expired'; await page.getByRole('button', { name: 'Download redirects' }).click();
  await expect(page.getByRole('alert')).toContainText('Sign in again');
  await expect(page.getByRole('button', { name: 'Download redirects' })).toBeDisabled(); await expect(row.getByRole('button', { name: 'Set destination' })).toBeDisabled();
  await expect(page.getByRole('heading', { name: 'Refresh status before continuing' })).toBeVisible(); await expect(input).toHaveValue('https://new.example/keep-this-draft');
  state.access = 'ok'; await page.getByRole('button', { name: 'Refresh status' }).click();
  await expect(page.getByRole('button', { name: 'Download redirects' })).toBeEnabled(); await expect(input).toHaveValue('https://new.example/keep-this-draft'); expect(state.downloads).toBe(1);
  state.access = 'deleted'; await page.getByRole('button', { name: 'Download redirects' }).click();
  await expect(page.getByRole('alert')).toContainText('This migration was not found');
  await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('alert')).toContainText('This migration was not found');
  await expect(page.getByText('Last loaded status: succeeded')).toBeVisible(); await expect(input).toBeEnabled(); await expect(input).toHaveValue('https://new.example/keep-this-draft');
  await expect(row.getByRole('button', { name: 'Set destination' })).toBeDisabled(); expect(state.writes).toHaveLength(2); expect(state.downloads).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.errors).toEqual([]); expect(state.unexpected).toEqual([]);
  await info.attach('stale-authority-evidence', { body: JSON.stringify({ width, reads: state.reads, writes: state.writes.length, authorizedDownloads: state.downloads, errors: state.errors, unexpected: state.unexpected }), contentType: 'application/json' });
});

for (const width of [1280, 390, 320]) test(`download keyboard recovery stays at the artifact at ${width}px`, async ({ page, baseURL }, info) => {
  await page.setViewportSize({ width, height: 900 });
  const { state } = await fixture(page, baseURL!);
  await page.goto('/migrations/stale');
  const download = page.getByRole('button', { name: 'Download redirects' });
  await expect(download).toBeEnabled();
  state.downloadFailure = true;
  await download.focus(); await page.keyboard.press('Enter');
  const alert = page.getByRole('alert');
  await expect(alert).toContainText('Download temporarily unavailable');
  await info.attach('failure-focus', { body: JSON.stringify(await page.evaluate(() => ({ focus: document.activeElement?.outerHTML, alertTop: document.querySelector('[role="alert"]')?.getBoundingClientRect().top, height: innerHeight }))), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('download-failure.png') });
  await expect(alert).toBeFocused();
  await expect(alert).toBeInViewport();
  await page.keyboard.press('Tab'); await expect(download).toBeFocused();
  state.downloadFailure = false;
  const saved = page.waitForEvent('download'); await page.keyboard.press('Enter');
  const file = await saved;
  expect(await readFile((await file.path())!, 'utf8')).toContain('https://new.example/original-499');
  await expect(download).toBeFocused();
  expect(state.downloads).toBe(1); expect(state.writes).toHaveLength(0);
  state.access = 'expired'; await page.keyboard.press('Enter');
  await expect(alert).toContainText('Sign in again'); await expect(alert).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Go to migration status' })).toBeFocused();
  await page.keyboard.press('Enter');
  const refresh = page.getByRole('button', { name: 'Refresh status', exact: true });
  await expect(refresh).toBeFocused();
  state.access = 'ok'; await page.keyboard.press('Enter');
  await expect(download).toBeEnabled(); await expect(refresh).toBeFocused();
  await expect(page.getByRole('region', { name: 'Redirect artifact' }).getByRole('alert')).toHaveCount(0);
  expect(state.downloads).toBe(1); expect(state.writes).toHaveLength(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.errors).toEqual([]); expect(state.unexpected).toEqual([]);
});
