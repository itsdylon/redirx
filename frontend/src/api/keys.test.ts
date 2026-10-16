import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiKey, listApiKeys, revokeApiKey } from './keys';

const meta = { id: 'key-1', name: 'Fixture', key_prefix: 'rdx_fixture', created_at: '2026-10-16' };
describe('API key response evidence', () => {
  const request = vi.fn();
  beforeEach(() => { request.mockReset(); vi.stubGlobal('fetch', request); });
  afterEach(() => vi.unstubAllGlobals());
  it('does not turn an incomplete list response into no keys', async () => {
    request.mockResolvedValue(new Response(JSON.stringify({ success: true })));
    await expect(listApiKeys()).rejects.toThrow('could not be verified');
  });
  it('keeps unexpected plaintext out of the reusable inventory response', async () => {
    request.mockResolvedValueOnce(new Response(JSON.stringify({ success: true, keys: [{ ...meta, key: 'rdx_should_not_be_cached' }] })));
    const keys = await listApiKeys();
    expect(keys[0]).toMatchObject(meta);
    expect(keys[0]).not.toHaveProperty('key');
  });
  it('requires the one-time secret and explicit creation success', async () => {
    request.mockResolvedValueOnce(new Response(JSON.stringify({ success: true, key: meta })));
    await expect(createApiKey('Fixture')).rejects.toThrow('may already exist');
    request.mockResolvedValueOnce(new Response(JSON.stringify({ success: true, key: { ...meta, key: 'rdx_once' } })));
    await expect(createApiKey('Fixture')).resolves.toMatchObject({ key: 'rdx_once' });
  });
  it('does not show a successful deletion toast for a false result', async () => {
    request.mockResolvedValueOnce(new Response(JSON.stringify({ success: false })));
    await expect(revokeApiKey('key/with?reserved')).rejects.toThrow('not confirmed');
    expect(request.mock.calls[0][0]).toContain('key%2Fwith%3Freserved');
  });
  it('retains structured unauthorized errors for session recovery', async () => {
    request.mockResolvedValueOnce(new Response(JSON.stringify({ error: 'Sign in again', code: 'unauthorized' }), { status: 401 }));
    await expect(listApiKeys()).rejects.toMatchObject({ status: 401 });
  });
});
