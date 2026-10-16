import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import App from './App';

const mockUseAuth = vi.fn();
const mockReviewInterface = vi.fn(({ layoutVariant }: { layoutVariant?: string }) => (
  <div>Review Page ({layoutVariant || 'dashboard'})</div>
));

vi.mock('./contexts/AuthContext', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('./components/LoginPage', () => ({ LoginPage: () => <div>Login Page</div> }));
vi.mock('./components/SignupPage', () => ({ SignupPage: () => <div>Signup Page</div> }));
vi.mock('./components/AuthCallback', () => ({ AuthCallback: () => <div>Auth Callback</div> }));
vi.mock('./components/OAuthConsentPage', () => ({ OAuthConsentPage: () => <div>OAuth Consent Page</div> }));
vi.mock('./components/Dashboard', () => ({ Dashboard: () => <div>Dashboard Page</div> }));
vi.mock('./components/AllProjects', () => ({ AllProjects: () => <div>Projects Page</div> }));
vi.mock('./components/UploadPage', () => ({ UploadPage: () => <div>Upload Page</div> }));
vi.mock('./components/ReviewInterface', () => ({ ReviewInterface: (props: { layoutVariant?: string }) => mockReviewInterface(props) }));
vi.mock('./components/AccountPage', () => ({ AccountPage: () => <div>Account Page</div> }));
vi.mock('./components/Settings', () => ({ Settings: () => <div>Settings Page</div> }));
vi.mock('./components/PricingPage', () => ({ PricingPage: () => <div>Pricing Page</div> }));
vi.mock('./components/DemoPage', () => ({ DemoPage: () => <div>Demo Page</div> }));
vi.mock('./components/QuickMatchLandingPage', () => ({
  QuickMatchLandingPage: () => <div>Quick Match Route</div>,
}));
vi.mock('./components/PivotCompanionPage', () => ({
  PivotCompanionPage: () => <div>Companion Page</div>,
}));
vi.mock('./components/CurrentBillingPage', () => ({ CurrentBillingPage: () => <div>Current Billing Page</div> }));
vi.mock('./components/CurrentBillingReturn', () => ({ CurrentBillingReturn: () => <div>Verified Checkout Return</div> }));
vi.mock('./components/ui/sonner', () => ({ Toaster: () => null }));

function renderAt(path: string, pivotEnabled?: boolean) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App pivotEnabled={pivotEnabled} />
    </MemoryRouter>
  );
}

describe('App routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReviewInterface.mockClear();
  });

  it('redirects unauthenticated root traffic to quick-match landing', async () => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    renderAt('/');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('routes authenticated free users to /quick-match from root', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'user@example.com', plan: 'free' },
    });
    renderAt('/');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('routes authenticated agency users to dashboard from root', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'user@example.com', plan: 'agency' },
    });
    renderAt('/');
    expect(await screen.findByText('Dashboard Page')).toBeInTheDocument();
  });

  it('routes /quick-match for unauthenticated users', async () => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    renderAt('/quick-match');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('routes OAuth consent without sending it through the normal home redirect', async () => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    renderAt('/oauth/consent?authorization_id=request-1');
    expect(await screen.findByText('OAuth Consent Page')).toBeInTheDocument();
  });

  it.each(['/login', '/signup'])('preserves consent for a signed-in visitor to %s', async (path) => {
    mockUseAuth.mockReturnValue({ user: { id: 'user-1', plan: 'free' }, loading: false });
    renderAt(`${path}?redirect=${encodeURIComponent('/oauth/consent?authorization_id=request-1')}`);
    expect(await screen.findByText('OAuth Consent Page')).toBeInTheDocument();
  });

  it('routes /quick-match for authenticated tool users', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'user@example.com', plan: 'free' },
    });
    renderAt('/quick-match');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('redirects enterprise users away from /quick-match', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'user@example.com', plan: 'agency' },
    });
    renderAt('/quick-match');
    expect(await screen.findByText('Upload Page')).toBeInTheDocument();
  });

  it('redirects non-agency users away from dashboard', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'user@example.com', plan: 'free' },
    });
    renderAt('/dashboard');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('does not expose /start route', () => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    renderAt('/start');
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in to RedirX' })).toHaveAttribute('href', '/login');
  });

  it('redirects tool users away from /upload', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'tool@example.com', plan: 'free' },
    });
    renderAt('/upload');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('uses tool layout for tool-user review routes', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'tool@example.com', plan: 'free' },
    });
    renderAt('/review/session-1');
    expect(await screen.findByText('Review Page (tool)')).toBeInTheDocument();
  });

  it('uses dashboard layout for enterprise review routes', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'agency@example.com', plan: 'agency' },
    });
    renderAt('/review/session-1');
    expect(await screen.findByText('Review Page (dashboard)')).toBeInTheDocument();
  });

  it('redirects tool users from /pricing without source session id', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'tool@example.com', plan: 'free' },
    });
    renderAt('/pricing');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it('allows tool users on /pricing with source session id', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'tool@example.com', plan: 'free' },
    });
    renderAt('/pricing?source_session_id=session-1');
    expect(await screen.findByText('Pricing Page')).toBeInTheDocument();
  });

  it('redirects tool users away from /settings and /account', async () => {
    mockUseAuth.mockReturnValue({
      loading: false,
      user: { id: 'u1', email: 'tool@example.com', plan: 'free' },
    });
    const settingsRender = renderAt('/settings');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
    settingsRender.unmount();

    renderAt('/account');
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });
});

function CurrentRoute() {
  const location = useLocation();
  return <output aria-label="Current route">{location.pathname + location.search + location.hash}</output>;
}

describe('unauthenticated pivot deep links', () => {
  it.each([
    '/companion?tab=history',
    '/migrations/migration-1?monitoring_id=monitor-1&checkout_id=oneoff-1',
    '/billing/subscriptions/return?checkout_id=subscription-1',
    '/billing/current?migration_id=m1&old_inventory_id=o1&new_inventory_id=n1',
    '/billing/current/return?checkout_id=c1',
  ])('preserves %s through the actual App login redirect', async target => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    render(<MemoryRouter initialEntries={[target]}><App /><CurrentRoute /></MemoryRouter>);
    expect(await screen.findByText('Login Page')).toBeInTheDocument();
    const current = new URL(screen.getByLabelText('Current route').textContent!, 'https://fixture.invalid');
    expect(current.pathname).toBe('/login');
    expect(current.searchParams.get('redirect')).toBe(target);
  });
});

describe('actual pivot retirement routes', () => {
  it.each(['free', 'pro', 'agency'])('keeps account and settings accessible to %s in pivot mode', async plan => {
    mockUseAuth.mockReturnValue({ user: { id: `user-${plan}`, plan }, loading: false });
    for (const [path, title] of [['/account', 'Account Page'], ['/settings?tab=billing', 'Settings Page']]) {
      const view = render(<MemoryRouter initialEntries={[path]}><App pivotEnabled /></MemoryRouter>);
      expect(await screen.findByText(title)).toBeInTheDocument();
      view.unmount();
    }
  });

  it.each(['/quick-match/', '/Quick-Match', '/upload/', '/UPLOAD', '/dashboard/', '/DASHBOARD'])
    ('sends signed-out retired entry %s to companion login', async target => {
      mockUseAuth.mockReturnValue({ user: null, loading: false });
      render(<MemoryRouter initialEntries={[target]}><App pivotEnabled /><CurrentRoute /></MemoryRouter>);
      expect(await screen.findByText('Login Page')).toBeInTheDocument();
      const current = new URL(screen.getByLabelText('Current route').textContent || '/login', 'https://fixture.invalid');
      expect(current.pathname).toBe('/login');
      expect(current.searchParams.get('redirect')).toBe('/companion');
    });

  it.each(['free', 'pro', 'agency'])('sends signed-in %s users from retired entries to companion', async plan => {
    mockUseAuth.mockReturnValue({ user: { id: `user-${plan}`, plan }, loading: false });
    for (const target of ['/quick-match/', '/Quick-Match', '/upload/', '/UPLOAD', '/dashboard/', '/DASHBOARD']) {
      const view = render(<MemoryRouter initialEntries={[target]}><App pivotEnabled /></MemoryRouter>);
      expect(await screen.findByText('Companion Page')).toBeInTheDocument();
      view.unmount();
    }
  });

  it('leaves retired entries unchanged when the flag is off', async () => {
    mockUseAuth.mockReturnValue({ user: { id: 'legacy', plan: 'free' }, loading: false });
    renderAt('/quick-match/', false);
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
  });

  it.each(['/review/session-1', '/projects', '/account', '/settings?tab=billing'])('preserves signed-out return paths for retained %s', async target => {
    mockUseAuth.mockReturnValue({ user: null, loading: false });
    render(<MemoryRouter initialEntries={[target]}><App pivotEnabled /><CurrentRoute /></MemoryRouter>);
    expect(await screen.findByText('Login Page')).toBeInTheDocument();
    const current = new URL(screen.getByLabelText('Current route').textContent || '/login', 'https://fixture.invalid');
    expect(current.searchParams.get('redirect')).toBe(target);
  });
});


describe('profile retry route barrier', () => {
  it('keeps the consent deep link through profile failure and retry, without mounting consent or login early', async () => {
    const retryAuth = vi.fn(); const logout = vi.fn();
    const path = '/oauth/consent?authorization_id=fixture';
    const Location = () => { const location = useLocation(); return <output>{location.pathname + location.search}</output>; };
    const tree = () => <MemoryRouter initialEntries={[path]}><App pivotEnabled /><Location /></MemoryRouter>;
    mockUseAuth.mockReturnValue({ user: null, loading: false, authError: 'Unable to load your profile right now. Your sign-in is saved. Please try again.', retryAuth, logout });
    const view = render(tree());
    expect(screen.getByRole('alert')).toHaveTextContent('Your sign-in is saved');
    expect(screen.queryByText('OAuth Consent Page')).not.toBeInTheDocument();
    expect(screen.queryByText('Login Page')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(path);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }));
    expect(retryAuth).toHaveBeenCalledTimes(1); expect(logout).not.toHaveBeenCalled();
    mockUseAuth.mockReturnValue({ user: null, loading: true, authError: null, retryAuth, logout });
    view.rerender(tree());
    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(screen.queryByText('OAuth Consent Page')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(path);
    mockUseAuth.mockReturnValue({ user: { id: 'verified', plan: 'free' }, loading: false, authError: null, retryAuth, logout });
    view.rerender(tree());
    expect(screen.getByText('OAuth Consent Page')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(path);
  });

  it('offers explicit sign-out during a profile outage', async () => {
    const logout = vi.fn();
    mockUseAuth.mockReturnValue({ user: null, loading: false, authError: 'Profile unavailable.', retryAuth: vi.fn(), logout });
    renderAt('/companion', true);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Sign out' }));
    expect(logout).toHaveBeenCalledTimes(1);
  });
});

describe('current billing access', () => {
  it.each([true, false])('allows signed-in free users with pivot=%s', async pivotEnabled => {
    mockUseAuth.mockReturnValue({ user: { id: 'u1', plan: 'free' }, loading: false });
    const first = renderAt('/billing/current', pivotEnabled);
    expect(await screen.findByText('Current Billing Page')).toBeInTheDocument(); first.unmount();
    renderAt('/billing/current/return?checkout_id=c1', pivotEnabled);
    expect(await screen.findByText('Verified Checkout Return')).toBeInTheDocument();
  });
});

it('offers the authenticated home for unknown links without reviving retired routes', () => {
  mockUseAuth.mockReturnValue({ user: { id: 'u1', plan: 'free' }, loading: false });
  renderAt('/missing-link', true);
  expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Return to your workspace' })).toHaveAttribute('href', '/companion');
});
