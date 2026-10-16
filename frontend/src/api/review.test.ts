import { afterEach, describe, expect, it, vi } from 'vitest';
import { getResults, persistReviewDecision } from './pipeline';
vi.mock('./config', () => ({ API_BASE_URL: '', getAuthHeaders: () => ({}), getAuthHeadersMultipart: () => ({}) }));
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('historical review acknowledgment', () => {
  it.each([{success:false}, {}, null])('rejects a 200 body without success: %j', async data => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data))); vi.stubGlobal('fetch', fetch);
    await expect(persistReviewDecision('session', 'row', {approved:true})).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('requires a positive acknowledgment and preserves exact target bytes', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"success":true}')); vi.stubGlobal('fetch', fetch);
    await persistReviewDecision('session/path', 'row/path', {new_url:'/Case/%2F?a=1&a=2', approved:false});
    expect(fetch.mock.calls[0][0]).toBe('/api/results/session%2Fpath/mappings/row%2Fpath');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({new_url:'/Case/%2F?a=1&a=2', approved:false});
  });
  it('preserves structured authorization errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":"Expired"}', {status:401})));
    await expect(persistReviewDecision('s','r',{approved:true})).rejects.toMatchObject({status:401});
  });
  it('passes cancellation to reads and rejects malformed mappings', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"success":true,"mappings":{}}')); vi.stubGlobal('fetch',fetch);
    const controller = new AbortController();
    await expect(getResults('s', controller.signal)).rejects.toThrow('valid review results');
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
  });
  it('bounds unknown mutation outcomes without retrying or claiming rollback', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn((_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(init.signal.reason))));
    vi.stubGlobal('fetch', fetch);
    const result = expect(persistReviewDecision('s', 'r', { approved: true })).rejects.toThrow('Reload results');
    await vi.advanceTimersByTimeAsync(20000); await result;
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([null, '', '/Case/%2f?x=2&x=1#Section'])('preserves the exact previous target precondition: %j', async expected => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"success":true}'));
    vi.stubGlobal('fetch', fetch);
    await persistReviewDecision('session','row',{new_url:'/chosen',approved:false,expected_new_url:expected});
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({new_url:'/chosen',approved:false,expected_new_url:expected});
    expect(fetch).toHaveBeenCalledTimes(1);
  });

});
