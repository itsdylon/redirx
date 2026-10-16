import { useEffect, useState } from 'react';
import { useNavigate, Link, useSearchParams } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Card } from './ui/card';
import { OAuthProviderIcon } from './OAuthProviderIcon';
import { validateEmail } from '../utils/validation';
import { ApiError } from '../utils/errorHandler';
import { consumeAuthRedirect, setAuthRedirect } from '../lib/authRedirect';

export function LoginPage() {
  type OAuthProvider = 'google' | 'github';
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [authCode, setAuthCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [oauthLoading, setOauthLoading] = useState<OAuthProvider | null>(null);
  const [emailError, setEmailError] = useState('');
  const [emailTouched, setEmailTouched] = useState(false);
  const [resendLoading, setResendLoading] = useState(false);
  const [resendSuccess, setResendSuccess] = useState('');
  const [resendError, setResendError] = useState('');
  const [resendCooldown, setResendCooldown] = useState(0);

  const { login, startOAuth, resendConfirmationEmail } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const redirectParam = searchParams.get('redirect');
  const sourceParam = searchParams.get('source');

  useEffect(() => {
    if (resendCooldown <= 0) {
      return;
    }
    const timer = window.setTimeout(() => {
      setResendCooldown((seconds) => Math.max(0, seconds - 1));
    }, 1000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [resendCooldown]);

  useEffect(() => {
    setAuthRedirect(redirectParam);
  }, [redirectParam]);

  const handleEmailBlur = () => {
    setEmailTouched(true);
    const validation = validateEmail(email);
    if (!validation.valid) {
      setEmailError(validation.error || 'Invalid email');
    } else {
      setEmailError('');
    }
  };

  const handleEmailChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setEmail(e.target.value);
    setResendSuccess('');
    setResendError('');
    setAuthCode(null);
    setError('');
    // Clear error when user starts typing again
    if (emailTouched) {
      setEmailError('');
    }
  };

  const handlePasswordChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setPassword(e.target.value);
    setAuthCode(null);
    setError('');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setAuthCode(null);
    setResendSuccess('');
    setResendError('');

    // Validate email before submission
    const emailValidation = validateEmail(email);
    if (!emailValidation.valid) {
      setEmailError(emailValidation.error || 'Invalid email');
      setEmailTouched(true);
      return;
    }

    setLoading(true);

    try {
      await login(email, password);
      const redirect = consumeAuthRedirect();
      if (redirect) {
        navigate(redirect);
      } else {
        navigate('/');
      }
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setAuthCode(err.code || null);
        if (err.code === 'auth_invalid_credentials') {
          setError('Email or password is incorrect.');
        } else if (err.code === 'auth_email_unconfirmed') {
          setError('Please confirm your email before signing in.');
        } else if (err.code === 'auth_service_unavailable') {
          setError('Sign-in is temporarily unavailable. Please try again shortly.');
        } else if (err.code === 'auth_rate_limited') {
          setError('Too many sign-in attempts. Please wait and try again.');
        } else {
          setError(err.user_message || err.message);
        }
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('Unable to sign in right now. Please try again.');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleResendConfirmation = async () => {
    const emailValidation = validateEmail(email);
    if (!emailValidation.valid) {
      setEmailError(emailValidation.error || 'Invalid email');
      setEmailTouched(true);
      return;
    }

    setResendLoading(true);
    setResendError('');
    setResendSuccess('');

    try {
      const result = await resendConfirmationEmail(email.trim());
      setResendSuccess(result.message);
      setResendCooldown(30);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setResendError(err.user_message || err.message);
      } else if (err instanceof Error) {
        setResendError(err.message);
      } else {
        setResendError('Unable to resend confirmation email right now.');
      }
    } finally {
      setResendLoading(false);
    }
  };

  const handleOAuth = async (provider: OAuthProvider) => {
    setError('');
    setAuthCode(null);
    setResendSuccess('');
    setResendError('');
    setOauthLoading(provider);

    try {
      await startOAuth(provider, redirectParam || undefined, sourceParam || undefined);
    } catch (err: unknown) {
      if (err instanceof Error && err.message) {
        setError(err.message);
      } else {
        setError(`Unable to continue with ${provider === 'google' ? 'Google' : 'GitHub'} right now.`);
      }
      setOauthLoading(null);
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-4">
      <Card className="w-full max-w-md p-8">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-foreground">Login to Redirx</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Enter your credentials to access your account
          </p>
        </div>

        {error && (
          <div role="alert" className="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded mb-4">
            {error}
          </div>
        )}

        {authCode === 'auth_email_unconfirmed' && (
          <div className="bg-amber-500/10 border border-amber-500/40 text-amber-700 px-4 py-3 rounded mb-4 space-y-3">
            <p className="text-sm">Didn&apos;t get the confirmation email?</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleResendConfirmation}
              disabled={resendLoading || resendCooldown > 0 || !email.trim()}
            >
              {resendLoading
                ? 'Sending...'
                : resendCooldown > 0
                  ? `Resend in ${resendCooldown}s`
                  : 'Resend confirmation email'}
            </Button>
          </div>
        )}

        {resendSuccess && (
          <div className="bg-emerald-500/10 border border-emerald-500/40 text-emerald-700 px-4 py-3 rounded mb-4">
            {resendSuccess}
          </div>
        )}

        {resendError && (
          <div role="alert" className="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded mb-4">
            {resendError}
          </div>
        )}

        <div className="space-y-2 mb-4">
          <Button
            type="button"
            variant="outline"
            className="w-full"
            onClick={() => handleOAuth('google')}
            disabled={loading || !!oauthLoading}
          >
            <OAuthProviderIcon provider="google" className="size-4" />
            <span>{oauthLoading === 'google' ? 'Connecting...' : 'Continue with Google'}</span>
          </Button>
          <Button
            type="button"
            variant="outline"
            className="w-full"
            onClick={() => handleOAuth('github')}
            disabled={loading || !!oauthLoading}
          >
            <OAuthProviderIcon provider="github" className="size-4" />
            <span>{oauthLoading === 'github' ? 'Connecting...' : 'Continue with GitHub'}</span>
          </Button>
        </div>

        <div className="relative mb-4">
          <div className="absolute inset-0 flex items-center">
            <span className="w-full border-t border-border" />
          </div>
          <div className="relative flex justify-center text-xs uppercase">
            <span className="bg-card px-2 text-muted-foreground">or continue with email</span>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="login-email" className="block text-sm font-medium text-foreground mb-2">
              Email
            </label>
            <Input
              type="email"
              id="login-email"
              value={email}
              onChange={handleEmailChange}
              onBlur={handleEmailBlur}
              placeholder="you@example.com"
              required
              autoComplete="email"
              aria-invalid={!!emailError}
              aria-describedby={emailError ? 'login-email-error' : undefined}
              className={`w-full ${emailError ? 'border-destructive' : ''}`}
            />
            {emailError && (
              <p id="login-email-error" role="alert" className="text-destructive text-sm mt-1">{emailError}</p>
            )}
          </div>

          <div>
            <label htmlFor="login-password" className="block text-sm font-medium text-foreground mb-2">
              Password
            </label>
            <Input
              type="password"
              id="login-password"
              value={password}
              onChange={handlePasswordChange}
              placeholder="Enter your password"
              required
              autoComplete="current-password"
              className="w-full"
            />
          </div>

          <Button
            type="submit"
            className="w-full"
            disabled={loading || !!oauthLoading || !!emailError || !email}
          >
            {loading ? 'Logging in...' : 'Login'}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">
          Don't have an account?{' '}
          <Link
            to={(() => {
              const params = new URLSearchParams();
              if (email) params.set('email', email);
              if (redirectParam) params.set('redirect', redirectParam);
              if (sourceParam) params.set('source', sourceParam);
              const query = params.toString();
              return query ? `/signup?${query}` : '/signup';
            })()}
            className="text-primary hover:underline font-medium"
          >
            Sign up
          </Link>
        </p>
      </Card>

    </div>
  );
}
