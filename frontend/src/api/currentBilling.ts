import { API_BASE_URL, getAuthHeaders } from './config';

export type CurrentSku = 'payg' | 'studio' | 'agency';
export interface CurrentPlan { sku: CurrentSku; name: string; monthly_amount_cents: number | null; included_old_urls: number; }
export interface CurrentUsage {
  enabled?: boolean; sku: CurrentSku | 'free' | null; subscription_status?: string | null;
  included_old_urls: number; used_old_urls: number; extra_old_urls: number; extra_amount_cents: number;
  period_start?: string | null; period_end?: string | null; portal_available: boolean;
}
export interface CurrentPricing { enabled: boolean; version: string; plans: CurrentPlan[]; usage?: CurrentUsage; }
export interface CurrentCheckout {
  checkout_id: string; status: 'paid' | 'open' | 'reserved' | 'expired' | 'revoked';
  migration_id?: string | null;
  checkout_url: string | null; amount_cents: number; currency: string; sku: CurrentSku;
}
export interface CurrentCheckoutRequest {
  sku: CurrentSku; migration_id?: string; inventory_ids?: { old: string; new: string };
  idempotency_key: string; recurring_consent: boolean;
}
export interface CurrentQuote { old_urls: number; amount_cents: number; stripe_quantity: number; }
const invalid = () => new Error('Billing returned an incomplete response. Try again.');
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/v2/billing/current${path}`, {
    ...init, headers: getAuthHeaders(), cache: 'no-store',
  });
  const data = await response.json().catch(() => null);
  if (!response.ok || data?.error) throw new Error(
    typeof data?.error?.message === 'string' ? data.error.message : response.status === 401
      ? 'Sign in again to load billing.' : 'Billing could not complete the request. Try again.');
  if (!data || typeof data !== 'object') throw invalid();
  return data;
}
export async function getCurrentPricing(signal?: AbortSignal): Promise<CurrentPricing> {
  const data = await request<CurrentPricing>('', { signal });
  if (typeof data.enabled !== 'boolean' || !Array.isArray(data.plans) || typeof data.version !== 'string'
    || data.plans.some(plan => !plan || !['payg', 'studio', 'agency'].includes(plan.sku)
      || typeof plan.name !== 'string' || !Number.isSafeInteger(plan.included_old_urls) || plan.included_old_urls < 0
      || (plan.sku !== 'payg' && (!Number.isSafeInteger(plan.monthly_amount_cents) || Number(plan.monthly_amount_cents) <= 0)))) throw invalid();
  return data;
}
export async function getCurrentUsage(signal?: AbortSignal): Promise<CurrentUsage> {
  const data = await request<CurrentUsage>('/usage', { signal });
  if (typeof data.portal_available !== 'boolean'
    || (data.sku !== null && !['free', 'payg', 'studio', 'agency'].includes(data.sku))
    || [data.included_old_urls, data.used_old_urls, data.extra_old_urls, data.extra_amount_cents].some(value => !Number.isSafeInteger(value) || value < 0)) throw invalid();
  return data;
}
function checkoutResult(data: CurrentCheckout): CurrentCheckout {
  if (!data.checkout_id || !['paid', 'open', 'reserved', 'expired', 'revoked'].includes(data.status)
    || !['payg', 'studio', 'agency'].includes(data.sku) || !Number.isSafeInteger(data.amount_cents)
    || data.amount_cents < 0 || !/^[a-z]{3}$/i.test(data.currency)) throw invalid();
  return data;
}
export async function createCurrentCheckout(body: CurrentCheckoutRequest, signal?: AbortSignal) {
  if (body.sku !== 'payg' && !body.recurring_consent) throw new Error('Approve monthly billing before continuing.');
  if (body.sku === 'payg' && (!body.migration_id || !body.inventory_ids?.old || !body.inventory_ids?.new))
    throw new Error('Select both URL inventories for this migration.');
  return checkoutResult(await request<CurrentCheckout>('/checkouts', { method: 'POST', body: JSON.stringify(body), signal }));
}
export async function getCurrentCheckout(id: string, signal?: AbortSignal) {
  const result = checkoutResult(await request<CurrentCheckout>(`/checkouts/${encodeURIComponent(id)}`, { signal }));
  if (result.checkout_id !== id) throw invalid();
  return result;
}
export const getCurrentQuote = (migration: string, old: string, next: string, signal?: AbortSignal) =>
  request<CurrentQuote>(`/migrations/${encodeURIComponent(migration)}/quote?${new URLSearchParams({ old_inventory_id: old, new_inventory_id: next })}`, { signal });
export async function createCurrentPortal(signal?: AbortSignal) {
  const data = await request<{ url: string }>('/portal', { method: 'POST', signal });
  return billingRedirectUrl(data.url);
}
/** Only navigate to hosted Stripe checkout/portal URLs returned by our API. */
export function billingRedirectUrl(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw invalid(); }
  if (url.protocol !== 'https:' || !['checkout.stripe.com', 'billing.stripe.com'].includes(url.hostname)
    || url.username || url.password || url.port) throw invalid();
  return url.href;
}
