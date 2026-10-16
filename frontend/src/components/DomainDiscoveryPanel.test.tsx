import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const auth = vi.hoisted(() => ({ user: { id: 'owner-a' } as { id: string } | null }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }));
const mockDiscoverSite = vi.fn();
vi.mock('../api/discovery', () => ({
  discoverSite: (...args: unknown[]) => mockDiscoverSite(...args),
}));

const mockGetGscStatus = vi.fn();
const mockGetGscProperties = vi.fn();
const mockGetGscConnectUrl = vi.fn();
vi.mock('../api/gsc', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/gsc')>();
  return {
    ...actual,
    getGscStatus: (...args: unknown[]) => mockGetGscStatus(...args),
    getGscProperties: (...args: unknown[]) => mockGetGscProperties(...args),
    getGscConnectUrl: (...args: unknown[]) => mockGetGscConnectUrl(...args),
  };
});

import { DomainDiscoveryPanel } from './DomainDiscoveryPanel';
import { formatProperty, propertyCovers } from '../api/gsc';

function renderPanel(side: 'old' | 'new' = 'old', onDiscovered = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <DomainDiscoveryPanel
        side={side}
        label={side === 'old' ? 'Old site' : 'New site'}
        onDiscovered={onDiscovered}
      />
    </QueryClientProvider>
  );
}

const DISCOVERY_RESULT = {
  success: true,
  root_url: 'https://example.com',
  urls: ['https://example.com/a'],
  count: 1,
  total_found: 1,
  truncated: false,
  max_urls: 1000,
  method: 'gsc',
  generator: null,
  plan: 'free',
};

beforeEach(() => {
  vi.clearAllMocks();
  auth.user = { id: 'owner-a' };
  mockDiscoverSite.mockResolvedValue(DISCOVERY_RESULT);
  mockGetGscStatus.mockResolvedValue({ success: true, connected: false, configured: true });
  mockGetGscProperties.mockResolvedValue([]);
});

describe('property formatting', () => {
  it('renders a domain property as a domain, not Google wire format', () => {
    expect(formatProperty('sc-domain:example.com')).toBe('example.com (all subdomains)');
  });

  it('strips scheme and trailing slash from a URL-prefix property', () => {
    expect(formatProperty('https://www.example.com/')).toBe('www.example.com');
  });
});

describe('property matching', () => {
  it('matches a domain property against its subdomains', () => {
    expect(propertyCovers('sc-domain:example.com', 'example.com')).toBe(true);
    expect(propertyCovers('sc-domain:example.com', 'shop.example.com')).toBe(true);
    expect(propertyCovers('sc-domain:example.com', 'notexample.com')).toBe(false);
  });

  it('is www-insensitive for URL-prefix properties', () => {
    expect(propertyCovers('https://www.example.com/', 'example.com')).toBe(true);
    expect(propertyCovers('https://example.com/', 'www.example.com')).toBe(true);
  });

  it('does not match a different host', () => {
    expect(propertyCovers('https://other.com/', 'example.com')).toBe(false);
  });

  it('tolerates a pasted URL rather than a bare host', () => {
    expect(propertyCovers('sc-domain:example.com', 'https://example.com/pricing')).toBe(true);
  });
});

describe('Search Console offer on the old side', () => {
  it('labels the action by outcome, never by the vendor integration', async () => {
    // Spec rule: the CTA sells what the user gets, not the plumbing.
    renderPanel('old');
    expect(await screen.findByRole('button', { name: /pages that get traffic/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect google search console/i })).toBeNull();
  });

  it('states the access is read-only before sending anyone to a consent screen', async () => {
    renderPanel('old');
    await screen.findByRole('button', { name: /pages that get traffic/i });
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
    expect(screen.getByText(/never change anything on your site/i)).toBeInTheDocument();
  });

  it('is absent on the new side, which has no traffic to lose', async () => {
    renderPanel('new');
    await screen.findByRole('button', { name: 'Find Pages' });
    expect(screen.queryByRole('button', { name: /pages that get traffic/i })).toBeNull();
    expect(mockGetGscStatus).not.toHaveBeenCalled();
  });

  it('offers nothing when the server has no OAuth client configured', async () => {
    mockGetGscStatus.mockResolvedValue({ success: true, connected: false, configured: false });
    renderPanel('old');
    await screen.findByRole('button', { name: 'Find Pages' });
    await waitFor(() => expect(mockGetGscStatus).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /pages that get traffic/i })).toBeNull();
  });
});

describe('property selection when connected', () => {
  beforeEach(() => {
    mockGetGscStatus.mockResolvedValue({ success: true, connected: true, configured: true });
  });

  it('names the property it will pull traffic from', async () => {
    mockGetGscProperties.mockResolvedValue([
      { site_url: 'sc-domain:example.com', permission_level: 'siteOwner' },
    ]);
    renderPanel('old');
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    expect(await screen.findByText(/example\.com \(all subdomains\)/)).toBeInTheDocument();
  });

  it('prefers the domain property when both cover the host', async () => {
    mockGetGscProperties.mockResolvedValue([
      { site_url: 'https://www.example.com/', permission_level: 'siteOwner' },
      { site_url: 'sc-domain:example.com', permission_level: 'siteOwner' },
    ]);
    renderPanel('old');
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    await screen.findByText(/all subdomains/);

    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    await waitFor(() =>
      expect(mockDiscoverSite).toHaveBeenCalledWith('example.com', 'old', 'sc-domain:example.com', expect.any(AbortSignal))
    );
  });

  it('says so plainly when no property covers the domain', async () => {
    mockGetGscProperties.mockResolvedValue([
      { site_url: 'sc-domain:other.com', permission_level: 'siteOwner' },
    ]);
    renderPanel('old');
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    expect(
      await screen.findByText(/No Search Console property covers this domain/)
    ).toBeInTheDocument();
  });

  it('discovers without a property when the user opts out of Search Console', async () => {
    mockGetGscProperties.mockResolvedValue([
      { site_url: 'sc-domain:example.com', permission_level: 'siteOwner' },
      { site_url: 'sc-domain:other.com', permission_level: 'siteOwner' },
    ]);
    renderPanel('old');
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    await userEvent.click(await screen.findByRole('button', { name: 'Change' }));
    await userEvent.selectOptions(screen.getByLabelText('Search Console property'), '');

    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    await waitFor(() =>
      expect(mockDiscoverSite).toHaveBeenCalledWith('example.com', 'old', null, expect.any(AbortSignal))
    );
  });
});


describe('discovery cancellation and account boundaries', () => {
  it('aborts a cancelled scan and ignores its late response', async () => {
    let finish!: (value: typeof DISCOVERY_RESULT) => void;
    mockDiscoverSite.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const onDiscovered = vi.fn();
    renderPanel('new', onDiscovered);
    await userEvent.type(screen.getByLabelText('New site domain'), 'example.com');
    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    const signal = mockDiscoverSite.mock.calls[0][3] as AbortSignal;
    await userEvent.click(screen.getByRole('button', { name: 'Cancel scan' }));
    expect(signal.aborted).toBe(true);
    await act(async () => finish(DISCOVERY_RESULT));
    expect(onDiscovered).toHaveBeenCalledTimes(1);
    expect(onDiscovered).toHaveBeenCalledWith('new', null);
    expect(screen.queryByText(/pages found via/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Find Pages' })).toBeEnabled();
  });
  it('aborts a pending scan on unmount without reporting its result', async () => {
    let finish!: (value: typeof DISCOVERY_RESULT) => void;
    mockDiscoverSite.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const callback = vi.fn();
    const view = renderPanel('new', callback);
    await userEvent.type(screen.getByLabelText('New site domain'), 'example.com');
    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    const signal = mockDiscoverSite.mock.calls[0][3] as AbortSignal;
    view.unmount();
    await act(async () => finish(DISCOVERY_RESULT));
    expect(signal.aborted).toBe(true);
    expect(callback).not.toHaveBeenCalled();
  });
  it('does not render a signed-out discovery surface', () => {
    auth.user = null;
    renderPanel();
    expect(screen.queryByRole('button', { name: 'Find Pages' })).not.toBeInTheDocument();
    expect(mockGetGscStatus).not.toHaveBeenCalled();
  });
});


describe('discovery owner isolation', () => {
  it('aborts the previous owner scan and does not reuse their cached properties', async () => {
    mockGetGscStatus.mockResolvedValue({ connected: true, configured: true });
    mockGetGscProperties.mockResolvedValue([{ site_url: 'sc-domain:example.com', permission_level: 'siteOwner' }]);
    let finish!: (value: unknown) => void;
    mockDiscoverSite.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const discovered = vi.fn();
    const ui = () => <QueryClientProvider client={client}><DomainDiscoveryPanel side="old" label="Old site" onDiscovered={discovered} /></QueryClientProvider>;
    const view = render(ui());
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    await screen.findByText(/all subdomains/);
    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    const signal = mockDiscoverSite.mock.calls[0][3] as AbortSignal;
    auth.user = { id: 'owner-b' };
    mockGetGscStatus.mockResolvedValue({ connected: false, configured: true });
    view.rerender(ui());
    expect(signal.aborted).toBe(true);
    expect(screen.getByLabelText('Old site domain')).toHaveValue('');
    expect(screen.queryByText(/all subdomains/)).toBeNull();
    await screen.findByRole('button', { name: /pages that get traffic/i });
    expect(mockGetGscStatus).toHaveBeenCalledTimes(2);
    expect(mockGetGscStatus.mock.calls[1][0]).toBeInstanceOf(AbortSignal);
    await act(async () => finish(DISCOVERY_RESULT));
    expect(discovered).not.toHaveBeenCalled();
    view.unmount();
    client.clear();
  });
  it('allows opting out when there is only one matching property', async () => {
    mockGetGscStatus.mockResolvedValue({ connected: true, configured: true });
    mockGetGscProperties.mockResolvedValue([{ site_url: 'sc-domain:example.com', permission_level: 'siteOwner' }]);
    renderPanel();
    await userEvent.type(screen.getByLabelText('Old site domain'), 'example.com');
    await userEvent.click(await screen.findByRole('button', { name: 'Change' }));
    await userEvent.selectOptions(screen.getByLabelText('Search Console property'), '');
    expect(screen.getByText('Search Console is off for this scan.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Find Pages' }));
    expect(mockDiscoverSite).toHaveBeenCalledWith('example.com', 'old', null, expect.any(AbortSignal));
  });
});
