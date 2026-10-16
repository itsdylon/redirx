import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card } from './ui/card';
import { Button } from './ui/button';
import { getAuthRedirect } from '../lib/authRedirect';
import { useAuth } from '../contexts/AuthContext';

export function AuthCallback() {
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const { completeOAuthCallback } = useAuth();
  // Hydrating the account recreates the context callback. Share one completion
  // for this mounted route so it cannot exchange/consume the return path twice.
  const completion = useRef<Promise<string> | null>(null);
  const recoveryPath = (route: '/login' | '/signup') => {
    const redirect = getAuthRedirect();
    return redirect ? `${route}?redirect=${encodeURIComponent(redirect)}` : route;
  };

  const parseCallbackError = (): string | null => {
    const searchParams = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams(
      window.location.hash.startsWith('#')
        ? window.location.hash.substring(1)
        : window.location.hash
    );

    const errorCode = searchParams.get('error') || hashParams.get('error');
    const errorDescription =
      searchParams.get('error_description') || hashParams.get('error_description');
    const errorStatus =
      searchParams.get('error_code') || hashParams.get('error_code');

    if (!errorCode && !errorDescription && !errorStatus) {
      return null;
    }

    if (errorDescription) {
      return errorDescription;
    }

    if (errorStatus) {
      return `Authentication failed (${errorStatus}). Please try again.`;
    }

    return 'Authentication was canceled or failed. Please try again.';
  };

  useEffect(() => {
    let active = true;
    const handleCallback = async () => {
      try {
        const callbackError = parseCallbackError();
        if (callbackError) {
          setError(callbackError);
          return;
        }

        completion.current ??= completeOAuthCallback();
        const redirect = await completion.current;
        if (active) navigate(redirect, { replace: true });
      } catch (err: any) {
        if (active) {
          console.error('Auth callback could not complete.');
          setError(err.message || 'Unable to complete sign-in. Please try again.');
        }
      } finally {
        if (active) setLoading(false);
      }
    };

    handleCallback();
    return () => { active = false; };
  }, [completeOAuthCallback, navigate]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md p-8 text-center">
          <div className="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto mb-4"></div>
          <p className="text-muted-foreground">Completing sign-in...</p>
        </Card>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md p-8 text-center">
          <div className="w-16 h-16 bg-destructive/10 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg
              className="w-8 h-8 text-destructive"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              />
            </svg>
          </div>
          <h1 className="text-xl font-bold text-foreground mb-2">Sign-in Failed</h1>
          <p role="alert" className="text-muted-foreground mb-6 break-words">{error}</p>
          <div className="space-y-2">
            <Button onClick={() => navigate(recoveryPath('/login'))} className="w-full">
              Try Logging In Again
            </Button>
            <Button variant="outline" onClick={() => navigate(recoveryPath('/signup'))} className="w-full">
              Go to Signup
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  return null;
}
