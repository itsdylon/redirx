import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { StrictMode } from 'react';
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { AuthCallback } from './AuthCallback';

const mockCompleteOAuthCallback = vi.fn();
let callbackImplementation = mockCompleteOAuthCallback;

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    completeOAuthCallback: callbackImplementation,
  }),
}));

function LoginDestination() {
  const location = useLocation();
  return <><div>Login Route</div><output>{location.search}</output></>;
}

function renderCallback(initialPath = '/auth/callback') {
  window.history.pushState({}, '', initialPath);
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/quick-match" element={<div>Quick Match Route</div>} />
        <Route path="/login" element={<LoginDestination />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('AuthCallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    callbackImplementation = mockCompleteOAuthCallback;
  });

  it('completes callback and navigates to resolved redirect', async () => {
    mockCompleteOAuthCallback.mockResolvedValueOnce('/quick-match');
    renderCallback();

    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
    expect(mockCompleteOAuthCallback).toHaveBeenCalledTimes(1);
  });

  it('renders provider error returned in callback URL', async () => {
    renderCallback('/auth/callback?error=access_denied&error_description=The+user+denied+access');

    expect(await screen.findByText('Sign-in Failed')).toBeInTheDocument();
    expect(screen.getByText('The user denied access')).toBeInTheDocument();
    expect(mockCompleteOAuthCallback).not.toHaveBeenCalled();
  });

  it('renders callback completion errors', async () => {
    mockCompleteOAuthCallback.mockRejectedValueOnce(new Error('Unable to establish session.'));
    renderCallback();

    expect(await screen.findByText('Sign-in Failed')).toBeInTheDocument();
    expect(screen.getByText('Unable to establish session.')).toBeInTheDocument();
  });

  it('does not restart completion when auth hydration changes the context function', async () => {
    let resolve!: (path: string) => void;
    mockCompleteOAuthCallback.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done; }));
    window.history.pushState({}, '', '/auth/callback');
    const tree = () => (
      <MemoryRouter initialEntries={['/auth/callback']}>
        <Routes>
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route path="/oauth/consent" element={<div>Consent Route</div>} />
          <Route path="/" element={<div>Unexpected Home</div>} />
        </Routes>
      </MemoryRouter>
    );
    const view = render(tree());
    // AuthProvider recreates completeOAuthCallback after setUser. A duplicate
    // completion would consume an already-consumed auth_redirect and return '/'.
    callbackImplementation = vi.fn().mockResolvedValue('/');
    view.rerender(tree());
    expect(callbackImplementation).not.toHaveBeenCalled();
    await act(async () => resolve('/oauth/consent?authorization_id=test-request'));
    expect(await screen.findByText('Consent Route')).toBeInTheDocument();
    expect(mockCompleteOAuthCallback).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Unexpected Home')).not.toBeInTheDocument();
  });

  it('completes once across StrictMode effect replay', async () => {
    mockCompleteOAuthCallback.mockResolvedValue('/quick-match');
    window.history.pushState({}, '', '/auth/callback');
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/auth/callback']}>
          <Routes>
            <Route path="/auth/callback" element={<AuthCallback />} />
            <Route path="/quick-match" element={<div>Quick Match Route</div>} />
          </Routes>
        </MemoryRouter>
      </StrictMode>
    );
    expect(await screen.findByText('Quick Match Route')).toBeInTheDocument();
    expect(mockCompleteOAuthCallback).toHaveBeenCalledTimes(1);
  });

  it('does not navigate after the user leaves a pending callback', async () => {
    let resolve!: (path: string) => void;
    mockCompleteOAuthCallback.mockImplementationOnce(() => new Promise<string>((done) => { resolve = done; }));
    window.history.pushState({}, '', '/auth/callback');
    render(
      <MemoryRouter initialEntries={['/auth/callback']}>
        <Link to="/login">Leave callback</Link>
        <Routes>
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route path="/login" element={<div>Login Route</div>} />
          <Route path="/quick-match" element={<div>Unexpected Home</div>} />
        </Routes>
      </MemoryRouter>
    );
    await userEvent.setup().click(screen.getByText('Leave callback'));
    await act(async () => resolve('/quick-match'));
    expect(screen.getByText('Login Route')).toBeInTheDocument();
    expect(screen.queryByText('Unexpected Home')).not.toBeInTheDocument();
  });
  it('keeps the reviewed migration deep link when restarting failed sign-in', async () => {
    const redirect = '/migrations/10000000-0000-0000-0000-000000000001?tab=review';
    localStorage.setItem('auth_redirect', redirect);
    renderCallback('/auth/callback?error=access_denied');
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Try Logging In Again' }));
    expect(screen.getByText(`?redirect=${encodeURIComponent(redirect)}`)).toBeInTheDocument();
    expect(localStorage.getItem('auth_redirect')).toBe(redirect);
  });

  it('never carries an external stored return URL into recovery', async () => {
    localStorage.setItem('auth_redirect', '//outside.example');
    renderCallback('/auth/callback?error=access_denied');
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Try Logging In Again' }));
    expect(screen.getByRole('status')).toHaveTextContent('');
  });

  it('decodes provider messages once, preserving literal percent and plus characters', async () => {
    renderCallback('/auth/callback?error=denied&error_description=literal%2Bplus%2520text');
    expect(await screen.findByText('literal+plus%20text')).toBeInTheDocument();
  });

});
