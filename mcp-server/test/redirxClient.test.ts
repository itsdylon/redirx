import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/config.js', () => ({
  config: { backendBaseUrl: 'https://backend.test' },
}));

const { RedirxClient, PIVOT_REQUEST_TIMEOUT_MS } = await import('../src/backend/redirxClient.js');

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('RedirxClient', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('sends the API key as a bearer token', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ user_id: 'u1', plan: 'free' }));
    const client = new RedirxClient('rdx_test');
    await client.getMigration('mig-1');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://backend.test/api/v1/migrations/mig-1');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer rdx_test');
  });

  it('surfaces a structured RedirxApiError on failure, not a thrown exception', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'not_found', message: 'No migration with that id.' } }, 404),
    );
    const client = new RedirxClient('rdx_test');
    const result = await client.getMigration('missing');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(404);
      expect(result.error.code).toBe('not_found');
    }
  });

  it('preserves the full error body so callers can read next_action/upgrade_url', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          error: {
            code: 'export_requires_payment',
            message: 'Exporting redirects for this migration requires payment.',
            next_action: 'pricing_checkout',
            upgrade_url: 'https://redirx.dev/review/mig-1',
            source_session_id: 'mig-1',
          },
        },
        402,
      ),
    );
    const client = new RedirxClient('rdx_test');
    const result = await client.getExport('mig-1', { format: 'csv', urlFormat: 'paths', minConfidence: 0 });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(402);
      expect(result.error.body.upgrade_url).toBe('https://redirx.dev/review/mig-1');
    }
  });

  it('parses export success from headers, not just the body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response('old,new\n/a,/b\n', {
        status: 200,
        headers: {
          'content-type': 'text/csv',
          'content-disposition': 'attachment; filename="redirects.csv"',
          'x-redirect-count': '1',
        },
      }),
    );
    const client = new RedirxClient('rdx_test');
    const result = await client.getExport('mig-1', { format: 'csv', urlFormat: 'paths', minConfidence: 0 });

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.filename).toBe('redirects.csv');
      expect(result.redirectCount).toBe(1);
      expect(result.content).toContain('/a,/b');
    }
  });
  it('bounds a stalled mutation without retrying or claiming rollback', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('sensitive transport detail')));
    }));
    const pending = new RedirxClient('rdx_test').pivot('POST', '/migrations', { idempotency_key: 'same-key' });
    await vi.advanceTimersByTimeAsync(PIVOT_REQUEST_TIMEOUT_MS);
    const result = await pending;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.message).toContain('mutation may have completed');
      expect(result.error.message).toContain('same idempotency key');
      expect(result.error.message).not.toContain('sensitive');
    }
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels the backend wait without dispatching another mutation', async () => {
    const abort = new AbortController();
    fetchMock.mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }));
    const pending = new RedirxClient('rdx_test').pivot('PATCH', '/matches', { idempotency_key: 'key' }, abort.signal);
    abort.abort();
    const result = await pending;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    if (result.ok) throw new Error('unexpected success');
    expect(result.error.message).toContain('cancelled');
    expect(result.error.message).toContain('may have completed');
    expect(result.error.body.retryable).toBe(false);
    await new RedirxClient('rdx_test').pivot('GET', '/migrations', undefined, abort.signal);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps the deadline active until the response body finishes', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(async (_url, init) => ({
      status: 200,
      json: () => new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new Error('body aborted')));
      }),
    }));
    const pending = new RedirxClient('rdx_test').pivot('GET', '/migrations');
    await vi.advanceTimersByTimeAsync(PIVOT_REQUEST_TIMEOUT_MS);
    const result = await pending;
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.message).toContain('Retry this read');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('rejects malformed success envelopes instead of claiming success', async () => {
    for (const data of [null, [], 'invalid']) {
      fetchMock.mockResolvedValueOnce(jsonResponse({ contract_version: '1.0.0', status: 'succeeded', next_action: 'none', data, error: null }));
      const result = await new RedirxClient('rdx_test').pivot('GET', '/migrations');
      expect(result.ok).toBe(false);
    }
  });

  it('retains safe error recovery from historical non-envelope middleware responses', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: { code: 'reconnect_required', message: 'Reconnect the account.', next_action: 'reconnect', retryable: false, private_debug: 'never forward' } }, 401));
    const result = await new RedirxClient('rdx_test').pivot('GET', '/migrations');
    if (result.ok) throw new Error('unexpected success');
    expect(result.error.code).toBe('reconnect_required');
    expect(result.error.body).toEqual({ next_action: 'reconnect', retryable: false });
  });

});
