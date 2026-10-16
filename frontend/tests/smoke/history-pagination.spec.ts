import { test, expect } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`whole-history search, paging and retry at ${width}px`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const user = { id: '10000000-0000-4000-8000-000000000001', email: 'history@fixture.invalid', full_name: 'History Fixture', plan: 'free' };
    const special = 'Needle 100%_\\ café';
    const rows = Array.from({ length: 1205 }, (_, i) => ({ id: `20000000-0000-4000-8000-${String(i + 1).padStart(12, '0')}`,
      project_name: i === 1204 ? special : `Historical project ${i + 1}`, created_at: '2026-01-01T00:00:00.123456Z',
      total_mappings: 15, approved_mappings: 5, pipeline_type: 'url_only', status: 'completed', source_session_id: null }));
    let failNext = true;
    const unexpected: string[] = [];
    const requests: URL[] = [];
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
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
      if (url.pathname === '/api/user/sessions') {
        requests.push(url);
        if (url.searchParams.get('pagination') !== 'cursor') { unexpected.push('unbounded list'); return json({}, 500); }
        const search = url.searchParams.get('q') || '';
        const cursor = url.searchParams.get('cursor');
        if (cursor && failNext) { failNext = false; return json({ error: 'Fixture page connection interrupted' }, 503); }
        const offset = cursor ? Number(cursor.split(':')[1]) : 0;
        const filtered = rows.filter(row => `${row.project_name} ${row.status} ${row.pipeline_type}`.toLowerCase().includes(search.toLowerCase()));
        const size = Number(url.searchParams.get('limit'));
        return json({ success: true, pagination: 'cursor', search, total_count: filtered.length,
          sessions: filtered.slice(offset, offset + size), next_cursor: offset + size < filtered.length ? `opaque:${offset + size}:.123456` : null });
      }
      if (url.pathname.startsWith('/api/user/sessions/') && route.request().method() === 'PUT') {
        const row = rows.find(row => row.id === url.pathname.split('/').at(-1))!;
        row.project_name = route.request().postDataJSON().project_name;
        return json({ success: true, session: row });
      }
      if (url.pathname.startsWith('/api/')) { unexpected.push(url.pathname); return json({ error: 'Unexpected fixture API' }, 500); }
      return route.continue();
    });
    await page.goto('/projects');
    await expect(page.getByText('1,205 projects · Page 1', { exact: true })).toBeVisible();
    expect(await page.getByRole('button', { name: 'Open Results' }).count()).toBe(50);
    expect(requests).toHaveLength(1);
    await page.screenshot({ path: testInfo.outputPath(`history-${width}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width === 390) {
      const table = page.getByRole('region', { name: 'Project history table' });
      await table.focus(); await page.keyboard.press('ArrowRight');
      await expect.poll(() => table.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
    }
    await page.getByRole('button', { name: 'Next page' }).click();
    await expect(page.getByRole('alert')).toContainText('Fixture page connection interrupted');
    await expect(page.getByText('No projects match your search.')).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`history-retry-${width}.png`) });
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByText('Historical project 51', { exact: true })).toBeVisible();
    expect(requests.at(-1)?.searchParams.get('cursor')).toBe('opaque:50:.123456');
    const search = page.getByRole('textbox', { name: 'Search project history' });
    await search.fill(special); await search.press('Enter');
    await expect(page.getByText('1 matching project · Page 1', { exact: true })).toBeVisible();
    await expect(page.getByText(special, { exact: true })).toBeVisible();
    expect(requests.at(-1)?.searchParams.get('q')).toBe(special);
    expect(requests.at(-1)?.searchParams.has('cursor')).toBe(false);
    await page.getByRole('button', { name: 'Edit project name' }).click();
    await page.getByRole('textbox', { name: 'Project name', exact: true }).fill('Renamed outside the active search');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByText('No projects match your search.')).toBeVisible();
    await expect(page.getByText('0 matching projects · Page 1', { exact: true })).toBeVisible();
    await search.fill('Renamed outside'); await search.press('Enter');
    await expect(page.getByText('Renamed outside the active search', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`history-search-${width}.png`) });
    expect(errors).toEqual([]); expect(unexpected).toEqual([]);
  });
}
