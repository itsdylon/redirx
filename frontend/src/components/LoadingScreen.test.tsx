import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mockNavigate = vi.fn();
const mockStatus = vi.fn();
vi.mock('react-router-dom', () => ({ useNavigate: () => mockNavigate }));
vi.mock('../api/sessions', () => ({ getSessionStatus: (...args: unknown[]) => mockStatus(...args) }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'owner', plan: 'free' } }) }));
import { LoadingScreen } from './LoadingScreen';
beforeEach(() => vi.clearAllMocks());
describe('LoadingScreen request lifetime', () => {
  it.each(['resolve', 'reject'])('ignores an unmounted poll %s', async outcome => {
    let finish!: (value: unknown) => void;
    mockStatus.mockImplementationOnce(() => new Promise((resolve, reject) => { finish = outcome === 'resolve' ? resolve : reject; }));
    const view = render(<LoadingScreen sessionId="old-session" />);
    expect(mockStatus).toHaveBeenCalledWith('old-session');
    view.unmount();
    await act(async () => finish(outcome === 'resolve' ? { status: 'completed' } : new Error('late')));
    expect(mockNavigate).not.toHaveBeenCalled();
  });
  it('ignores an old session completion while allowing the current session to navigate', async () => {
    let finish!: (value: unknown) => void;
    mockStatus.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    mockStatus.mockResolvedValueOnce({ status: 'completed' });
    const view = render(<LoadingScreen sessionId="old-session" />);
    view.rerender(<LoadingScreen sessionId="new-session" />);
    await act(async () => finish({ status: 'completed' }));
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/review/new-session');
  });
});


describe('LoadingScreen real backoff scheduling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockStatus.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });
  it('waits 6 then 12 seconds after failures instead of immediately restarting', async () => {
    mockStatus.mockRejectedValue(new Error('offline'));
    await act(async () => { render(<LoadingScreen sessionId="session" />); });
    expect(mockStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5999); });
    expect(mockStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mockStatus).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(11999); });
    expect(mockStatus).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mockStatus).toHaveBeenCalledTimes(3);
    expect(screen.getByText(/Retrying in 24 seconds/)).toBeInTheDocument();
  });
  it('resets the backoff after a successful running-status response', async () => {
    mockStatus.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ status: 'processing' }).mockRejectedValue(new Error('offline again'));
    await act(async () => { render(<LoadingScreen sessionId="session" />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(mockStatus).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(mockStatus).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(5999); });
    expect(mockStatus).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mockStatus).toHaveBeenCalledTimes(4);
  });
  it('stops after five spaced failures and explicitly retries on request', async () => {
    mockStatus.mockRejectedValue(new Error('offline'));
    await act(async () => { render(<LoadingScreen sessionId="session" />); });
    for (const delay of [6000, 12000, 24000, 30000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(delay); });
    }
    expect(mockStatus).toHaveBeenCalledTimes(5);
    expect(screen.getByText('Unable to Check Job Status')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(120000); });
    expect(mockStatus).toHaveBeenCalledTimes(5);
    mockStatus.mockResolvedValue({ status: 'completed' });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })); });
    expect(mockStatus).toHaveBeenCalledTimes(6);
    expect(mockNavigate).toHaveBeenCalledWith('/review/session');
    expect(screen.queryByText('Unable to Check Job Status')).toBeNull();
  });
  it('clears a pending backoff when the poller unmounts', async () => {
    mockStatus.mockRejectedValue(new Error('offline'));
    let view!: ReturnType<typeof render>;
    await act(async () => { view = render(<LoadingScreen sessionId="session" />); });
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(mockStatus).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
