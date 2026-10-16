import { API_BASE_URL, getAuthHeaders } from './config';
import { throwApiErrorFromResponse } from '../utils/errorHandler';

export interface PricingLineItem {
  from_page: number;
  to_page: number;
  pages: number;
  unit_price_usd: string;
  amount_usd: string;
  amount_cents: number;
}

export interface PricingEstimate {
  pricing_version: string;
  currency: string;
  contact_required: boolean;
  billable_pages: number;
  line_items: PricingLineItem[];
  subtotal_usd: string | null;
  subtotal_cents: number | null;
  effective_rate_usd: string | null;
}

export interface ProjectQuote {
  id: string;
  source_session_id: string;
  user_id: string;
  old_url_count: number;
  new_url_count: number;
  billable_pages: number;
  pricing_version: string;
  currency: string;
  line_items: PricingLineItem[];
  subtotal_cents: number | null;
  status: 'draft' | 'contact_required' | 'checkout_created' | 'paid' | 'cancelled' | 'expired';
  stripe_checkout_session_id?: string | null;
  deep_session_id?: string | null;
  checkout_created_at?: string | null;
  paid_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface BillingStatus {
  plan: 'free' | 'agency' | 'enterprise' | string;
  pricing_version: string;
  manage_portal_available: boolean;
  agency: {
    has_subscription: boolean;
    subscription_id: string | null;
    status: string | null;
    current_period_start: string | null;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    usage_pages: number;
    overage_enabled: boolean;
  };
}

export interface ProjectUnlockStatus {
  source_session_id: string;
  has_quote: boolean;
  quote_id?: string;
  quote_status: string | null;
  contact_required: boolean;
  billable_pages: number | null;
  subtotal_cents: number | null;
  currency?: string;
  pricing_version?: string;
  is_unlocked: boolean;
  deep_session_id: string | null;
  deep_session_status: string | null;
}

export async function getPricingEstimate(pageCount: number): Promise<PricingEstimate> {
  const res = await fetch(`${API_BASE_URL}/api/pricing/estimate?page_count=${encodeURIComponent(String(pageCount))}`);
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to generate pricing estimate right now.');
  }
  const data = await res.json();
  return data;
}

export async function createProjectQuote(sourceSessionId: string): Promise<ProjectQuote> {
  const res = await fetch(`${API_BASE_URL}/api/pricing/quote`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ source_session_id: sourceSessionId }),
  });
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to create your project quote right now.');
  }
  const data = await res.json();
  return data.quote;
}

export async function createProjectCheckout(params: {
  sourceSessionId?: string;
  quoteId?: string;
  successUrl?: string;
  cancelUrl?: string;
}): Promise<{
  success: boolean;
  already_paid?: boolean;
  quote_id: string;
  deep_session_id?: string | null;
  url?: string;
  checkout_session_id?: string;
}> {
  const body: Record<string, string> = {};
  if (params.sourceSessionId) body.source_session_id = params.sourceSessionId;
  if (params.quoteId) body.quote_id = params.quoteId;
  if (params.successUrl) body.success_url = params.successUrl;
  if (params.cancelUrl) body.cancel_url = params.cancelUrl;

  const res = await fetch(`${API_BASE_URL}/api/billing/project/checkout`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to start checkout right now. Please try again.');
  }

  return res.json();
}

export async function createAgencyCheckout(params?: {
  billingCycle?: 'monthly' | 'annual';
  successUrl?: string;
  cancelUrl?: string;
}): Promise<{ success: boolean; url: string; checkout_session_id: string }> {
  const body: Record<string, string> = {};
  if (params?.billingCycle) body.billing_cycle = params.billingCycle;
  if (params?.successUrl) body.success_url = params.successUrl;
  if (params?.cancelUrl) body.cancel_url = params.cancelUrl;

  const res = await fetch(`${API_BASE_URL}/api/billing/agency/checkout`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to start checkout right now. Please try again.');
  }
  return res.json();
}

export async function getBillingStatus(signal?: AbortSignal): Promise<BillingStatus> {
  const res = await fetch(`${API_BASE_URL}/api/billing/status`, {
    signal,
    headers: getAuthHeaders(),
  });
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to load billing details right now.');
  }
  const data = await res.json();
  return data;
}

export async function getProjectUnlockStatus(sourceSessionId: string, signal?: AbortSignal): Promise<ProjectUnlockStatus> {
  const res = await fetch(`${API_BASE_URL}/api/projects/${encodeURIComponent(sourceSessionId)}/unlock-status`, {
    signal,
    headers: getAuthHeaders(),
  });
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to load project unlock status right now.');
  }
  const data = await res.json();
  return data;
}

export async function createPortalSession(): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/billing/create-portal-session`, {
    method: 'POST',
    headers: getAuthHeaders(),
  });
  if (!res.ok) {
    await throwApiErrorFromResponse(res, 'Unable to open billing portal right now.');
  }
  const data = await res.json();
  return data.url;
}
