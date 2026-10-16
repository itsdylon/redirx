import userEvent from '@testing-library/user-event';
import { PivotMigrationDetail } from './PivotMigrationDetail';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PivotCompanionPage } from './PivotCompanionPage';
const auth = vi.hoisted(() => ({ user: { id: 'a' } }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('./ToolLayout', () => ({ ToolLayout: ({ children }: any) => <>{children}</> }));
const data = (name: string) => new Response(JSON.stringify({ contract_version: '1.0.0', status: 'succeeded', data: { items: [{ id: name, name }], next_cursor: null }, error: null }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('aborts prior-owner migration history and ignores a late private response', async () => {
  auth.user = { id: 'a' }; let resolve!: (value: Response) => void; let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => auth.user.id === 'a' ? (signal = init.signal as AbortSignal, new Promise<Response>(r => { resolve = r; })) : Promise.resolve(data('Owner B migration'))));
  const tree = () => <MemoryRouter><PivotCompanionPage /></MemoryRouter>;
  const view = render(tree()); auth.user = { id: 'b' }; view.rerender(tree());
  expect(signal?.aborted).toBe(true); await screen.findByText('Owner B migration');
  await act(async () => resolve(data('Owner A private migration')));
  expect(screen.queryByText('Owner A private migration')).not.toBeInTheDocument();
});

it('resets migration metadata and aborts a pending refresh when the same migration URL changes owner', async () => {
  auth.user = { id: 'a' }; let reads = 0; let resolve!: (value: Response) => void; let signal: AbortSignal | undefined;
  const detail = () => new Response(JSON.stringify({ contract_version: '1.0.0', status: 'succeeded', data: { migration: { id: 'm1', name: 'Private migration metadata' }, jev: { engine: 'jev-url-v1', pass: 1, limits: { passes: 3 } } }, error: null }));
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => {
    if (auth.user.id === 'b') return Promise.resolve(new Response(JSON.stringify({ error: { message: 'Not available' } }), { status: 503 }));
    if (++reads === 1) return Promise.resolve(detail());
    signal = init.signal as AbortSignal; return new Promise<Response>(r => { resolve = r; });
  }));
  const tree = () => <MemoryRouter initialEntries={['/migrations/m1']}><Routes><Route path="/migrations/:migrationId" element={<PivotMigrationDetail />} /></Routes></MemoryRouter>;
  const view = render(tree()); await screen.findByText('Private migration metadata');
  await userEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
  auth.user = { id: 'b' }; view.rerender(tree());
  expect(screen.queryByText('Private migration metadata')).not.toBeInTheDocument(); expect(signal?.aborted).toBe(true);
  await act(async () => resolve(detail()));
  expect(screen.queryByText('Private migration metadata')).not.toBeInTheDocument();
});
