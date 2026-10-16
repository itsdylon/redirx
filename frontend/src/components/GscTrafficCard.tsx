import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams, useLocation } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { BarChart3, Loader2, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from './ui/button';
import {
  getGscConnectUrl,
  getGscStatus,
  syncGscSession,
  type GscResultsMeta,
} from '../api/gsc';
import { queryKeys } from '../queries/queryKeys';
import { monitoringKeys } from '../queries/monitoringKeys';
import { useAuth } from '../contexts/AuthContext';
import { useRequestScope } from './pivot/useRequestScope';

interface GscTrafficCardProps {
  sessionId: string;
  gscMeta: GscResultsMeta | null;
}

/**
 * Search Console integration card on the review page.
 *
 * Walks the user through connect -> pull traffic -> risk summary. Once metrics
 * are synced, shows which high-traffic URLs lack a confident match.
 */
export function GscTrafficCard({ sessionId, gscMeta }: GscTrafficCardProps) {
  const { user } = useAuth();
  if (!user) return null;
  return <GscTrafficContent key={`${user.id}:${sessionId}`} sessionId={sessionId} gscMeta={gscMeta} ownerId={user.id} />;
}

function GscTrafficContent({ sessionId, gscMeta, ownerId }: GscTrafficCardProps & { ownerId: string }) {
  const scope = useRequestScope(`${ownerId}:${sessionId}`);
  const queryClient = useQueryClient();
  const posthog = usePostHog();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const autoSyncTriggeredRef = useRef(false);

  const statusQuery = useQuery({
    queryKey: monitoringKeys.gscStatus(ownerId),
    queryFn: ({ signal }) => getGscStatus(signal),
  });

  const syncMutation = useMutation({
    mutationFn: () => syncGscSession(sessionId, undefined, scope.signal()),
    onSuccess: (data) => {
      if (scope.signal().aborted) return;
      posthog?.capture('gsc_sync_completed', {
        session_id: sessionId,
        matched_urls: data.matched_urls,
        total_clicks: data.total_clicks,
        property: data.property,
      });
      toast.success(
        `Search Console data pulled: traffic found for ${data.matched_urls} of ${data.session_url_count} URLs`
      );
      queryClient.invalidateQueries({ queryKey: queryKeys.results.bySession(sessionId) });
    },
    onError: (error) => {
      if (scope.signal().aborted) return;
      toast.error(error instanceof Error ? error.message : 'Failed to pull Search Console data');
    },
  });

  const connected = !!statusQuery.data?.connected;
  const synced = !!gscMeta?.synced;

  // Returning from the Google consent screen: clean the query param and, on
  // success, immediately pull traffic data for this session.
  useEffect(() => {
    const gscParam = searchParams.get('gsc');
    if (!gscParam) return;

    const next = new URLSearchParams(searchParams);
    next.delete('gsc');
    setSearchParams(next, { replace: true });

    if (gscParam === 'connected') {
      // Return parameters are navigation hints, never proof of authorization.
      void getGscStatus(scope.signal()).then(status => {
        if (scope.signal().aborted) return;
        queryClient.setQueryData(monitoringKeys.gscStatus(ownerId), status);
        if (!status.connected) { toast.info('Search Console is not connected. Connect to continue.'); return; }
        toast.success('Google Search Console connected');
        if (!autoSyncTriggeredRef.current && !synced) {
          autoSyncTriggeredRef.current = true;
          syncMutation.mutate();
        }
      }).catch(() => { if (!scope.signal().aborted) toast.error('Search Console connection could not be verified. Retry its status.'); });
    } else if (gscParam === 'unconfirmed') {
      toast.info('The connection attempt was not confirmed. Checking saved connection status.');
      // A lost callback acknowledgment may follow a committed connection. Read
      // authoritative state, but never repeat consent or automatically sync.
      void statusQuery.refetch().then(result => {
        if (scope.signal().aborted) return;
        if (result.isError || !result.data) {
          toast.error('Connection status is still unconfirmed. Retry Search Console status.');
        } else if (result.data.connected) {
          toast.info('A Search Console connection is saved. You can pull traffic when ready.');
        } else {
          toast.info('No Search Console connection is saved. Start a new connection if needed.');
        }
      });
    } else if (gscParam === 'cancelled') {
      toast.info('Search Console connection cancelled');
    } else if (gscParam === 'error') {
      toast.error('Search Console connection failed. Please try again.');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const handleConnect = async () => {
    posthog?.capture('gsc_connect_clicked', { session_id: sessionId });
    try {
      const url = await getGscConnectUrl(location.pathname, scope.signal());
      if (!scope.signal().aborted) window.location.href = url;
    } catch (error) {
      if (scope.signal().aborted) return;
      toast.error(
        error instanceof Error ? error.message : 'Could not start the Search Console connection'
      );
    }
  };

  // Deployment has no Google OAuth client configured; stay out of the way.
  if (statusQuery.data?.configured === false) return null;
  if (statusQuery.isLoading) return null;
  if (statusQuery.isError) return <div className="mb-4 border border-border bg-card p-4"><p role="alert">Search Console status could not load. No connection status has been confirmed.</p><Button className="mt-3" variant="outline" disabled={statusQuery.isFetching} onClick={() => void statusQuery.refetch()}>Retry Search Console status</Button></div>;

  if (synced && gscMeta?.riskSummary) {
    const summary = gscMeta.riskSummary;
    if (summary.urlsWithTraffic === 0) {
      return (
        <div className="mb-4 border border-border bg-card p-4 flex items-center gap-3">
          <BarChart3 className="h-4 w-4 text-muted-foreground flex-shrink-0" />
          <p className="text-sm text-muted-foreground">
            Search Console recorded no clicks or impressions for these URLs in the last 90 days
            {gscMeta.property ? ` (property: ${gscMeta.property})` : ''}.
          </p>
        </div>
      );
    }

    const atRisk = summary.topUrlsAtRisk > 0;
    return (
      <div
        className={`mb-4 border p-4 ${
          atRisk ? 'border-amber-500/50 bg-amber-500/10' : 'border-emerald-500/40 bg-emerald-500/10'
        }`}
      >
        <div className="flex items-start gap-3">
          <ShieldAlert
            className={`h-5 w-5 flex-shrink-0 mt-0.5 ${
              atRisk ? 'text-amber-600' : 'text-emerald-600'
            }`}
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              {summary.topUrlCount} URL{summary.topUrlCount !== 1 ? 's' : ''} carry{' '}
              {summary.trafficShareTarget}% of your organic {summary.weightMetric} —{' '}
              {atRisk
                ? `${summary.topUrlsAtRisk} of them lack a confident match.`
                : 'all of them have a confident match.'}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {summary.totalClicks.toLocaleString()} clicks /{' '}
              {summary.totalImpressions.toLocaleString()} impressions across{' '}
              {summary.urlsWithTraffic} URLs with traffic (last 90 days
              {gscMeta.property ? `, ${gscMeta.property}` : ''}). Sorted by traffic so the
              riskiest URLs come first. A confident suggestion still needs explicit review; it is not verified deployment.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mb-4 border border-border bg-card p-4 flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-3">
        <BarChart3 className="h-5 w-5 text-primary flex-shrink-0" />
        <div>
          <p className="text-sm font-medium text-foreground">
            {connected
              ? 'Pull Search Console traffic for this project'
              : 'Which of these URLs actually matter?'}
          </p>
          <p className="text-xs text-muted-foreground">
            {connected
              ? 'Rank every redirect by real organic clicks so you review the riskiest URLs first.'
              : 'Connect Google Search Console to weight this review by real organic traffic.'}
          </p>
        </div>
      </div>
      {connected ? (
        <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
          {syncMutation.isPending ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Pulling traffic data...
            </>
          ) : (
            'Pull Traffic Data'
          )}
        </Button>
      ) : (
        <Button onClick={handleConnect}>Connect Search Console</Button>
      )}
    </div>
  );
}
