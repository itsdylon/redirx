import { ApiError } from '../utils/errorHandler';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Download,
  Loader2,
  Pause,
  Play,
  RefreshCw,
} from 'lucide-react';
import { toast } from 'sonner';

import { ToolLayout } from './ToolLayout';
import { Card } from './ui/card';
import { Button } from './ui/button';
import { monitoringKeys } from '../queries/monitoringKeys';
import { useAuth } from '../contexts/AuthContext';
import { useRequestScope } from './pivot/useRequestScope';
import { watchOutcome } from '../lib/watchOutcome';
import { formatDateTime } from '../utils/date';
import {
  ISSUE_COPY,
  checkNow,
  downloadFixes,
  getWatch,
  setWatchStatus,
  type WatchIssue,
} from '../api/watch';

const FIX_FORMATS = [
  { value: 'apache', label: '.htaccess' },
  { value: 'nginx', label: 'Nginx' },
  { value: 'vercel', label: 'Vercel' },
  { value: 'cloudflare', label: 'Cloudflare' },
  { value: 'wordpress', label: 'WordPress' },
  { value: 'shopify', label: 'Shopify' },
  { value: 'csv', label: 'CSV' },
  { value: 'json', label: 'JSON' },
];

function IssueRow({ issue }: { issue: WatchIssue }) {
  const copy = ISSUE_COPY[issue.issue_type] ?? {
    label: issue.issue_type,
    hint: '',
  };
  const critical = issue.severity === 'critical';

  return (
    <div className="border-b border-border px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <code className="break-all text-sm text-foreground">{issue.old_url}</code>
        {issue.clicks_at_risk > 0 && (
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {issue.clicks_at_risk.toLocaleString()} clicks/mo
          </span>
        )}
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <span
          className={
            critical
              ? 'rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive'
              : 'rounded-full bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-500'
          }
        >
          {copy.label}
        </span>
        {issue.detail && (
          <span className="text-xs text-muted-foreground">{issue.detail}</span>
        )}
      </div>

      {copy.hint && <p className="mt-1.5 text-xs text-muted-foreground">{copy.hint}</p>}

      {issue.suggested_target && (
        <p className="mt-2 text-xs text-muted-foreground">
          Should point to{' '}
          <code className="break-all text-foreground">{issue.suggested_target}</code>
        </p>
      )}
    </div>
  );
}

// Access/deletion errors require an explicit read after the underlying issue is
// resolved. Repeating them cannot advance a queued sweep.
function requiresMonitorRecovery(error: unknown) {
  return error instanceof ApiError && [401, 403, 404].includes(error.status ?? 0);
}

export function WatchPage() {
  const { watchId } = useParams<{ watchId: string }>();
  const { user } = useAuth();
  if (!user || !watchId) return <ToolLayout title="Redirect monitoring"><p role="status">Sign in to view this monitor.</p></ToolLayout>;
  return <WatchContent key={`${user.id}:${watchId}`} ownerId={user.id} watchId={watchId} />;
}

function WatchContent({ ownerId, watchId }: { ownerId: string; watchId: string }) {
  const navigate = useNavigate();
  const scope = useRequestScope(`${ownerId}:${watchId}`);
  const queryClient = useQueryClient();
  const [format, setFormat] = useState('apache');
  const [downloading, setDownloading] = useState(false);

  const watchQuery = useQuery({
    queryKey: monitoringKeys.watch(ownerId, watchId),
    queryFn: ({ signal }) => getWatch(watchId, signal),
    enabled: Boolean(watchId),
    // A queued sweep lands asynchronously in the worker, so the page polls
    // rather than leaving the user to guess when to reload.
    refetchInterval: query => requiresMonitorRecovery(query.state.error) ? false : 30_000,
    retry: (failures, error) => !requiresMonitorRecovery(error) && failures < 1,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: monitoringKeys.watch(ownerId, watchId) });

  const statusMutation = useMutation({
    mutationFn: (status: 'active' | 'paused') => setWatchStatus(watchId, status, scope.signal()),
    onSuccess: (_data, status) => {
      if (scope.signal().aborted) return;
      toast.success(status === 'active' ? 'Monitoring resumed' : 'Monitoring paused');
      invalidate();
    },
    onError: (error: Error) => { if (!scope.signal().aborted) toast.error(error.message); },
  });

  const checkMutation = useMutation({
    mutationFn: () => checkNow(watchId, scope.signal()),
    onSuccess: () => {
      if (scope.signal().aborted) return;
      toast.success('Check queued — results usually appear within a few minutes.');
      invalidate();
    },
    onError: (error: Error) => { if (!scope.signal().aborted) toast.error(error.message); },
  });

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await downloadFixes(watchId, format, scope.signal());
    } catch (error) {
      if (!scope.signal().aborted) toast.error(error instanceof Error ? error.message : 'Download failed');
    } finally {
      if (!scope.signal().aborted) setDownloading(false);
    }
  };

  if (watchQuery.isLoading) {
    return (
      <ToolLayout title="Redirect monitoring">
        <div className="flex items-center justify-center py-24">
          <Loader2 aria-hidden="true" className="h-6 w-6 animate-spin text-muted-foreground" /><p role="status" className="ml-3">Loading monitor…</p>
        </div>
      </ToolLayout>
    );
  }

  if (watchQuery.error || !watchQuery.data) {
    return (
      <ToolLayout title="Redirect monitoring">
        <div className="mx-auto max-w-2xl px-4 py-16 text-center">
          <p role="alert" className="text-sm text-muted-foreground">
            {watchQuery.error instanceof Error
              ? watchQuery.error.message
              : 'This watch could not be loaded.'}
          </p>
          <Button className="mt-4" variant="outline" disabled={watchQuery.isFetching} onClick={() => void watchQuery.refetch()}>Retry loading monitor</Button>
        </div>
      </ToolLayout>
    );
  }

  const { watch, issues, summary, checks } = watchQuery.data;
  const lastCheck = checks[0];
  const neverChecked = !watch.last_checked_at;
  const outcome = watchOutcome(lastCheck, Math.max(issues.length, summary.open_issues));
  const fixable = issues.filter((i) => i.suggested_target);

  return (
    <ToolLayout title="Redirect monitoring">
      <div className="mx-auto max-w-4xl px-4 py-8">
        <button
          onClick={() => navigate(-1)}
          className="mb-6 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </button>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="break-all text-2xl font-semibold tracking-tight text-foreground">
              Monitoring {watch.old_domain}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {neverChecked
                ? 'Waiting for the first check.'
                : `Last checked ${formatDateTime(watch.last_checked_at!)}`}
              {watch.status === 'paused' && ' · Paused'}
              {watch.status === 'ended' && ' · Ended'}
            </p>
          </div>

          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => checkMutation.mutate()}
              disabled={checkMutation.isPending || statusMutation.isPending || lastCheck?.status === 'running' || watch.status !== 'active'}
            >
              <RefreshCw className="mr-1.5 h-4 w-4" />
              Check now
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                statusMutation.mutate(watch.status === 'active' ? 'paused' : 'active')
              }
              disabled={statusMutation.isPending || checkMutation.isPending}
            >
              {watch.status === 'active' ? (
                <>
                  <Pause className="mr-1.5 h-4 w-4" />
                  Pause
                </>
              ) : (
                <>
                  <Play className="mr-1.5 h-4 w-4" />
                  {watch.status === 'ended' ? 'Restart monitoring' : 'Resume'}
                </>
              )}
            </Button>
          </div>
        </div>

        {/*
          Traffic first. "9 redirects are broken" is a support ticket; the same
          sentence with a clicks number attached is a decision about today.
        */}
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Card className="p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Recorded clicks at risk
            </p>
            <p className="mt-1 text-2xl font-semibold text-foreground">
              {summary.clicks_at_risk.toLocaleString()}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">per month from the saved baseline; missing traffic is not measured</p>
          </Card>
          <Card className="p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Critical
            </p>
            <p className="mt-1 text-2xl font-semibold text-foreground">{summary.critical}</p>
          </Card>
          <Card className="p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Open issues
            </p>
            <p className="mt-1 text-2xl font-semibold text-foreground">
              {summary.open_issues}
            </p>
            {lastCheck && (
              <p className="mt-0.5 text-xs text-muted-foreground">
                Latest sweep: {lastCheck.urls_checked.toLocaleString()} checked
              </p>
            )}
          </Card>
        </div>

        <Card className="mt-6 p-6" role="status">
          {outcome.healthy ? <CheckCircle2 aria-hidden="true" className="h-7 w-7 text-emerald-600" /> : <AlertTriangle aria-hidden="true" className="h-6 w-6 text-muted-foreground" />}
          <h2 className="mt-3 text-base font-semibold">{outcome.title}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{outcome.detail}</p>
          {watch.status !== 'active' && <p className="mt-2 text-sm">Monitoring is {watch.status}. No recurring checks are scheduled until you {watch.status === 'ended' ? 'restart monitoring' : 'resume'}.</p>}
          <p className="mt-2 text-sm text-muted-foreground">{watch.alert_email
            ? `Alert address: ${watch.alert_email}. Email delivery depends on alert eligibility and successful delivery.`
            : 'Alerts use your account email when available. Delivery is not confirmed by a clean check.'}</p>
        </Card>
        {issues.length > 0 && (
          <>
            {fixable.length > 0 && (
              <Card className="mt-6 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-foreground">
                      Download suggested corrections
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {fixable.length} suggested redirect{fixable.length === 1 ? '' : 's'},
                      to review before installing. Downloading does not deploy or verify them.
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      aria-label="Correction export format"
                      value={format}
                      onChange={(e) => setFormat(e.target.value)}
                      className="h-9 rounded-md border border-input bg-background px-2 text-sm"
                    >
                      {FIX_FORMATS.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                    </select>
                    <Button size="sm" onClick={handleDownload} disabled={downloading}>
                      {downloading ? (
                        <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
                      ) : (
                        <Download className="mr-1.5 h-4 w-4" />
                      )}
                      Download
                    </Button>
                  </div>
                </div>
              </Card>
            )}

            <Card className="mt-4 overflow-hidden">
              <div className="flex items-center gap-2 border-b border-border px-4 py-3">
                <AlertTriangle className="h-4 w-4 text-destructive" />
                <p className="text-sm font-medium text-foreground">
                  {issues.length} issue{issues.length === 1 ? '' : 's'}, highest traffic
                  first
                </p>
              </div>
              {issues.map((issue) => (
                <IssueRow key={issue.id} issue={issue} />
              ))}
            </Card>
          </>
        )}

        {(lastCheck?.error || watch.last_error) && (
          <p className="mt-4 text-xs text-muted-foreground">
            Last check reported: {lastCheck?.error || watch.last_error}
          </p>
        )}
      </div>
    </ToolLayout>
  );
}
