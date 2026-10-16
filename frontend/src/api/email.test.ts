import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { getEmailPreferences, updateEmailPreference } from './email';

// ---------------------------------------------------------------------------
// Mock global fetch
// ---------------------------------------------------------------------------

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// Ensure auth header is present by seeding a token
beforeEach(() => {
  localStorage.setItem('access_token', 'test-token-123');
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

// ---------------------------------------------------------------------------
// getEmailPreferences
// ---------------------------------------------------------------------------

describe('getEmailPreferences', () => {
  it('returns preferences array on success', async () => {
    const prefs = [
      { email_type: 'mapping_complete', opted_out: false },
      { email_type: 'mapping_failed', opted_out: true },
    ];
    mockFetch.mockReturnValueOnce(jsonResponse({ preferences: prefs }));

    const result = await getEmailPreferences();

    expect(result).toEqual(prefs);
    expect(result).toHaveLength(2);
  });

  it('calls the correct endpoint with auth headers', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ preferences: [] }));

    await getEmailPreferences();

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain('/api/email/preferences');
    expect(options.headers).toHaveProperty('Authorization', 'Bearer test-token-123');
  });

  it('uses GET method (default)', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ preferences: [] }));

    await getEmailPreferences();

    const [, options] = mockFetch.mock.calls[0];
    // fetch defaults to GET when no method is specified
    expect(options.method).toBeUndefined();
  });

  it('throws with server error message on non-OK response', async () => {
    mockFetch.mockReturnValueOnce(
      jsonResponse({ error: 'Unauthorized' }, 401)
    );

    await expect(getEmailPreferences()).rejects.toThrow('Unauthorized');
  });

  it('throws fallback message when server returns no error field', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({}, 500));

    await expect(getEmailPreferences()).rejects.toThrow(
      'Failed to fetch email preferences'
    );
  });

  it('returns empty array when server has no preferences', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ preferences: [] }));

    const result = await getEmailPreferences();

    expect(result).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// updateEmailPreference
// ---------------------------------------------------------------------------

describe('updateEmailPreference', () => {
  it('sends PUT request with correct body for opting out', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ success: true }));

    await updateEmailPreference('mapping_complete', true);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain('/api/email/preferences');
    expect(options.method).toBe('PUT');
    expect(JSON.parse(options.body)).toEqual({
      email_type: 'mapping_complete',
      opted_out: true,
    });
  });

  it('sends PUT request with correct body for opting in', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ success: true }));

    await updateEmailPreference('mapping_failed', false);

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body).toEqual({
      email_type: 'mapping_failed',
      opted_out: false,
    });
  });

  it('includes auth headers', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ success: true }));

    await updateEmailPreference('mapping_complete', true);

    const headers = mockFetch.mock.calls[0][1].headers;
    expect(headers).toHaveProperty('Authorization', 'Bearer test-token-123');
    expect(headers).toHaveProperty('Content-Type', 'application/json');
  });

  it('resolves without returning a value on success', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({ success: true }));

    const result = await updateEmailPreference('mapping_complete', false);

    expect(result).toBeUndefined();
  });

  it('throws with server error message on non-OK response', async () => {
    mockFetch.mockReturnValueOnce(
      jsonResponse({ error: 'Invalid email type' }, 400)
    );

    await expect(
      updateEmailPreference('mapping_complete', true)
    ).rejects.toThrow('Invalid email type');
  });

  it('throws fallback message when server returns no error field', async () => {
    mockFetch.mockReturnValueOnce(jsonResponse({}, 500));

    await expect(
      updateEmailPreference('mapping_complete', true)
    ).rejects.toThrow('Failed to update email preference');
  });
});


it.each([{}, { preferences: null }, { preferences: [{ email_type: 'watch_alert', opted_out: 'false' }] }])('rejects unknown preference reads %s', async response => {
  mockFetch.mockReturnValueOnce(jsonResponse(response));
  await expect(getEmailPreferences()).rejects.toThrow('could not be verified');
});
it.each([{}, { success: false }])('requires positive preference write acknowledgment %s', async response => {
  mockFetch.mockReturnValueOnce(jsonResponse(response));
  await expect(updateEmailPreference('watch_alert', true)).rejects.toThrow('not confirmed');
});
