import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ApiKeysPanel } from './ApiKeysPanel';

let owner = 'user-1';
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: owner ? { id: owner } : null }) }));

const listApiKeys = vi.fn();
const createApiKey = vi.fn();
const revokeApiKey = vi.fn();

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../api/keys', async () => {
  const actual = await vi.importActual<typeof import('../api/keys')>('../api/keys');
  return {
    ...actual,
    listApiKeys: (...a: unknown[]) => listApiKeys(...a),
    createApiKey: (...a: unknown[]) => createApiKey(...a),
    revokeApiKey: (...a: unknown[]) => revokeApiKey(...a),
  };
});

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = () => <QueryClientProvider client={client}><ApiKeysPanel /></QueryClientProvider>;
  const view = render(tree());
  return { ...view, client, rerenderOwner: () => view.rerender(tree()) };
}

const KEY = {
  id: 'k1',
  name: 'Claude Code',
  key_prefix: 'rdx_abc12345',
  created_at: '2026-08-01T00:00:00Z',
  last_used_at: null,
  revoked_at: null,
};

describe('ApiKeysPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    owner = 'user-1';
    listApiKeys.mockResolvedValue([]);
  });

  it('invites a first key when there are none', async () => {
    renderPanel();
    expect(await screen.findByText(/no keys yet/i)).toBeTruthy();
  });

  it('shows the plaintext exactly once, after creating', async () => {
    createApiKey.mockResolvedValue({
      id: 'k2',
      name: 'Agent',
      key_prefix: 'rdx_zzz',
      created_at: '2026-08-20T00:00:00Z',
      key: 'rdx_the_only_copy_of_this_secret',
    });
    renderPanel();

    await userEvent.type(await screen.findByLabelText(/name/i), 'Agent');
    await userEvent.click(screen.getByRole('button', { name: /create key/i }));

    expect(await screen.findByText('rdx_the_only_copy_of_this_secret')).toBeTruthy();
    // The warning is the whole point: the server keeps a hash, so a key that
    // is not copied here is unrecoverable.
    expect(screen.getByText(/only time it will be shown/i)).toBeTruthy();
  });

  it('hides the plaintext once acknowledged', async () => {
    createApiKey.mockResolvedValue({
      id: 'k2',
      name: 'Agent',
      key_prefix: 'rdx_zzz',
      created_at: '2026-08-20T00:00:00Z',
      key: 'rdx_secret_value',
    });
    renderPanel();

    await userEvent.click(await screen.findByRole('button', { name: /create key/i }));
    expect(await screen.findByText('rdx_secret_value')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: /saved it/i }));
    await waitFor(() => expect(screen.queryByText('rdx_secret_value')).toBeNull());
  });

  it('never renders a full key for an existing one — only its prefix', async () => {
    listApiKeys.mockResolvedValue([KEY]);
    renderPanel();

    expect(await screen.findByText('Claude Code')).toBeTruthy();
    expect(screen.getByText(/rdx_abc12345/)).toBeTruthy();
    expect(screen.getByText(/never used/i)).toBeTruthy();
  });

  it('requires confirmation before deleting, and says what breaks', async () => {
    listApiKeys.mockResolvedValue([KEY]);
    renderPanel();

    await userEvent.click(await screen.findByRole('button', { name: /delete/i }));

    expect(await screen.findByText(/stops working immediately/i)).toBeTruthy();
    expect(revokeApiKey).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /delete key/i }));
    await waitFor(() => expect(revokeApiKey).toHaveBeenCalledWith('k1'));
  });

  it('cancelling the confirmation deletes nothing', async () => {
    listApiKeys.mockResolvedValue([KEY]);
    renderPanel();

    await userEvent.click(await screen.findByRole('button', { name: /delete/i }));
    await userEvent.click(await screen.findByRole('button', { name: /cancel/i }));

    expect(revokeApiKey).not.toHaveBeenCalled();
  });

  it('keeps a pending deletion open when Escape is pressed', async () => {
    listApiKeys.mockResolvedValue([KEY]);
    let finish!: () => void;
    revokeApiKey.mockReturnValueOnce(new Promise<void>(resolve => { finish = resolve; }));
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: /^delete$/i }));
    await userEvent.click(screen.getByRole('button', { name: /delete key/i }));
    expect(await screen.findByRole('button', { name: 'Deleting…' })).toBeDisabled();
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(revokeApiKey).toHaveBeenCalledTimes(1);
    listApiKeys.mockResolvedValue([{ ...KEY, revoked_at: '2026-10-16T00:00:00Z' }]);
    await act(async () => finish());
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  });

  it('offers no delete action on an already-revoked key', async () => {
    listApiKeys.mockResolvedValue([{ ...KEY, revoked_at: '2026-08-10T00:00:00Z' }]);
    renderPanel();

    expect(await screen.findByText(/revoked/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^delete$/i })).toBeNull();
  });

  it('falls back to a default name when none is given', async () => {
    createApiKey.mockResolvedValue({
      id: 'k3',
      name: 'API key',
      key_prefix: 'rdx_q',
      created_at: '2026-08-20T00:00:00Z',
      key: 'rdx_x',
    });
    renderPanel();

    await userEvent.click(await screen.findByRole('button', { name: /create key/i }));
    await waitFor(() => expect(createApiKey).toHaveBeenCalledWith('API key'));
  });
  it('cannot replace an unacknowledged one-time key through click or Enter', async () => {
    createApiKey.mockResolvedValue({ ...KEY, key: 'rdx_preserve_this_secret' });
    const view = renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: /create key/i }));
    expect(await screen.findByText('rdx_preserve_this_secret')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create key/i })).toBeDisabled();
    expect(screen.getByLabelText('Name')).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: /create key/i }));
    await userEvent.type(screen.getByLabelText('Name'), '{Enter}');
    expect(createApiKey).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(view.client.getMutationCache().getAll().map(m => m.state))).not.toContain('rdx_preserve_this_secret');
    await userEvent.click(screen.getByRole('button', { name: /saved it/i }));
    expect(screen.getByRole('button', { name: /create key/i })).not.toBeDisabled();
  });

  it('retains a failed revoke in its dialog for explicit recovery', async () => {
    listApiKeys.mockResolvedValue([KEY]);
    revokeApiKey.mockRejectedValueOnce(new Error('Network unavailable'));
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: /^delete$/i }));
    await userEvent.click(screen.getByRole('button', { name: /delete key/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Network unavailable');
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(revokeApiKey).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: /delete key/i })).not.toBeDisabled();
  });

  it('does not show another owner metadata or an in-flight plaintext result after switching', async () => {
    let finish!: (value: unknown) => void;
    listApiKeys.mockResolvedValueOnce([KEY]).mockResolvedValue([]);
    createApiKey.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const view = renderPanel();
    await screen.findByText('Claude Code');
    await userEvent.click(screen.getByRole('button', { name: /create key/i }));
    owner = 'user-2';
    view.rerenderOwner();
    expect(screen.queryByText('Claude Code')).not.toBeInTheDocument();
    await act(async () => finish({ ...KEY, key: 'rdx_first_owner_secret' }));
    expect(screen.queryByText('rdx_first_owner_secret')).not.toBeInTheDocument();
    expect(JSON.stringify(view.client.getMutationCache().getAll().map(m => m.state))).not.toContain('rdx_first_owner_secret');
    expect(await screen.findByText(/no keys yet/i)).toBeInTheDocument();
    expect(view.client.getQueryData(['api-keys', 'all', 'user-1'])).toEqual([KEY]);
    expect(view.client.getQueryData(['api-keys', 'all', 'user-2'])).toEqual([]);
  });

  it('offers a read retry when the key inventory could not load', async () => {
    listApiKeys.mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValueOnce([]);
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Reload keys' }));
    expect(await screen.findByText(/no keys yet/i)).toBeInTheDocument();
  });

});
