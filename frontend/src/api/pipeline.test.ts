import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { uploadCSVs, UploadNotConfirmedError } from './pipeline';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

beforeEach(() => {
  localStorage.setItem('access_token', 'test-token-123');
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  }));
}

describe('uploadCSVs', () => {
  it('throws structured content URL cap error from 422 responses', async () => {
    mockFetch.mockReturnValueOnce(
      jsonResponse(
        {
          reason_code: 'content_old_url_cap_exceeded',
          user_message: 'Deep Match has a per-file limit of 5,000 URLs. Split your CSV or switch to Quick Match.',
          old_url_count: 5001,
          new_url_count: 40,
          max_old_urls: 5000,
          max_new_urls: 5000,
          affected_file: 'old',
          next_action: 'reduce_csv_rows_or_switch_pipeline',
          retryable: false,
        },
        422
      )
    );

    const oldFile = new File(['https://old.example.com/a'], 'old.csv', { type: 'text/csv' });
    const newFile = new File(['https://new.example.com/a'], 'new.csv', { type: 'text/csv' });

    await expect(uploadCSVs(oldFile, newFile)).rejects.toMatchObject({
      type: 'content_url_cap_exceeded',
      reason_code: 'content_old_url_cap_exceeded',
      old_url_count: 5001,
      max_old_urls: 5000,
      affected_file: 'old',
    });
  });

  it('maps non-cap API errors through centralized handler', async () => {
    mockFetch.mockReturnValueOnce(
      jsonResponse(
        {
          user_message: 'Free accounts can only start Quick Match from upload.',
          code: 'deep_match_requires_project_checkout',
        },
        403
      )
    );

    const oldFile = new File(['https://old.example.com/a'], 'old.csv', { type: 'text/csv' });
    const newFile = new File(['https://new.example.com/a'], 'new.csv', { type: 'text/csv' });

    await expect(uploadCSVs(oldFile, newFile)).rejects.toThrow(
      'Free accounts can only start Quick Match from upload.'
    );
  });
});


describe('upload acknowledgment', () => {
  it.each([{}, { success: false, session_id: 'not-confirmed' }, { success: true, session_id: ' ' }, { success: true, session_id: 123 }, { success: true, session_id: 'valid', is_duplicate: 'false' }, { success: true, session_id: 'valid' }, null, []])('rejects unconfirmed success %j without retry', async body => {
    mockFetch.mockReturnValueOnce(jsonResponse(body));
    const file = new File(['https://example.com/a'], 'urls.csv', { type: 'text/csv' });
    await expect(uploadCSVs(file, file)).rejects.toThrow(/not confirmed.*Project History/i);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});


describe('native upload transport boundaries', () => {
  const file = () => new File(['https://example.com/a'], 'urls.csv', { type: 'text/csv' });
  it('forwards an optional abort signal and treats interruption as unknown without retry', async () => {
    const controller = new AbortController();
    mockFetch.mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
      expect(options.signal).toBe(controller.signal);
      options.signal.addEventListener('abort', () => reject(new DOMException('Interrupted', 'AbortError')), { once: true });
    }));
    const result = uploadCSVs(file(), file(), false, 'url_only', controller.signal);
    const rejected = expect(result).rejects.toBeInstanceOf(UploadNotConfirmedError);
    controller.abort();
    await rejected;
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
  it.each([false, true])('accepts the backend acknowledgment with duplicate=%s and preserves the ID', async isDuplicate => {
    const body = { success: true, session_id: 'c3fdccad-bbb4-492b-9f10-1fa42e717f66', is_duplicate: isDuplicate, pipeline_type: 'url_only' };
    mockFetch.mockReturnValueOnce(jsonResponse(body));
    await expect(uploadCSVs(file(), file(), true, 'url_only')).resolves.toEqual(body);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const options = mockFetch.mock.calls[0][1];
    expect(options.method).toBe('POST');
    expect(options.body.get('force')).toBe('true');
    expect(options.body.get('pipeline_type')).toBe('url_only');
    expect(options.body.get('old_csv').name).toBe('urls.csv');
    expect(options.body.get('new_csv').name).toBe('urls.csv');
  });
  it.each(['', '{"success":true,', '<html>Upstream unavailable</html>'])('marks an unreadable acknowledgment unconfirmed, without retry: %s', async body => {
    mockFetch.mockResolvedValueOnce(new Response(body, { status: 200 }));
    await expect(uploadCSVs(file(), file())).rejects.toBeInstanceOf(UploadNotConfirmedError);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
  it.each([new TypeError('Failed to fetch'), new DOMException('Request interrupted', 'AbortError')])('does not claim a failed network response rolled back the POST', async error => {
    mockFetch.mockRejectedValueOnce(error);
    await expect(uploadCSVs(file(), file())).rejects.toThrow('Check Project History');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
  it('retains quota instructions and structured metadata from a single native body read', async () => {
    const response = new Response(JSON.stringify({ success: false, user_message: 'Your run allowance is used. Existing projects remain available.', code: 'run_allowance_exceeded', next_action: 'view_existing_projects', retryable: false }), { status: 429, headers: { 'Retry-After': '120' } });
    mockFetch.mockResolvedValueOnce(response);
    await expect(uploadCSVs(file(), file())).rejects.toMatchObject({ message: 'Your run allowance is used. Existing projects remain available.', status: 429, code: 'run_allowance_exceeded', next_action: 'view_existing_projects', retryable: false, retry_after_seconds: 120 });
    expect(response.bodyUsed).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
  it('preserves safe plain-text errors when JSON parsing is impossible', async () => {
    mockFetch.mockResolvedValueOnce(new Response('Upload size exceeds the server limit.', { status: 413 }));
    await expect(uploadCSVs(file(), file())).rejects.toThrow('Upload size exceeds the server limit.');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});
