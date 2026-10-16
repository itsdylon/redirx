import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReviewInterface } from './ReviewInterface';

const mockUseAuth = vi.fn();
const mockGetResults = vi.fn();
const mockGetDeepPreview = vi.fn();
const mockGetProjectUnlockStatus = vi.fn();

vi.mock('@posthog/react', () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

vi.mock('react-hotkeys-hook', () => ({
  useHotkeys: vi.fn(),
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('../contexts/OnboardingContext', () => ({
  useOnboarding: () => ({
    onboarding: null,
    completeStep: vi.fn(),
    completeOnboarding: vi.fn(),
    isStepCompleted: vi.fn().mockReturnValue(false),
  }),
}));

vi.mock('../api/pipeline', () => ({
  getResults: (...args: unknown[]) => mockGetResults(...args),
  getDeepPreview: (...args: unknown[]) => mockGetDeepPreview(...args),
}));

vi.mock('../api/billing', () => ({
  getProjectUnlockStatus: (...args: unknown[]) => mockGetProjectUnlockStatus(...args),
}));

vi.mock('./StatsBar', () => ({
  StatsBar: () => <div>Stats Bar</div>,
}));

vi.mock('./ReviewToolbar', () => ({
  ReviewToolbar: ({ onExportClick }: { onExportClick: () => void }) => <button onClick={onExportClick}>Export existing results</button>,
}));

vi.mock('./RedirectTable', () => ({
  RedirectTable: () => <div>Redirect Table</div>,
}));

vi.mock('./InlineEditDialog', () => ({
  InlineEditDialog: () => null,
}));

vi.mock('./ExportModal', () => ({
  ExportModal: ({ open }: { open: boolean }) => open ? <div>Existing result export options</div> : null,
}));
vi.mock('./WatchPrompt', () => ({ WatchPrompt: () => null }));

vi.mock('./KeyboardShortcutsDialog', () => ({
  KeyboardShortcutsDialog: () => null,
}));

vi.mock('./DeepMatchPreviewCard', () => ({
  DeepMatchPreviewCard: () => null,
}));

function renderReviewPage(pivotEnabled = false) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/review/session-1']}>
        <Routes>
          <Route path="/review/:sessionId" element={<ReviewInterface layoutVariant="tool" pivotEnabled={pivotEnabled} />} />
          <Route path="/review/purchased-result" element={<div>Purchased result route</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ReviewInterface regression', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({
      user: { id: 'tool-user', email: 'tool@example.com', plan: 'free' },
      logout: vi.fn().mockResolvedValue(undefined),
    });

    mockGetResults.mockResolvedValue({
      success: true,
      session: { id: 'session-1', pipeline_type: 'content' },
      mappings: [
        {
          id: 'row-1',
          oldUrl: '/old',
          newUrl: '/new',
          confidence: 0.9,
          confidenceBand: 'high',
          matchScore: 90,
          approved: true,
          warnings: [],
          pathSimilarity: 0.9,
          titleSimilarity: 0.8,
          contentSimilarity: 0.85,
        },
      ],
    });
    mockGetDeepPreview.mockResolvedValue(null);
    mockGetProjectUnlockStatus.mockResolvedValue(null);
  });

  it('does not render the deprecated in-content navigation helper box', async () => {
    renderReviewPage();

    await waitFor(() => {
      expect(mockGetResults).toHaveBeenCalledWith('session-1', expect.any(AbortSignal));
    });

    expect(screen.queryByText('Navigate back to previous projects or return to the main tool page.')).not.toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeInTheDocument();
  });

  it('keeps legacy paid results and export entry while retiring sample and quote acquisition', async () => {
    mockGetResults.mockResolvedValue({ success: true, session: { id: 'session-1', pipeline_type: 'url_only' }, mappings: [] });
    mockGetProjectUnlockStatus.mockResolvedValue({ has_quote: true, is_unlocked: true,
      quote_status: 'paid', deep_session_status: 'completed', deep_session_id: 'purchased-result' });
    renderReviewPage(true);
    expect(await screen.findByRole('button', { name: 'Open Deep Match Results' })).toBeInTheDocument();
    expect(mockGetDeepPreview).not.toHaveBeenCalled();
    expect(screen.queryByText(/Unlock Deep Match for semantic content/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View Project Pricing' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Export existing results' }));
    expect(screen.getByText('Existing result export options')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Open Deep Match Results' }));
    expect(await screen.findByText('Purchased result route')).toBeInTheDocument();
  });

  it.each([null, 'checkout_created'])('retains pending purchase status without an unlock CTA (%s)', async quoteStatus => {
    mockGetResults.mockResolvedValue({ success: true, session: { id: 'session-1', pipeline_type: 'url_only' }, mappings: [] });
    mockGetProjectUnlockStatus.mockResolvedValue({ has_quote: !!quoteStatus, is_unlocked: false, quote_status: quoteStatus });
    renderReviewPage(true);
    await waitFor(() => expect(mockGetProjectUnlockStatus).toHaveBeenCalledWith('session-1', expect.any(AbortSignal)));
    if (quoteStatus) expect(await screen.findByText(/Waiting for confirmation and deep-run queueing/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unlock Deep Match' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View Project Pricing' })).not.toBeInTheDocument();
    expect(mockGetDeepPreview).not.toHaveBeenCalled();
  });
});
