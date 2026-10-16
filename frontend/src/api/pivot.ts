import { API_BASE_URL, getAuthHeaders } from './config';

export interface PivotEnvelope<T = Record<string, unknown>> {
  contract_version: string; migration_id: string | null; operation_id: string | null;
  status: string; next_action: string; retry_after_seconds?: number; data: T;
  error: { code: string; message: string; retryable: boolean; next_action: string } | null;
}
export interface PivotMigration { id: string; name?: string | null; old_origin?: string; new_origin?: string; status?: string; }
export type PivotMatchFilter = 'all' | 'needs_review' | 'unmatched' | 'rejected' | 'approved';
export interface JevRun {
  engine: 'jev-url-v1'; model: string; pass: number; seed_revision: number;
  limits: { old_pages: number; new_pages: number; inventory_bytes: number; passes: number; new_runs_per_24h: number };
}
export interface PivotMatch {
  mapping_id: string; old_url: string; new_url?: string | null; decision_target?: string | null;
  jev_proposal?: { target_url?: string | null; confidence?: number; confidence_kind?: string; candidates?: unknown[]; pass: number; seed_revision: number; stale: boolean; confirmation_required?: boolean };
  revision: number; review_status: string; traffic_observed: boolean; traffic_clicks: number | null;
}
export interface MigrationDetail {
  jev?: JevRun; migration?: PivotMigration; summary?: string; run_id?: string; quote_id?: string;
  run?: { inventory_ids?: { old: string; new: string }; status: string; progress?: { current_stage: number | null; total_stages: number | null } };
  quote?: { amount_cents: number; currency: string; kind: string };
  artifact?: { artifact_id: string; format: string; included_count: number; excluded_count: number };
  deployment_id?: string; monitoring_id?: string;
  verification?: { outcome: string; checked: number; total: number; unchecked: number };
}
export interface MonitoringIssue {
  issue_id: string; source_url: string; expected_url?: string; issue?: string;
  suggested_action?: string; evidence?: { issue?: string; measurement?: string };
}
export interface MonitoringData {
  state?: string; summary?: string; items?: MonitoringIssue[]; next_cursor?: number | null;
  coverage?: { outcome?: string; total?: number; checked?: number; unchecked?: number; failed?: number };
}
export interface GscData {
  authorization_url?: string; connection_state?: string; summary?: string;
  properties?: Array<{ site_url: string; permission_level?: string }>;
}
export interface SubscriptionCheckout {
  checkout_id: string; checkout_url?: string; state: string; sku: string;
  monthly_amount_cents: number; currency: string; interval: string;
  subscription?: { status: string; eligible: boolean };
}
export class PivotRequestError extends Error {
  constructor(message: string, public code: string, public status: number) { super(message); }
}
async function request<T>(path: string, init: RequestInit = {}): Promise<PivotEnvelope<T>> {
  const response = await fetch(`${API_BASE_URL}/api/v2${path}`, {
    ...init, headers: { ...getAuthHeaders(), ...init.headers },
  });
  const value = await response.json().catch(() => null) as PivotEnvelope<T> | null;
  if (!response.ok || value?.error) {
    throw new PivotRequestError(value?.error?.message || (response.status === 401
      ? 'Sign in again to continue.' : response.status === 404 ? 'This record was not found.'
      : 'The request could not finish. Try again.'), value?.error?.code || 'unavailable', response.status);
  }
  if (!value || !value.data || typeof value.data !== 'object' || value.contract_version !== '1.0.0') {
    throw new PivotRequestError('The server returned an incomplete response. Try again.', 'invalid_response', response.status);
  }
  return value;
}
const part = encodeURIComponent;
const post = (data: unknown, signal?: AbortSignal): RequestInit => ({ method: 'POST', body: JSON.stringify(data), signal });
export const listPivotMigrations = (cursor?: string, signal?: AbortSignal) =>
  request<{ items: PivotMigration[]; next_cursor?: string | null }>(`/migrations?limit=20${cursor ? `&cursor=${part(cursor)}` : ''}`, { signal });
export const getPivotMigration = (id: string, signal?: AbortSignal) =>
  request<MigrationDetail>(`/migrations/${part(id)}`, { signal });
export const listPivotMatches = (migration: string, run: string, cursor?: string, signal?: AbortSignal, filter: PivotMatchFilter = 'all', limit: 20 | 500 = 20) =>
  request<{ items: PivotMatch[]; next_cursor?: string | null; selection_revision: number; jev?: JevRun }>(
    `/migrations/${part(migration)}/runs/${part(run)}/matches?filter=${part(filter)}&limit=${limit}${cursor ? `&cursor=${part(cursor)}` : ''}`, { signal });
export const resolvePivotMatch = (migration: string, run: string, decision: Record<string, unknown>, key: string, signal?: AbortSignal) =>
  request<{ outcomes: Array<{ code: string; message?: string }> }>(`/migrations/${part(migration)}/runs/${part(run)}/matches`,
    { ...post({ decisions: [decision], idempotency_key: key }, signal), method: 'PATCH' });
export const monitoring = (migration: string, fixes = false, id?: string | null, after = -1, signal?: AbortSignal) => {
  const query = new URLSearchParams();
  if (id) query.set('monitoring_id', id);
  if (fixes) { query.set('after', String(after)); query.set('limit', '20'); }
  return request<MonitoringData>(`/migrations/${part(migration)}/monitoring${fixes ? '/fixes' : ''}${query.size ? `?${query}` : ''}`, { signal });
};
export const gscAction = (data: Record<string, unknown>, key?: string, signal?: AbortSignal) =>
  request<GscData>('/connections/search-console/actions', post({ ...data, ...(key ? { idempotency_key: key } : {}) }, signal));
export const checkout = async (migration: string, quote: string, operation: string, key: string, signal?: AbortSignal) => {
  // This is the persisted run operation from the status envelope, not the
  // quote's operation or a new browser-generated identifier.
  if (typeof operation !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(operation)) {
    throw new PivotRequestError('Payment details are incomplete. Refresh the migration before trying checkout again.', 'invalid_response', 0);
  }
  return request<{ checkout_url?: string; state?: string }>(`/migrations/${part(migration)}/quotes/${part(quote)}/checkout`,
    post({ operation_id: operation, idempotency_key: key }, signal));
};
export const createSubscriptionCheckout = (data: { sku: 'studio' | 'monitoring'; deployment_id?: string; recurring_consent: true }, key: string, signal?: AbortSignal) =>
  request<SubscriptionCheckout>('/billing/subscription-checkouts', post({ ...data, idempotency_key: key }, signal));
export const getSubscriptionCheckout = (id: string, returned = false, signal?: AbortSignal) =>
  request<SubscriptionCheckout>(`/billing/subscription-checkouts/${part(id)}${returned ? '/return' : ''}`, { signal });
export const downloadArtifact = (migration: string, artifact: string, signal?: AbortSignal) =>
  request<{ content: string; format: string; artifact_id: string }>(`/migrations/${part(migration)}/artifacts/${part(artifact)}`, { signal });

export const refinePivotMatches = (migration: string, run: string, expectedSeedRevision: number, key: string, signal?: AbortSignal) =>
  request<MigrationDetail>(`/migrations/${part(migration)}/runs/${part(run)}/refine`,
    post({ expected_seed_revision: expectedSeedRevision, idempotency_key: key }, signal));
