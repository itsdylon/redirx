import { test, expect, type Page } from '@playwright/test';
import type { PivotMatch } from '../../src/api/pivot';

async function fixture(page: Page, baseURL: string) {
  const user = { id: '10000000-0000-4000-8000-000000000001', email: 'fixture@example.com', full_name: 'Fixture User', plan: 'free' };
  const rows: PivotMatch[] = Array.from({ length: 500 }, (_, i) => ({
    mapping_id: `row-${i}`, old_url: `https://old.example/services/page-${i}`, new_url: null,
    revision: 7, review_status: ['set_target', 'reject', 'unmatched', 'defer', 'needs_review'][i % 5],
    decision_target: i % 5 === 0 ? `https://new.example/approved-${i}` : null,
    traffic_observed: i < 20, traffic_clicks: i < 20 ? 1000 - i : null,
    jev_proposal: { target_url: i % 5 === 2 ? null : `https://new.example/proposed-${i}`,
      confidence: i === 499 ? .11 : i % 5 === 2 ? undefined : .8, pass: 1, seed_revision: 0, stale: i % 11 === 0 },
  }));
  const reads: { filter: string; limit: number; cursor: string | null }[] = [];
  const decisions: unknown[] = [];
  const errors: string[] = [], unexpected: string[] = [];
  let failBulk = true, failSave = true, seed = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(user => {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const token = `${btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))}.${btoa(JSON.stringify({ sub: user.id, exp: expires }))}.fixture-only`;
    localStorage.setItem('sb-127-auth-token', JSON.stringify({ access_token: token, refresh_token: 'fixture-refresh', token_type: 'bearer', expires_in: 3600, expires_at: expires, user: { ...user, aud: 'authenticated', role: 'authenticated', app_metadata: {}, user_metadata: {} } }));
  }, user);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    const envelope = (data: unknown) => ({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', error: null, data });
    if (url.origin !== new URL(baseURL).origin) return route.abort('blockedbyclient');
    if (url.pathname === '/api/auth/me') return json({ success: true, user });
    if (url.pathname === '/api/user/onboarding') return json({ success: true, onboarding_status: 'dismissed', onboarding_state: { steps: {} } });
    if (url.pathname === '/api/v2/migrations/fixture') return json(envelope({ migration: { id: 'fixture', name: '500-page services migration' }, run_id: 'run-fixture', run: { status: 'succeeded' }, jev: { engine: 'jev-url-v1', model: 'fixture-only', pass: 1, seed_revision: seed, limits: { old_pages: 500, new_pages: 2000, inventory_bytes: 2097152, passes: 3, new_runs_per_24h: 5 } } }));
    if (url.pathname.endsWith('/runs/run-fixture/matches')) {
      if (route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON(); decisions.push(body);
        expect(body.decisions).toHaveLength(1);
        const decision = body.decisions[0];
        expect(decision).toEqual({ mapping_id: 'row-499', expected_revision: 7, action: 'set_target', target_url: 'https://new.example/Corrected%2FCase?x=A%20B' });
        if (failSave) { failSave = false; return json({ ...envelope({}), error: { code: 'unavailable', message: 'Fixture save failed. Your decision was not saved.' } }, 503); }
        rows[499] = { ...rows[499], revision: 8, review_status: 'set_target', decision_target: decision.target_url };
        rows.forEach(row => { if (row.jev_proposal) row.jev_proposal.stale = true; }); seed++;
        return json(envelope({ outcomes: [{ code: 'ok' }] }));
      }
      const filter = url.searchParams.get('filter') || 'all', limit = Number(url.searchParams.get('limit'));
      const cursor = url.searchParams.get('cursor'); reads.push({ filter, limit, cursor });
      if (limit === 500 && failBulk) { failBulk = false; return json({ ...envelope({}), error: { code: 'unavailable', message: 'Fixture full-list read failed. Try again.' } }, 503); }
      const selected = rows.filter(row => filter === 'all' || (filter === 'needs_review' && ['needs_review', 'defer'].includes(row.review_status)) || (filter === 'unmatched' && row.review_status === 'unmatched') || (filter === 'approved' && row.review_status === 'set_target'));
      const offset = cursor ? Number(cursor.slice('opaque-page-'.length)) : 0;
      return json(envelope({ items: selected.slice(offset, offset + limit), next_cursor: offset + limit < selected.length ? `opaque-page-${offset + limit}` : null, selection_revision: seed }));
    }
    if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture request' }, 500); }
    return route.continue();
  });
  return { rows, reads, decisions, errors, unexpected };
}

for (const width of [1280, 390]) test(`500-row review, correction recovery and export comprehension at ${width}px`, async ({ page, baseURL }, info) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width, height: 900 });
  const observed = await fixture(page, baseURL!);
  await page.goto('/migrations/fixture');
  const mappings = page.getByRole('list', { name: 'Mapping decisions' });
  await expect(mappings.getByRole('listitem')).toHaveCount(20);
  await expect(page.getByText(/Find and ordering cover loaded mappings only/)).toBeVisible();
  const approved = mappings.getByRole('listitem').first();
  await expect(approved.getByText('Approved destination', { exact: true })).toBeVisible();
  await expect(approved.getByRole('button', { name: 'Confirm proposed destination' })).toBeDisabled();
  const draft = approved.getByRole('textbox'); await draft.fill('https://new.example/unsaved-draft');
  const bulk = page.getByRole('button', { name: 'Load up to 500 mappings for this filter' });
  await bulk.click(); await expect(page.getByRole('alert')).toHaveText('Fixture full-list read failed. Try again.');
  await expect(mappings.getByRole('listitem')).toHaveCount(20); await expect(draft).toHaveValue('https://new.example/unsaved-draft');
  await expect(page.getByText(/Find and ordering cover loaded mappings only/)).toBeVisible();
  await bulk.focus(); await page.keyboard.press('Enter');
  await expect(mappings.getByRole('listitem')).toHaveCount(500);
  await expect(page.getByText('500 shown. All saved mappings for this filter are loaded.')).toBeVisible();
  await expect(draft).toHaveValue('https://new.example/unsaved-draft');
  await page.getByLabel('Order loaded mappings').selectOption('estimate');
  await expect(mappings.getByRole('listitem').first()).toContainText('Unmatched.');
  await expect(mappings.getByRole('listitem').nth(100)).toContainText('https://old.example/services/page-499');
  expect(observed.decisions).toHaveLength(0);
  const find = page.getByRole('searchbox', { name: 'Find in loaded mappings' });
  await find.fill('page-499');
  const row = mappings.getByRole('listitem'); await expect(row).toHaveCount(1);
  await expect(row).toContainText('Model estimate: 11.0%');
  const target = row.getByRole('textbox'); await target.fill('https://new.example/Corrected%2FCase?x=A%20B');
  await target.press('Enter');
  const error = row.getByRole('alert'); await expect(error).toContainText('Your decision was not saved'); await expect(error).toBeFocused();
  await expect(target).toHaveValue('https://new.example/Corrected%2FCase?x=A%20B');
  await expect(page.getByText(/Destination approved for/)).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('failed-save.png'), fullPage: false });
  await row.getByRole('button', { name: 'Set destination', exact: true }).focus(); await page.keyboard.press('Enter');
  await expect(row.getByRole('status')).toContainText('Destination approved for'); await expect(row.getByRole('status')).toBeFocused();
  await expect(row.getByText('Approved destination', { exact: true })).toBeVisible();
  await expect(row).toContainText('Model proposal (does not replace your decision)');
  await expect(row.getByRole('button', { name: 'Confirm proposed destination' })).toBeDisabled();
  await expect(target).toHaveValue('');
  expect(observed.decisions).toHaveLength(2); expect(observed.decisions[0]).toEqual(observed.decisions[1]);
  expect(observed.reads.at(-1)).toEqual({ filter: 'all', limit: 500, cursor: null });
  await find.fill(''); await page.getByLabel('Order loaded mappings').selectOption('traffic');
  await expect(mappings.getByRole('listitem').first()).toContainText('https://old.example/services/page-0');
  await page.getByLabel('Show mappings').selectOption('needs_review');
  await expect(mappings.getByRole('listitem')).toHaveCount(20);
  await expect(page.getByLabel('Order loaded mappings')).toHaveValue('traffic');
  await expect(find).toHaveValue('');
  await expect(page.getByText(/Needs review includes deferred decisions. Check Unmatched separately/)).toBeVisible();
  await page.getByRole('link', { name: 'Jump to export guidance' }).focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: 'Prepare an export with your agent' })).toBeInViewport();
  await expect(page.getByText(/explicitly allow a partial export/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('export-guidance.png'), fullPage: false });
  expect(observed.errors).toEqual([]); expect(observed.unexpected).toEqual([]);
  await info.attach('review-evidence', { body: JSON.stringify({ width, reads: observed.reads, decisionAttempts: observed.decisions.length, pageErrors: observed.errors, unexpected: observed.unexpected }), contentType: 'application/json' });
});
