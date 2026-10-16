vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'owner-fixture' } }) }));
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import { PivotCompanionPage } from '../PivotCompanionPage';
import { PivotMigrationDetail } from '../PivotMigrationDetail';
import { SubscriptionCheckoutPanel } from '../SubscriptionCheckoutPanel';

// Keep the actual screens, controls, request-scope hook, API and auth headers.
// Only the unrelated authenticated navigation shell is replaced.
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'fixture-owner' } }) }));
vi.mock('../ToolLayout', () => ({ ToolLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
const envelope = (data: unknown, changes: Record<string, unknown> = {}) => ({ contract_version: '1.0.0',
  migration_id: 'm1', operation_id: 'op1', status: 'succeeded', next_action: 'none', data, error: null, ...changes });
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status,
  headers: { 'Content-Type': 'application/json' } });
const failure = (message = 'Try this request again.') => json(envelope({}, { error: {
  code: 'unavailable', message, retryable: true, next_action: 'retry',
} }), 503);
const row = (id: string, revision = 0) => ({ mapping_id: id, old_url: `https://old.example/${id}`,
  new_url: `https://new.example/${id}`, revision, review_status: 'needs_review', traffic_observed: false, traffic_clicks: null });
const detail = (changes = {}) => ({ migration: { id: 'm1', name: 'Autumn migration' }, run_id: 'run1',
  run: { status: 'succeeded' }, ...changes });
type Request = { url: URL; init: RequestInit; body: Record<string, any> };
let requests: Request[];
let handler: (request: Request) => Response | Promise<Response>;
const paths = (part: string, method?: string) => requests.filter(r => r.url.pathname.includes(part) && (!method || r.init.method === method));
function mountDetail(search = '') {
  return render(<MemoryRouter initialEntries={[`/migrations/m1${search}`]}><Routes>
    <Route path="/migrations/:migrationId" element={<PivotMigrationDetail />} />
  </Routes></MemoryRouter>);
}
function mountHistory() { return render(<MemoryRouter><PivotCompanionPage /></MemoryRouter>); }
function mountBilling(search = '', deploymentId?: string) {
  return render(<MemoryRouter initialEntries={[`/billing/subscriptions/return${search}`]}><SubscriptionCheckoutPanel deploymentId={deploymentId} /></MemoryRouter>);
}
function ordinary(r: Request): Response {
  if (r.url.pathname === '/api/v2/migrations/m1') return json(envelope(detail()));
  if (r.url.pathname.endsWith('/matches') && !r.init.method) return json(envelope({ items: [row('a')], next_cursor: null }));
  throw new Error(`Unexpected fixture request: ${r.init.method || 'GET'} ${r.url}`);
}

beforeEach(() => {
  requests = []; handler = ordinary; localStorage.setItem('access_token', 'browser-fixture');
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const request = { url: new URL(url, 'https://fixture.invalid'), init, body: init.body ? JSON.parse(String(init.body)) : {} };
    requests.push(request); return Promise.resolve(handler(request));
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); localStorage.clear(); vi.unstubAllGlobals(); });

describe('companion history through its actual API', () => {
  it.each([401, 200])('shows an auth failure on HTTP%s without claiming empty history', async status => {
    handler = () => json(envelope({ items: [] }, { error: { code: 'reconnect_required', message: 'Sign in again to view migrations.', retryable: false, next_action: 'reconnect' } }), status);
    mountHistory();
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in again');
    expect(screen.queryByText(/No migrations yet/)).not.toBeInTheDocument();
    expect(requests[0].init.headers).toMatchObject({ Authorization: 'Bearer browser-fixture' });
  });

  it('appends cursor pages and refreshes back to the first page', async () => {
    handler = r => json(envelope({ items: [{ id: r.url.searchParams.has('cursor') ? 'm2' : 'm1',
      name: r.url.searchParams.has('cursor') ? 'Second migration' : 'First migration' }],
      next_cursor: r.url.searchParams.has('cursor') ? null : 'opaque+/cursor' }));
    const user = userEvent.setup(); mountHistory();
    await user.click(await screen.findByRole('button', { name: 'Load more migrations' }));
    expect(await screen.findByRole('link', { name: /Second migration/ })).toHaveAttribute('href', '/migrations/m2');
    expect(screen.getByRole('link', { name: /First migration/ })).toBeInTheDocument();
    expect(requests[1].url.searchParams.get('cursor')).toBe('opaque+/cursor');
    await user.click(screen.getByRole('button', { name: 'Refresh history' }));
    await waitFor(() => expect(screen.queryByRole('link', { name: /Second migration/ })).not.toBeInTheDocument());
    expect(requests[2].url.searchParams.has('cursor')).toBe(false);
  });
});

describe('migration decisions and payment', () => {
  it('shows one-off checkout only for payment-required status and retains its retry key', async () => {
    let paid = false; let attempts = 0;
    handler = r => {
      if (r.url.pathname.endsWith('/checkout')) { attempts++; return attempts === 1 ? failure() : json(envelope({ state: 'open' })); }
      if (r.url.pathname === '/api/v2/migrations/m1') return json(envelope(detail({ run_id: undefined,
        quote_id: 'q1', quote: { amount_cents: 4900, currency: 'usd', kind: 'paid' } }),
        { operation_id: '11111111-1111-4111-8111-111111111111', status: paid ? 'succeeded' : 'payment_required', next_action: paid ? 'none' : 'complete_payment' }));
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail('?paid=true&payment_return=success');
    const button = await screen.findByRole('button', { name: 'Continue to checkout' });
    expect(screen.getByText('$49.00 for this migration.')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Payment required' })).getByText(/Test checkout only/)).toBeInTheDocument();
    expect(paths('/checkout')).toHaveLength(0);
    await user.click(button); await screen.findByText('Try this request again.');
    await user.click(button); await screen.findByText(/Checkout status: open/);
    const calls = paths('/quotes/q1/checkout', 'POST'); expect(calls).toHaveLength(2);
    expect(calls[0].body.operation_id).toBe('11111111-1111-4111-8111-111111111111');
    expect(calls[0].body.idempotency_key).toBeTruthy(); expect(calls[0].body).toEqual(calls[1].body);
    paid = true; await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Continue to checkout' })).not.toBeInTheDocument());
  });

  it('uses the persisted payment operation after page reentry, and changes retry identity for a new operation', async () => {
    let operation = '11111111-1111-4111-8111-111111111111';
    handler = r => r.url.pathname.endsWith('/checkout') ? json(envelope({ state: 'open' })) : json(envelope(
      detail({ run_id: undefined, quote_id: 'q1', quote: { operation_id: '33333333-3333-4333-8333-333333333333', amount_cents: 4900, currency: 'usd', kind: 'paid' } }),
      { operation_id: operation, status: 'payment_required', next_action: 'complete_payment' }));
    const user = userEvent.setup();
    const first = mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Continue to checkout' }));
    await screen.findByText(/Checkout status: open/);
    first.unmount();
    mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Continue to checkout' }));
    await screen.findByText(/Checkout status: open/);
    expect(paths('/checkout', 'POST').map(r => r.body.operation_id)).toEqual([operation, operation]);
    const previousKey = paths('/checkout', 'POST')[1].body.idempotency_key;
    operation = '22222222-2222-4222-8222-222222222222';
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue to checkout' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Continue to checkout' }));
    await screen.findByText(/Checkout status: open/);
    const changed = paths('/checkout', 'POST')[2];
    expect(changed.body.operation_id).toBe(operation);
    expect(changed.body.idempotency_key).not.toBe(previousKey);
  });

  it('does not create checkout or invent an operation when the server summary lacks payment identity', async () => {
    handler = () => json(envelope(detail({ run_id: undefined, quote_id: 'q1' }), {
      operation_id: null, status: 'payment_required', next_action: 'complete_payment',
    }));
    const user = userEvent.setup(); mountDetail();
    const button = await screen.findByRole('button', { name: 'Continue to checkout' });
    expect(button).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Payment details are incomplete');
    await user.click(button);
    expect(paths('/checkout')).toHaveLength(0);
  });

  it('refreshes exceptions after a saved decision even when the run ID is unchanged', async () => {
    let saved = false;
    handler = r => {
      if (r.init.method === 'PATCH') { saved = true; return json(envelope({ outcomes: [{ code: 'ok' }] })); }
      if (r.url.pathname.endsWith('/matches')) return json(envelope({ items: saved ? [row('b', 1)] : [row('a')], next_cursor: null }));
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByLabelText('Destination for https://old.example/b')).toBeInTheDocument();
    expect(screen.queryByLabelText('Destination for https://old.example/a')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Destination approved for https://old.example/a.')).toHaveFocus());
    expect(paths('/matches').every(r => r.url.pathname.includes('/runs/run1/'))).toBe(true);
    expect(paths('/matches', 'PATCH')[0].body.decisions).toEqual([{ mapping_id: 'a', expected_revision: 0, action: 'approve' }]);
    expect(paths('/api/v2/migrations/m1').filter(r => r.url.pathname === '/api/v2/migrations/m1')).toHaveLength(2);
  });

  it('does not claim there are no exceptions when the exception request fails', async () => {
    handler = r => r.url.pathname.endsWith('/matches') ? failure('Exception review is temporarily unavailable.') : ordinary(r);
    mountDetail();
    expect(await screen.findByRole('alert')).toHaveTextContent('Exception review is temporarily unavailable.');
    expect(screen.queryByText('No saved mappings match this filter.')).not.toBeInTheDocument();
  });

  it('retains a row and explains a per-row partial failure rather than reporting a saved decision', async () => {
    handler = r => r.init.method === 'PATCH'
      ? json(envelope({ outcomes: [{ code: 'revision_conflict', message: 'This mapping changed. Refresh before retrying.' }] }, { status: 'partial' })) : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This mapping changed');
    expect(screen.getByLabelText('Destination for https://old.example/a')).toBeInTheDocument();
    expect(paths('/matches').filter(r => !r.init.method)).toHaveLength(1);
  });

  it('reuses decision keys on retry but changes identity when the target changes', async () => {
    handler = r => r.init.method === 'PATCH' ? failure() : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a');
    await user.type(input, 'https://new.example/first');
    const save = screen.getByRole('button', { name: 'Set destination' });
    await user.click(save); await screen.findByRole('alert');
    await user.click(save); await screen.findByRole('alert');
    await user.clear(input); await user.type(input, 'https://new.example/second');
    await user.click(save); await screen.findByRole('alert');
    const calls = paths('/matches', 'PATCH'); expect(calls).toHaveLength(3);
    expect(calls[0].body.idempotency_key).toBe(calls[1].body.idempotency_key);
    expect(calls[2].body.idempotency_key).not.toBe(calls[1].body.idempotency_key);
    expect(calls[2].body.decisions[0]).toMatchObject({ expected_revision: 0, target_url: 'https://new.example/second' });
  });

  it('appends exceptions with the exact opaque cursor and renders unmeasured traffic honestly', async () => {
    handler = r => r.url.pathname.endsWith('/matches') ? json(envelope({
      items: [row(r.url.searchParams.has('cursor') ? 'b' : 'a')],
      next_cursor: r.url.searchParams.has('cursor') ? null : 'exceptions+/next',
    })) : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Load more mappings' }));
    expect(await screen.findByLabelText('Destination for https://old.example/b')).toBeInTheDocument();
    expect(screen.getByLabelText('Destination for https://old.example/a')).toBeInTheDocument();
    expect(paths('/matches')[1].url.searchParams.get('cursor')).toBe('exceptions+/next');
    expect(screen.getAllByText(/Search traffic not measured/)).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Load more mappings' })).not.toBeInTheDocument();
  });
});

describe('mapping visibility and filter pagination', () => {
  const unmatched = { ...row('unmatched'), new_url: null, review_status: 'unmatched' };
  const deferred = { ...row('deferred'), review_status: 'defer' };
  const approved = { ...row('approved'), review_status: 'approved' };

  it('defaults to all saved mappings including null targets, with explicit filters and loaded counts', async () => {
    handler = r => {
      if (!r.url.pathname.endsWith('/matches')) return ordinary(r);
      const filter = r.url.searchParams.get('filter');
      const items = filter === 'unmatched' ? [unmatched] : filter === 'needs_review' ? [row('review'), deferred]
        : filter === 'approved' ? [approved] : r.url.searchParams.has('cursor') ? [deferred, approved] : [unmatched, row('review')];
      return json(envelope({ items, next_cursor: filter === 'all' && !r.url.searchParams.has('cursor') ? 'all+/next' : null }));
    };
    const user = userEvent.setup(); mountDetail();
    expect(await screen.findByRole('heading', { name: 'Mapping review' })).toBeInTheDocument();
    await screen.findByLabelText('Destination for https://old.example/unmatched');
    expect(screen.getByRole('combobox', { name: 'Show mappings' })).toHaveValue('all');
    expect(paths('/matches')[0].url.searchParams.get('filter')).toBe('all');
    expect(screen.getByText('2 saved mappings loaded for this filter; more available.')).toBeInTheDocument();
    const unmatchedRow = screen.getByLabelText('Destination for https://old.example/unmatched').closest('li')!;
    expect(within(unmatchedRow).getByText(/No destination/)).toBeInTheDocument();
    expect(within(unmatchedRow).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(unmatchedRow).getByRole('button', { name: 'Defer' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Load more mappings' }));
    await screen.findByLabelText('Destination for https://old.example/approved');
    expect(screen.getByText('4 saved mappings loaded for this filter.')).toBeInTheDocument();
    expect(paths('/matches')[1].url.searchParams.get('filter')).toBe('all');
    expect(paths('/matches')[1].url.searchParams.get('cursor')).toBe('all+/next');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Show mappings' }), 'needs_review');
    await screen.findByLabelText('Destination for https://old.example/deferred');
    expect(screen.queryByLabelText('Destination for https://old.example/unmatched')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Destination for https://old.example/approved')).not.toBeInTheDocument();
    expect(paths('/matches')[2].url.searchParams.has('cursor')).toBe(false);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Show mappings' }), 'unmatched');
    await screen.findByLabelText('Destination for https://old.example/unmatched');
    expect(screen.getByText('1 saved mapping loaded for this filter.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Destination for https://old.example/deferred')).not.toBeInTheDocument();
  });

  it('aborts old-filter pagination and ignores late rows when the selection changes', async () => {
    let finish!: (response: Response) => void;
    handler = r => {
      if (!r.url.pathname.endsWith('/matches')) return ordinary(r);
      if (r.url.searchParams.has('cursor')) return new Promise(resolve => { finish = resolve; });
      const isUnmatched = r.url.searchParams.get('filter') === 'unmatched';
      return json(envelope({ items: isUnmatched ? [unmatched] : [row('a')], next_cursor: isUnmatched ? null : 'old-filter-cursor' }));
    };
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Load more mappings' }));
    const oldSignal = paths('/matches')[1].init.signal!;
    expect(oldSignal.aborted).toBe(false);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Show mappings' }), 'unmatched');
    await screen.findByLabelText('Destination for https://old.example/unmatched');
    expect(oldSignal.aborted).toBe(true);
    await act(async () => { finish(json(envelope({ items: [approved], next_cursor: 'stale-cursor' }))); });
    expect(screen.queryByLabelText('Destination for https://old.example/approved')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more mappings' })).not.toBeInTheDocument();
    expect(paths('/matches')[2].url.searchParams.get('filter')).toBe('unmatched');
    expect(paths('/matches')[2].url.searchParams.has('cursor')).toBe(false);
  });

  it('labels saved results from a failed run as incomplete instead of implying full coverage', async () => {
    handler = r => r.url.pathname.endsWith('/matches') ? json(envelope({ items: [], next_cursor: null }))
      : json(envelope(detail({ run: { status: 'failed' } }), { status: 'failed', next_action: 'retry' }));
    mountDetail();
    expect(await screen.findByText('No saved mappings match this filter.')).toBeInTheDocument();
    expect(screen.getByText('This run has not completed successfully. Saved mappings may be incomplete.')).toBeInTheDocument();
    expect(screen.queryByText(/No exceptions/)).not.toBeInTheDocument();
  });
});

describe('Search Console and monitoring', () => {
  it('loads properties and syncs only the explicitly selected property', async () => {
    handler = r => r.url.pathname.endsWith('/connections/search-console/actions') ? json(envelope(r.body.action === 'properties'
      ? { properties: [{ site_url: 'sc-domain:old.example' }, { site_url: 'https://old.example/blog/' }] }
      : { summary: 'Historical traffic synced.' })) : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    expect(await screen.findByRole('button', { name: 'Sync selected property' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Load properties' }));
    await user.selectOptions(await screen.findByLabelText('Search Console property'), 'https://old.example/blog/');
    await user.click(screen.getByRole('button', { name: 'Sync selected property' }));
    expect(await screen.findByText('Historical traffic synced.')).toBeInTheDocument();
    const calls = paths('/connections/search-console/actions');
    expect(calls.map(r => r.body.action)).toEqual(['properties', 'sync']);
    expect(calls[1].body).toMatchObject({ migration_id: 'm1', property: 'https://old.example/blog/' });
    expect(calls[1].body.idempotency_key).toBeTruthy();
  });

  it('uses the reentry monitoring ID for status and paged fixes, rendering measured coverage', async () => {
    handler = r => {
      if (r.url.pathname.endsWith('/monitoring')) return json(envelope({ state: 'active', coverage: { checked: 2, total: 5, unchecked: 3 } }));
      if (r.url.pathname.endsWith('/monitoring/fixes')) return json(envelope({ items: [{ issue_id: r.url.searchParams.get('after') === '-1' ? 'i1' : 'i2',
        source_url: r.url.searchParams.get('after') === '-1' ? 'https://old.example/broken' : 'https://old.example/offline',
        expected_url: 'https://new.example/fixed', evidence: { issue: 'wrong_destination' } }],
        next_cursor: r.url.searchParams.get('after') === '-1' ? 0 : null }));
      if (r.url.pathname === '/api/v2/migrations/m1') return json(envelope(detail({ monitoring_id: 'latest-other-monitor' })));
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail('?monitoring_id=monitor-reentry');
    expect(await screen.findByText('https://old.example/broken')).toBeInTheDocument();
    expect(screen.getByText('Coverage: 2 of 5 URLs checked. 3 unchecked.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Load more corrections' }));
    expect(await screen.findByText('https://old.example/offline')).toBeInTheDocument();
    expect(screen.getAllByText('wrong destination')).toHaveLength(2);
    const calls = paths('/monitoring'); expect(calls).toHaveLength(3);
    expect(calls.every(r => r.url.searchParams.get('monitoring_id') === 'monitor-reentry')).toBe(true);
    expect(calls[2].url.searchParams.get('after')).toBe('0');
  });
});

describe('subscription consent and request cancellation', () => {
  it('requires explicit recurring consent, keeps retry identity and resets consent when the SKU changes', async () => {
    handler = () => failure();
    const user = userEvent.setup(); mountBilling('', 'installed-deployment');
    const button = screen.getByRole('button', { name: 'Continue to secure checkout' });
    expect(screen.getByText(/Test checkout only/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox')).toHaveAccessibleName(/in test mode/);
    expect(button).toBeDisabled(); expect(requests).toHaveLength(0);
    await user.click(screen.getByRole('checkbox')); await user.click(button); await screen.findByRole('alert');
    await user.click(button); await screen.findByRole('alert');
    expect(requests[0].body).toMatchObject({ sku: 'studio', recurring_consent: true });
    expect(requests[0].body).not.toHaveProperty('deployment_id');
    expect(requests[0].body.idempotency_key).toBe(requests[1].body.idempotency_key);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Subscription' }), 'monitoring');
    expect(screen.getByRole('checkbox')).not.toBeChecked(); expect(button).toBeDisabled();
    await user.click(screen.getByRole('checkbox')); await user.click(button); await screen.findByRole('alert');
    expect(requests[2].body).toMatchObject({ sku: 'monitoring', deployment_id: 'installed-deployment', recurring_consent: true });
    expect(requests[2].body.idempotency_key).not.toBe(requests[0].body.idempotency_key);
  });

  it('does not treat a migration checkout return as a subscription checkout', async () => {
    handler = r => r.url.pathname.includes('/billing/subscription-checkouts/')
      ? json(envelope({}, { error: { code: 'not_found', message: 'Subscription checkout not found.', retryable: false, next_action: 'none' } }), 404)
      : ordinary(r);
    mountDetail('?checkout_id=oneoff-checkout&payment_return=success');
    await screen.findByLabelText('Destination for https://old.example/a');
    expect(paths('/billing/subscription-checkouts/')).toHaveLength(0);
    expect(screen.queryByText('Subscription checkout not found.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh checkout status' })).not.toBeInTheDocument();
  });

  it('treats a forged success return as a read of server state, never as paid authority', async () => {
    handler = () => json(envelope({ checkout_id: 'c1', state: 'open', sku: 'studio', monthly_amount_cents: 9900,
      currency: 'usd', interval: 'month', subscription: { status: 'incomplete', eligible: false } }));
    const user = userEvent.setup(); mountBilling('?checkout_id=c1&paid=true&success=true');
    expect(await screen.findByText('Checkout status: open')).toBeInTheDocument();
    expect(screen.getByText(/No current allowance is available/)).toBeInTheDocument();
    expect(screen.queryByText('Current allowance is available.')).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Refresh checkout status' }));
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests.every(r => r.url.pathname.endsWith('/subscription-checkouts/c1/return') && !r.init.method && !r.init.body)).toBe(true);
  });

  it('aborts a pending history request on unmount and ignores a late response', async () => {
    let finish!: (response: Response) => void;
    handler = () => new Promise(resolve => { finish = resolve; });
    const view = mountHistory();
    expect(requests).toHaveLength(1);
    const signal = requests[0].init.signal!; expect(signal.aborted).toBe(false);
    view.unmount(); expect(signal.aborted).toBe(true);
    await act(async () => { finish(json(envelope({ items: [{ id: 'late', name: 'Late history' }] }))); });
    expect(screen.queryByText('Late history')).not.toBeInTheDocument();
  });

  it('aborts an in-flight decision when the detail unmounts', async () => {
    let finish!: (response: Response) => void;
    handler = r => r.init.method === 'PATCH' ? new Promise(resolve => { finish = resolve; }) : ordinary(r);
    const user = userEvent.setup(); const view = mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Approve' }));
    const signal = paths('/matches', 'PATCH')[0].init.signal!;
    view.unmount(); expect(signal.aborted).toBe(true);
    await act(async () => { finish(json(envelope({ outcomes: [{ code: 'ok' }] }))); });
    expect(paths('/matches').filter(r => !r.init.method)).toHaveLength(1);
  });
});


describe('review continuity and correction safety', () => {
  it('reloads all opened pages with fresh cursors and revisions after a decision', async () => {
    let saved = false;
    handler = r => {
      if (r.init.method === 'PATCH') { saved = true; return json(envelope({ outcomes: [{ code: 'ok' }] })); }
      if (r.url.pathname.endsWith('/matches')) {
        const more = r.url.searchParams.has('cursor');
        return json(envelope({ items: [row(more ? 'b' : 'a', saved ? 7 : 2)], next_cursor: more ? null : saved ? 'fresh-cursor' : 'original-cursor' }));
      }
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Load more mappings' }));
    const second = (await screen.findByLabelText('Destination for https://old.example/b')).closest('li')!;
    await user.click(within(second).getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Approve' })[1]).toBeEnabled());
    expect(screen.getByLabelText('Destination for https://old.example/a')).toBeInTheDocument();
    expect(screen.getByLabelText('Destination for https://old.example/b')).toBeInTheDocument();
    const reads = paths('/matches').filter(r => !r.init.method);
    expect(reads.map(r => r.url.searchParams.get('cursor'))).toEqual([null, 'original-cursor', null, 'fresh-cursor']);
    await user.click(screen.getAllByRole('button', { name: 'Approve' })[1]);
    await waitFor(() => expect(paths('/matches', 'PATCH')).toHaveLength(2));
    expect(paths('/matches', 'PATCH')[1].body.decisions[0].expected_revision).toBe(7);
  });

  it('preserves drafts but locks stale decisions when reloading mappings fails', async () => {
    let broken = false;
    handler = r => r.url.pathname.endsWith('/matches') && broken ? failure('Review unavailable.') : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a');
    await user.type(input, 'https://new.example/draft');
    broken = true;
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByText('Review unavailable.');
    expect(input).toHaveValue('https://new.example/draft');
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Set destination' })).toBeDisabled();
    broken = false;
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Set destination' })).toBeEnabled());
  });

  it.each(['https', 'HTTPS', 'hTtPs'])('validates correction URLs and preserves the %s scheme on keyboard submission', async (scheme) => {
    handler = r => r.init.method === 'PATCH' ? failure() : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a');
    await user.type(input, 'javascript:alert(1)');
    await user.click(screen.getByRole('button', { name: 'Set destination' }));
    expect(paths('/matches', 'PATCH')).toHaveLength(0);
    await user.clear(input);
    const destination = `${scheme}://new.example/Case%2FPath?x=A%20B`;
    await user.type(input, `${destination}{Enter}`);
    await screen.findByRole('alert');
    expect(paths('/matches', 'PATCH')[0].body.decisions[0].target_url).toBe(destination);
    expect(input).toHaveValue(destination);
  });

  it('never retains another migration’s details, draft or actions after navigation fails', async () => {
    handler = r => r.url.pathname === '/api/v2/migrations/m2' ? failure('Second migration unavailable.') : ordinary(r);
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={['/migrations/m1']}><Link to="/migrations/m2">Other migration</Link><Routes>
      <Route path="/migrations/:migrationId" element={<PivotMigrationDetail />} />
    </Routes></MemoryRouter>);
    await user.type(await screen.findByLabelText('Destination for https://old.example/a'), 'https://new.example/draft');
    await user.click(screen.getByRole('link', { name: 'Other migration' }));
    await screen.findByText('Second migration unavailable.');
    expect(screen.queryByText('Autumn migration')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('https://new.example/draft')).not.toBeInTheDocument();
  });
});


describe('review decisions, provenance and bounded browsing', () => {
  const jev = { engine: 'jev-url-v1', model: 'fixture', pass: 1, seed_revision: 0,
    limits: { old_pages: 500, new_pages: 2000, inventory_bytes: 2097152, passes: 3, new_runs_per_24h: 5 } };
  const proposal = { target_url: 'https://new.example/proposed', confidence: .3, pass: 1, seed_revision: 0, stale: false };
  it.each(['approved', 'approve', 'set_target', 'accept_repair'])('recognizes authoritative approved state %s without replacing it with a proposal', async review_status => {
    handler = r => r.url.pathname.endsWith('/matches') ? json(envelope({ items: [{ ...row('a'), review_status,
      decision_target: 'https://new.example/confirmed', jev_proposal: proposal }], next_cursor: null })) : json(envelope(detail({ jev })));
    mountDetail();
    expect(await screen.findByText('Approved destination')).toBeInTheDocument();
    expect(screen.getByText('https://new.example/confirmed')).toBeInTheDocument();
    expect(screen.getByText('Model proposal (does not replace your decision): https://new.example/proposed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm proposed destination' })).toBeDisabled();
    expect(paths('/matches', 'PATCH')).toHaveLength(0);
  });

  it('retains opaque cursors and incomplete coverage even when a 500-row request still has a next page', async () => {
    handler = r => r.url.pathname.endsWith('/matches') ? json(envelope({ items: [row(r.url.searchParams.has('cursor') ? 'b' : 'a')],
      next_cursor: r.url.searchParams.has('cursor') ? null : 'opaque+/cursor' })) : json(envelope(detail({ jev })));
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Load up to 500 mappings for this filter' }));
    await waitFor(() => expect(paths('/matches')).toHaveLength(2));
    expect(paths('/matches')[1].url.searchParams.get('limit')).toBe('500');
    expect(screen.getByText(/Find and ordering cover loaded mappings only/)).toBeInTheDocument();
    expect(screen.queryByText(/All saved mappings for this filter are loaded/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Load more mappings' }));
    await screen.findByLabelText('Destination for https://old.example/b');
    expect(paths('/matches')[2].url.searchParams.get('cursor')).toBe('opaque+/cursor');
    expect(paths('/matches')[2].url.searchParams.get('limit')).toBe('500');
    expect(screen.getByText(/All saved mappings for this filter are loaded/)).toBeInTheDocument();
  });

  it('keeps historical runs paginated without a Jev load-all or estimate-order control', async () => {
    mountDetail(); await screen.findByLabelText('Destination for https://old.example/a');
    expect(screen.queryByRole('button', { name: 'Load up to 500 mappings for this filter' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Order loaded mappings')).not.toBeInTheDocument();
  });

  it('focuses save failures in the affected row, clears obsolete success and retains the correction draft', async () => {
    let saves = 0;
    handler = r => r.init.method === 'PATCH' ? ++saves === 1 ? json(envelope({ outcomes: [{ code: 'ok' }] })) : failure('Decision unavailable.') : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Approve' }));
    await screen.findByText(/Destination approved for/);
    const input = screen.getByLabelText('Destination for https://old.example/a');
    await waitFor(() => expect(input).toBeEnabled()); await user.type(input, 'https://new.example/draft');
    await user.click(screen.getByRole('button', { name: 'Set destination' }));
    const error = await screen.findByRole('alert'); expect(error).toHaveTextContent('Decision unavailable.');
    expect(error.closest('li')).toBe(input.closest('li')); expect(error).toHaveFocus();
    expect(input).toHaveValue('https://new.example/draft');
    expect(screen.queryByText(/Destination approved for/)).not.toBeInTheDocument();
    const refresh = screen.getByRole('button', { name: 'Refresh status' });
    await user.click(refresh);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Set destination' })).toBeEnabled());
    expect(refresh).toHaveFocus();
  });
});

describe('selection revision consistency across pages', () => {
  it('rejects a newer page without merging it, keeps drafts and restores actions only after refresh', async () => {
    let revision = 4;
    handler = r => r.url.pathname.endsWith('/matches') ? json(envelope({ items: [row(r.url.searchParams.has('cursor') ? 'b' : 'a', revision)],
      next_cursor: r.url.searchParams.has('cursor') ? null : 'opaque', selection_revision: revision })) : ordinary(r);
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a');
    await user.type(input, 'https://new.example/draft'); revision = 5;
    await user.click(screen.getByRole('button', { name: 'Load more mappings' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Mappings changed since the previous page');
    expect(screen.queryByLabelText('Destination for https://old.example/b')).not.toBeInTheDocument();
    expect(input).toHaveValue('https://new.example/draft');
    expect(screen.getByRole('button', { name: 'Set destination' })).toBeDisabled();
    expect(screen.queryByText(/All saved mappings for this filter are loaded/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Set destination' })).toBeEnabled());
    expect(input).toHaveValue('https://new.example/draft');
    await user.click(screen.getByRole('button', { name: 'Load more mappings' }));
    await screen.findByLabelText('Destination for https://old.example/b');
  });

  it('does not install a mixed-revision reconstruction during an explicit refresh', async () => {
    let refreshing = false;
    handler = r => {
      if (r.url.pathname.endsWith('/matches')) {
        const second = r.url.searchParams.has('cursor');
        return json(envelope({ items: [row(second ? 'b' : 'a', refreshing ? 9 : 4)], next_cursor: second ? null : 'opaque',
          selection_revision: refreshing ? second ? 10 : 9 : 4 }));
      }
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail();
    await user.click(await screen.findByRole('button', { name: 'Load more mappings' }));
    const input = await screen.findByLabelText('Destination for https://old.example/b');
    await user.type(input, 'https://new.example/draft'); refreshing = true;
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Mappings changed while pages were loading');
    expect(input).toHaveValue('https://new.example/draft');
    expect(screen.getAllByRole('button', { name: 'Approve' }).every(button => button.hasAttribute('disabled'))).toBe(true);
    expect(screen.queryByText(/All saved mappings for this filter are loaded/)).not.toBeInTheDocument();
  });
});

describe('known stale authority recovery', () => {
  it('requires a fresh mapping read after conflict, preserves the draft, and submits only after another explicit click', async () => {
    let revision = 0;
    handler = r => {
      if (r.init.method === 'PATCH') {
        if (revision === 0) { revision = 1; return json(envelope({ outcomes: [{ code: 'revision_conflict' }] }, { status: 'partial' })); }
        expect(r.body.decisions[0]).toMatchObject({ expected_revision: 1, target_url: 'https://new.example/Typed%2FCase' });
        revision = 2; return json(envelope({ outcomes: [{ code: 'ok' }] }));
      }
      if (r.url.pathname.endsWith('/matches')) return json(envelope({ items: [{ ...row('a', revision), review_status: revision ? 'reject' : 'approve' }], next_cursor: null, selection_revision: revision }));
      if (r.url.pathname === '/api/v2/migrations/m1') return json(envelope(detail({ artifact: { artifact_id: 'snapshot', format: 'csv', included_count: 1, excluded_count: 0 } })));
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a');
    await user.type(input, 'https://new.example/Typed%2FCase');
    const save = screen.getByRole('button', { name: 'Set destination' });
    await user.click(save); await screen.findByRole('alert');
    expect(save).toBeDisabled(); expect(input).toHaveValue('https://new.example/Typed%2FCase');
    expect(screen.getByRole('button', { name: 'Download redirects' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Refresh mappings' }));
    expect(await screen.findByText(/Current mappings loaded/)).toBeInTheDocument();
    expect(screen.getByText(/Rejected\. Search traffic/)).toBeInTheDocument();
    expect(input).toHaveValue('https://new.example/Typed%2FCase'); expect(save).toBeEnabled();
    expect(paths('/matches', 'PATCH')).toHaveLength(1);
    await user.click(save); await screen.findByText(/Destination approved for/);
    const writes = paths('/matches', 'PATCH'); expect(writes).toHaveLength(2);
    expect(writes[1].body.idempotency_key).not.toBe(writes[0].body.idempotency_key);
  });
  it.each([401, 403, 404, 200])('invalidates action availability after HTTP%s download failure while retaining editable drafts', async status => {
    let accessLost = false;
    handler = r => {
      if (r.url.pathname.includes('/artifacts/')) {
        accessLost = true;
        return json(envelope({}, { error: { code: status === 404 ? 'not_found' : 'reconnect_required', message: 'Access to the saved file could not be confirmed.' } }), status);
      }
      if (r.url.pathname === '/api/v2/migrations/m1') return json(envelope(detail({
        artifact: accessLost ? undefined : { artifact_id: 'a1', format: 'csv', included_count: 1, excluded_count: 0 },
        jev: { engine: 'jev-url-v1', model: 'fixture', pass: 1, seed_revision: 0, limits: { passes: 3 } },
      })));
      return ordinary(r);
    };
    const user = userEvent.setup(); mountDetail();
    const input = await screen.findByLabelText('Destination for https://old.example/a'); await user.type(input, 'https://new.example/unsaved');
    await user.click(screen.getByRole('button', { name: 'Download redirects' })); await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: 'Download redirects' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Set destination' })).toBeDisabled();
    expect(screen.getByRole('heading', { name: 'Refresh status before continuing' })).toBeInTheDocument();
    expect(input).toBeEnabled(); expect(input).toHaveValue('https://new.example/unsaved');
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByRole('heading', { name: 'Prepare an export with your agent' });
    expect(screen.queryByRole('button', { name: 'Download redirects' })).toBeNull();
    expect(input).toHaveValue('https://new.example/unsaved');
    expect(paths('/matches', 'PATCH')).toHaveLength(0);
  });
});

it('keeps a correction draft after auth expires and after a failed recovery read', async () => {
  let blocked = false;
  handler = r => {
    if (r.init.method === 'PATCH') blocked = true;
    return blocked ? json(envelope({}, { error: { code: 'reconnect_required', message: 'Sign in again.' } }), 401) : ordinary(r);
  };
  const user = userEvent.setup(); mountDetail();
  const input = await screen.findByLabelText('Destination for https://old.example/a'); await user.type(input, 'https://new.example/private-draft');
  await user.click(screen.getByRole('button', { name: 'Set destination' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Copy unsaved destinations before leaving');
  expect(screen.getByRole('button', { name: 'Set destination' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Refresh mappings' }));
  await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(2));
  expect(input).toHaveValue('https://new.example/private-draft'); expect(input).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Set destination' })).toBeDisabled();
  expect(paths('/matches', 'PATCH')).toHaveLength(1);
});

it('keeps a draft discoverable when another reviewer removes its approval and the refreshed filter no longer contains it', async () => {
  let changed = false;
  handler = r => {
    if (r.init.method === 'PATCH') { changed = true; return json(envelope({ outcomes: [{ code: 'revision_conflict' }] }, { status: 'partial' })); }
    if (r.url.pathname.endsWith('/matches')) return json(envelope({
      items: changed && r.url.searchParams.get('filter') === 'approved' ? [] : [{ ...row('a', changed ? 1 : 0), review_status: changed ? 'reject' : 'approve' }],
      next_cursor: null, selection_revision: changed ? 1 : 0,
    }));
    return ordinary(r);
  };
  const user = userEvent.setup(); mountDetail(); await screen.findByLabelText('Destination for https://old.example/a');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Show mappings' }), 'approved');
  const input = await screen.findByLabelText('Destination for https://old.example/a'); await user.type(input, 'https://new.example/keep-draft');
  await user.click(screen.getByRole('button', { name: 'Set destination' })); await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: 'Refresh mappings' }));
  const notice = await screen.findByText(/Current mappings loaded/); expect(notice).toHaveFocus();
  expect(screen.queryByLabelText('Destination for https://old.example/a')).toBeNull();
  await user.selectOptions(screen.getByRole('combobox', { name: 'Show mappings' }), 'all');
  expect(await screen.findByLabelText('Destination for https://old.example/a')).toHaveValue('https://new.example/keep-draft');
  expect(paths('/matches', 'PATCH')).toHaveLength(1);
});


describe('authoritative companion polling lifetime', () => {
  it('does not overlap delayed reads, stops at terminal status and clears the pending timer on unmount', async () => {
    vi.useFakeTimers();
    let finish!: (response: Response) => void;
    handler = () => new Promise(resolve => { finish = resolve; });
    const view = mountDetail();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(requests).toHaveLength(1);
    await act(async () => { finish(json(envelope(detail({run:{status:'running'}}), {next_action:'poll',retry_after_seconds:10}))); });
    expect(paths('/matches')).toHaveLength(0);
    handler = () => json(envelope(detail({run_id:undefined,run:undefined}),{status:'failed',next_action:'refine_matches'}));
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    const terminalCount = requests.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(requests).toHaveLength(terminalCount);
    handler = () => json(envelope(detail({run:{status:'running'}}),{next_action:'poll',retry_after_seconds:10}));
    await act(async () => { screen.getByRole('button',{name:'Refresh status'}).click(); });
    const beforeUnmount = requests.length;
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(requests).toHaveLength(beforeUnmount);
  });
});
