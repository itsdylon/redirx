import { type ReactNode } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import type { AuthChangeEvent, Session } from '@supabase/supabase-js';
import { AuthProvider, useAuth } from '../../contexts/AuthContext';
import { PivotMigrationDetail } from '../PivotMigrationDetail';

// Real session bridge, component, API, headers and request scopes. External SDK
// auth verdict and unrelated navigation shell are the only component fixtures.
const sdk = vi.hoisted(() => ({ getSession: vi.fn(), onAuthStateChange: vi.fn(), signOut: vi.fn() }));
vi.mock('../../lib/supabase', () => ({ supabase: { auth: sdk } }));
vi.mock('@posthog/react', () => ({ usePostHog: () => null }));
vi.mock('../ToolLayout', () => ({ ToolLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const envelope = (data: unknown) => ({ contract_version: '1.0.0', migration_id: 'fixture', operation_id: null,
  status: 'succeeded', next_action: 'none', data, error: null });
const session = (owner: string, suffix = '') => ({ access_token: `token-${owner}${suffix}`,
  refresh_token: 'fixture', user: { id: owner }, expires_in: 3600, token_type: 'bearer' }) as Session;
const deferred = <T,>() => { let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
type Call = { path: string; init: RequestInit; owner: string; body: Record<string, any> };
let listeners: Set<(event: AuthChangeEvent, value: Session | null) => void>;
let calls: Call[];
let handler: (call: Call) => Response | Promise<Response>;
let current: Session;
const currentOwner = () => screen.getByTestId('current-owner');
const draft = (owner: string) => `https://new.example/${owner}-private-draft?Case=%2FKeep`;
const row = (owner: string) => ({ mapping_id: `mapping-${owner}`, old_url: `https://old.example/${owner}-private-source`,
  new_url: `https://new.example/${owner}`, revision: 0, review_status: 'needs_review',
  traffic_observed: false, traffic_clicks: null, jev_proposal: { target_url: `https://new.example/${owner}`,
    confidence: .7, pass: 1, seed_revision: 0, stale: false } });
const summary = (owner: string) => envelope({ migration: { id: `migration-${owner}`, name: `${owner}-private-migration` },
  run_id: `run-${owner}`, run: { status: 'succeeded' }, jev: { engine: 'jev-url-v1', pass: 1, seed_revision: 0, limits: { passes: 3 } },
  artifact: { artifact_id: `artifact-${owner}`, format: 'csv', included_count: 1, excluded_count: 0 } });
function ordinary(call: Call): Response {
  const { path, owner, init } = call;
  if (path === '/api/auth/me') return json({ user: { id: owner, email: `${owner}@example.test`, plan: 'free' } });
  if (!path.includes(`migration-${owner}`)) return json({ ...envelope({}), error: {
    code: 'not_found', message: 'Migration unavailable for this account.', retryable: false, next_action: 'none' } }, 404);
  if (init.method === 'PATCH') return json(envelope({ outcomes: [{ code: 'ok' }] }));
  if (path.endsWith('/matches')) return json(envelope({ items: [row(owner)], next_cursor: null, selection_revision: 0 }));
  if (path.includes('/artifacts/')) return json(envelope({ artifact_id: `artifact-${owner}`, format: 'csv', content: `${owner}-private-artifact` }));
  return json(summary(owner));
}
function Identity() { const { user } = useAuth(); return <><span data-testid="current-owner">{user?.id || 'none'}</span>
  <Link to="/migrations/migration-b">Open B migration</Link></>; }
function mount() { return render(<MemoryRouter initialEntries={['/migrations/migration-a']}><AuthProvider>
  <Identity /><Routes><Route path="/migrations/:migrationId" element={<PivotMigrationDetail />} /></Routes>
</AuthProvider></MemoryRouter>); }
function emit(value: Session, event: AuthChangeEvent = 'SIGNED_IN') {
  current = value; listeners.forEach(listener => listener(event, value));
}
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); current = session('a'); listeners = new Set(); calls = []; handler = ordinary;
  sdk.getSession.mockImplementation(async () => ({ data: { session: current }, error: null }));
  sdk.onAuthStateChange.mockImplementation(listener => { listeners.add(listener);
    return { data: { subscription: { unsubscribe: () => listeners.delete(listener) } } }; });
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const token = new Headers(init.headers).get('Authorization') || '';
    const owner = token.startsWith('Bearer token-b') ? 'b' : 'a';
    const call = { path: new URL(url, 'https://fixture.invalid').pathname, init, owner,
      body: init.body ? JSON.parse(String(init.body)) : {} }; calls.push(call);
    return Promise.resolve(handler(call));
  }));
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:fixture'), revokeObjectURL: vi.fn() }));
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe('real AuthProvider and companion account isolation', () => {
  it.each([
    { phase: 'summary', lateStatus: 200 }, { phase: 'matches', lateStatus: 200 },
    { phase: 'mutation', lateStatus: 200 }, { phase: 'artifact', lateStatus: 200 },
    { phase: 'mutation', lateStatus: 503 }, { phase: 'artifact', lateStatus: 401 },
  ])('ignores a late A $phase HTTP$lateStatus response after switching through a pending B profile', async ({ phase, lateStatus }) => {
    const user = userEvent.setup(); mount();
    const input = await screen.findByLabelText('Destination for https://old.example/a-private-source');
    await user.type(input, draft('a'));
    const oldResponse = deferred<Response>(); const bProfile = deferred<Response>();
    let held: Call | undefined;
    handler = call => {
      if (call.path === '/api/auth/me' && call.owner === 'b') return bProfile.promise;
      const shouldHold = call.owner === 'a' && (
        phase === 'summary' && call.path === '/api/v2/migrations/migration-a'
        || phase === 'matches' && call.path.endsWith('/matches') && !call.init.method
        || phase === 'mutation' && call.init.method === 'PATCH'
        || phase === 'artifact' && call.path.includes('/artifacts/'));
      if (shouldHold && !held) { held = call; return oldResponse.promise; }
      return ordinary(call);
    };
    if (phase === 'mutation') await user.click(screen.getByRole('button', { name: 'Set destination' }));
    else if (phase === 'artifact') await user.click(screen.getByRole('button', { name: 'Download redirects' }));
    else await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(held).toBeDefined());
    expect(held!.init.signal?.aborted).toBe(false);
    await act(async () => emit(session('b')));
    expect(currentOwner()).toHaveTextContent('none');
    expect(held!.init.signal?.aborted).toBe(true);
    expect(screen.queryByText('a-private-migration')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(draft('a'))).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download redirects' })).not.toBeInTheDocument();
    await act(async () => bProfile.resolve(json({ user: { id: 'b', email: 'b@example.test', plan: 'free' } })));
    await waitFor(() => expect(currentOwner()).toHaveTextContent('b'));
    await screen.findByText('Migration unavailable for this account.');
    await user.click(screen.getByRole('link', { name: 'Open B migration' }));
    const bInput = await screen.findByLabelText('Destination for https://old.example/b-private-source');
    expect(bInput).toHaveValue('');
    const before = calls.length;
    bInput.focus();
    // Complete despite abortion: exercise acknowledgement isolation, not only
    // cooperative network cancellation.
    await act(async () => oldResponse.resolve(lateStatus === 200 ? ordinary(held!) : json({ ...envelope({}), error: {
      code: 'unavailable', message: 'A-only stale error.', retryable: false, next_action: 'reconnect' } }, lateStatus)));
    expect(calls).toHaveLength(before);
    expect(bInput).toHaveFocus();
    expect(screen.queryByText(/Download prepared/)).not.toBeInTheDocument();
    expect(screen.queryByText(/a-private-source/)).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(draft('a'))).not.toBeInTheDocument();
    expect(screen.queryByText(/Destination approved/)).not.toBeInTheDocument();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(bInput).toHaveValue('');
    const bReads = calls.filter(call => call.path.includes('migration-b'));
    expect(bReads).toHaveLength(2);
    expect(bReads.every(call => call.owner === 'b')).toBe(true);
    expect(screen.queryByText('A-only stale error.')).not.toBeInTheDocument();
    if (phase === 'mutation') {
      await user.type(bInput, draft('b'));
      await user.click(screen.getByRole('button', { name: 'Set destination' }));
      await screen.findByText(/Destination approved for https:\/\/old.example\/b-private-source/);
      const bMutation = calls.find(call => call.owner === 'b' && call.init.method === 'PATCH')!;
      expect(bMutation.body.idempotency_key).not.toBe(held!.body.idempotency_key);
      expect(bMutation.body.decisions[0].target_url).toBe(draft('b'));
    }
  });

  it('preserves same-owner drafts and request identity through token rotation and a recoverable failure', async () => {
    const user = userEvent.setup(); mount();
    const input = await screen.findByLabelText('Destination for https://old.example/a-private-source');
    await user.type(input, draft('a'));
    handler = call => call.init.method === 'PATCH' ? json({ ...envelope({}), error: {
      code: 'unavailable', message: 'Temporary write outage.', retryable: true, next_action: 'retry' } }, 503) : ordinary(call);
    await user.click(screen.getByRole('button', { name: 'Set destination' }));
    await screen.findByText('Temporary write outage.');
    const first = calls.find(call => call.init.method === 'PATCH')!;
    await act(async () => emit(session('a', '-rotated'), 'TOKEN_REFRESHED'));
    expect(input).toHaveValue(draft('a')); expect(currentOwner()).toHaveTextContent('a');
    await user.click(screen.getByRole('button', { name: 'Set destination' }));
    const writes = calls.filter(call => call.init.method === 'PATCH');
    expect(writes).toHaveLength(2);
    expect(writes[1].body.idempotency_key).toBe(first.body.idempotency_key);
    expect(new Headers(writes[1].init.headers).get('Authorization')).toBe('Bearer token-a-rotated');
    expect(input).toHaveValue(draft('a'));
  });
});
