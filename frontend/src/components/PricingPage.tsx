import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';

import { DashboardLayout } from './DashboardLayout';
import { ToolLayout } from './ToolLayout';
import { Card } from './ui/card';
import { Button } from './ui/button';
import { Slider } from './ui/slider';
import { Badge } from './ui/badge';

import {
  createAgencyCheckout,
  createProjectCheckout,
  createProjectQuote,
  getBillingStatus,
  getPricingEstimate,
  getProjectUnlockStatus,
  type PricingEstimate,
  type ProjectQuote,
} from '../api/billing';
import { queryKeys } from '../queries/queryKeys';
import { ApiError } from '../utils/errorHandler';
import { useAuth } from '../contexts/AuthContext';
import { isEnterprisePlan } from '../lib/plans';
import { MCP_PIVOT_ENABLED } from '../api/config';

function formatUsdFromCents(value: number | null | undefined): string {
  if (value == null) return '—';
  return `$${(value / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function SliderEstimate({ estimate }: { estimate: PricingEstimate | null }) {
  if (!estimate) {
    return <p className="text-sm text-muted-foreground">Loading estimate...</p>;
  }

  if (estimate.contact_required) {
    return (
      <div className="border border-amber-500/40 bg-amber-500/10 p-4">
        <p className="font-medium">Contact Sales Required</p>
        <p className="text-sm text-muted-foreground mt-1">
          This project is above 100,000 pages. Contact sales for enterprise pricing.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-end justify-between gap-3 border-b border-border pb-3">
        <div>
          <p className="text-sm text-muted-foreground">Estimated One-Time Total</p>
          <p className="text-3xl font-semibold text-foreground">{formatUsdFromCents(estimate.subtotal_cents)}</p>
        </div>
        <div className="text-right">
          <p className="text-sm text-muted-foreground">Effective Rate</p>
          <p className="text-lg font-medium text-foreground">${estimate.effective_rate_usd}/page</p>
        </div>
      </div>

      <div className="space-y-2">
        {estimate.line_items.map((item) => (
          <div key={`${item.from_page}-${item.to_page}`} className="flex items-center justify-between text-sm border border-border p-2">
            <div>
              {item.from_page.toLocaleString()} - {item.to_page.toLocaleString()} pages
              <span className="text-muted-foreground"> @ ${item.unit_price_usd}</span>
            </div>
            <div className="font-medium">{formatUsdFromCents(item.amount_cents)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function QuoteSummary({ quote }: { quote: ProjectQuote }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Quoted Project Size</p>
        <Badge variant="outline">{quote.billable_pages.toLocaleString()} pages</Badge>
      </div>
      <div className="text-3xl font-semibold">{formatUsdFromCents(quote.subtotal_cents)}</div>
      <p className="text-xs text-muted-foreground">
        Billable pages are computed as max(old sitemap URLs, new sitemap URLs).
      </p>

      <div className="space-y-2">
        {quote.line_items.map((item) => (
          <div key={`${item.from_page}-${item.to_page}`} className="flex items-center justify-between text-sm border border-border p-2">
            <div>
              {item.from_page.toLocaleString()} - {item.to_page.toLocaleString()} pages
            </div>
            <div className="font-medium">{formatUsdFromCents(item.amount_cents)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Keep compatibility acquisition completely unmounted during the pivot:
 * its quote query creates a record even without a checkout click. */
export function PricingPage({ pivotEnabled = MCP_PIVOT_ENABLED }: { pivotEnabled?: boolean } = {}) {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const key = `${user?.id ?? 'signed-out'}:${params.get('source_session_id') ?? ''}`;
  return pivotEnabled ? <RetiredPricingHandoff key={key} /> : <LegacyPricingPage key={key} />;
}

function RetiredPricingHandoff() {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const sourceSessionId = params.get('source_session_id') || '';
  const status = useQuery({
    queryKey: queryKeys.billing.unlockStatusForOwner(user?.id || '', sourceSessionId),
    queryFn: ({ signal }) => getProjectUnlockStatus(sourceSessionId, signal),
    enabled: !!sourceSessionId && !!user?.id,
    retry: false,
    refetchInterval: query => query.state.data?.quote_status === 'checkout_created' ? 4000 : false,
  });
  const paid = status.data?.is_unlocked;
  return <ToolLayout title="Migration billing"><Card className="max-w-2xl space-y-4 p-6">
    <h1 className="text-xl font-semibold">Continue in the migration companion</h1>
    <p>Plan new migrations with your connected agent. The companion shows the quote and payment step when needed.</p>
    <Button onClick={() => navigate('/companion')}>Open migration companion</Button>
    {sourceSessionId && <section className="space-y-3 border-t pt-4" aria-label="Previous project purchase">
      <h2 className="font-medium">Previous project purchase</h2>
      <p className="text-sm">Existing purchases and results remain available. Checkout returns are checked against the server.</p>
      {status.isLoading && <p role="status">Checking purchase status…</p>}
      {status.error && <p role="alert">Purchase status could not load. Open the project or try again.</p>}
      {status.data && <p role="status">{paid ? 'This project is paid.'
        : status.data.quote_status === 'checkout_created' ? 'Waiting for payment confirmation.'
        : 'No confirmed purchase is available for this project.'}</p>}
      <Button variant="outline" onClick={() => navigate(`/review/${encodeURIComponent(sourceSessionId)}`)}>Open previous project</Button>
      {paid && status.data?.deep_session_id && <Button variant="outline"
        onClick={() => navigate(`/review/${encodeURIComponent(status.data!.deep_session_id!)}`)}>Open purchased results</Button>}
      <Button variant="outline" disabled={status.isFetching} onClick={() => void status.refetch()}>Refresh purchase status</Button>
    </section>}
  </Card></ToolLayout>;
}

function LegacyPricingPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  const sourceSessionId = searchParams.get('source_session_id') || '';
  const enterpriseUser = isEnterprisePlan(user?.plan);
  const toolUnlockOnly = !enterpriseUser;
  const Layout = enterpriseUser ? DashboardLayout : ToolLayout;
  const [pageCount, setPageCount] = useState(5000);
  const [billingCycle, setBillingCycle] = useState<'monthly' | 'annual'>('monthly');

  const estimateQuery = useQuery({
    queryKey: queryKeys.billing.estimate(pageCount),
    queryFn: () => getPricingEstimate(pageCount),
    enabled: enterpriseUser,
  });

  const quoteQuery = useQuery({
    queryKey: queryKeys.billing.quoteForOwner(user?.id || '', sourceSessionId),
    queryFn: () => createProjectQuote(sourceSessionId),
    enabled: !!sourceSessionId && !!user?.id,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const billingStatusQuery = useQuery({
    queryKey: queryKeys.billing.forOwner(user?.id || ''),
    queryFn: ({ signal }) => getBillingStatus(signal),
    enabled: enterpriseUser && !!user?.id,
    retry: false,
  });

  const projectCheckout = useMutation({
    retry: false,
    mutationFn: async () => {
      if (!sourceSessionId) throw new Error('No source session selected');
      const quoteId = quoteQuery.data?.id;
      return createProjectCheckout({
        sourceSessionId,
        quoteId,
        successUrl: `${window.location.origin}/review/${encodeURIComponent(sourceSessionId)}?unlock=success`,
        cancelUrl: `${window.location.origin}/pricing?source_session_id=${encodeURIComponent(sourceSessionId)}&status=cancelled`,
      });
    },
    onSuccess: (data) => {
      if (!active.current) return;
      if (data.already_paid) {
        if (data.deep_session_id) {
          navigate(`/review/${encodeURIComponent(data.deep_session_id)}`);
          return;
        }
        toast.success('Project is already unlocked.');
        return;
      }

      if (data.url) {
        window.location.assign(data.url);
        return;
      }

      toast.error('Checkout link was not returned by billing service.');
    },
    onError: (err) => {
      if (!active.current) return;
      if (err instanceof ApiError) {
        toast.error(err.user_message || err.message);
        return;
      }
      toast.error(err instanceof Error ? err.message : 'Unable to start checkout right now.');
    },
  });

  const agencyCheckout = useMutation({
    retry: false,
    mutationFn: () => createAgencyCheckout({ billingCycle }),
    onSuccess: (data) => {
      if (!active.current) return;
      if (data.url) {
        window.location.assign(data.url);
        return;
      }
      toast.error('Checkout link was not returned by billing service.');
    },
    onError: (err) => {
      if (!active.current) return;
      if (err instanceof ApiError) {
        toast.error(err.user_message || err.message);
        return;
      }
      toast.error(err instanceof Error ? err.message : 'Unable to start checkout right now.');
    },
  });

  const projectPanelTitle = sourceSessionId ? 'Unlock Deep Match For This Project' : 'Project Pricing Estimator';
  const projectQuote = quoteQuery.data ?? null;
  const estimate = estimateQuery.data ?? null;
  const quoteIsContactRequired = projectQuote?.status === 'contact_required';
  const status = billingStatusQuery.data;

  const agencyPriceCopy = useMemo(() => {
    return billingCycle === 'monthly' ? '$349 / month' : '$299 / month (billed annually)';
  }, [billingCycle]);

  if (toolUnlockOnly && !sourceSessionId) {
    return (
      <Layout title="Project Unlock">
        <div className="max-w-xl">
          <Card className="p-6 space-y-4">
            <p className="text-lg font-semibold text-foreground">No project selected</p>
            <p className="text-sm text-muted-foreground">
              Open pricing from a Quick Match results page to unlock Deep Match for that project.
            </p>
            <Button onClick={() => navigate('/quick-match')}>Back to Quick Match</Button>
          </Card>
        </div>
      </Layout>
    );
  }

  if (toolUnlockOnly) {
    return (
      <Layout title="Project Unlock">
        <div className="max-w-2xl">
          <Card className="p-6 space-y-5">
            <div>
              <p className="text-xl font-semibold">Unlock Deep Match For This Project</p>
              <p className="text-sm text-muted-foreground mt-1">
                One-time project pricing based on your uploaded sitemap counts.
              </p>
            </div>

            {quoteQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Building quote from your uploaded sitemap counts...
              </div>
            ) : projectQuote ? (
              <>
                <QuoteSummary quote={projectQuote} />
                {quoteIsContactRequired ? (
                  <Button asChild>
                    <a href="mailto:sales@redirx.dev?subject=RedirX%20Enterprise%20Quote">Contact Sales</a>
                  </Button>
                ) : (
                  <Button onClick={() => projectCheckout.mutate()} disabled={projectCheckout.isPending}>
                    {projectCheckout.isPending && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
                    Unlock Deep Match
                  </Button>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">Unable to load quote for this project.</p>
            )}
          </Card>
        </div>
      </Layout>
    );
  }

  return (
    <Layout title="Pricing">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 max-w-6xl">
        <Card className="p-6 space-y-5">
          <div>
            <p className="text-xl font-semibold">{projectPanelTitle}</p>
            <p className="text-sm text-muted-foreground mt-1">
              Graduated one-time pricing for Deep Match based on billable pages.
            </p>
          </div>

          {sourceSessionId ? (
            quoteQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Building quote from your uploaded sitemap counts...
              </div>
            ) : projectQuote ? (
              <>
                <QuoteSummary quote={projectQuote} />

                {quoteIsContactRequired ? (
                  <Button asChild>
                    <a href="mailto:sales@redirx.dev?subject=RedirX%20Enterprise%20Quote">Contact Sales</a>
                  </Button>
                ) : (
                  <Button onClick={() => projectCheckout.mutate()} disabled={projectCheckout.isPending}>
                    {projectCheckout.isPending && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
                    Unlock Deep Match
                  </Button>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">Unable to load quote for this project.</p>
            )
          ) : (
            <>
              <div>
                <div className="flex items-center justify-between text-sm text-muted-foreground mb-2">
                  <span>Page Count</span>
                  <span>{pageCount.toLocaleString()} pages</span>
                </div>
                <Slider
                  min={100}
                  max={100000}
                  step={100}
                  value={[pageCount]}
                  onValueChange={(value) => setPageCount(value[0] ?? 5000)}
                />
              </div>

              <SliderEstimate estimate={estimate} />

              {estimate?.contact_required && (
                <Button asChild>
                  <a href="mailto:sales@redirx.dev?subject=RedirX%20Enterprise%20Quote">Contact Sales</a>
                </Button>
              )}
            </>
          )}
        </Card>

        <Card className="p-6 space-y-5">
          <div>
            <p className="text-xl font-semibold">Agency Plan</p>
            <p className="text-sm text-muted-foreground mt-1">
              Recurring plan for teams running Deep Match regularly.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant={billingCycle === 'monthly' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setBillingCycle('monthly')}
            >
              Monthly
            </Button>
            <Button
              variant={billingCycle === 'annual' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setBillingCycle('annual')}
            >
              Annual
            </Button>
          </div>

          <div className="space-y-2 border border-border p-4">
            <p className="text-3xl font-semibold">{agencyPriceCopy}</p>
            <p className="text-sm text-muted-foreground">Includes 50,000 Deep Match pages / month</p>
            <p className="text-sm text-muted-foreground">Unlimited Quick Match and priority support</p>
            <p className="text-sm text-muted-foreground">Overage billed at $0.015 per page</p>
          </div>

          <Button onClick={() => agencyCheckout.mutate()} disabled={agencyCheckout.isPending}>
            {agencyCheckout.isPending && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
            Start Agency Checkout
          </Button>

          {status?.agency?.has_subscription && (
            <p className="text-sm text-muted-foreground">
              Current period usage: {status.agency.usage_pages.toLocaleString()} pages
            </p>
          )}
        </Card>
      </div>
    </Layout>
  );
}
