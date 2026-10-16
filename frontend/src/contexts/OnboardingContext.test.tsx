import * as React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { OnboardingProvider, useOnboarding } from './OnboardingContext';
const auth = vi.hoisted(() => ({ user: { id: 'a' } as { id: string } | null, loading: false }));
vi.mock('./AuthContext', () => ({ useAuth: () => auth }));
let latest: ReturnType<typeof useOnboarding>;
function Harness() { latest = useOnboarding(); return <div>{latest.onboarding?.onboarding_status || 'empty'}{latest.error && <p role="alert">{latest.error}</p>}</div>; }
const state = (status: string) => ({ success: true, onboarding_status: status, onboarding_state: { steps: {} } });
const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
beforeEach(() => { auth.user = { id: 'a' }; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); delete (auth as any).completeOAuthCallback; });
it('hides old state immediately and ignores late reads after changing accounts', async () => {
  let resolve!: (value: Response) => void; let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => auth.user?.id === 'a' ? (signal = init.signal as AbortSignal, new Promise<Response>(r => { resolve = r; })) : Promise.resolve(json(state('not_started')))));
  const view = render(<OnboardingProvider><Harness /></OnboardingProvider>);
  auth.user = { id: 'b' }; view.rerender(<OnboardingProvider><Harness /></OnboardingProvider>);
  expect(signal?.aborted).toBe(true); await screen.findByText('not_started');
  await act(async () => resolve(json(state('completed'))));
  expect(screen.queryByText('completed')).not.toBeInTheDocument();
});
it('returns no old-account mutation success and blocks chained requests after sign-out', async () => {
  let resolve!: (value: Response) => void; let signal: AbortSignal | undefined;
  const fetcher = vi.fn((_url, init: RequestInit) => init.method === 'PATCH' ? (signal = init.signal as AbortSignal, new Promise<Response>(r => { resolve = r; })) : Promise.resolve(json(state('not_started'))));
  vi.stubGlobal('fetch', fetcher);
  const view = render(<OnboardingProvider><Harness /></OnboardingProvider>); await screen.findByText('not_started');
  const old = latest; let pending!: Promise<unknown>;
  act(() => { pending = old.startOnboarding(); });
  auth.user = null; view.rerender(<OnboardingProvider><Harness /></OnboardingProvider>);
  expect(signal?.aborted).toBe(true);
  await act(async () => { resolve(json(state('in_progress'))); expect(await pending).toBeNull(); });
  expect(await old.selectPath('sample')).toBeNull();
  expect(fetcher.mock.calls.filter(call => call[1].method === 'PATCH')).toHaveLength(1);
  expect(screen.getByText('empty')).toBeInTheDocument();
});
it('does not let a slow earlier read overwrite a confirmed update', async () => {
  let resolve!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => init.method === 'GET' ? new Promise<Response>(r => { resolve = r; }) : Promise.resolve(json(state('dismissed')))));
  render(<OnboardingProvider><Harness /></OnboardingProvider>);
  await act(async () => { await latest.dismissOnboarding(); });
  expect(screen.getByText('dismissed')).toBeInTheDocument();
  await act(async () => resolve(json(state('not_started'))));
  expect(screen.getByText('dismissed')).toBeInTheDocument(); expect(latest.loading).toBe(false);
});
it('does not report a rejected update as completed', async () => {
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => Promise.resolve(json(init.method === 'GET' ? state('not_started') : { success: false }))));
  render(<OnboardingProvider><Harness /></OnboardingProvider>); await screen.findByText('not_started');
  await act(async () => { expect(await latest.completeOnboarding()).toBeNull(); });
  expect(screen.getByRole('alert')).toHaveTextContent('not confirmed');
});

it('keeps the callback subtree mounted through successful hydration and consumes its exact return once', async () => {
  // Mirror main.tsx's provider placement around the router/App, while exercising
  // the real callback and redirect consumer. Hydration recreates its function.
  const { AuthCallback } = await import('../components/AuthCallback');
  const { MemoryRouter, Route, Routes, useLocation } = await import('react-router-dom');
  const { consumeAuthRedirect } = await import('../lib/authRedirect');
  let finish!: () => void;
  const complete = vi.fn(async () => { await new Promise<void>(resolve => { finish = resolve; }); return consumeAuthRedirect() || '/'; });
  const duplicate = vi.fn(async () => consumeAuthRedirect() || '/');
  const target = '/oauth/consent?authorization_id=preserved-request';
  auth.user = null; (auth as any).completeOAuthCallback = complete;
  localStorage.setItem('auth_redirect', target);
  vi.stubGlobal('fetch', vi.fn(async () => json(state('not_started'))));
  let destinations = 0;
  function Destination() { const location = useLocation(); React.useEffect(() => { destinations++; }, []); return <output>{location.pathname + location.search}</output>; }
  const tree = () => <OnboardingProvider><MemoryRouter initialEntries={['/auth/callback']}><Routes>
    <Route path="/auth/callback" element={<AuthCallback />} />
    <Route path="/oauth/consent" element={<Destination />} />
    <Route path="/" element={<p>Unexpected home</p>} />
  </Routes></MemoryRouter></OnboardingProvider>;
  window.history.replaceState({}, '', '/auth/callback');
  const view = render(tree()); expect(complete).toHaveBeenCalledTimes(1);
  auth.user = { id: 'hydrated-owner' }; (auth as any).completeOAuthCallback = duplicate;
  view.rerender(tree()); expect(duplicate).not.toHaveBeenCalled();
  await act(async () => finish());
  expect(await screen.findByText(target)).toBeInTheDocument();
  expect(localStorage.getItem('auth_redirect')).toBeNull(); expect(destinations).toBe(1);
  expect(complete).toHaveBeenCalledTimes(1); expect(duplicate).not.toHaveBeenCalled();
  expect(screen.queryByText('Unexpected home')).not.toBeInTheDocument();
});
