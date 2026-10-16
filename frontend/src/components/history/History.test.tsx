import { cloneElement } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { AllProjects } from '../AllProjects';
import { Dashboard } from '../Dashboard';
import { queryKeys } from '../../queries/queryKeys';
const auth = vi.hoisted(() => ({ user: { id: 'a', plan: 'free' } }));
const success = vi.hoisted(() => vi.fn());
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('../../contexts/OnboardingContext', () => ({ useOnboarding: () => ({ onboarding: null, loading: false, entryModalOpen: false, setEntryModalOpen: vi.fn() }) }));
vi.mock('../ToolLayout', () => ({ ToolLayout: ({ children }: any) => <>{children}</> }));
vi.mock('../DashboardLayout', () => ({ DashboardLayout: ({ children }: any) => <>{children}</> }));
vi.mock('sonner', () => ({ toast: { success, error: vi.fn() } }));
const row = { id: 's1', project_name: 'Private Alpha', created_at: '2026-01-01', total_mappings: 10, approved_mappings: 5, status: 'completed', pipeline_type: 'url_only', source_session_id: null };
const summary = { success: true, total_sessions: 20, total_redirects: 200, approval_progress: 80, average_confidence: 0, recent_sessions: [row] };
const pageData = (sessions = [row], search = '') => ({ success: true, pagination: 'cursor', sessions, total_count: sessions.length, next_cursor: null, search });
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
let deleted: boolean;
beforeEach(() => {
  auth.user = { id: 'a', plan: 'free' }; deleted = false; success.mockClear();
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    if (init.method === 'DELETE') { deleted = true; return json({ success: true }); }
    if (url.endsWith('/dashboard')) return json({ ...summary, recent_sessions: deleted ? [] : [row] });
    return json(pageData(deleted ? [] : [row]));
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function mount(component = <AllProjects />, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const tree = () => <QueryClientProvider client={client}><MemoryRouter>{cloneElement(component)}</MemoryRouter></QueryClientProvider>;
  return { ...render(tree()), changeOwner: () => { auth.user = { id: 'b', plan: 'free' }; return tree(); }, client };
}
for (const [name, component] of [['history', <AllProjects />], ['dashboard', <Dashboard />]] as const) {
  it(`${name} names the permanent consequence and offers no fake Undo`, async () => {
    mount(component); const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Delete Private Alpha' }));
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText(/cannot be undone/)).toHaveTextContent('Private Alpha');
    expect(within(dialog).getByRole('button', { name: 'Keep project' })).toHaveFocus();
    await user.click(within(dialog).getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() => expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument());
    expect(success).toHaveBeenCalledWith('Project permanently deleted');
    expect(success.mock.calls.some(call => call[1]?.action)).toBe(false);
  });
}
it('retains the project and a retryable dialog when HTTP 200 does not confirm deletion', async () => {
  const original = fetch;
  vi.stubGlobal('fetch', vi.fn((url, init) => init?.method === 'DELETE' ? Promise.resolve(json({ success: false })) : original(url, init)));
  mount(); const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Delete Private Alpha' }));
  await user.click(screen.getByRole('button', { name: 'Delete permanently' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('not confirmed');
  expect(screen.getByText('Private Alpha')).toBeInTheDocument(); expect(success).not.toHaveBeenCalled();
});
it('Escape closes confirmation and returns keyboard focus to its trigger', async () => {
  mount(); const user = userEvent.setup(); const trigger = await screen.findByRole('button', { name: 'Delete Private Alpha' });
  await user.click(trigger); await user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  expect(trigger).toHaveFocus();
});
it('clears account history and pending deletion when ownership changes; ignores late success', async () => {
  let finish!: (response: Response) => void; let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (_url, init: RequestInit = {}) => {
    if (init.method === 'DELETE') { signal = init.signal as AbortSignal; return new Promise<Response>(resolve => { finish = resolve; }); }
    return auth.user.id === 'a' ? json(pageData()) : json({ error: 'unavailable' }, 503);
  }));
  const view = mount(); const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Delete Private Alpha' })); await user.click(screen.getByRole('button', { name: 'Delete permanently' }));
  view.rerender(view.changeOwner());
  expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument(); expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  expect(signal?.aborted).toBe(true);
  await act(async () => finish(json({ success: true })));
  expect(success).not.toHaveBeenCalled(); expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument();
});
it('uses whole-account approval progress and does not replace zero confidence with approval rates', async () => {
  mount(<Dashboard />); await screen.findByText('Private Alpha');
  expect(screen.getByText('80%')).toBeInTheDocument(); expect(screen.getByText('0%')).toBeInTheDocument();
  expect(screen.queryByText('50%')).not.toBeInTheDocument(); expect(screen.getByText('Model estimate; not a guarantee')).toBeInTheDocument();
});
it('does not display a prior owner dashboard cache after account change', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(queryKeys.dashboard.forOwner('a'), summary);
  auth.user = { id: 'b', plan: 'free' }; vi.stubGlobal('fetch', vi.fn(async () => json({}, 503)));
  mount(<Dashboard />, client);
  expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument();
  await screen.findByRole('button', { name: 'Retry' }); expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument();
});

it('searches all history on the server and preserves search during page failure and retry', async () => {
  let fail = true;
  const many = Array.from({ length: 50 }, (_, i) => ({ ...row, id: `s${i}`, project_name: `Visible ${i}` }));
  const requests: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'https://fixture.invalid'); requests.push(url);
    const search = url.searchParams.get('q') || '';
    if (search === 'Older unique') return json({ ...pageData([{ ...row, id: 'older', project_name: search }], search), total_count: 1 });
    if (url.searchParams.has('cursor')) {
      if (fail) return json({ error: 'History connection interrupted' }, 503);
      return json({ ...pageData([{ ...row, id: 'last', project_name: 'Last visible' }]), total_count: 1205 });
    }
    return json({ ...pageData(many), total_count: 1205, next_cursor: 'opaque-microseconds-preserved' });
  }));
  mount(); const user = userEvent.setup();
  await screen.findByText('1,205 projects · Page 1');
  expect(screen.getAllByRole('button', { name: 'Open Results' })).toHaveLength(50);
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('History connection interrupted');
  expect(screen.queryByText(/No projects found/)).not.toBeInTheDocument();
  expect(requests.at(-1)?.searchParams.get('cursor')).toBe('opaque-microseconds-preserved');
  fail = false;
  await user.click(screen.getByRole('button', { name: 'Retry' }));
  await screen.findByText('Last visible');
  await user.type(screen.getByRole('textbox', { name: 'Search project history' }), 'Older unique');
  await user.click(screen.getByRole('button', { name: /^Search$/ }));
  await screen.findByText('Older unique', { selector: 'span' });
  expect(screen.getByText('1 matching project · Page 1')).toBeInTheDocument();
  expect(requests.at(-1)?.searchParams.has('cursor')).toBe(false);
  expect(requests.at(-1)?.searchParams.get('q')).toBe('Older unique');
  expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
});

it('distinguishes a failed search from no matches and allows a new search', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const search = new URL(input, 'https://fixture.invalid').searchParams.get('q') || '';
    return search === 'failed' ? json({ error: 'Search unavailable' }, 503) : json(pageData([], search));
  }));
  mount(); const user = userEvent.setup();
  await screen.findByText('0 projects · Page 1');
  const input = screen.getByRole('textbox', { name: 'Search project history' });
  await user.type(input, 'failed'); await user.keyboard('{Enter}');
  await screen.findByText('Search unavailable'); expect(input).toHaveValue('failed');
  expect(screen.queryByText('No projects match your search.')).not.toBeInTheDocument();
  await user.clear(input); await user.type(input, 'missing'); await user.keyboard('{Enter}');
  await screen.findByText('No projects match your search.');
});

it('does not display a late history page after changing accounts', async () => {
  let finish!: (response: Response) => void;
  let oldSignal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn(async (_input: string, init: RequestInit = {}) => {
    if (auth.user.id === 'a') {
      oldSignal = init.signal as AbortSignal;
      return new Promise<Response>(resolve => { finish = resolve; });
    }
    return json(pageData([]));
  }));
  const view = mount();
  await waitFor(() => expect(oldSignal).toBeDefined());
  view.rerender(view.changeOwner());
  await screen.findByText('0 projects · Page 1');
  expect(oldSignal?.aborted).toBe(true);
  await act(async () => finish(json(pageData())));
  expect(screen.queryByText('Private Alpha')).not.toBeInTheDocument();
});
