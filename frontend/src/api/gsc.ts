import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError } from '../utils/errorHandler';

export interface GscStatusResponse {
  success: boolean;
  connected: boolean;
  configured?: boolean;
  google_email?: string;
  connected_at?: string;
}

export interface GscRiskSummary {
  totalClicks: number;
  totalImpressions: number;
  weightMetric: 'clicks' | 'impressions';
  trafficShareTarget: number;
  topUrlCount: number;
  topUrlsAtRisk: number;
  urlsWithTraffic: number;
}

export interface GscResultsMeta {
  synced: boolean;
  property?: string;
  synced_at?: string;
  riskSummary?: GscRiskSummary;
}

export interface GscSyncResponse {
  success: boolean;
  property: string;
  matched_urls: number;
  session_url_count: number;
  total_clicks: number;
  total_impressions: number;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, init);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const message =
        typeof data.user_message === 'string' && data.user_message
          ? data.user_message
          : undefined;
      if (message) {
        throw new Error(message);
      }
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }
    const data = await response.json();
    if (data?.success === false) throw new Error(typeof data.user_message === 'string' ? data.user_message : 'Search Console could not complete this request. Try again.');
    return data;
  } catch (error: any) {
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }
    throw error;
  }
}

export interface GscProperty {
  site_url: string;
  permission_level: string;
}

export async function getGscStatus(signal?: AbortSignal): Promise<GscStatusResponse> {
  const data = await request<GscStatusResponse>('/api/gsc/status', { method: 'GET', headers: getAuthHeaders(), signal });
  if (!data || data.success !== true || typeof data.connected !== 'boolean'
      || (data.configured !== undefined && typeof data.configured !== 'boolean')) {
    throw new Error('Search Console returned an invalid connection status. Retry its status.');
  }
  return data;
}

export async function getGscProperties(signal?: AbortSignal): Promise<GscProperty[]> {
  const data = await request<{ success: boolean; properties: GscProperty[] }>(
    '/api/gsc/properties',
    { method: 'GET', headers: getAuthHeaders(), signal }
  );
  return data.properties ?? [];
}

/**
 * How a Search Console property reads to a human.
 *
 * `sc-domain:example.com` is Google's wire format for a domain property, which
 * covers every subdomain and both schemes. Showing it raw makes people think
 * something is broken.
 */
export function formatProperty(siteUrl: string): string {
  if (siteUrl.startsWith('sc-domain:')) {
    return `${siteUrl.slice('sc-domain:'.length)} (all subdomains)`;
  }
  return siteUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

/** Whether a property plausibly covers the host the user typed. */
export function propertyCovers(siteUrl: string, domain: string): boolean {
  const host = domain
    .trim()
    .replace(/^https?:\/\//, '')
    .split('/')[0]
    .toLowerCase()
    .replace(/^www\./, '');
  if (!host) return false;
  if (siteUrl.startsWith('sc-domain:')) {
    const bare = siteUrl.slice('sc-domain:'.length).toLowerCase();
    return host === bare || host.endsWith(`.${bare}`);
  }
  try {
    const propHost = new URL(siteUrl).hostname.toLowerCase().replace(/^www\./, '');
    return propHost === host;
  } catch {
    return false;
  }
}

export async function getGscConnectUrl(returnTo: string, signal?: AbortSignal): Promise<string> {
  const data = await request<{ success: boolean; authorization_url: string }>(
    `/api/gsc/connect?return_to=${encodeURIComponent(returnTo)}`,
    { method: 'GET', headers: getAuthHeaders(), signal }
  );
  return data.authorization_url;
}

export function syncGscSession(sessionId: string, property?: string, signal?: AbortSignal): Promise<GscSyncResponse> {
  return request(`/api/gsc/sessions/${sessionId}/sync`, {
    method: 'POST', signal,
    headers: getAuthHeaders(),
    body: JSON.stringify(property ? { property } : {}),
  });
}

export function disconnectGsc(): Promise<{ success: boolean }> {
  return request('/api/gsc/disconnect', { method: 'POST', headers: getAuthHeaders() });
}
