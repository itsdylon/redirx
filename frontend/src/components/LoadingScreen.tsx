import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Loader2, AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';
import { Progress } from './ui/progress';
import { Alert, AlertTitle, AlertDescription } from './ui/alert';
import { getSessionStatus, SessionStatus } from '../api/sessions';
import { useAuth } from '../contexts/AuthContext';
import { isEnterprisePlan } from '../lib/plans';
import { ROUTES, getRetryRouteForPlan } from '../routes';

interface LoadingScreenProps {
  sessionId?: string | null;
  tutorialMode?: boolean;
  minDisplayMs?: number;
}

const POLL_INTERVAL = 3000; // 3 seconds
const WARNING_THRESHOLD = 3; // Show warning after 3 failures
const ERROR_THRESHOLD = 5; // Show error after 5 failures

export function LoadingScreen({
  sessionId,
  tutorialMode = false,
  minDisplayMs = 0,
}: LoadingScreenProps) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const enterpriseUser = isEnterprisePlan(user?.plan);
  const retryRoute = getRetryRouteForPlan(user?.plan);
  const fallbackRoute = enterpriseUser ? ROUTES.dashboard : ROUTES.quickMatch;
  const [progress, setProgress] = useState<{
    currentStage: number | null;
    stageName: string | null;
    totalStages: number | null;
  }>({ currentStage: null, stageName: null, totalStages: null });
  const [error, setError] = useState<string | null>(null);
  const [failureCount, setFailureCount] = useState(0);
  const [nextRetryIn, setNextRetryIn] = useState(0);
  const [pollingError, setPollingError] = useState(false);
  const [retryAttempt, setRetryAttempt] = useState(0);
  const [isDelayingCompletion, setIsDelayingCompletion] = useState(false);
  const startedAtRef = useRef<number>(Date.now());

  useEffect(() => {
    startedAtRef.current = Date.now();
    setIsDelayingCompletion(false);
  }, [sessionId, minDisplayMs]);

  // Calculate exponential backoff delay
  const getBackoffDelay = (failures: number): number => {
    return Math.min(POLL_INTERVAL * Math.pow(2, failures), 30000);
  };

  // Poll for job completion with failure handling
  useEffect(() => {
    if (!sessionId) return;

    let active = true;
    // Keep retry accounting inside this request lifetime. UI state updates
    // must not restart the effect and cancel its scheduled backoff.
    let failures = 0;
    setFailureCount(0);
    setNextRetryIn(0);
    setPollingError(false);
    setError(null);
    setProgress({ currentStage: null, stageName: null, totalStages: null });
    let timeoutId: NodeJS.Timeout;
    let countdownIntervalId: NodeJS.Timeout | null = null;

    const poll = async () => {
      if (!active) return;
      // Stop polling if we've hit the error threshold
      if (failures >= ERROR_THRESHOLD) {
        setPollingError(true);
        return;
      }

      try {
        const status: SessionStatus = await getSessionStatus(sessionId);
        if (!active) return;

        // Successful poll - reset failure count and stop the retry countdown.
        failures = 0;
        if (countdownIntervalId) {
          clearInterval(countdownIntervalId);
          countdownIntervalId = null;
        }
        setFailureCount(0);
        setNextRetryIn(0);

        // Update progress state
        setProgress({
          currentStage: status.current_stage ?? null,
          stageName: status.stage_name ?? null,
          totalStages: status.total_stages ?? null,
        });

        if (status.status === 'completed') {
          const elapsedMs = Date.now() - startedAtRef.current;
          const remainingMs = Math.max(0, minDisplayMs - elapsedMs);

          if (remainingMs > 0) {
            setIsDelayingCompletion(true);
            setProgress({
              currentStage: status.total_stages ?? status.current_stage ?? 4,
              stageName: tutorialMode ? 'Finalizing sample tutorial flow' : status.stage_name ?? 'Finalizing',
              totalStages: status.total_stages ?? 4,
            });
            timeoutId = setTimeout(poll, Math.min(remainingMs, 500));
            return;
          }

          setIsDelayingCompletion(false);
          navigate(tutorialMode ? `/review/${sessionId}?tutorial=1` : `/review/${sessionId}`);
          return;
        } else if (status.status === 'failed') {
          setError('The processing job failed. This could be due to invalid URLs, network issues, or an internal error. Please try uploading your files again.');
          console.error('Job failed');
          return;
        }

        // Schedule next poll with normal interval
        timeoutId = setTimeout(poll, POLL_INTERVAL);
      } catch (error) {
        if (!active) return;
        console.error('Error polling status:', error);

        // Increment failure count
        failures += 1;
        setFailureCount(failures);
        if (failures >= ERROR_THRESHOLD) {
          if (countdownIntervalId) clearInterval(countdownIntervalId);
          countdownIntervalId = null;
          setNextRetryIn(0);
          setPollingError(true);
          return;
        }

        // Calculate backoff delay
        const delay = getBackoffDelay(failures);
        setNextRetryIn(Math.ceil(delay / 1000)); // Convert to seconds

        // Start countdown timer
        if (countdownIntervalId) {
          clearInterval(countdownIntervalId);
        }
        countdownIntervalId = setInterval(() => {
          setNextRetryIn((prev) => Math.max(0, prev - 1));
        }, 1000);

        // Schedule next poll with backoff
        timeoutId = setTimeout(poll, delay);
      }
    };

    // Start polling
    poll();

    return () => {
      active = false;
      clearTimeout(timeoutId);
      if (countdownIntervalId) {
        clearInterval(countdownIntervalId);
      }
    };
  }, [retryAttempt, minDisplayMs, navigate, sessionId, tutorialMode, user?.id]);

  const handleContinueInBackground = () => {
    navigate(fallbackRoute);
  };

  const handleRetry = () => {
    setRetryAttempt(attempt => attempt + 1);
  };

  const hasProgress = progress.currentStage != null && progress.totalStages != null;
  const progressPercent = hasProgress
    ? (progress.currentStage! / progress.totalStages!) * 100
    : 0;

  // Show error UI if job failed
  if (error) {
    return (
      <div className="min-h-0 flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-xl">
          <div className="space-y-6">
            <Alert variant="destructive">
              <AlertTriangle className="h-5 w-5" />
              <AlertTitle>Processing Failed</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>

            <div className="flex flex-col sm:flex-row gap-3">
              <Button
                onClick={() => navigate(retryRoute)}
                className="flex-1"
              >
                Try Again
              </Button>
              <Button
                variant="outline"
                onClick={() => navigate(fallbackRoute)}
                className="flex-1"
              >
                {enterpriseUser ? 'View Dashboard' : 'Back to Quick Match'}
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Show error UI if polling failed too many times
  if (pollingError) {
    return (
      <div className="min-h-0 flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-xl">
          <div className="space-y-6">
            <Alert variant="destructive">
              <AlertTriangle className="h-5 w-5" />
              <AlertTitle>Unable to Check Job Status</AlertTitle>
              <AlertDescription>
                Unable to check job status. The job may still be running. Please try refreshing or return to your previous workspace.
              </AlertDescription>
            </Alert>

            <div className="flex flex-col sm:flex-row gap-3">
              <Button
                onClick={handleRetry}
                className="flex-1"
              >
                Retry
              </Button>
              <Button
                variant="outline"
                onClick={() => navigate(fallbackRoute)}
                className="flex-1"
              >
                {enterpriseUser ? 'Go to Dashboard' : 'Back to Quick Match'}
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 flex items-center justify-center p-8">
      <div className="w-full max-w-xl">
        <div className="text-center">
          {/* Warning banner for polling failures */}
          {failureCount >= WARNING_THRESHOLD && failureCount < ERROR_THRESHOLD && (
            <Alert variant="default" className="mb-6 text-left border-amber-500 dark:border-amber-600">
              <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-500" />
              <AlertTitle>Having Trouble Checking Status</AlertTitle>
              <AlertDescription className="space-y-3">
                <p>
                  Having trouble checking status. Retrying in {nextRetryIn} second{nextRetryIn !== 1 ? 's' : ''}...
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleRetry}
                  className="w-full sm:w-auto"
                >
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Refresh Now
                </Button>
              </AlertDescription>
            </Alert>
          )}

          {/* Progress heading */}
          <h1 className="text-foreground mb-2">
            {hasProgress
              ? `Step ${progress.currentStage} of ${progress.totalStages}`
              : 'Preparing...'}
          </h1>

          {/* Stage name with spinner */}
          <div className="flex items-center justify-center gap-2 mb-6">
            <Loader2 className="h-4 w-4 text-muted-foreground animate-spin" />
            <p className="text-muted-foreground">
              {progress.stageName ? `${progress.stageName}...` : 'Analyzing and matching your URLs.'}
            </p>
          </div>

          {/* Progress bar */}
          <div className="mb-8">
            <Progress value={progressPercent} className="h-3" />
          </div>

          {/* Info box */}
          <div className="mb-6 bg-card border border-border p-4 text-left">
            <p className="text-sm text-muted-foreground">
              {tutorialMode
                ? (isDelayingCompletion
                    ? 'Holding for a brief tutorial pause before opening your sample results.'
                    : 'Tutorial mode: this sample run intentionally includes a short processing pause so the flow is easier to follow.')
                : `You can wait here or continue working. Your job will process in the background and you can view the results from your ${enterpriseUser ? 'dashboard' : 'quick-match history'}.`
              }
            </p>
          </div>

          {/* Continue in Background Button */}
          <Button
            variant="outline"
            onClick={handleContinueInBackground}
            className="w-full"
          >
            Continue in background
          </Button>
        </div>
      </div>
    </div>
  );
}
