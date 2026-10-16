import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverMock);

const mockNavigate = vi.fn();
const mockSearchParams = new URLSearchParams();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useSearchParams: () => [mockSearchParams, vi.fn()],
  };
});

vi.mock('./DashboardLayout', () => ({
  DashboardLayout: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div data-testid="dashboard-layout" data-title={title}>{children}</div>
  ),
}));
vi.mock('./ToolLayout', () => ({
  ToolLayout: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div data-testid="tool-layout" data-title={title}>{children}</div>
  ),
}));

const mockUseAuth = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => mockUseAuth(),
}));

const mockGetPricingEstimate = vi.fn();
const mockCreateProjectQuote = vi.fn();
const mockCreateProjectCheckout = vi.fn();
const mockCreateAgencyCheckout = vi.fn();
const mockGetBillingStatus = vi.fn();
const mockGetProjectUnlockStatus = vi.fn();
vi.mock('../api/billing', () => ({
  getPricingEstimate: (...args: unknown[]) => mockGetPricingEstimate(...args),
  createProjectQuote: (...args: unknown[]) => mockCreateProjectQuote(...args),
  createProjectCheckout: (...args: unknown[]) => mockCreateProjectCheckout(...args),
  createAgencyCheckout: (...args: unknown[]) => mockCreateAgencyCheckout(...args),
  getBillingStatus: (...args: unknown[]) => mockGetBillingStatus(...args),
  getProjectUnlockStatus: (...args: unknown[]) => mockGetProjectUnlockStatus(...args),
}));

import { PricingPage } from './PricingPage';

function renderPage(pivotEnabled = false) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const view = render(
    <QueryClientProvider client={queryClient}>
      <PricingPage pivotEnabled={pivotEnabled} />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({
    user: { id: 'agency-1', email: 'agency@example.com', plan: 'agency' },
  });
  mockSearchParams.forEach((_v, k) => mockSearchParams.delete(k));

  mockGetPricingEstimate.mockResolvedValue({
    pricing_version: 'v1_2026_03',
    currency: 'usd',
    contact_required: false,
    billable_pages: 5000,
    line_items: [
      { from_page: 1, to_page: 500, pages: 500, unit_price_usd: '0.100', amount_usd: '50.00', amount_cents: 5000 },
      { from_page: 501, to_page: 2000, pages: 1500, unit_price_usd: '0.050', amount_usd: '75.00', amount_cents: 7500 },
      { from_page: 2001, to_page: 5000, pages: 3000, unit_price_usd: '0.035', amount_usd: '105.00', amount_cents: 10500 },
    ],
    subtotal_usd: '230.00',
    subtotal_cents: 23000,
    effective_rate_usd: '0.046',
  });

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
    checkout_session_id: 'cs_agency',
  });

  mockCreateProjectCheckout.mockResolvedValue({
    success: true,
    already_paid: true,
    quote_id: 'quote-1',
    deep_session_id: '22222222-2222-2222-2222-222222222222',
  });
  mockGetProjectUnlockStatus.mockResolvedValue({ is_unlocked: false, quote_status: null });
});

describe('PricingPage', () => {
  it('unmounts acquisition in pivot mode without creating a quote or loading the estimator', async () => {
    mockSearchParams.set('source_session_id', 'previous-project');
    renderPage(true);
    expect(await screen.findByText('No confirmed purchase is available for this project.')).toBeInTheDocument();
    expect(mockGetProjectUnlockStatus).toHaveBeenCalledWith('previous-project', expect.any(AbortSignal));
    expect(mockCreateProjectQuote).not.toHaveBeenCalled();
    expect(mockGetPricingEstimate).not.toHaveBeenCalled();
    expect(mockGetBillingStatus).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Unlock Deep Match' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Start Agency Checkout' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Open migration companion' }));
    expect(mockNavigate).toHaveBeenCalledWith('/companion');
    expect(mockCreateProjectCheckout).not.toHaveBeenCalled();
    expect(mockCreateAgencyCheckout).not.toHaveBeenCalled();
  });

  it('keeps cancellation-return recovery read-only and trusts server-confirmed paid results', async () => {
    mockSearchParams.set('source_session_id', 'previous-project');
    mockSearchParams.set('status', 'cancelled');
    mockSearchParams.set('paid', 'true');
    mockGetProjectUnlockStatus.mockResolvedValueOnce({ is_unlocked: false, quote_status: 'checkout_created' })
      .mockResolvedValue({ is_unlocked: true, quote_status: 'paid', deep_session_id: 'paid-result' });
    renderPage(true);
    expect(await screen.findByText('Waiting for payment confirmation.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open purchased results' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh purchase status' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Open purchased results' }));
    expect(mockNavigate).toHaveBeenCalledWith('/review/paid-result');
    await userEvent.click(screen.getByRole('button', { name: 'Open previous project' }));
    expect(mockNavigate).toHaveBeenCalledWith('/review/previous-project');
    expect(mockCreateProjectQuote).not.toHaveBeenCalled();
    expect(mockCreateProjectCheckout).not.toHaveBeenCalled();
  });

  it('does not reuse prior-owner purchase status during an account switch', async () => {
    mockSearchParams.set('source_session_id', 'previous-project');
    mockGetProjectUnlockStatus.mockResolvedValue({is_unlocked:true,quote_status:'paid',deep_session_id:'owner-a-result'});
    const view=renderPage(true);
    await screen.findByRole('button',{name:'Open purchased results'});
    mockUseAuth.mockReturnValue({user:{id:'owner-b',plan:'free'}});
    let resolve!:(value:unknown)=>void;
    mockGetProjectUnlockStatus.mockReturnValue(new Promise(yes=>{resolve=yes;}));
    view.rerender(<QueryClientProvider client={view.queryClient}><PricingPage pivotEnabled /></QueryClientProvider>);
    expect(screen.queryByRole('button',{name:'Open purchased results'})).not.toBeInTheDocument();
    await act(async()=>resolve({is_unlocked:false,quote_status:null}));
    expect(await screen.findByText('No confirmed purchase is available for this project.')).toBeInTheDocument();
  });

  it('ignores a checkout completion from a retired owner', async () => {
    mockSearchParams.set('source_session_id','project-a');
    mockCreateProjectQuote.mockResolvedValue({id:'quote-a',source_session_id:'project-a',status:'draft',billable_pages:500,line_items:[],subtotal_cents:5000});
    let resolve!:(value:unknown)=>void;
    mockCreateProjectCheckout.mockReturnValue(new Promise(yes=>{resolve=yes;}));
    const view=renderPage();
    await userEvent.click(await screen.findByRole('button',{name:'Unlock Deep Match'}));
    mockUseAuth.mockReturnValue({user:{id:'owner-b',plan:'free'}});
    view.rerender(<QueryClientProvider client={view.queryClient}><PricingPage pivotEnabled={false} /></QueryClientProvider>);
    await act(async()=>resolve({already_paid:true,deep_session_id:'private-old-result'}));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('renders estimator mode without source_session_id', async () => {
    renderPage();

    expect(await screen.findByText('Project Pricing Estimator')).toBeInTheDocument();
    expect(await screen.findByText('$230.00')).toBeInTheDocument();
    expect(screen.getByText('Agency Plan')).toBeInTheDocument();

    const copy = (screen.getByTestId('dashboard-layout').textContent || '').toLowerCase();
    for (const legacyTerm of ['credits', 'starter', 'growth', 'scale']) {
      expect(copy).not.toContain(legacyTerm);
    }
  });

  it('renders quote mode and starts project checkout', async () => {
    mockSearchParams.set('source_session_id', '11111111-1111-1111-1111-111111111111');
    mockCreateProjectQuote.mockResolvedValue({
      id: 'quote-1',
      source_session_id: '11111111-1111-1111-1111-111111111111',
      user_id: 'user-1',
      old_url_count: 5000,
      new_url_count: 4500,
      billable_pages: 5000,
      pricing_version: 'v1_2026_03',
      currency: 'usd',
      line_items: [],
      subtotal_cents: 23000,
      status: 'draft',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('Unlock Deep Match For This Project')).toBeInTheDocument();
    const unlockButton = await screen.findByRole('button', { name: 'Unlock Deep Match' });

    await user.click(unlockButton);

    await waitFor(() => {
      expect(mockCreateProjectCheckout).toHaveBeenCalled();
      expect(mockNavigate).toHaveBeenCalledWith('/review/22222222-2222-2222-2222-222222222222');
    });
  });

  it('renders tool unlock-only mode for non-enterprise users', async () => {
    mockUseAuth.mockReturnValue({
      user: { id: 'tool-1', email: 'tool@example.com', plan: 'free' },
    });
    mockSearchParams.set('source_session_id', '11111111-1111-1111-1111-111111111111');
    mockCreateProjectQuote.mockResolvedValue({
      id: 'quote-1',
      source_session_id: '11111111-1111-1111-1111-111111111111',
      user_id: 'tool-1',
      old_url_count: 1200,
      new_url_count: 1100,
      billable_pages: 1200,
      pricing_version: 'v1_2026_03',
      currency: 'usd',
      line_items: [],
      subtotal_cents: 9000,
      status: 'draft',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderPage();

    expect(await screen.findByTestId('tool-layout')).toBeInTheDocument();
    expect(await screen.findByText('Unlock Deep Match For This Project')).toBeInTheDocument();
    expect(screen.queryByText('Agency Plan')).not.toBeInTheDocument();
  });
});
