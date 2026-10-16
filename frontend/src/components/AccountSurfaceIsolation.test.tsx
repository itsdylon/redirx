import { type ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { AuthChangeEvent, Session } from '@supabase/supabase-js';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from '../contexts/AuthContext';
import { ApiKeysPanel } from './ApiKeysPanel';
import { Settings } from './Settings';
import { AllProjects } from './AllProjects';
import { WatchPage } from './WatchPage';
import { UploadPage } from './UploadPage';
import { queryKeys } from '../queries/queryKeys';

// Keep actual auth/profile bridge, owner remounts, React Query, API helpers,
// request headers and file validation. Only SDK verdict, transport and shell
// dependencies are fixtures; no real account, clipboard or provider is used.
const sdk = vi.hoisted(() => ({ getSession: vi.fn(), onAuthStateChange: vi.fn(), signOut: vi.fn(), refreshSession: vi.fn() }));
const notices = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('../lib/supabase', () => ({ supabase: { auth: sdk } }));
vi.mock('@posthog/react', () => ({ usePostHog: () => null }));
vi.mock('sonner', () => ({ toast: notices }));
vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock('./DashboardLayout', () => ({ DashboardLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock('../contexts/OnboardingContext', () => ({ useOnboarding: () => ({ onboarding: null, loading: false }) }));
vi.mock('./DomainDiscoveryPanel', () => ({ DomainDiscoveryPanel: () => <p>Discovery fixture</p> }));
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const session = (owner: string, suffix = '') => ({ access_token: `token-${owner}${suffix}`, refresh_token: 'fixture',
  user: { id: owner }, expires_in: 3600, token_type: 'bearer' }) as Session;
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; }); return { promise, resolve, reject }; };
type Call = { path: string; init: RequestInit; owner: string };
let current: Session;
let listeners: Set<(event: AuthChangeEvent, value: Session | null) => void>;
let calls: Call[];
let handler: (call: Call) => Response | Promise<Response>;
const profile = (owner: string) => ({ id: owner, email: `${owner}@example.test`, full_name: `${owner}-private-name`, plan: 'free' });
const key = (owner: string) => ({ id: `key-${owner}`, name: `${owner}-private-key-name`, key_prefix: `rdx_${owner}`, created_at: '2026-10-15T00:00:00Z' });
const project = (owner: string) => ({ id: `project-${owner}`, project_name: `${owner}-private-project`, created_at: '2026-10-15T00:00:00Z',
  total_mappings: 10, approved_mappings: 5, status: 'completed', pipeline_type: 'url_only', source_session_id: null });
const monitor = () => ({ watch: { id: 'watch-a', session_id: 'project-a', old_domain: 'a-private.example', status: 'active',
  check_interval_minutes: 1440, created_at: '2026-10-15T00:00:00Z' }, issues: [{ id: 'issue-a', old_url: 'https://a-private.example/source',
  suggested_target: 'https://new.example/target', issue_type: 'wrong_target', severity: 'critical', hops: 1, clicks_at_risk: 1 }],
  checks: [], summary: { open_issues: 1, critical: 1, clicks_at_risk: 1 } });
function ordinary({ path, init, owner }: Call): Response {
  if (path === '/api/auth/me') return json({ user: profile(owner) });
  if (path.startsWith('/api/keys')) return json(init.method === 'POST' ? { success: true, key: { ...key(owner), key: `rdx_${owner}_private_plaintext` } }
    : init.method === 'DELETE' ? { success: true } : { success: true, keys: [key(owner)] });
  if (path === '/api/billing/status') return json({ plan: 'free', agency: { usage_pages: 0 }, manage_portal_available: false });
  if (path === '/api/email/preferences') return json(init.method === 'PUT' ? { success: true } : { preferences: [{ email_type: 'watch_alert', opted_out: owner === 'b' }] });
  if (path === '/api/user/profile') return json({ success: true, profile: profile(owner) });
  if (path.startsWith('/api/user/sessions')) return json(init.method === 'DELETE' ? { success: true } : { success: true, pagination: 'cursor',
    sessions: [project(owner)], total_count: 1, next_cursor: null, search: '' });
  if (path.startsWith('/api/watches')) {
    if (owner === 'b') return json({ error: 'This monitor is unavailable for B.' }, 404);
    return path.includes('/fixes/export') ? new Response('a-private-redirect-bytes')
      : json(init.method === 'PATCH' ? { success: true, watch: { ...monitor().watch, status: 'paused' } } : monitor());
  }
  throw new Error(`Unhandled fixture route ${path}`);
}
function Identity() { const { user, loading } = useAuth(); return <output data-testid="identity">{user?.id || (loading ? 'pending' : 'none')}</output>; }
function mount(surface: ReactNode, path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><AuthProvider><Identity />
    <Routes><Route path="*" element={surface} /><Route path="/watch/:watchId" element={surface} /></Routes>
  </AuthProvider></MemoryRouter></QueryClientProvider>);
  return { ...view, client };
}
function emit(owner: string, suffix = '', event: AuthChangeEvent = 'SIGNED_IN') {
  current = session(owner, suffix); listeners.forEach(listener => listener(event, current));
}
async function switchToPendingB(heldProfile: ReturnType<typeof deferred<Response>>) {
  const original = handler;
  handler = call => call.path === '/api/auth/me' && call.owner === 'b' ? heldProfile.promise : original(call);
  await act(async () => emit('b'));
  expect(screen.getByTestId('identity')).toHaveTextContent('pending');
  expect(localStorage.getItem('access_token')).toBe('token-b');
}
async function finishB(heldProfile: ReturnType<typeof deferred<Response>>) {
  await act(async () => heldProfile.resolve(json({ user: profile('b') })));
  await waitFor(() => expect(screen.getByTestId('identity')).toHaveTextContent('b'));
}
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); current = session('a'); calls = []; listeners = new Set(); handler = ordinary;
  sdk.getSession.mockImplementation(async () => ({ data: { session: current }, error: null }));
  sdk.refreshSession.mockImplementation(async () => ({ data: { session: current }, error: null }));
  sdk.onAuthStateChange.mockImplementation(listener => { listeners.add(listener);
    return { data: { subscription: { unsubscribe: () => listeners.delete(listener) } } }; });
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const owner = new Headers(init.headers).get('Authorization')?.startsWith('Bearer token-b') ? 'b' : 'a';
    const call = { path: new URL(url, 'https://fixture.invalid').pathname, init, owner }; calls.push(call);
    return Promise.resolve(handler(call));
  }));
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:fixture'), revokeObjectURL: vi.fn() }));
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe('account surfaces through real pending profile transitions', () => {
  it.each(['resolve', 'reject'])('does not publish retired one-time-key clipboard %s feedback into B', async outcome => {
    const user = userEvent.setup(); mount(<ApiKeysPanel />);
    await user.click(await screen.findByRole('button', { name: 'Create key' }));
    expect(await screen.findByLabelText('New API key')).toHaveValue('rdx_a_private_plaintext');
    const copied = deferred<void>(); vi.spyOn(navigator.clipboard, 'writeText').mockReturnValue(copied.promise);
    await user.click(screen.getByRole('button', { name: 'Copy' }));
    const b = deferred<Response>(); await switchToPendingB(b);
    expect(screen.queryByLabelText('New API key')).not.toBeInTheDocument();
    await finishB(b); await screen.findByText('b-private-key-name');
    await act(async () => outcome === 'resolve' ? copied.resolve() : copied.reject(new Error('retired clipboard failure')));
    expect(notices.success).not.toHaveBeenCalled(); expect(notices.error).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue('rdx_a_private_plaintext')).not.toBeInTheDocument();
  });

  it.each(['resolve', 'reject'])('still publishes current-owner clipboard %s feedback after same-owner rotation', async outcome => {
    const user = userEvent.setup(); mount(<ApiKeysPanel />);
    await user.click(await screen.findByRole('button', { name: 'Create key' }));
    await screen.findByLabelText('New API key');
    const copied = deferred<void>(); vi.spyOn(navigator.clipboard, 'writeText').mockReturnValue(copied.promise);
    await user.click(screen.getByRole('button', { name: 'Copy' }));
    await act(async () => emit('a', '-rotated', 'TOKEN_REFRESHED'));
    await act(async () => outcome === 'resolve' ? copied.resolve() : copied.reject(new Error('fixture clipboard unavailable')));
    if (outcome === 'resolve') {
      expect(notices.success).toHaveBeenCalledWith('Key copied');
      expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument();
    } else expect(notices.error).toHaveBeenCalledWith('Could not copy automatically — select the key and copy it');
    expect(screen.getByLabelText('New API key')).toHaveValue('rdx_a_private_plaintext');
  });

  it('keeps the same-owner key name after a recoverable error and retries with the rotated token', async () => {
    const user = userEvent.setup(); mount(<ApiKeysPanel />);
    await user.type(await screen.findByLabelText('Name'), 'unsaved key name');
    handler = call => call.path === '/api/keys' && call.init.method === 'POST'
      ? json({ error: 'Temporary key outage.' }, 503) : ordinary(call);
    await user.click(screen.getByRole('button', { name: 'Create key' }));
    await waitFor(() => expect(notices.error).toHaveBeenCalledWith('Temporary key outage.'));
    await act(async () => emit('a', '-rotated', 'TOKEN_REFRESHED'));
    expect(screen.getByLabelText('Name')).toHaveValue('unsaved key name');
    handler = ordinary;
    await user.click(screen.getByRole('button', { name: 'Create key' }));
    await screen.findByLabelText('New API key');
    const writes = calls.filter(call => call.path === '/api/keys' && call.init.method === 'POST');
    expect(writes).toHaveLength(2);
    expect(writes[1].init.body).toBe(JSON.stringify({ name: 'unsaved key name' }));
    expect(new Headers(writes[1].init.headers).get('Authorization')).toBe('Bearer token-a-rotated');
  });

  const phases = ['keys-read', 'keys-create', 'keys-delete', 'preferences-read', 'preferences-save', 'profile-save',
    'history-read', 'history-delete', 'watch-read', 'watch-save', 'watch-download'] as const;
  it.each(phases.flatMap(phase => [200, 401].map(status => ({ phase, status }))))(
    'ignores late A $phase HTTP$status after pending B then B', async ({ phase, status }) => {
      const user = userEvent.setup();
      const surface = phase.startsWith('keys') ? <ApiKeysPanel /> : phase.startsWith('preferences') || phase === 'profile-save'
        ? <Settings pivotEnabled /> : phase.startsWith('history') ? <AllProjects /> : <WatchPage />;
      const path = phase.startsWith('preferences') ? '/settings?tab=notifications' : phase.startsWith('watch') ? '/watch/watch-a' : '/';
      const view = mount(surface, path);
      await waitFor(() => expect(screen.getByTestId('identity')).toHaveTextContent('a'));
      if (phase.startsWith('keys')) await screen.findByText('a-private-key-name');
      else if (phase.startsWith('preferences')) await screen.findByRole('switch', { name: 'Monitoring Alert Emails' });
      else if (phase === 'profile-save') await screen.findByLabelText('Full Name');
      else if (phase.startsWith('history')) await screen.findByText('a-private-project');
      else await screen.findByText('Monitoring a-private.example');
      const old = deferred<Response>(); let held: Call | undefined;
      handler = call => {
        const match = call.owner === 'a' && (phase.startsWith('keys') && call.path.startsWith('/api/keys')
          || phase.startsWith('preferences') && call.path === '/api/email/preferences'
          || phase === 'profile-save' && call.path === '/api/user/profile'
          || phase.startsWith('history') && call.path.startsWith('/api/user/sessions')
          || phase.startsWith('watch') && call.path.startsWith('/api/watches'));
        if (match && !held) { held = call; return old.promise; }
        return ordinary(call);
      };
      if (phase === 'keys-read') void view.client.invalidateQueries({ queryKey: queryKeys.apiKeys.forOwner('a') });
      if (phase === 'keys-create') await user.click(screen.getByRole('button', { name: 'Create key' }));
      if (phase === 'keys-delete') { await user.click(screen.getByRole('button', { name: 'Delete' })); await user.click(screen.getByRole('button', { name: 'Delete key' })); }
      if (phase === 'preferences-read') void view.client.invalidateQueries({ queryKey: queryKeys.email.forOwner('a') });
      if (phase === 'preferences-save') await user.click(screen.getByRole('button', { name: 'Save Notification Preferences' }));
      if (phase === 'profile-save') await user.click(screen.getByRole('button', { name: 'Save Changes' }));
      if (phase === 'history-read') await user.click(screen.getByRole('button', { name: 'Reload first page' }));
      if (phase === 'history-delete') { await user.click(screen.getByRole('button', { name: 'Delete a-private-project' })); await user.click(screen.getByRole('button', { name: 'Delete permanently' })); }
      if (phase === 'watch-read') void view.client.refetchQueries();
      if (phase === 'watch-save') await user.click(screen.getByRole('button', { name: 'Pause' }));
      if (phase === 'watch-download') await user.click(screen.getByRole('button', { name: 'Download' }));
      await waitFor(() => expect(held).toBeDefined());
      const b = deferred<Response>(); await switchToPendingB(b);
      expect(screen.queryByText(/a-private-(key-name|project)/)).not.toBeInTheDocument();
      expect(screen.queryByLabelText('New API key')).not.toBeInTheDocument();
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(screen.queryByText('Monitoring a-private.example')).not.toBeInTheDocument();
      if (held!.init.signal) expect(held!.init.signal.aborted).toBe(true);
      await finishB(b);
      if (phase.startsWith('keys')) await screen.findByText('b-private-key-name');
      else if (phase.startsWith('preferences')) expect(await screen.findByRole('switch', { name: 'Monitoring Alert Emails' })).not.toBeChecked();
      else if (phase === 'profile-save') expect(await screen.findByLabelText('Full Name')).toHaveValue('b-private-name');
      else if (phase.startsWith('history')) await screen.findByText('b-private-project');
      else await screen.findByText('This monitor is unavailable for B.');
      const callCount = calls.length;
      await act(async () => old.resolve(status === 200 ? ordinary(held!) : json({ error: 'Unauthorized A-only stale error.', code: 'auth_invalid_token' }, status)));
      expect(screen.getByTestId('identity')).toHaveTextContent('b'); expect(localStorage.getItem('access_token')).toBe('token-b');
      expect(calls).toHaveLength(callCount); expect(sdk.signOut).not.toHaveBeenCalled();
      expect(notices.success).not.toHaveBeenCalled(); expect(notices.error).not.toHaveBeenCalled();
      expect(screen.queryByText('Unauthorized A-only stale error.')).not.toBeInTheDocument();
      expect(JSON.stringify(view.client.getMutationCache().getAll().map(m => m.state))).not.toContain('rdx_a_private_plaintext');
      expect(screen.queryByDisplayValue('rdx_a_private_plaintext')).not.toBeInTheDocument();
      expect(URL.createObjectURL).not.toHaveBeenCalled();
    });

  it('retains same-owner one-time key and profile draft across token rotation', async () => {
    const user = userEvent.setup(); const view = mount(<><ApiKeysPanel /><Settings pivotEnabled /></>);
    const input = await screen.findByLabelText('Full Name'); await user.clear(input); await user.type(input, 'unsaved name');
    await user.click(screen.getByRole('button', { name: 'Create key' }));
    expect(await screen.findByLabelText('New API key')).toHaveValue('rdx_a_private_plaintext');
    await act(async () => emit('a', '-rotated', 'TOKEN_REFRESHED'));
    expect(input).toHaveValue('unsaved name'); expect(screen.getByLabelText('New API key')).toHaveValue('rdx_a_private_plaintext');
    expect(JSON.stringify(view.client.getMutationCache().getAll().map(m => m.state))).not.toContain('rdx_a_private_plaintext');
    await user.click(screen.getByRole('button', { name: "I've saved it" }));
    await user.click(screen.getByRole('button', { name: 'Create key' }));
    const writes = calls.filter(call => call.path === '/api/keys' && call.init.method === 'POST');
    expect(writes).toHaveLength(2); expect(new Headers(writes[1].init.headers).get('Authorization')).toBe('Bearer token-a-rotated');
  });

  it.each(['load', 'error'])('ignores retired file-reader %s after owner loss and accepts B’s own file afterward', async event => {
    const user = userEvent.setup(); mount(<UploadPage quickOnly layoutVariant="tool" />);
    await user.click(await screen.findByRole('button', { name: 'Import a CSV or sitemap file' }));
    let reader: FileReader | undefined;
    const read = vi.spyOn(FileReader.prototype, 'readAsText').mockImplementation(function (this: FileReader) { reader = this; });
    const abort = vi.spyOn(FileReader.prototype, 'abort');
    fireEvent.change(screen.getByLabelText('Old Site CSV'), { target: { files: [new File(['/a-private-source'], 'a-private.csv', { type: 'text/csv' })] } });
    await screen.findByText('Checking a-private.csv…'); expect(reader).toBeDefined();
    const b = deferred<Response>(); await switchToPendingB(b); expect(abort).toHaveBeenCalledOnce();
    await finishB(b); await user.click(await screen.findByRole('button', { name: 'Import a CSV or sitemap file' }));
    Object.defineProperty(reader!, 'result', { value: '/a-private-source' });
    await act(async () => reader!.dispatchEvent(new ProgressEvent(event)));
    expect(screen.queryByText('a-private.csv')).not.toBeInTheDocument(); expect(screen.queryByText('Checking a-private.csv…')).not.toBeInTheDocument();
    read.mockRestore();
    fireEvent.change(screen.getByLabelText('Old Site CSV'), { target: { files: [new File(['/B%2FRaw?Case=Keep'], 'b-private.csv', { type: 'text/csv' })] } });
    await screen.findByText('b-private.csv'); expect(screen.queryByText('a-private.csv')).not.toBeInTheDocument();
    expect(calls.every(call => call.path === '/api/auth/me')).toBe(true);
  });
});
