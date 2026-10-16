import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError, throwApiErrorFromResponse } from '../utils/errorHandler';

export type WatchStatus = 'active' | 'paused' | 'ended';

export type IssueType =
  | 'no_redirect'
  | 'not_found'
  | 'server_error'
  | 'wrong_target'
  | 'redirect_chain'
  | 'temporary_redirect'
  | 'redirect_loop'
  | 'unreachable'
  | 'blocked';

export type IssueSeverity = 'critical' | 'warning';

export interface Watch {
  id: string;
  session_id: string;
  old_domain: string;
  new_domain?: string | null;
  status: WatchStatus;
  alert_email?: string | null;
  check_interval_minutes: number;
  intensive_until?: string | null;
  next_check_at?: string | null;
  last_checked_at?: string | null;
  last_error?: string | null;
  created_at: string;
}

export interface WatchIssue {
  id: string;
  old_url: string;
  expected_url?: string | null;
  issue_type: IssueType;
  severity: IssueSeverity;
  http_status?: number | null;
  final_url?: string | null;
  hops: number;
  detail?: string | null;
  clicks_at_risk: number;
  suggested_target?: string | null;
  fix_source?: string | null;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at?: string | null;
}

export interface WatchCheck {
  id: string;
  started_at: string;
  finished_at?: string | null;
  status: 'running' | 'completed' | 'failed';
  urls_checked: number;
  urls_ok: number;
  issues_open: number;
  issues_new: number;
  issues_resolved: number;
  clicks_at_risk: number;
  error?: string | null;
}

export interface WatchDetail {
  watch: Watch;
  issues: WatchIssue[];
  checks: WatchCheck[];
  summary: {
    open_issues: number;
    critical: number;
    clicks_at_risk: number;
  };
}

/**
 * Wording for each failure, kept in one place.
 *
 * `label` is what the user reads; `hint` is what to do about it. Both live
 * next to the type union so a new issue type cannot reach the UI as a raw
 * snake_case string.
 */
export const ISSUE_COPY: Record<IssueType, { label: string; hint: string }> = {
  no_redirect: {
    label: 'Redirect never deployed',
    hint: 'The old URL still serves a page. The rule is missing from your config.',
  },
  not_found: {
    label: 'Returns 404',
    hint: 'Visitors and search crawlers hit a dead end here.',
  },
  server_error: {
    label: 'Server error',
    hint: 'The old URL is erroring rather than redirecting.',
  },
  wrong_target: {
    label: 'Goes to the wrong page',
    hint: 'Often a catch-all rule sending everything to the homepage.',
  },
  redirect_chain: {
    label: 'Extra hops',
    hint: 'It gets there, but through more redirects than it needs.',
  },
  temporary_redirect: {
    label: 'Temporary redirect',
    hint: 'For a permanent move, use a 301 or 308. Temporary redirects do not give Google the same signal to select the destination as canonical.',
  },
  redirect_loop: {
    label: 'Redirect loop',
    hint: 'Two rules point at each other and the browser gives up.',
  },
  unreachable: {
    label: 'Could not be reached',
    hint: 'The request timed out or the connection failed.',
  },
  blocked: {
    label: 'Request refused',
    hint: 'The address resolves somewhere we will not follow.',
  },
};

async function request<T>(path: string, init: RequestInit): Promise<T> {
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, init);
    if (!response.ok) {
      await throwApiErrorFromResponse(response);
    }
    const data = await response.json();
    if (data?.success === false) throw new Error(typeof data.error === 'string' ? data.error : 'The request did not complete. Try again.');
    return data;
  } catch (error: any) {
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }
    throw error;
  }
}

export async function listWatches(signal?: AbortSignal): Promise<Watch[]> {
  const data = await request<{ success: boolean; watches: Watch[] }>('/api/watches', {
    method: 'GET', signal,
    headers: getAuthHeaders(),
  });
  if (!Array.isArray(data.watches)) throw new Error('Monitoring history was incomplete. Retry loading it.');
  return data.watches;
}

export async function createWatch(params: {
  sessionId: string;
  alertEmail?: string;
  checkIntervalMinutes?: number;
}, signal?: AbortSignal): Promise<Watch> {
  const data = await request<{ success: boolean; watch: Watch }>('/api/watches', {
    method: 'POST', signal,
    headers: getAuthHeaders(),
    body: JSON.stringify({
      session_id: params.sessionId,
      alert_email: params.alertEmail,
      check_interval_minutes: params.checkIntervalMinutes,
    }),
  });
  return data.watch;
}

export async function getWatch(watchId: string, signal?: AbortSignal): Promise<WatchDetail> {
  return request<WatchDetail>(`/api/watches/${watchId}`, {
    method: 'GET', signal,
    headers: getAuthHeaders(),
  });
}

export async function setWatchStatus(watchId: string, status: WatchStatus, signal?: AbortSignal): Promise<Watch> {
  const data = await request<{ success: boolean; watch: Watch }>(`/api/watches/${watchId}`, {
    method: 'PATCH', signal,
    headers: getAuthHeaders(),
    body: JSON.stringify({ status }),
  });
  if (data.watch?.id !== watchId || data.watch.status !== status) throw new Error('The monitoring status was not confirmed. Refresh before trying again.');
  return data.watch;
}

export async function checkNow(watchId: string, signal?: AbortSignal): Promise<void> {
  const data = await request<{ queued?: boolean }>(`/api/watches/${watchId}/check-now`, {
    method: 'POST', signal,
    headers: getAuthHeaders(),
  });
  if (data.queued !== true) throw new Error('The check was not confirmed as queued. Refresh the monitor before retrying.');
}

/**
 * Download the corrective redirect file.
 *
 * Fetched rather than linked because the endpoint needs an Authorization
 * header, which a plain <a href> cannot carry.
 */
export async function downloadFixes(watchId: string, format: string, signal?: AbortSignal): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/watches/${watchId}/fixes/export?format=${encodeURIComponent(format)}`,
    { method: 'GET', headers: getAuthHeaders(), signal }
  );
  if (!response.ok) {
    const userError = await handleApiError(null, response);
    throw new Error(userError.message);
  }

  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : `fix-redirects.${format}`;

  const blob = await response.blob();
  if (signal?.aborted) return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
