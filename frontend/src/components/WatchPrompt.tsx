import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Activity, ArrowRight, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

import { Card } from './ui/card';
import { Button } from './ui/button';
import { monitoringKeys } from '../queries/monitoringKeys';
import { useRequestScope } from './pivot/useRequestScope';
import { createWatch, listWatches } from '../api/watch';
import { useAuth } from '../contexts/AuthContext';
import { isEnterprisePlan } from '../lib/plans';
import { MCP_PIVOT_ENABLED } from '../api/config';

/**
 * Offer post-cutover monitoring from the review page.
 *
 * Placed here rather than in the export modal because the offer only makes
 * sense once, and a modal the user dismisses to download their file is the
 * wrong place to put something they should still be able to find tomorrow.
 *
 * Renders as a link to the existing watch when there already is one, so this
 * doubles as the way back into monitoring for a returning user.
 */
export function WatchPrompt({ sessionId, pivotEnabled = MCP_PIVOT_ENABLED }: { sessionId: string; pivotEnabled?: boolean }) {
  const { user } = useAuth();
  if (!user) return null;
  return <WatchPromptContent key={`${user.id}:${sessionId}`} sessionId={sessionId} pivotEnabled={pivotEnabled} ownerId={user.id} plan={user.plan} />;
}

function WatchPromptContent({ sessionId, pivotEnabled, ownerId, plan }: { sessionId: string; pivotEnabled: boolean; ownerId: string; plan?: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const scope = useRequestScope(`${ownerId}:${sessionId}`);
  // Legacy creation is plan-gated. Pivot mode retains existing watch access,
  // including for lapsed plans, but acquires no new legacy watches.
  const paidPlan = isEnterprisePlan(plan);

  const watchesQuery = useQuery({
    queryKey: monitoringKeys.watches(ownerId),
    queryFn: ({ signal }) => listWatches(signal),
    enabled: paidPlan || pivotEnabled,
  });

  const existing = watchesQuery.data?.find((w) => w.session_id === sessionId);

  const createMutation = useMutation({
    mutationFn: () => createWatch({ sessionId }, scope.signal()),
    onSuccess: (watch) => {
      if (scope.signal().aborted) return;
      queryClient.invalidateQueries({ queryKey: monitoringKeys.watches(ownerId) });
      toast.success('Monitoring started. Open the monitor to check coverage and alert status.');
      navigate(`/watch/${watch.id}`);
    },
    onError: (error: Error) => { if (!scope.signal().aborted) toast.error(error.message); },
  });

  if ((paidPlan || pivotEnabled) && watchesQuery.isLoading) return null;
  if ((paidPlan || pivotEnabled) && watchesQuery.isError) return <Card className="mt-6 p-4"><p role="alert">Monitoring status could not load. Retry before starting another monitor.</p><Button variant="outline" className="mt-3" disabled={watchesQuery.isFetching} onClick={() => void watchesQuery.refetch()}>Retry monitoring status</Button></Card>;
  if (pivotEnabled && !existing) return null;

  if (!pivotEnabled && !paidPlan) {
    return (
      <Card className="mt-6 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-start gap-3">
            <Activity className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
            <div>
              <p className="text-sm font-medium text-foreground">
                Watch these redirects after you deploy
              </p>
              <p className="mt-0.5 text-sm text-muted-foreground">
                On paid plans, we check the live site on a schedule and email you
                if a redirect 404s, lands on the wrong page, or never shipped.
              </p>
            </div>
          </div>
          <Button size="sm" onClick={() => navigate('/pricing')}>
            See plans
            <ArrowRight className="ml-1.5 h-4 w-4" />
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <Card className="mt-6 p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-start gap-3">
          <Activity className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
          <div>
            <p className="text-sm font-medium text-foreground">
              {existing ? (pivotEnabled ? 'Existing redirect monitor' : existing.status === 'active' ? 'Monitoring is on' : existing.status === 'ended' ? 'Monitoring ended' : 'Monitoring paused') : 'Watch these redirects after you deploy'}
            </p>
            <p className="mt-0.5 text-sm text-muted-foreground">
              {pivotEnabled
                ? 'Open the monitor to inspect its current status and results.'
                : existing
                ? 'Open the monitor to inspect its current coverage, schedule and alert status.'
                : 'We check the live site on a schedule and email you if a redirect 404s, lands on the wrong page, or never shipped.'}
            </p>
          </div>
        </div>

        {existing ? (
          <Button variant="outline" size="sm" onClick={() => navigate(`/watch/${existing.id}`)}>
            View
            <ArrowRight className="ml-1.5 h-4 w-4" />
          </Button>
        ) : (
          <Button
            size="sm"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending}
          >
            {createMutation.isPending && (
              <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
            )}
            Start monitoring
          </Button>
        )}
      </div>
    </Card>
  );
}
