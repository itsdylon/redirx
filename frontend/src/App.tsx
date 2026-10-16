import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';
import { getAuthRedirect, sanitizeRedirectPath } from './lib/authRedirect';
import { MCP_PIVOT_ENABLED } from './api/config';
import { LoadBoundary } from './components/LoadBoundary';
import { Toaster } from './components/ui/sonner';
import { isEnterprisePlan } from './lib/plans';
import { Button } from './components/ui/button';
import {
  ROUTES,
  canAccessDashboard,
  canAccessPricing,
  canAccessQuickMatch,
  canAccessSettingsAndAccount,
  canAccessUpload,
  getAuthedHomeRoute,
  getPivotEntryRedirect,
} from './routes';

const LoginPage = lazy(() => import('./components/LoginPage').then(module => ({ default: module.LoginPage })));
const SignupPage = lazy(() => import('./components/SignupPage').then(module => ({ default: module.SignupPage })));
const AuthCallback = lazy(() => import('./components/AuthCallback').then(module => ({ default: module.AuthCallback })));
const OAuthConsentPage = lazy(() => import('./components/OAuthConsentPage').then(module => ({ default: module.OAuthConsentPage })));
const Dashboard = lazy(() => import('./components/Dashboard').then(module => ({ default: module.Dashboard })));
const AllProjects = lazy(() => import('./components/AllProjects').then(module => ({ default: module.AllProjects })));
const UploadPage = lazy(() => import('./components/UploadPage').then(module => ({ default: module.UploadPage })));
const ReviewInterface = lazy(() => import('./components/ReviewInterface').then(module => ({ default: module.ReviewInterface })));
const AccountPage = lazy(() => import('./components/AccountPage').then(module => ({ default: module.AccountPage })));
const Settings = lazy(() => import('./components/Settings').then(module => ({ default: module.Settings })));
const PricingPage = lazy(() => import('./components/PricingPage').then(module => ({ default: module.PricingPage })));
const DemoPage = lazy(() => import('./components/DemoPage').then(module => ({ default: module.DemoPage })));
const QuickMatchLandingPage = lazy(() => import('./components/QuickMatchLandingPage').then(module => ({ default: module.QuickMatchLandingPage })));
const WatchPage = lazy(() => import('./components/WatchPage').then(module => ({ default: module.WatchPage })));
const ApiKeysPage = lazy(() => import('./components/ApiKeysPage').then(module => ({ default: module.ApiKeysPage })));
const PivotCompanionPage = lazy(() => import('./components/PivotCompanionPage').then(module => ({ default: module.PivotCompanionPage })));
const PivotMigrationDetail = lazy(() => import('./components/PivotMigrationDetail').then(module => ({ default: module.PivotMigrationDetail })));

const CurrentBillingPage = lazy(() => import('./components/CurrentBillingPage').then(module => ({ default: module.CurrentBillingPage })));
const CurrentBillingReturn = lazy(() => import('./components/CurrentBillingReturn').then(module => ({ default: module.CurrentBillingReturn })));

const routeLoading = (
  <div className="min-h-screen flex items-center justify-center" role="status">
    <div className="text-muted-foreground">Loading...</div>
  </div>
);

export default function App({ pivotEnabled = MCP_PIVOT_ENABLED }: { pivotEnabled?: boolean } = {}) {
  const { user, loading, authError, retryAuth, logout } = useAuth();
  const location = useLocation();
  const authedHome = pivotEnabled ? ROUTES.companion : getAuthedHomeRoute(user?.plan);
  const pendingReturn = sanitizeRedirectPath(new URLSearchParams(location.search).get('redirect')) || getAuthRedirect();
  // A session may already exist when an MCP client opens the login URL.
  // Preserve its consent target without allowing login/signup redirect loops.
  const authReturn = pendingReturn && !['/login', '/signup'].includes(pendingReturn.split(/[?#]/)[0])
    ? pendingReturn : authedHome;
  const pricingSourceSessionId = new URLSearchParams(location.search).get('source_session_id');
  const reviewLayoutVariant = isEnterprisePlan(user?.plan) ? 'dashboard' : 'tool';
  const loginWithReturn = `${ROUTES.login}?redirect=${encodeURIComponent(location.pathname + location.search)}`;
  const retainedLogin = pivotEnabled ? loginWithReturn : ROUTES.login;

  // Show loading state while checking authentication
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-muted-foreground">Loading...</div>
      </div>
    );
  }

  // Keep the current deep link and SDK session while profile data is unavailable.
  // No account/plan-dependent route or consent request runs behind this barrier.
  if (authError) {
    return <main className="min-h-screen flex items-center justify-center p-4">
      <section className="max-w-md space-y-4 text-center" aria-labelledby="profile-retry-heading">
        <h1 id="profile-retry-heading" className="text-xl font-semibold">Your profile could not load</h1>
        <p role="alert" className="text-muted-foreground">{authError}</p>
        <div className="flex justify-center gap-3">
          <Button onClick={() => void retryAuth()}>Try again</Button>
          <Button variant="outline" onClick={() => void logout()}>Sign out</Button>
        </div>
      </section>
    </main>;
  }

  return (
    <LoadBoundary resetKey={location.pathname} homeHref={user ? authedHome : ROUTES.login}>
    <Suspense fallback={routeLoading}>
      <Routes>
        {/* Public routes */}
        <Route
          path={ROUTES.login}
          element={user ? <Navigate to={authReturn} replace /> : <LoginPage />}
        />
        <Route
          path={ROUTES.signup}
          element={user ? <Navigate to={authReturn} replace /> : <SignupPage />}
        />
        <Route
          path={ROUTES.authCallback}
          element={<AuthCallback />}
        />
        <Route path={ROUTES.oauthConsent} element={<OAuthConsentPage />} />
        <Route
          path={ROUTES.quickMatch}
          element={
            getPivotEntryRedirect(ROUTES.quickMatch, !!user, pivotEnabled)
              ? <Navigate to={getPivotEntryRedirect(ROUTES.quickMatch, !!user, pivotEnabled)!} replace />
              : user
              ? (
                canAccessQuickMatch(user?.plan)
                  ? <QuickMatchLandingPage />
                  : <Navigate to={`${ROUTES.upload}?mode=url_only`} replace />
              )
              : <QuickMatchLandingPage />
          }
        />
        <Route path={ROUTES.companion} element={user ? (pivotEnabled ? <PivotCompanionPage /> : <Navigate to={ROUTES.quickMatch} replace />) : <Navigate to={loginWithReturn} replace />} />
        <Route path={ROUTES.migration} element={user ? (pivotEnabled ? <PivotMigrationDetail /> : <Navigate to={ROUTES.quickMatch} replace />) : <Navigate to={loginWithReturn} replace />} />
        <Route path={ROUTES.currentBilling} element={user ? <CurrentBillingPage key={user.id + location.search} /> : <Navigate to={loginWithReturn} replace />} />
        <Route path={ROUTES.currentBillingReturn} element={user ? <CurrentBillingReturn key={user.id + location.search} /> : <Navigate to={loginWithReturn} replace />} />
        <Route path="/billing/subscriptions/return" element={user
          ? (pivotEnabled ? <PivotCompanionPage /> : <Navigate to={authedHome} replace />)
          : <Navigate to={loginWithReturn} replace />} />
        <Route
          path={ROUTES.demo}
          element={<DemoPage />}
        />
        {/* Protected routes */}
        <Route
          path={ROUTES.root}
          element={user ? <Navigate to={authedHome} replace /> : <Navigate to={ROUTES.quickMatch} replace />}
        />
        <Route
          path={ROUTES.dashboard}
          element={
            getPivotEntryRedirect(ROUTES.dashboard, !!user, pivotEnabled)
              ? <Navigate to={getPivotEntryRedirect(ROUTES.dashboard, !!user, pivotEnabled)!} replace />
              : user
              ? (canAccessDashboard(user?.plan) ? <Dashboard /> : <Navigate to={ROUTES.quickMatch} replace />)
              : <Navigate to={ROUTES.login} replace />
          }
        />
        <Route
          path={ROUTES.projects}
          element={user ? <AllProjects /> : <Navigate to={retainedLogin} replace />}
        />
        <Route
          path={ROUTES.upload}
          element={
            getPivotEntryRedirect(ROUTES.upload, !!user, pivotEnabled)
              ? <Navigate to={getPivotEntryRedirect(ROUTES.upload, !!user, pivotEnabled)!} replace />
              : user
              ? (canAccessUpload(user?.plan) ? <UploadPage /> : <Navigate to={ROUTES.quickMatch} replace />)
              : <Navigate to={ROUTES.login} replace />
          }
        />
        {/* Any signed-in account: a free plan can drive Quick Match over the
            API, so key management cannot sit behind the enterprise-only
            Settings page. */}
        <Route
          path={ROUTES.apiKeys}
          element={user ? <ApiKeysPage /> : <Navigate to={ROUTES.login} replace />}
        />
        <Route
          path={ROUTES.watch}
          element={user ? <WatchPage /> : <Navigate to={ROUTES.login} replace />}
        />
        <Route
          path={ROUTES.review}
          element={user ? <ReviewInterface layoutVariant={reviewLayoutVariant} /> : <Navigate to={retainedLogin} replace />}
        />
        <Route
          path={ROUTES.settings}
          element={
            user
              ? (
                (pivotEnabled || canAccessSettingsAndAccount(user?.plan))
                  ? <Settings />
                  : <Navigate to={ROUTES.quickMatch} replace />
              )
              : <Navigate to={retainedLogin} replace />
          }
        />
        <Route
          path={ROUTES.pricing}
          element={
            user
              ? (
                canAccessPricing(user?.plan, pricingSourceSessionId)
                  ? <PricingPage />
                  : <Navigate to={ROUTES.quickMatch} replace />
              )
              : <Navigate to={ROUTES.login} replace />
          }
        />
        <Route
          path={ROUTES.account}
          element={
            user
              ? (
                (pivotEnabled || canAccessSettingsAndAccount(user?.plan))
                  ? <AccountPage />
                  : <Navigate to={ROUTES.quickMatch} replace />
              )
              : <Navigate to={retainedLogin} replace />
          }
        />
        <Route path="*" element={<main className="mx-auto max-w-xl space-y-4 px-6 py-20">
          <h1 className="text-2xl font-semibold">Page not found</h1>
          <p className="text-muted-foreground">This address does not match a RedirX page. Check the link or return to your workspace.</p>
          <Button asChild><Link to={user ? authedHome : ROUTES.login}>{user ? 'Return to your workspace' : 'Sign in to RedirX'}</Link></Button>
        </main>} />
      </Routes>
    </Suspense>
    </LoadBoundary>
  );
}

// Add Toaster at the app level
export function AppWithToaster() {
  return (
    <>
      <App />
      <Toaster position="top-right" />
    </>
  );
}
