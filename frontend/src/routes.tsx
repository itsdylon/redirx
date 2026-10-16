import { isEnterprisePlan, isToolPlan } from './lib/plans';

export const ROUTES = {
  root: '/',
  login: '/login',
  signup: '/signup',
  authCallback: '/auth/callback',
  oauthConsent: '/oauth/consent',
  quickMatch: '/quick-match',
  dashboard: '/dashboard',
  projects: '/projects',
  upload: '/upload',
  review: '/review/:sessionId',
  settings: '/settings',
  pricing: '/pricing',
  account: '/account',
  demo: '/demo',
  watch: '/watch/:watchId',
  apiKeys: '/api-keys',
  companion: '/companion',
  currentBilling: '/billing/current',
  currentBillingReturn: '/billing/current/return',
  migration: '/migrations/:migrationId',
} as const;

export function getAuthedHomeRoute(plan?: string): string {
  return isEnterprisePlan(plan) ? ROUTES.dashboard : ROUTES.quickMatch;
}

/** Retired funnel entries deliberately return to the companion after login. */
export function getPivotEntryRedirect(pathname: string, authenticated: boolean, pivotEnabled: boolean): string | null {
  if (!pivotEnabled || !['/quick-match', '/upload', '/dashboard'].includes(pathname)) return null;
  if (authenticated) return ROUTES.companion;
  return `${ROUTES.login}?redirect=${encodeURIComponent(ROUTES.companion)}`;
}

export function canAccessDashboard(plan?: string): boolean {
  return isEnterprisePlan(plan);
}

export function canAccessUpload(plan?: string): boolean {
  return isEnterprisePlan(plan);
}

export function canAccessQuickMatch(plan?: string): boolean {
  return isToolPlan(plan);
}

export function canAccessSettingsAndAccount(plan?: string): boolean {
  return isEnterprisePlan(plan);
}

export function canAccessPricing(plan?: string, sourceSessionId?: string | null): boolean {
  if (isEnterprisePlan(plan)) return true;
  return !!sourceSessionId;
}

export function getRetryRouteForPlan(plan?: string): string {
  return isEnterprisePlan(plan) ? ROUTES.upload : ROUTES.quickMatch;
}
