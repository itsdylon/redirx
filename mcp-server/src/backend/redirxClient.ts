import { pivotEnvelopeSchema } from './pivotEnvelope.js';
import { config } from '../config.js';

export const PIVOT_REQUEST_TIMEOUT_MS = 25_000;

export class RedirxApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly body: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'RedirxApiError';
  }
}

export interface CreateMigrationInput {
  oldUrls: string[];
  newUrls: string[];
  name?: string;
  pipeline: 'content' | 'url_only';
}

export interface MigrationStatus {
  id: string;
  status: string;
  pipeline: string;
  name: string | null;
  stage: string | null;
  stage_index: number | null;
  total_stages: number | null;
  total_mappings: number | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  done: boolean;
  warning?: string;
  warning_message?: string;
}

export interface Match {
  old_url: string;
  new_url: string | null;
  confidence: number | null;
  match_type: string | null;
  clicks: number | null;
  impressions: number | null;
}

export interface PreviewResult {
  migration_id: string;
  status: string;
  total_mappings: number;
  unmatched_old_urls: number | null;
  confidence_distribution: { high: number; medium: number; low: number };
  needs_review_count: number;
  gsc: Record<string, unknown>;
  sample: Array<{
    old_url: string;
    new_url: string;
    confidence: number;
    confidence_band: string;
    warnings: string[];
  }>;
}

export interface DiscoverResult {
  root_url: string;
  side: 'old' | 'new';
  urls: string[];
  count: number;
  total_found: number;
  truncated: boolean;
  max_urls: number;
  discovery_method: string;
}

export interface PivotEnvelope {
  contract_version: string;
  migration_id?: string | null;
  operation_id?: string | null;
  status: string;
  next_action: string;
  retry_after_seconds?: number;
  progress?: Record<string, unknown>;
  data: Record<string, unknown>;
  error: {
    code: string;
    message: string;
    retryable: boolean;
    next_action: string;
    details?: Record<string, unknown>;
  } | null;
}

/**
 * A thin, typed client for the RedirX v1 API — the "thin gateway" from
 * docs/architecture/agentic-pivot.md: every method here maps to exactly one
 * existing (or newly-added, see v1_routes.py's preview/discover) v1 endpoint.
 * No business logic lives here — entitlement, pricing, and pairing all stay
 * server-side in Flask, exactly as the doc argues they should.
 */
export class RedirxClient {
  constructor(private readonly apiKey: string) {}

  private async request<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<{ ok: true; data: T } | { ok: false; error: RedirxApiError }> {
    const response = await fetch(`${config.backendBaseUrl}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
        ...init.headers,
      },
    });

    const contentType = response.headers.get('content-type') ?? '';
    if (!response.ok) {
      const body = contentType.includes('application/json')
        ? ((await response.json().catch(() => ({}))) as Record<string, unknown>)
        : {};
      const errorBody = (body.error as Record<string, unknown>) ?? {};
      return {
        ok: false,
        error: new RedirxApiError(
          response.status,
          String(errorBody.code ?? 'unknown_error'),
          String(errorBody.message ?? `Request failed with status ${response.status}`),
          errorBody,
        ),
      };
    }

    return { ok: true, data: (await response.json()) as T };
  }

  async createMigration(input: CreateMigrationInput) {
    return this.request<MigrationStatus & { id: string }>('/api/v1/migrations', {
      method: 'POST',
      body: JSON.stringify({
        old_urls: input.oldUrls,
        new_urls: input.newUrls,
        name: input.name,
        pipeline: input.pipeline,
      }),
    });
  }

  async getMigration(id: string) {
    return this.request<MigrationStatus>(`/api/v1/migrations/${encodeURIComponent(id)}`);
  }

  async getMatches(id: string, minConfidence = 0) {
    return this.request<{ migration_id: string; count: number; matches: Match[] }>(
      `/api/v1/migrations/${encodeURIComponent(id)}/matches?min_confidence=${minConfidence}`,
    );
  }

  async getPreview(id: string) {
    return this.request<PreviewResult>(`/api/v1/migrations/${encodeURIComponent(id)}/preview`);
  }

  async getExport(
    id: string,
    opts: { format: string; urlFormat: 'paths' | 'full'; minConfidence: number },
  ): Promise<
    | { ok: true; content: string; contentType: string; filename: string; redirectCount: number }
    | { ok: false; error: RedirxApiError }
  > {
    const params = new URLSearchParams({
      format: opts.format,
      url_format: opts.urlFormat,
      min_confidence: String(opts.minConfidence),
    });
    const response = await fetch(
      `${config.backendBaseUrl}/api/v1/migrations/${encodeURIComponent(id)}/export?${params}`,
      { headers: { Authorization: `Bearer ${this.apiKey}` } },
    );

    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { error?: Record<string, unknown> };
      const errorBody = body.error ?? {};
      return {
        ok: false,
        error: new RedirxApiError(
          response.status,
          String(errorBody.code ?? 'unknown_error'),
          String(errorBody.message ?? `Export failed with status ${response.status}`),
          errorBody,
        ),
      };
    }

    const disposition = response.headers.get('content-disposition') ?? '';
    const filenameMatch = /filename="([^"]+)"/.exec(disposition);
    return {
      ok: true,
      content: await response.text(),
      contentType: response.headers.get('content-type') ?? 'text/plain',
      filename: filenameMatch?.[1] ?? 'redirects.txt',
      redirectCount: Number(response.headers.get('x-redirect-count') ?? '0'),
    };
  }

  async discover(url: string, side: 'old' | 'new') {
    return this.request<DiscoverResult>('/api/v1/discover', {
      method: 'POST',
      body: JSON.stringify({ url, side }),
    });
  }

  /**
   * The pivot surface is deliberately transport-only: authorization remains
   * the short-lived backend delegation resolved by context.ts and all policy,
   * ownership, payment, consent, and artifact authority stay in /api/v2.
   */
  async pivot<T extends Record<string, unknown> = Record<string, unknown>>(
    method: 'GET' | 'POST' | 'PATCH', path: string, body?: Record<string, unknown>, signal?: AbortSignal,
  ): Promise<{ ok: true; data: PivotEnvelope & { data: T } } | { ok: false; error: RedirxApiError }> {
    const init: RequestInit = {
      method,
      headers: { Authorization: `Bearer ${this.apiKey}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    };
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(cancel, PIVOT_REQUEST_TIMEOUT_MS);
    try {
      if (signal?.aborted) controller.abort();
      controller.signal.throwIfAborted();
      const response = await fetch(`${config.backendBaseUrl}/api/v2${path}`, { ...init, signal: controller.signal });
      // The deadline covers response body consumption as well as the headers.
      const payload: unknown = await response.json();
      const parsed = pivotEnvelopeSchema.safeParse(payload);
      // Recoverable business states stay intact even on 4xx. Failed envelopes
      // are flagged by the tool layer, not flattened into a transport exception.
      if (parsed.success) return { ok: true, data: parsed.data as PivotEnvelope & { data: T } };
      // Older API/auth middleware may return the published error object without
      // the v2 envelope. Retain safe recovery fields, never arbitrary body text.
      if (!response.ok && payload && typeof payload === 'object' && !Array.isArray(payload)
          && !('contract_version' in payload) && 'error' in payload) {
        const error = payload.error;
        if (error && typeof error === 'object' && !Array.isArray(error)
            && 'code' in error && typeof error.code === 'string'
            && 'message' in error && typeof error.message === 'string') {
          return { ok: false, error: new RedirxApiError(response.status, error.code, error.message, {
            retryable: 'retryable' in error && error.retryable === true,
            next_action: 'next_action' in error && typeof error.next_action === 'string' ? error.next_action : 'none',
          }) };
        }
      }
      return {
        ok: false,
        error: new RedirxApiError(response.status, 'internal_error',
          'The backend returned an invalid response. No outcome was confirmed. Read migration status if known; retry a mutation only with identical input and the same idempotency key.',
          { retryable: false, next_action: 'retry' }),
      };
    } catch {
      // Aborting our wait does NOT roll back a mutation. Never automatically
      // dispatch again: replay is explicitly controlled by the original key.
      const cancelled = signal?.aborted === true;
      return {
        ok: false,
        error: new RedirxApiError(0, 'internal_error',
          `${cancelled ? 'The gateway request was cancelled.' : 'The gateway could not confirm the backend response.'} ${method === 'GET'
            ? 'Retry this read to recover the current state.'
            : 'The mutation may have completed. Read migration status if known; retry only with identical input and the same idempotency key.'}`,
          { retryable: !cancelled, next_action: 'retry' }),
      };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancel);
    }
  }
}
