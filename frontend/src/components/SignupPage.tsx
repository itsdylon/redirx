import { useState, useMemo, useEffect } from 'react';
import { useNavigate, Link, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Card } from './ui/card';
import { Progress } from './ui/progress';
import { OAuthProviderIcon } from './OAuthProviderIcon';
import { CheckCircle2, Circle } from 'lucide-react';
import { calculatePasswordStrength, validateEmail } from '../utils/validation';
import { consumeAuthRedirect, setAuthRedirect } from '../lib/authRedirect';

export function SignupPage() {
  type OAuthProvider = 'google' | 'github';
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [oauthLoading, setOauthLoading] = useState<OAuthProvider | null>(null);
  const [emailSent, setEmailSent] = useState(false);
  const [sentToEmail, setSentToEmail] = useState('');
  const [emailError, setEmailError] = useState('');
  const [emailTouched, setEmailTouched] = useState(false);

  const { register, startOAuth } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const posthog = usePostHog();
  const redirectParam = searchParams.get('redirect');
  const sourceParam = searchParams.get('source');

  useEffect(() => {
    const prefillEmail = (searchParams.get('email') || '').trim();
    if (!prefillEmail || email) {
      return;
    }

    setEmail(prefillEmail);
    const validation = validateEmail(prefillEmail);
    if (!validation.valid) {
      setEmailError(validation.error || 'Invalid email');
      setEmailTouched(true);
    }
  }, [searchParams, email]);

  useEffect(() => {
    setAuthRedirect(redirectParam);
  }, [redirectParam]);

  // Calculate password strength in real-time
  const passwordStrength = useMemo(
    () => calculatePasswordStrength(password),
    [password]
  );

  // Get color based on strength
  const getStrengthColor = (strength: string) => {
    switch (strength) {
      case 'weak':
        return 'bg-red-500';
      case 'fair':
        return 'bg-yellow-500';
      case 'good':
        return 'bg-green-500';
      case 'strong':
        return 'bg-green-600';
      default:
        return 'bg-gray-300';
    }
  };

  // Get progress value (0-100)
  const getProgressValue = (score: number) => {
    return (score / 5) * 100;
  };

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
    // Clear error when user starts typing again
    if (emailTouched) {
      setEmailError('');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    // Validate email before submission
    const emailValidation = validateEmail(email);
    if (!emailValidation.valid) {
      setEmailError(emailValidation.error || 'Invalid email');
      setEmailTouched(true);
      return;
    }

    // Validation
    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    // Require at least 'fair' password strength
    if (passwordStrength.strength === 'weak') {
      setError('Password is too weak. Please meet more security requirements.');
      return;
    }

    setLoading(true);

    try {
      const result = await register(email, password, fullName);

      if (sourceParam === 'quick-match') {
        posthog?.capture('signup_from_quick_match', {
          source: sourceParam,
          redirect: redirectParam || '/quick-match',
          email_confirmation_required: result.emailConfirmationRequired,
        });
      }

      if (result.emailConfirmationRequired) {
        // Show email confirmation message
        setEmailSent(true);
        setSentToEmail(result.email || email);
      } else {
        // Immediate login - check for pending redirect
        const redirect = consumeAuthRedirect();
        if (redirect) {
          navigate(redirect);
        } else {
          navigate('/');
        }
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleOAuth = async (provider: OAuthProvider) => {
    setError('');
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

  // Show success message after email is sent
  if (emailSent) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md p-8 text-center">
          <div className="mb-6">
            <div className="w-16 h-16 bg-primary/10 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg
                className="w-8 h-8 text-primary"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
                />
              </svg>
            </div>
            <h1 className="text-2xl font-bold text-foreground">Check your email</h1>
            <p className="text-muted-foreground mt-2">
              We've sent a confirmation link to
            </p>
            <p className="text-foreground font-medium mt-1">{sentToEmail}</p>
          </div>

          <p className="text-muted-foreground text-sm mb-6">
            Click the link in the email to confirm your account and get started.
          </p>

          <Link
            to={(() => {
              const params = new URLSearchParams();
              if (sentToEmail) params.set('email', sentToEmail);
              if (redirectParam) params.set('redirect', redirectParam);
              if (sourceParam) params.set('source', sourceParam);
              const query = params.toString();
              return query ? `/login?${query}` : '/login';
            })()}
          >
            <Button variant="outline" className="w-full">
              Back to Login
            </Button>
          </Link>
        </Card>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-md p-8">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-foreground">Create Account</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Sign up to start generating redirect mappings
          </p>
        </div>

        {error && (
          <div role="alert" className="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded mb-4">
            {error}
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
            <label htmlFor="signup-fullName" className="block text-sm font-medium text-foreground mb-2">
              Full Name
            </label>
            <Input
              type="text"
              id="signup-fullName"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="John Doe"
              required
              autoComplete="name"
              className="w-full"
            />
          </div>

          <div>
            <label htmlFor="signup-email" className="block text-sm font-medium text-foreground mb-2">
              Email
            </label>
            <Input
              type="email"
              id="signup-email"
              value={email}
              onChange={handleEmailChange}
              onBlur={handleEmailBlur}
              placeholder="you@example.com"
              required
              autoComplete="email"
              aria-invalid={!!emailError}
              aria-describedby={emailError ? 'signup-email-error' : undefined}
              className={`w-full ${emailError ? 'border-destructive' : ''}`}
            />
            {emailError && (
              <p id="signup-email-error" role="alert" className="text-destructive text-sm mt-1">{emailError}</p>
            )}
          </div>

          <div>
            <label htmlFor="signup-password" className="block text-sm font-medium text-foreground mb-2">
              Password
            </label>
            <Input
              type="password"
              id="signup-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
              required
              autoComplete="new-password"
              className="w-full"
            />

            {/* Password Strength Meter */}
            {password && (
              <div className="mt-3 space-y-2">
                {/* Strength Bar */}
                <div className="space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">
                      Password strength:
                    </span>
                    <span className={`text-xs font-medium capitalize ${
                      passwordStrength.strength === 'weak' ? 'text-red-500' :
                      passwordStrength.strength === 'fair' ? 'text-yellow-500' :
                      passwordStrength.strength === 'good' ? 'text-green-500' :
                      'text-green-600'
                    }`}>
                      {passwordStrength.strength}
                    </span>
                  </div>
                  <div className="relative">
                    <Progress
                      value={getProgressValue(passwordStrength.score)}
                      className="h-2"
                    />
                    <div
                      className={`absolute top-0 left-0 h-2 rounded-full transition-all ${getStrengthColor(passwordStrength.strength)}`}
                      style={{ width: `${getProgressValue(passwordStrength.score)}%` }}
                    />
                  </div>
                </div>

                {/* Requirements Checklist */}
                <div className="space-y-1.5 pt-1">
                  <div className="flex items-center gap-2">
                    {passwordStrength.criteria.minLength ? (
                      <CheckCircle2 className="w-4 h-4 text-green-500" />
                    ) : (
                      <Circle className="w-4 h-4 text-muted-foreground/50" />
                    )}
                    <span className={`text-xs ${
                      passwordStrength.criteria.minLength
                        ? 'text-foreground'
                        : 'text-muted-foreground'
                    }`}>
                      At least 8 characters
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {passwordStrength.criteria.hasUppercase ? (
                      <CheckCircle2 className="w-4 h-4 text-green-500" />
                    ) : (
                      <Circle className="w-4 h-4 text-muted-foreground/50" />
                    )}
                    <span className={`text-xs ${
                      passwordStrength.criteria.hasUppercase
                        ? 'text-foreground'
                        : 'text-muted-foreground'
                    }`}>
                      Uppercase letter
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {passwordStrength.criteria.hasLowercase ? (
                      <CheckCircle2 className="w-4 h-4 text-green-500" />
                    ) : (
                      <Circle className="w-4 h-4 text-muted-foreground/50" />
                    )}
                    <span className={`text-xs ${
                      passwordStrength.criteria.hasLowercase
                        ? 'text-foreground'
                        : 'text-muted-foreground'
                    }`}>
                      Lowercase letter
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {passwordStrength.criteria.hasNumber ? (
                      <CheckCircle2 className="w-4 h-4 text-green-500" />
                    ) : (
                      <Circle className="w-4 h-4 text-muted-foreground/50" />
                    )}
                    <span className={`text-xs ${
                      passwordStrength.criteria.hasNumber
                        ? 'text-foreground'
                        : 'text-muted-foreground'
                    }`}>
                      Number
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {passwordStrength.criteria.hasSpecial ? (
                      <CheckCircle2 className="w-4 h-4 text-green-500" />
                    ) : (
                      <Circle className="w-4 h-4 text-muted-foreground/50" />
                    )}
                    <span className={`text-xs ${
                      passwordStrength.criteria.hasSpecial
                        ? 'text-foreground'
                        : 'text-muted-foreground'
                    }`}>
                      Special character
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div>
            <label htmlFor="signup-confirmPassword" className="block text-sm font-medium text-foreground mb-2">
              Confirm Password
            </label>
            <Input
              type="password"
              id="signup-confirmPassword"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="Re-enter password"
              required
              autoComplete="new-password"
              className="w-full"
            />
          </div>

          <Button
            type="submit"
            className="w-full"
            disabled={loading || !!oauthLoading || (password.length > 0 && passwordStrength.strength === 'weak') || !!emailError || !email}
          >
            {loading ? 'Creating account...' : 'Sign Up'}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">
          Already have an account?{' '}
          <Link
            to={(() => {
              const params = new URLSearchParams();
              if (email) params.set('email', email);
              if (redirectParam) params.set('redirect', redirectParam);
              if (sourceParam) params.set('source', sourceParam);
              const query = params.toString();
              return query ? `/login?${query}` : '/login';
            })()}
            className="text-primary hover:underline font-medium"
          >
            Login
          </Link>
        </p>
      </Card>
    </div>
  );
}
