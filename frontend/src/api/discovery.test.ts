import { afterEach, describe, expect, it, vi } from 'vitest';
import { discoverSite } from './discovery';
vi.mock('./config', () => ({ API_BASE_URL: '', getAuthHeaders: () => ({}) }));
afterEach(() => vi.unstubAllGlobals());
describe('discovery request controls', () => {
  it.each([
    [undefined, { url: 'example.com', side: 'old' }],
    [null, { url: 'example.com', side: 'old', use_gsc: false }],
    ['sc-domain:example.com', { url: 'example.com', side: 'old', gsc_property: 'sc-domain:example.com' }],
  ])('preserves the explicit GSC choice %s', async (choice, expected) => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ success: true }) });
    vi.stubGlobal('fetch', fetcher);
    const controller = new AbortController();
    await discoverSite('example.com', 'old', choice as string | null | undefined, controller.signal);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(expected);
    expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal);
  });
  it('preserves cancellation rather than converting it to a network error', async () => {
    const abort = new DOMException('Cancelled', 'AbortError');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort));
    await expect(discoverSite('example.com')).rejects.toBe(abort);
  });
});
