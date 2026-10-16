import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { WatchPage } from './WatchPage';
import type { WatchCheck, WatchDetail } from '../api/watch';

let owner = 'owner-a';
const success = vi.fn(), error = vi.fn();
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: owner } }) }));
vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock('sonner', () => ({ toast: { success: (...args: unknown[]) => success(...args), error: (...args: unknown[]) => error(...args) } }));
const check = (changes: Partial<WatchCheck> = {}): WatchCheck => ({ id: 'check-1', started_at: '2026-10-15T12:00:00Z', finished_at: '2026-10-15T12:01:00Z', status: 'completed', urls_checked: 10, urls_ok: 10, issues_open: 0, issues_new: 0, issues_resolved: 0, clicks_at_risk: 0, ...changes });
const data = (changes: Partial<WatchDetail> = {}): WatchDetail => ({ watch: { id: 'watch-1', session_id: 'session-1', old_domain: 'old.example', status: 'active', last_checked_at: '2026-10-15T12:01:00Z', check_interval_minutes: 1440, created_at: '2026-10-14T12:00:00Z' }, issues: [], checks: [check()], summary: { open_issues: 0, critical: 0, clicks_at_risk: 0 }, ...changes });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let fetchMock: ReturnType<typeof vi.fn>;
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 1, staleTime: Infinity }, mutations: { retry: false } } });
  const tree = () => <QueryClientProvider client={client}><MemoryRouter initialEntries={['/watch/watch-1']}><Routes><Route path="/watch/:watchId" element={<WatchPage />} /></Routes></MemoryRouter></QueryClientProvider>;
  const view = render(tree());
  return { ...view, switchOwner: () => { owner = 'owner-b'; view.rerender(tree()); } };
}
beforeEach(() => { owner = 'owner-a'; success.mockReset(); error.mockReset(); fetchMock = vi.fn(async () => json(data())); vi.stubGlobal('fetch', fetchMock); localStorage.setItem('access_token', 'fixture'); });
afterEach(() => { cleanup(); focusManager.setFocused(undefined); vi.useRealTimers(); vi.unstubAllGlobals(); localStorage.clear(); });

describe('measured monitoring outcomes', () => {
  it.each([
    [undefined, 'No completed check yet'],
    [check({status:'running'}), 'Check in progress'],
    [check({status:'failed', error:'Database unavailable'}), 'The latest check failed'],
    [check({urls_checked:0, urls_ok:0}), 'No URLs were checked'],
    [check({urls_checked:10, urls_ok:8}), 'Redirects need attention'],
    [check({issues_open:2}), 'Redirects need attention'],
    [check(), 'All 10 checked URLs passed'],
  ])('does not turn an empty issue list into full coverage (%s)', async (last, title) => {
    fetchMock.mockResolvedValue(json(data({checks:last ? [last as WatchCheck] : []})));
    mount();
    await screen.findByRole('heading',{name:title as string});
    expect(screen.queryByText('Every redirect is working')).not.toBeInTheDocument();
    expect(screen.queryByText("We'll email you if that changes.")).not.toBeInTheDocument();
  });
  it('shows a completed sweep as a measured subset and explains fallback alert delivery', async () => {
    mount();await screen.findByText('All 10 checked URLs passed');
    expect(screen.getByText(/Unchecked URLs have not been verified/)).toBeInTheDocument();
    expect(screen.getByText(/Alerts use your account email when available/)).toBeInTheDocument();
  });
  it('distinguishes ended monitoring and requires a deliberate restart', async () => {
    const detail=data();detail.watch.status='ended';fetchMock.mockResolvedValue(json(detail));
    mount();await screen.findByRole('button',{name:'Restart monitoring'});
    expect(screen.queryByRole('button',{name:'Resume'})).not.toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Check now'})).toBeDisabled();
    expect(screen.getByText(/Monitoring is ended/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('provides real retry after a read failure', async () => {
    fetchMock.mockResolvedValueOnce(json({error:'Monitor unavailable.'},503)).mockResolvedValueOnce(json({error:'Monitor unavailable.'},503)).mockImplementation(async () => json(data()));
    const user=userEvent.setup();mount();await screen.findByRole('alert');
    await user.click(screen.getByRole('button',{name:'Retry loading monitor'}));
    await screen.findByText('All 10 checked URLs passed');expect(fetchMock).toHaveBeenCalledTimes(3);
  });
  it('never reuses owner A’s cached monitor when owner B fails to load', async () => {
    fetchMock.mockResolvedValueOnce(json(data())).mockResolvedValue(json({error:'Watch not found.'},404));
    const view=mount();await screen.findByText('All 10 checked URLs passed');view.switchOwner();
    await screen.findByText('Watch not found.');
    expect(screen.queryByText('Monitoring old.example')).not.toBeInTheDocument();
    expect(screen.queryByRole('button',{name:'Pause'})).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it('does not announce a queued check for an HTTP-200 failure envelope', async () => {
    fetchMock.mockImplementation(async (_url:string,init:RequestInit={}) => init.method==='POST' ? json({success:false,error:'Check was not queued.'}) : json(data()));
    const user=userEvent.setup();mount();await user.click(await screen.findByRole('button',{name:'Check now'}));
    await waitFor(()=>expect(error).toHaveBeenCalledWith('Check was not queued.'));
    expect(success).not.toHaveBeenCalled();
  });
  it('aborts an old owner’s mutation and ignores its late completion', async () => {
    let finish!: (response:Response)=>void;let mutationSignal:AbortSignal|undefined;
    fetchMock.mockImplementation(async (_url:string,init:RequestInit={}) => {
      if(init.method==='PATCH'){mutationSignal=init.signal as AbortSignal;return new Promise(resolve=>{finish=resolve});}
      return json(data());
    });
    const user=userEvent.setup();const view=mount();await user.click(await screen.findByRole('button',{name:'Pause'}));
    view.switchOwner();await screen.findByText('All 10 checked URLs passed');expect(mutationSignal!.aborted).toBe(true);
    await act(async()=>finish(json({success:true,watch:{...data().watch,status:'paused'}})));
    expect(success).not.toHaveBeenCalled();expect(error).not.toHaveBeenCalled();
  });
});


describe('monitor request lifetime', () => {
  it.each([401, 403, 404])('does not poll a terminal HTTP %s error and recovers through explicit retry', async status => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(async () => json({error:'Monitor access unavailable.'}, status));
    mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByRole('alert')).toHaveTextContent('Monitor access unavailable.');
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockImplementation(async () => json(data()));
    await act(async () => { screen.getByRole('button', {name:'Retry loading monitor'}).click(); await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText('All 10 checked URLs passed')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

it('keeps transient retries bounded, polls healthy data and cancels an abandoned read', async () => {
  vi.useFakeTimers();
  fetchMock.mockImplementation(async () => json({error:'Temporarily unavailable.'},503));
  const view = mount();
  await act(async () => { await vi.advanceTimersByTimeAsync(2); });
  expect(fetchMock).toHaveBeenCalledTimes(2); // the existing one immediate retry
  expect(screen.getByRole('alert')).toHaveTextContent('Temporarily unavailable.');
  await act(async () => { await vi.advanceTimersByTimeAsync(29_000); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  fetchMock.mockImplementation(async () => json(data()));
  await act(async () => { await vi.advanceTimersByTimeAsync(1_010); });
  expect(screen.getByText('All 10 checked URLs passed')).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(3);
  focusManager.setFocused(false);
  await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
  expect(fetchMock).toHaveBeenCalledTimes(3);
  focusManager.setFocused(true);
  let signal: AbortSignal | undefined;
  fetchMock.mockImplementation((_url: string, init: RequestInit) => {
    signal = init.signal as AbortSignal; return new Promise(() => {});
  });
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(fetchMock).toHaveBeenCalledTimes(4);
  expect(signal?.aborted).toBe(false);
  view.unmount(); expect(signal?.aborted).toBe(true);
});
