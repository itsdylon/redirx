import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import type { OAuthAuthorizationDetails } from '@supabase/auth-js';
import { useAuth } from '../contexts/AuthContext';
import { supabase } from '../lib/supabase';
import { setAuthRedirect } from '../lib/authRedirect';
import { FrontendEvent, safeCapture } from '../lib/analyticsEvents';
import { Button } from './ui/button';
import { Card } from './ui/card';

type ConsentState = 'loading' | 'ready' | 'expired' | 'missing' | 'error' | 'redirecting';

// Supabase's OAuth methods do not expose a caller AbortSignal. Bound the UI's
// wait, ignore late results, and recover by reading state, never replaying consent.
async function boundedConsentRequest<T>(request: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  try {
    return await Promise.race([request, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('The connection request timed out. Its outcome is not confirmed; reload the request to check.')), 15_000);
    })]);
  } finally {
    clearTimeout(timer!);
  }
}


function messageForError(error: unknown): ConsentState {
  const message = error && typeof error === 'object' && 'message' in error && typeof error.message === 'string' ? error.message.toLowerCase() : '';
  if (message.includes('expired') || message.includes('not found') || message.includes('invalid authorization')) {
    return 'expired';
  }
  return 'error';
}

function goToClient(redirectUrl: string): void {
  window.location.assign(redirectUrl);
}

export function OAuthConsentPage() {
  const { user, loading: authLoading, authError, retryAuth } = useAuth();
  const navigate = useNavigate();
  const posthog = usePostHog();
  const [searchParams] = useSearchParams();
  const authorizationId = searchParams.get('authorization_id');
  const [details, setDetails] = useState<OAuthAuthorizationDetails | null>(null);
  const [state, setState] = useState<ConsentState>('loading');
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const decisionPending = useRef(false);
  const decisionContext = useRef(0);
  useEffect(() => {
    decisionContext.current++;
    decisionPending.current = false;
    setSubmitting(null);
    return () => { decisionContext.current++; };
  }, [authorizationId, user?.id]);
  const [submitting, setSubmitting] = useState<'approve' | 'deny' | null>(null);
  const readyHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (state === 'ready') readyHeading.current?.focus();
  }, [state, authorizationId, user?.id]);

  const consentPath = useMemo(() => {
    const query = searchParams.toString();
    return `${window.location.pathname}${query ? `?${query}` : ''}`;
  }, [searchParams]);

  useEffect(() => {
    if (!authorizationId) {
      setState('missing');
      return;
    }
    if (!user && !authLoading && !authError) {
      setAuthRedirect(consentPath);
      navigate(`/login?redirect=${encodeURIComponent(consentPath)}&source=oauth-consent`, { replace: true });
    }
  }, [authorizationId, consentPath, navigate, user, authLoading, authError]);

  useEffect(() => {
    if (!user || !authorizationId || authLoading) return;

    let cancelled = false;
    setState('loading');
    setDetails(null);
    setError('');
    boundedConsentRequest(supabase.auth.oauth.getAuthorizationDetails(authorizationId))
      .then(({ data, error: requestError }) => {
        if (cancelled) return;
        if (requestError || !data) {
          setError(requestError?.message || 'This authorization request is no longer available.');
          setState(requestError ? messageForError(requestError) : 'expired');
          return;
        }
        if (data.redirect_url) {
          setState('redirecting');
          goToClient(data.redirect_url);
          return;
        }
        setDetails(data);
        setState('ready');
      })
      .catch((requestError: unknown) => {
        if (cancelled) return;
        setError(requestError instanceof Error ? requestError.message : 'Unable to load the authorization request.');
        setState(messageForError(requestError));
      });

    return () => { cancelled = true; };
  }, [authorizationId, user, authLoading, attempt]);

  const decide = async (action: 'approve' | 'deny') => {
    if (!authorizationId || decisionPending.current || state !== 'ready') return;
    decisionPending.current = true;
    const context = decisionContext.current;
    setSubmitting(action);
    setError('');
    const method = action === 'approve'
      ? supabase.auth.oauth.approveAuthorization
      : supabase.auth.oauth.denyAuthorization;
    try {
      const { data, error: decisionError } = await boundedConsentRequest(method(authorizationId, { skipBrowserRedirect: true }));
      if (context !== decisionContext.current) return;
      if (decisionError || !data?.redirect_url) {
        setError(decisionError?.message || 'The authorization response was incomplete. Please try again.');
        setState('error');
        return;
      }
      // No client identifiers beyond what PostHog already has on this
      // identified user (distinct_id) — deliberately no client_name/client_id,
      // since a requesting-client property here would let one property value
      // fan out into per-integration breakdowns nobody asked to track.
      safeCapture(posthog, FrontendEvent.OAUTH_CONSENT_DECIDED, { decision: action === 'approve' ? 'approved' : 'denied' });
      setState('redirecting');
      goToClient(data.redirect_url);
    } catch (decisionError: unknown) {
      if (context !== decisionContext.current) return;
      setError(decisionError instanceof Error ? decisionError.message : 'Unable to complete this authorization request.');
      setState('error');
    } finally {
      if (context === decisionContext.current) {
        decisionPending.current = false;
        setSubmitting(null);
      }
    }
  };

  if (!authorizationId || state === 'missing') {
    return <ConsentMessage title="Authorization link is incomplete" body="Return to your MCP client and start connecting again." />;
  }

  if (authLoading) return <ConsentMessage title="Checking your sign-in" body="Keeping this connection request while your account loads…" loading />;
  if (authError) return <ConsentMessage title="Your sign-in needs attention" body={authError}
    retry={() => { void retryAuth().catch(() => undefined); }} retryLabel="Retry account loading" />;
  if (!user) return null;
  if (state === 'loading') return <ConsentMessage title="Preparing secure connection" body="Loading the access requested by your MCP client…" loading />;
  if (state === 'redirecting') return <ConsentMessage title="Returning to your MCP client" body="Your authorization response is being sent back securely…" loading />;
  if (state === 'expired') return <ConsentMessage title="Authorization request expired" body="This request is no longer valid. Return to your MCP client and start a new connection." error={error} />;
  if (state === 'error') return <ConsentMessage title="Unable to load authorization" body="Reload the request to check its current state. Your approval will not be sent again automatically." error={error} retry={() => setAttempt(value => value + 1)} />;
  if (!details) return null;

  const scopes = details.scope.split(/\s+/).filter(Boolean);
  return (
    <main className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-lg p-8">
        <div className="mb-7">
          <p className="text-sm font-medium text-primary mb-2">Secure connection</p>
          <h1 ref={readyHeading} tabIndex={-1} className="text-2xl font-bold text-foreground break-words">Connect {details.client.name}</h1>
          <p className="text-muted-foreground mt-2">
            Review what this MCP client is asking to access before continuing.
          </p>
        </div>

        <div className="border border-border rounded-lg p-4 space-y-4 mb-6">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Requesting client</p>
            <p className="text-foreground font-medium mt-1 break-words">{details.client.name}</p>
            {details.client.uri && <p className="text-sm text-muted-foreground break-all mt-1">{details.client.uri}</p>}
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Redirect destination</p>
            <p className="text-sm text-foreground mt-1">Validated by Supabase for this registered client</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Access requested</p>
            <ul className="mt-2 space-y-1">
              {scopes.map((scope) => <li className="text-sm text-foreground" key={scope}>• {scope}</li>)}
            </ul>
          </div>
        </div>

        <p className="text-xs text-muted-foreground mb-6">
          Only approve a client you recognize. A registered name or website alone does not prove trust.
        </p>
        {error && <div role="alert" className="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded mb-4">{error}</div>}
        <div className="flex flex-col-reverse sm:flex-row gap-3">
          <Button variant="outline" className="flex-1" onClick={() => decide('deny')} disabled={!!submitting}>
            {submitting === 'deny' ? 'Declining…' : 'Decline'}
          </Button>
          <Button className="flex-1" onClick={() => decide('approve')} disabled={!!submitting}>
            {submitting === 'approve' ? 'Connecting…' : 'Approve connection'}
          </Button>
        </div>
      </Card>
    </main>
  );
}

function ConsentMessage({ title, body, error, loading = false, retry, retryLabel = 'Reload authorization request' }: { title: string; body: string; error?: string; loading?: boolean; retry?: () => void; retryLabel?: string }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [title]);
  return (
    <main className="min-h-screen flex items-center justify-center p-4" aria-busy={loading}>
      <Card className="w-full max-w-md p-8 text-center">
        {loading && <div className="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto mb-4" />}
        <h1 ref={heading} tabIndex={-1} className="text-xl font-bold text-foreground mb-2">{title}</h1>
        <p role={loading ? 'status' : undefined} className="text-muted-foreground">{body}</p>
        {error && <p role="alert" className="text-destructive text-sm mt-4 break-words">{error}</p>}
        {retry && <Button className="mt-6 w-full" onClick={retry}>{retryLabel}</Button>}
      </Card>
    </main>
  );
}
