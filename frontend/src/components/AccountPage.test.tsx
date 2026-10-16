import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AccountPage } from './AccountPage';

let owner = 'owner-a';
const profile = vi.fn();
const sessions = vi.fn();
const billing = vi.fn();
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: owner, email: `${owner}@example.com`, plan: 'free' }, refreshSession: vi.fn() }) }));
vi.mock('./DashboardLayout', () => ({ DashboardLayout: ({ children }: any) => <div>{children}</div> }));
vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: any) => <div>{children}</div> }));
vi.mock('../api/user', () => ({ getUserProfile: (...args: unknown[]) => profile(...args), updateUserProfile: vi.fn() }));
vi.mock('../api/sessions', () => ({ fetchSessionPage: (...args: unknown[]) => sessions(...args) }));
vi.mock('../api/billing', () => ({ getBillingStatus: (...args: unknown[]) => billing(...args) }));

function mounted() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const tree = () => <QueryClientProvider client={client}><MemoryRouter><AccountPage /></MemoryRouter></QueryClientProvider>;
  const view = render(tree());
  return { client, unmount: view.unmount, changeOwner: () => view.rerender(tree()) };
}

describe('owned account surface', () => {
  beforeEach(() => {
    vi.clearAllMocks(); owner = 'owner-a';
    profile.mockImplementation(async () => ({ profile: { id: owner, email: `${owner}@example.com`, full_name: owner, company: `${owner} company`, plan: 'free' } }));
    sessions.mockResolvedValue({ sessions: [] });
    billing.mockResolvedValue({ plan: 'free' });
  });
  it('never reuses cached profile, sessions or billing for the next account', async () => {
    const view = mounted();
    expect(await screen.findByText('owner-a company')).toBeInTheDocument();
    owner = 'owner-b';
    view.changeOwner();
    expect(screen.queryByText('owner-a company')).not.toBeInTheDocument();
    expect(await screen.findByText('owner-b company')).toBeInTheDocument();
    expect(profile).toHaveBeenCalledTimes(2);
    expect(sessions).toHaveBeenCalledTimes(2);
    expect(sessions).toHaveBeenCalledWith({ limit: 5 }, expect.any(AbortSignal));
    expect(billing).toHaveBeenCalledTimes(2);
  });
  it('shows unknown data as unavailable and does not edit fallback profile fields', async () => {
    profile.mockRejectedValue(new Error('Profile offline'));
    sessions.mockRejectedValue(new Error('Sessions offline'));
    billing.mockRejectedValue(new Error('Billing offline'));
    mounted();
    expect(await screen.findByRole('button', { name: 'Reload account data' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit Profile' })).toBeDisabled();
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
    expect(screen.queryByText('No projects yet.')).not.toBeInTheDocument();
  });

  it.each(['unmount', 'owner change'])('cancels all three unfinished account reads on %s', async action => {
    profile.mockImplementation(() => new Promise(() => {}));
    billing.mockImplementation(() => new Promise(() => {}));
    sessions.mockImplementation(() => new Promise(() => {}));
    const view = mounted();
    const signals = [profile.mock.calls[0][0], billing.mock.calls[0][0], sessions.mock.calls[0][1]] as AbortSignal[];
    expect(signals.every(signal => signal instanceof AbortSignal)).toBe(true);
    expect(signals.map(signal => signal.aborted)).toEqual([false, false, false]);
    if (action === 'unmount') view.unmount();
    else { owner = 'owner-b'; view.changeOwner(); }
    expect(signals.map(signal => signal.aborted)).toEqual([true, true, true]);
    if (action === 'owner change') {
      expect(profile.mock.calls[1][0].aborted).toBe(false);
      expect(billing.mock.calls[1][0].aborted).toBe(false);
      expect(sessions.mock.calls[1][1].aborted).toBe(false);
    }
  });
});
