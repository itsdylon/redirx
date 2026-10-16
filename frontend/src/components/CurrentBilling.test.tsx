import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import * as api from '../api/currentBilling';
import { CurrentBillingPage } from './CurrentBillingPage';
import { CurrentBillingReturn } from './CurrentBillingReturn';
vi.mock('../api/currentBilling', () => ({ getCurrentPricing: vi.fn(), getCurrentUsage: vi.fn(), getCurrentQuote: vi.fn(), createCurrentCheckout: vi.fn(), getCurrentCheckout: vi.fn(), createCurrentPortal: vi.fn(), billingRedirectUrl: vi.fn() }));
vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
const catalog = { enabled: true, version: 'current', plans: [
  { sku: 'studio', name: 'Studio', monthly_amount_cents: 7900, included_old_urls: 5000 },
  { sku: 'agency', name: 'Agency', monthly_amount_cents: 19900, included_old_urls: 15000 },
] } as api.CurrentPricing;
const paid = { checkout_id: 'c1', status: 'paid', sku: 'studio', amount_cents: 7900, currency: 'usd', checkout_url: null } as api.CurrentCheckout;
function show(path = '/billing/current') {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/billing/current" element={<CurrentBillingPage />} />
    <Route path="/billing/current/return" element={<CurrentBillingReturn />} />
  </Routes></MemoryRouter>);
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getCurrentPricing).mockResolvedValue(catalog);
  vi.mocked(api.getCurrentUsage).mockResolvedValue({ sku: 'free', included_old_urls: 0, used_old_urls: 0, extra_old_urls: 0, extra_amount_cents: 0, portal_available: false });
  vi.mocked(api.getCurrentCheckout).mockResolvedValue(paid);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('billing decisions', () => {
  it('shows no purchasing controls when disabled', async () => {
    vi.mocked(api.getCurrentPricing).mockResolvedValue({ ...catalog, enabled: false });
    show();
    expect(await screen.findByText('Paid checkout is not available yet.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Continue with|Pay .* once/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(api.getCurrentUsage).not.toHaveBeenCalled();
  });
  it('requires plan-specific recurring consent, then verifies server state', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createCurrentCheckout).mockResolvedValue({ ...paid, status: 'reserved' });
    show();
    const studio = await screen.findByRole('button', { name: 'Continue with Studio' });
    expect(studio).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /I agree to Studio/ }));
    await waitFor(() => expect(studio).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Continue with Agency' })).toBeDisabled();
    await user.click(studio);
    expect(api.createCurrentCheckout).toHaveBeenCalledWith(expect.objectContaining({ sku: 'studio', recurring_consent: true, idempotency_key: expect.any(String) }), expect.any(AbortSignal));
    expect(await screen.findByText(/Payment confirmed/)).toBeInTheDocument();
    expect(api.getCurrentCheckout).toHaveBeenCalledWith('c1', expect.any(AbortSignal));
  });
  it('preserves retry identity after an uncertain checkout failure', async () => {
    const user = userEvent.setup();
    vi.mocked(api.createCurrentCheckout).mockRejectedValue(new Error('Connection lost. Try again.'));
    show();
    await screen.findByText('No current subscription.');
    await user.click(screen.getByRole('checkbox', { name: /I agree to Studio/ }));
    await user.click(screen.getByRole('button', { name: 'Continue with Studio' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost');
    await user.click(screen.getByRole('button', { name: 'Continue with Studio' }));
    const calls = vi.mocked(api.createCurrentCheckout).mock.calls;
    expect(calls).toHaveLength(2); expect(calls[0][0]).toEqual(calls[1][0]);
  });
  it('uses server quote and exact inventories for a one-time checkout', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getCurrentQuote).mockResolvedValue({ old_urls: 501, amount_cents: 1100, stripe_quantity: 11 });
    vi.mocked(api.createCurrentCheckout).mockResolvedValue({ ...paid, sku: 'payg' });
    show('/billing/current?migration_id=m1&old_inventory_id=o1&new_inventory_id=n1');
    const button = await screen.findByRole('button', { name: 'Pay $11 once' });
    await waitFor(() => expect(button).toBeEnabled()); await user.click(button);
    expect(api.getCurrentQuote).toHaveBeenCalledWith('m1', 'o1', 'n1', expect.any(AbortSignal));
    expect(api.createCurrentCheckout).toHaveBeenCalledWith(expect.objectContaining({ sku: 'payg', migration_id: 'm1', inventory_ids: { old: 'o1', new: 'n1' }, recurring_consent: false }), expect.any(AbortSignal));
  });
  it('cannot purchase PAYG without explicit inventory pair or when quote fails', async () => {
    const first = show('/billing/current?migration_id=m1');
    await screen.findByText('No current subscription.');
    expect(api.getCurrentQuote).not.toHaveBeenCalled(); expect(screen.queryByRole('button', { name: /^Pay / })).not.toBeInTheDocument(); first.unmount();
    vi.mocked(api.getCurrentQuote).mockRejectedValue(new Error('Inventory not owned.'));
    show('/billing/current?migration_id=m1&old_inventory_id=o1&new_inventory_id=n1');
    expect(await screen.findByRole('alert')).toHaveTextContent('Inventory not owned');
    expect(screen.queryByRole('button', { name: /^Pay / })).not.toBeInTheDocument();
  });
  it('fails closed on catalog errors and allows a read retry', async () => {
    vi.mocked(api.getCurrentPricing).mockRejectedValueOnce(new Error('Unavailable'));
    const user = userEvent.setup(); show();
    expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable');
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Refresh billing' }));
    expect(await screen.findByRole('button', { name: 'Continue with Studio' })).toBeDisabled();
  });
});
describe('checkout return verification', () => {
  it('links back only to the migration returned by verified checkout', async () => {
    vi.mocked(api.getCurrentCheckout).mockResolvedValue({ ...paid, migration_id: 'owned-migration' });
    show('/billing/current/return?checkout_id=c1&migration_id=untrusted');
    expect(await screen.findByRole('link', { name: 'Return to migration' })).toHaveAttribute('href', '/migrations/owned-migration');
  });

  it.each(['open', 'reserved', 'expired', 'revoked'] as const)('does not trust success query when server says %s', async status => {
    vi.mocked(api.getCurrentCheckout).mockResolvedValue({ ...paid, status });
    show('/billing/current/return?checkout_id=c1&success=true&status=paid');
    await waitFor(() => expect(api.getCurrentCheckout).toHaveBeenCalled());
    expect(screen.queryByText(/Payment confirmed/)).not.toBeInTheDocument();
    expect(await screen.findByText(status === 'expired' ? /This checkout expired/ : status === 'revoked' ? /This checkout was revoked/ : /Payment has not been confirmed/)).toBeInTheDocument();
  });
  it('does not show success for missing ID or failed verification', async () => {
    const first = show('/billing/current/return?success=true');
    expect(await screen.findByRole('alert')).toHaveTextContent('missing its ID'); expect(api.getCurrentCheckout).not.toHaveBeenCalled(); first.unmount();
    vi.mocked(api.getCurrentCheckout).mockRejectedValue(new Error('Sign in again.'));
    show('/billing/current/return?checkout_id=c1&success=true');
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in again');
    expect(screen.queryByText(/Payment confirmed/)).not.toBeInTheDocument();
  });
  it('polls pending payment to a verified paid state, then stops', async () => {
    vi.useFakeTimers();
    vi.mocked(api.getCurrentCheckout).mockResolvedValueOnce({ ...paid, status: 'reserved' }).mockResolvedValue(paid);
    show('/billing/current/return?checkout_id=c1');
    await act(async () => {});
    expect(screen.getByText(/Payment has not been confirmed/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText(/Payment confirmed/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(api.getCurrentCheckout).toHaveBeenCalledTimes(2);
  });
  it('aborts pending verification when leaving the page', async () => {
    vi.mocked(api.getCurrentCheckout).mockImplementation(() => new Promise(() => {}));
    const view = show('/billing/current/return?checkout_id=c1');
    const signal = vi.mocked(api.getCurrentCheckout).mock.calls[0][1]; view.unmount();
    expect(signal?.aborted).toBe(true);
  });
});
