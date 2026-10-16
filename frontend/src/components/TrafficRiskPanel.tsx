import { AlertTriangle, ArrowRight, ShieldCheck } from 'lucide-react';
import { Button } from './ui/button';
import type { TrafficRisk } from '../lib/riskSummary';

interface TrafficRiskPanelProps {
  risk: TrafficRisk;
  /**
   * 'screen' gives the number its own beat before matching runs; 'inline'
   * renders the same content compactly inside the review page. Both use this
   * component so the placement can change without a rewrite.
   */
  variant?: 'screen' | 'inline';
  onContinue?: () => void;
  continueLabel?: string;
  isBusy?: boolean;
}

function pathOf(url: string): string {
  try {
    return new URL(url).pathname || '/';
  } catch {
    return url;
  }
}

export function TrafficRiskPanel({
  risk,
  variant = 'screen',
  onContinue,
  continueLabel = 'Map these to the new site',
  isBusy = false,
}: TrafficRiskPanelProps) {
  const compact = variant === 'inline';

  // Without Search Console we know what exists but not what matters. Say so
  // rather than presenting a page count as if it were a finding.
  if (!risk.hasGsc) {
    return (
      <div className="border border-border bg-card p-5">
        <div className="flex items-start gap-3">
          <AlertTriangle className="h-5 w-5 text-muted-foreground flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-foreground">
              Found {risk.total.toLocaleString()} pages on {risk.domain}
            </p>
            <p className="text-sm text-muted-foreground mt-1">
              Search traffic has not been measured. Connect Search Console to prioritize
              redirects using recorded clicks and impressions.
            </p>
          </div>
        </div>
        {onContinue && (
          <div className="mt-4">
            <Button onClick={onContinue} disabled={isBusy} variant="outline">
              Continue without it
            </Button>
          </div>
        )}
      </div>
    );
  }

  const metric = risk.weightMetric === 'clicks' ? 'clicks' : 'impressions';
  const totalWeight = risk.weightMetric === 'clicks' ? risk.totalClicks : risk.totalImpressions;
  const concentration = risk.total > 0 ? Math.round((risk.topUrlCount / risk.total) * 100) : 0;

  return (
    <div className={compact ? 'border border-border bg-card p-4' : ''}>
      <div className="flex items-start gap-3">
        <ShieldCheck className="h-5 w-5 text-primary flex-shrink-0 mt-1" />
        <div className="flex-1">
          <p className={compact ? 'text-base font-medium text-foreground' : 'text-xl font-semibold text-foreground'}>
            {risk.topUrlCount.toLocaleString()} of {risk.total.toLocaleString()} pages carry{' '}
            at least {risk.shareTarget}% of recorded organic {metric}
          </p>
          <p className="text-sm text-muted-foreground mt-1">
            That&rsquo;s {concentration}% of the pages on {risk.domain}. Prioritize checking their
            destinations before and after the move.
          </p>

          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground tabular-nums">
            <span>
              <strong className="text-foreground">{totalWeight.toLocaleString()}</strong> {metric} / 90 days
            </span>
            <span>
              <strong className="text-foreground">{risk.withTraffic.toLocaleString()}</strong> pages with traffic
            </span>
            {risk.noRecordedTraffic > 0 && (
              <span>
                <strong className="text-foreground">{risk.noRecordedTraffic.toLocaleString()}</strong> with none
              </span>
            )}
          </div>

          {!compact && risk.topUrls.length > 0 && (
            <div className="mt-5 border-t border-border pt-4">
              <p className="text-xs uppercase tracking-wide text-muted-foreground mb-2">
                Review first
              </p>
              <ul className="space-y-1">
                {risk.topUrls.map((u) => (
                  <li key={u.url} className="flex items-baseline gap-3 text-sm">
                    <span className="tabular-nums text-foreground w-16 text-right flex-shrink-0">
                      {(risk.weightMetric === 'clicks' ? u.clicks : u.impressions).toLocaleString()}
                    </span>
                    <span className="text-muted-foreground text-xs w-20 flex-shrink-0">{metric}</span>
                    <span className="font-mono text-xs text-muted-foreground truncate">
                      {pathOf(u.url)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {onContinue && (
            <div className="mt-5">
              <Button onClick={onContinue} disabled={isBusy} size={compact ? 'default' : 'lg'}>
                {continueLabel}
                <ArrowRight className="h-4 w-4 ml-2" />
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
