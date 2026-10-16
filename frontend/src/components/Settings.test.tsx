import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();
const mockToastInfo = vi.fn();
vi.mock('sonner', () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    error: (...args: unknown[]) => mockToastError(...args),
    info: (...args: unknown[]) => mockToastInfo(...args),
  },
}));

const mockRefreshSession = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      id: 'user-1',
      email: 'jane@example.com',
      full_name: 'Jane Doe',
      plan: 'free',
    },
    loading: false,
    refreshSession: mockRefreshSession,
  }),
}));

const mockNavigate = vi.fn();
const mockSearchParams = new URLSearchParams();
const mockSetSearchParams = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useSearchParams: () => [mockSearchParams, mockSetSearchParams],
  };
});

vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: { children: React.ReactNode }) => <div data-testid="dashboard-layout">{children}</div> }));

vi.mock('./DashboardLayout', () => ({
  DashboardLayout: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div data-testid="dashboard-layout" data-title={title}>{children}</div>
  ),
}));

const mockGetBillingStatus = vi.fn();
const mockCreateAgencyCheckout = vi.fn();
const mockCreatePortalSession = vi.fn();
vi.mock('../api/billing', () => ({
  getBillingStatus: (...args: unknown[]) => mockGetBillingStatus(...args),
  createAgencyCheckout: (...args: unknown[]) => mockCreateAgencyCheckout(...args),
  createPortalSession: (...args: unknown[]) => mockCreatePortalSession(...args),
}));

const mockGetEmailPreferences = vi.fn();
const mockUpdateEmailPreference = vi.fn();
vi.mock('../api/email', () => ({
  getEmailPreferences: (...args: unknown[]) => mockGetEmailPreferences(...args),
  updateEmailPreference: (...args: unknown[]) => mockUpdateEmailPreference(...args),
}));

const mockUpdateUserProfile = vi.fn();
vi.mock('../api/user', () => ({
  updateUserProfile: (...args: unknown[]) => mockUpdateUserProfile(...args),
}));

import { Settings } from './Settings';

function renderSettings(initialTab = 'profile', pivotEnabled = false) {
  mockSearchParams.set('tab', initialTab);
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <Settings pivotEnabled={pivotEnabled} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockSearchParams.delete('tab');
  mockSearchParams.delete('status');
  localStorage.clear();

  mockGetBillingStatus.mockResolvedValue({
    plan: 'free',
    pricing_version: 'v1_2026_03',
    manage_portal_available: false,
    agency: {
      has_subscription: false,
      subscription_id: null,
      status: null,
      current_period_start: null,
      current_period_end: null,
      cancel_at_period_end: false,
      usage_pages: 0,
      overage_enabled: false,
    },
  });
  mockCreateAgencyCheckout.mockResolvedValue({
    success: true,
    url: '',
    checkout_session_id: 'cs_test_123',
  });
  mockCreatePortalSession.mockResolvedValue('https://billing.example.com');

  mockGetEmailPreferences.mockResolvedValue([
    { email_type: 'mapping_complete', opted_out: false },
    { email_type: 'mapping_failed', opted_out: false },
  ]);
  mockUpdateEmailPreference.mockResolvedValue(undefined);

  mockUpdateUserProfile.mockResolvedValue({ success: true, profile: { full_name: 'Jane Doe' } });
  mockRefreshSession.mockResolvedValue(undefined);

});

describe('Settings', () => {
  it('routes pivot allowances to the companion without offering legacy agency checkout', async () => {
    const user = userEvent.setup();
    renderSettings('subscription', true);
    await user.click(await screen.findByRole('button', { name: 'Manage migration allowances' }));
    expect(mockNavigate).toHaveBeenCalledWith('/companion');
    expect(screen.queryByRole('button', { name: 'Start Agency Checkout' })).not.toBeInTheDocument();
    expect(mockCreateAgencyCheckout).not.toHaveBeenCalled();
  });

  it('keeps existing agency usage and billing management available under the pivot', async () => {
    const user = userEvent.setup();
    const billing = await mockGetBillingStatus();
    mockGetBillingStatus.mockResolvedValue({ ...billing, plan: 'agency', manage_portal_available: true,
      agency: { ...billing.agency, has_subscription: true, usage_pages: 321 } });
    // An empty portal response lets us verify the real click without navigating jsdom.
    mockCreatePortalSession.mockResolvedValue('');
    renderSettings('subscription', true);
    expect(await screen.findByText('Usage this period: 321 pages')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Manage Billing' }));
    await waitFor(() => expect(mockCreatePortalSession).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Agency Plan')).not.toBeInTheDocument();
  });

  it('renders profile data and saves profile changes', async () => {
    const user = userEvent.setup();
    renderSettings('profile');

    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
    expect(screen.getByDisplayValue('jane@example.com')).toBeDisabled();

    const input = screen.getByLabelText('Full Name');
    await user.clear(input);
    await user.type(input, 'Jane Smith');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => {
      expect(mockUpdateUserProfile).toHaveBeenCalledWith({ full_name: 'Jane Smith' });
      expect(mockRefreshSession).toHaveBeenCalled();
    });
  });

  it('saves default settings to localStorage', async () => {
    const user = userEvent.setup();
    renderSettings('defaults');

    await user.click(screen.getByRole('button', { name: 'Save Defaults' }));

    expect(localStorage.getItem('redirx_default_export_format')).toBeTruthy();
    expect(localStorage.getItem('redirx_default_url_format')).toBeTruthy();
    expect(mockToastSuccess).toHaveBeenCalledWith('Default settings saved successfully.');
  });

  it('normalizes the legacy htaccess default to the actual apache exporter', async () => {
    localStorage.setItem('redirx_default_export_format', 'htaccess');
    localStorage.setItem('redirx_default_url_format', 'not-a-supported-format');
    renderSettings('defaults');
    expect(screen.getByLabelText('Default Export Format')).toHaveValue('apache');
    expect(screen.getByLabelText('Default URL Format')).toHaveValue('paths');
    await userEvent.click(screen.getByRole('button', { name: 'Save Defaults' }));
    expect(localStorage.getItem('redirx_default_export_format')).toBe('apache');
  });

  it('saves notification preferences', async () => {
    const user = userEvent.setup();
    renderSettings('notifications');

    await user.click(await screen.findByRole('button', { name: 'Save Notification Preferences' }));

    await waitFor(() => {
      expect(mockUpdateEmailPreference).toHaveBeenCalledTimes(4);
    });
  });

  it('starts agency checkout from subscription tab', async () => {
    const user = userEvent.setup();
    renderSettings('subscription');

    await user.click(await screen.findByRole('button', { name: 'Start Agency Checkout' }));

    await waitFor(() => {
      expect(mockCreateAgencyCheckout).toHaveBeenCalled();
      expect(mockToastError).toHaveBeenCalledWith('Checkout URL was not returned by billing service.');
    });
  });

  it('does not render legacy billing terminology in subscription UI', async () => {
    renderSettings('subscription');
    await screen.findByText('Agency Plan');

    const copy = (screen.getByTestId('dashboard-layout').textContent || '').toLowerCase();
    for (const legacyTerm of ['credits', 'starter', 'growth', 'scale']) {
      expect(copy).not.toContain(legacyTerm);
    }
  });
  it('does not overwrite unknown notification preferences after their read failed', async () => {
    mockGetEmailPreferences.mockRejectedValueOnce(new Error('Preferences unavailable'));
    renderSettings('notifications');
    expect(await screen.findByRole('button', { name: 'Reload notification preferences' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save Notification Preferences' })).not.toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(mockUpdateEmailPreference).not.toHaveBeenCalled();
  });

  it('does not claim payment success from a checkout return query parameter', async () => {
    mockSearchParams.set('status', 'success');
    renderSettings('subscription', true);
    await screen.findByText('Legacy account plan');
    expect(mockToastSuccess).not.toHaveBeenCalled();
    expect(mockToastInfo).toHaveBeenCalledWith('Returned from checkout. Checking billing status; payment is not confirmed yet.');
    expect(mockCreateAgencyCheckout).not.toHaveBeenCalled();
  });

  it('supports the historical billing tab alias and safely defaults unknown tabs', async () => {
    renderSettings('billing', true);
    expect(await screen.findByText('Legacy account plan')).toBeInTheDocument();
  });

  it('offers a safe retry when billing status is unavailable', async () => {
    mockGetBillingStatus.mockRejectedValueOnce(new Error('Billing unavailable'));
    renderSettings('subscription', true);
    expect(await screen.findByRole('button', { name: 'Reload billing status' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Manage Billing' })).not.toBeInTheDocument();
  });

});


it('loads and saves monitoring and legacy-tip opt-outs without activating pivot nudges', async () => {
  mockGetEmailPreferences.mockResolvedValue([
    { email_type: 'mapping_complete', opted_out: false },
    { email_type: 'mapping_failed', opted_out: false },
    { email_type: 'watch_alert', opted_out: true },
    { email_type: 'onboard_nudge', opted_out: true },
  ]);
  renderSettings('notifications', true);
  const watch = await screen.findByRole('switch', { name: 'Monitoring Alert Emails' });
  const tips = screen.getByRole('switch', { name: 'Onboarding Tip Emails' });
  expect(watch).not.toBeChecked();
  expect(tips).not.toBeChecked();
  expect(screen.getByText('Legacy onboarding tips are paused for the current workflow. Saving this preference does not activate them.')).toBeInTheDocument();
  await userEvent.click(watch);
  await userEvent.click(screen.getByRole('button', { name: 'Save Notification Preferences' }));
  await waitFor(() => expect(mockUpdateEmailPreference).toHaveBeenCalledWith('watch_alert', false));
  expect(mockUpdateEmailPreference).toHaveBeenCalledWith('onboard_nudge', true);
});

it('does not claim all preferences saved when one of four updates fails', async () => {
  mockUpdateEmailPreference.mockImplementation((type: string) => type === 'watch_alert' ? Promise.reject(new Error('Watch preference unavailable')) : Promise.resolve());
  renderSettings('notifications');
  await userEvent.click(await screen.findByRole('button', { name: 'Save Notification Preferences' }));
  await waitFor(() => expect(mockToastError).toHaveBeenCalledWith('Watch preference unavailable Some preferences may have saved; reload to check.'));
  expect(mockToastSuccess).not.toHaveBeenCalled();
});
