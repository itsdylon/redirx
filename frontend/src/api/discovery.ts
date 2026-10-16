import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError } from '../utils/errorHandler';

/** Which source(s) surfaced a URL. GSC-sourced URLs carry traffic weight. */
export type UrlSource = 'gsc' | 'sitemap' | 'crawl' | 'csv';

export interface DiscoveredEntry {
  url: string;
  sources: UrlSource[];
  clicks: number;
  impressions: number;
}

export interface DiscoverySummary {
  total: number;
  with_traffic: number;
  gsc_only: number;
  no_recorded_traffic: number;
  total_clicks: number;
  total_impressions: number;
}

export interface DiscoveryResponse {
  success: boolean;
  root_url: string;
  urls: string[];
  entries?: DiscoveredEntry[];
  count: number;
  total_found: number;
  truncated: boolean;
  max_urls: number;
  method: 'gsc' | 'sitemap' | 'wordpress_api' | 'shopify_api' | 'crawl' | 'none';
  discovery_method?: string;
  generator: string | null;
  side?: 'old' | 'new';
  summary?: DiscoverySummary;
  gsc_property?: string | null;
  gsc_url_count?: number;
  baseline_captured?: boolean;
  duration_ms?: number;
  plan: string;
}

export async function discoverSite(
  url: string,
  side: 'old' | 'new' = 'old',
  gscProperty?: string | null,
  signal?: AbortSignal
): Promise<DiscoveryResponse> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/discovery/discover`, {
      method: 'POST',
      headers: getAuthHeaders(),
      signal,
      // The old side leads with Search Console; the new site isn't indexed yet.
      body: JSON.stringify(
        gscProperty === null ? { url, side, use_gsc: false }
          : gscProperty ? { url, side, gsc_property: gscProperty } : { url, side }
      ),
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const message =
        (typeof data.user_message === 'string' && data.user_message) ||
        (typeof data.error === 'string' && data.error) ||
        '';
      if (message) {
        throw new Error(message);
      }
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    return await response.json();
  } catch (error: any) {
    if (error.name === 'AbortError') throw error;
    if (error instanceof TypeError) {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }
    throw error;
  }
}
