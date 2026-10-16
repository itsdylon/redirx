import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { WatchPrompt } from './WatchPrompt';
import { ISSUE_COPY, type IssueType } from '../api/watch';

const mockNavigate = vi.fn();

vi.mock('react-router-dom', () => ({
  useNavigate: () => mockNavigate,
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

// Plan decides which variant renders; default to a paid plan so the existing
// behaviour tests exercise the real prompt.
let mockPlan = 'agency';
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 'u1', plan: mockPlan } }),
}));

const listWatches = vi.fn();
const createWatch = vi.fn();

vi.mock('../api/watch', async () => {
  const actual = await vi.importActual<typeof import('../api/watch')>('../api/watch');
  return {
    ...actual,
    listWatches: (...args: unknown[]) => listWatches(...args),
    createWatch: (...args: unknown[]) => createWatch(...args),
  };
});

function renderPrompt(sessionId = 'session-123', pivotEnabled = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <WatchPrompt sessionId={sessionId} pivotEnabled={pivotEnabled} />
    </QueryClientProvider>
  );
  return { ...view, client };
}

describe('WatchPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPlan = 'agency';
  });

  it.each(['free', 'agency'])('retires new legacy monitoring acquisition for %s plans under the pivot', async plan => {
    mockPlan = plan;
    listWatches.mockResolvedValue([]);
    const { client } = renderPrompt('session-123', true);
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(listWatches).toHaveBeenCalled();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText(/On paid plans/)).not.toBeInTheDocument();
    expect(createWatch).not.toHaveBeenCalled();
  });

  it('preserves existing monitoring access after a plan lapses', async () => {
    mockPlan = 'free';
    listWatches.mockResolvedValue([{ id: 'watch-1', session_id: 'session-123', status: 'paused' }]);
    renderPrompt('session-123', true);
    await userEvent.click(await screen.findByRole('button', { name: 'View' }));
    expect(mockNavigate).toHaveBeenCalledWith('/watch/watch-1');
    expect(screen.getByText('Existing redirect monitor')).toBeInTheDocument();
    expect(screen.queryByText('Monitoring is on')).not.toBeInTheDocument();
    expect(createWatch).not.toHaveBeenCalled();
  });

  it('offers to start monitoring when the migration has no watch', async () => {
    listWatches.mockResolvedValue([]);
    renderPrompt();

    expect(await screen.findByRole('button', { name: /start monitoring/i })).toBeTruthy();
  });

  it('shows retry and never offers a duplicate monitor when history fails', async () => {
    listWatches.mockRejectedValue(new Error('Unavailable'));
    renderPrompt();
    expect(await screen.findByRole('alert')).toHaveTextContent('Monitoring status could not load');
    expect(screen.getByRole('button', { name: 'Retry monitoring status' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start monitoring/i })).not.toBeInTheDocument();
    expect(createWatch).not.toHaveBeenCalled();
  });

  it('starts a watch and opens it', async () => {
    listWatches.mockResolvedValue([]);
    createWatch.mockResolvedValue({ id: 'watch-9', session_id: 'session-123' });
    renderPrompt();

    await userEvent.click(await screen.findByRole('button', { name: /start monitoring/i }));

    await waitFor(() => {
      expect(createWatch).toHaveBeenCalledWith({ sessionId: 'session-123' }, expect.any(AbortSignal));
      expect(mockNavigate).toHaveBeenCalledWith('/watch/watch-9');
    });
  });

  it('links to the existing watch instead of offering a second one', async () => {
    // Otherwise a returning user doubles the probe traffic against their own
    // origin just by revisiting the review page.
    listWatches.mockResolvedValue([
      { id: 'watch-1', session_id: 'session-123', status: 'active' },
    ]);
    renderPrompt();

    expect(await screen.findByText(/monitoring is on/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /start monitoring/i })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /view/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/watch/watch-1');
  });

  it('shows an upsell instead of the prompt on a free plan', async () => {
    // Monitoring is paid; the server refuses creation for free accounts, so
    // offering the button would be a dead end. The card stays visible as an
    // upsell rather than vanishing.
    mockPlan = 'free';
    renderPrompt();

    expect(await screen.findByText(/on paid plans/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /start monitoring/i })).toBeNull();
    expect(listWatches).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /see plans/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/pricing');
  });

  it('ignores a watch belonging to a different migration', async () => {
    listWatches.mockResolvedValue([
      { id: 'watch-1', session_id: 'some-other-session', status: 'active' },
    ]);
    renderPrompt();

    expect(await screen.findByRole('button', { name: /start monitoring/i })).toBeTruthy();
  });
});

describe('issue copy', () => {
  it('has wording for every issue type the API can return', () => {
    // The union is the contract with the backend taxonomy; a type without
    // copy would render to the user as a raw snake_case string.
    const types: IssueType[] = [
      'no_redirect',
      'not_found',
      'server_error',
      'wrong_target',
      'redirect_chain',
      'temporary_redirect',
      'redirect_loop',
      'unreachable',
      'blocked',
    ];
    for (const type of types) {
      expect(ISSUE_COPY[type]?.label, type).toBeTruthy();
      expect(ISSUE_COPY[type]?.hint, type).toBeTruthy();
    }
    expect(Object.keys(ISSUE_COPY).sort()).toEqual([...types].sort());
  });
});
