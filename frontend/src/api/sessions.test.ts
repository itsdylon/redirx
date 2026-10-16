import { afterEach, expect, it, vi } from 'vitest';
import { deleteSession, updateSessionName, fetchAllSessions, fetchSessionPage } from './sessions';
const reply = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());
it('requires explicit deletion acknowledgment even with a successful HTTP response', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => reply({ message: 'ok' })));
  await expect(deleteSession('s1')).rejects.toThrow('did not confirm');
});
it('requires the rename response to identify the changed project and saved name', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => reply({ success: true, session: { id: 'other', project_name: 'Changed' } })));
  await expect(updateSessionName('s1', 'Changed')).rejects.toThrow('did not confirm');
  vi.stubGlobal('fetch', vi.fn(async () => reply({ success: true, session: { id: 's1', project_name: 'Old' } })));
  await expect(updateSessionName('s1', 'Changed')).rejects.toThrow('did not confirm');
});
it('passes cancellation and rejects an incomplete session list', async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => reply({ success: true })); vi.stubGlobal('fetch', fetcher);
  const signal = new AbortController().signal;
  await expect(fetchAllSessions(signal)).rejects.toThrow('did not confirm');
  expect(fetcher.mock.calls[0][1]).toMatchObject({ signal });
});

it('passes the opaque cursor unchanged and encodes literal full-history search', async () => {
  const token = 'opaque-2026-01-01T00:00:00.123456Z';
  const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => reply({ success: true, pagination: 'cursor',
    sessions: [], total_count: 10, next_cursor: null, search: '100%_\\' }));
  vi.stubGlobal('fetch', fetcher);
  const signal = new AbortController().signal;
  await fetchSessionPage({ cursor: token, search: '100%_\\', limit: 50 }, signal);
  const url = new URL(String(fetcher.mock.calls[0][0]), 'https://fixture.invalid');
  expect(url.searchParams.get('cursor')).toBe(token);
  expect(url.searchParams.get('q')).toBe('100%_\\');
  expect(url.searchParams.get('pagination')).toBe('cursor');
  expect(fetcher.mock.calls[0][1]?.signal).toBe(signal);
});
it('rejects a partial or unlabeled page rather than showing false empty history', async () => {
  for (const data of [{ success: true, sessions: [] }, { success: true, pagination: 'cursor', sessions: [],
    total_count: 0, search: '', next_cursor: 'impossible' }]) {
    vi.stubGlobal('fetch', vi.fn(async () => reply(data)));
    await expect(fetchSessionPage()).rejects.toThrow('incomplete');
  }
});
it('retains HTTP status for authentication and paging recovery', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'Start again from the first page.' }), { status: 400 })));
  await expect(fetchSessionPage()).rejects.toMatchObject({ status: 400, message: 'Start again from the first page.' });
});

it('rejects invalid summary counts and missing projection fields', async () => {
  const row = { id: 's1', project_name: 'Fixture', created_at: null, status: 'completed',
    total_mappings: 1, approved_mappings: 0, pipeline_type: 'content', source_session_id: null };
  for (const broken of [{ ...row, total_mappings: -1 }, { ...row, approved_mappings: 0.5 },
    { ...row, total_mappings: Number.MAX_SAFE_INTEGER + 1 }, { ...row, pipeline_type: undefined }]) {
    vi.stubGlobal('fetch', vi.fn(async () => reply({ success: true, pagination: 'cursor', sessions: [broken],
      total_count: 1, next_cursor: null, search: '' })));
    await expect(fetchSessionPage()).rejects.toThrow('incomplete');
  }
});
