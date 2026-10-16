import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { usePostHog } from '@posthog/react';
import { BarChart3, CheckCircle2, Globe, Loader2 } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { discoverSite, type DiscoveryResponse } from '../api/discovery';
import {
  formatProperty,
  getGscConnectUrl,
  getGscProperties,
  getGscStatus,
  propertyCovers,
} from '../api/gsc';
import { monitoringKeys } from '../queries/monitoringKeys';
import { useAuth } from '../contexts/AuthContext';

const METHOD_LABELS: Record<string, string> = {
  gsc: 'Search Console',
  sitemap: 'sitemap',
  wordpress_api: 'WordPress API',
  shopify_api: 'Shopify API',
  crawl: 'site crawl',
};

const GENERATOR_LABELS: Record<string, string> = {
  wordpress: 'WordPress',
  shopify: 'Shopify',
  webflow: 'Webflow',
  squarespace: 'Squarespace',
  wix: 'Wix',
};

interface DomainDiscoveryPanelProps {
  side: 'old' | 'new';
  label: string;
  /** What this side actually does — the two are not interchangeable. */
  hint?: string;
  /** The old side carries the traffic worth protecting, so it leads. */
  emphasis?: boolean;
  onDiscovered: (side: 'old' | 'new', result: DiscoveryResponse | null) => void;
}

/**
 * One half of the paste-two-domains ingestion flow: a domain input that
 * discovers the site's pages (sitemap -> CMS API -> crawl) and reports the
 * result upward as if a URL file had been uploaded.
 */
export function DomainDiscoveryPanel(props: DomainDiscoveryPanelProps) {
  const { user } = useAuth();
  if (!user) return null;
  return <DomainDiscoveryContent key={`${user.id}:${props.side}`} {...props} owner={user.id} />;
}

function DomainDiscoveryContent({ side, label, hint, emphasis = false, onDiscovered, owner }: DomainDiscoveryPanelProps & { owner: string }) {
  const posthog = usePostHog();
  const [domain, setDomain] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DiscoveryResponse | null>(null);
  // undefined = let auto-detection decide; null = the user explicitly chose to
  // run without Search Console. Collapsing those two into null would make
  // opting out silently fall back to the auto-detected property.
  const [chosenProperty, setChosenProperty] = useState<string | null | undefined>(undefined);
  const [pickingProperty, setPickingProperty] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const revision = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      revision.current += 1;
      pending.current?.abort();
    };
  }, []);

  // Only the old side has traffic to lose, so it is the only side where
  // Search Console changes the answer.
  const isTrafficSide = side === 'old';

  const statusQuery = useQuery({
    queryKey: monitoringKeys.gscStatus(owner),
    queryFn: ({ signal }) => getGscStatus(signal),
    enabled: isTrafficSide,
  });
  const connected = !!statusQuery.data?.connected;
  // `configured` false means the server has no OAuth client — offering to
  // connect would dead-end.
  const gscAvailable = statusQuery.data?.configured !== false;

  const propertiesQuery = useQuery({
    queryKey: monitoringKeys.gscProperties(owner),
    queryFn: ({ signal }) => getGscProperties(signal),
    enabled: isTrafficSide && connected,
    staleTime: 5 * 60 * 1000,
  });
  const properties = propertiesQuery.data ?? [];

  /**
   * The property we would use for what has been typed so far.
   *
   * Mirrors the server's preference for a domain property, which covers www
   * and bare alike, so the UI never promises one property and the API uses
   * another.
   */
  const matchedProperty = useMemo(() => {
    if (!domain.trim() || properties.length === 0) return null;
    const covering = properties
      .map((p) => p.site_url)
      .filter((p) => propertyCovers(p, domain));
    if (covering.length === 0) return null;
    covering.sort((a, b) => (a.startsWith('sc-domain:') ? 0 : 1) - (b.startsWith('sc-domain:') ? 0 : 1));
    return covering[0];
  }, [domain, properties]);

  const effectiveProperty = chosenProperty === undefined ? matchedProperty : chosenProperty;

  const handleConnect = async () => {
    setConnecting(true);
    posthog?.capture('gsc_connect_started', { source: 'ingestion_old_side' });
    try {
      // BrowserRouter with no basename, so this is the router's path too —
      // and it keeps this panel usable outside a Router.
      const url = await getGscConnectUrl(window.location.pathname);
      if (!mounted.current) return;
      window.location.href = url;
    } catch (err) {
      if (!mounted.current) return;
      setError(err instanceof Error ? err.message : 'Could not start Search Console sign-in.');
      setConnecting(false);
    }
  };

  const handleDiscover = async () => {
    const trimmed = domain.trim();
    if (!trimmed || pending.current) return;
    const requestRevision = ++revision.current;
    const controller = new AbortController();
    pending.current = controller;

    setIsLoading(true);
    setError(null);
    posthog?.capture('domain_discovery_started', {
      side,
      domain: trimmed,
      gsc_connected: connected,
      gsc_property: effectiveProperty ?? undefined,
    });

    try {
      const response = await discoverSite(trimmed, side,
        chosenProperty === null ? null : effectiveProperty ?? undefined, controller.signal);
      if (requestRevision !== revision.current) return;
      setResult(response);
      onDiscovered(side, response);
      posthog?.capture('domain_discovery_completed', {
        side,
        domain: trimmed,
        count: response.count,
        method: response.method,
        generator: response.generator,
        truncated: response.truncated,
        duration_ms: response.duration_ms,
      });
    } catch (err) {
      if (requestRevision !== revision.current || controller.signal.aborted) return;
      const message = err instanceof Error ? err.message : 'Could not scan this domain.';
      setError(message);
      setResult(null);
      onDiscovered(side, null);
      posthog?.capture('domain_discovery_failed', { side, domain: trimmed, error: message });
    } finally {
      if (requestRevision === revision.current) {
        pending.current = null;
        setIsLoading(false);
      }
    }
  };

  const handleClear = () => {
    revision.current += 1;
    pending.current?.abort();
    pending.current = null;
    setIsLoading(false);
    setResult(null);
    setError(null);
    onDiscovered(side, null);
  };

  if (result) {
    const methodLabel = METHOD_LABELS[result.method] || result.method;
    const generatorLabel = result.generator ? GENERATOR_LABELS[result.generator] : null;
    return (
      <div className="border border-emerald-500/40 bg-emerald-500/5 p-5 rounded-lg">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="h-5 w-5 text-emerald-600 flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-foreground">
                {new URL(result.root_url).hostname}
                {generatorLabel && (
                  <span className="ml-2 text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded">
                    {generatorLabel}
                  </span>
                )}
              </p>
              <p className="text-sm text-muted-foreground mt-1">
                {result.count.toLocaleString()} pages found via {methodLabel}
              </p>
              {result.summary && result.summary.with_traffic > 0 && (
                <p className="text-xs text-muted-foreground mt-1">
                  {result.summary.with_traffic.toLocaleString()} carry organic traffic
                  {result.summary.no_recorded_traffic > 0 && (
                    <> · {result.summary.no_recorded_traffic.toLocaleString()} with no recorded traffic</>
                  )}
                </p>
              )}
              {result.gsc_url_count === 0 && side === 'old' && (
                <p className="text-xs text-muted-foreground mt-1">
                  We can&rsquo;t tell which of these matter — connect Search Console to see
                  which pages carry your traffic.
                </p>
              )}
              {result.truncated && (
                <p className="text-xs text-muted-foreground mt-1">
                  Site has {result.total_found.toLocaleString()}+ pages — capped at{' '}
                  {result.max_urls.toLocaleString()} on your plan.
                </p>
              )}
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={handleClear}>
            Change
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="border border-dashed border-border bg-background p-5 rounded-lg">
      <div className="flex items-center gap-2 mb-1">
        <Globe className={`h-4 w-4 ${emphasis ? 'text-primary' : 'text-muted-foreground'}`} />
        <p className="text-sm font-medium text-foreground">{label}</p>
      </div>
      {hint && <p className="text-xs text-muted-foreground mb-3">{hint}</p>}
      <div className="flex flex-wrap gap-2">
        <Input
          type="text"
          placeholder={side === 'old' ? 'old-site.com' : 'new-site.com'}
          value={domain}
          onChange={(e) => { setDomain(e.target.value); setChosenProperty(undefined); }}
          className="min-w-0 flex-1 basis-40 text-base"
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              handleDiscover();
            }
          }}
          disabled={isLoading}
          aria-label={`${label} domain`}
        />
        <Button onClick={handleDiscover} disabled={!domain.trim() || isLoading}>
          {isLoading ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Scanning...
            </>
          ) : (
            'Find Pages'
          )}
        </Button>
        {isLoading && <Button type="button" variant="outline" onClick={handleClear}>Cancel scan</Button>}
      </div>
      {isLoading && (
        <p role="status" className="text-sm text-muted-foreground mt-2">
          Checking sitemaps and other available page sources. Large sites may take longer.
        </p>
      )}
      {error && <p role="alert" className="text-sm text-destructive mt-2">{error}</p>}

      {isTrafficSide && gscAvailable && !connected && !statusQuery.isLoading && (
        <div className="mt-3 pt-3 border-t border-border/60">
          <Button
            variant="outline"
            size="sm"
            onClick={handleConnect}
            disabled={connecting}
            className="w-full sm:w-auto"
          >
            {connecting ? (
              <Loader2 className="h-3.5 w-3.5 mr-2 animate-spin" />
            ) : (
              <BarChart3 className="h-3.5 w-3.5 mr-2" />
            )}
            Import the pages that get traffic
          </Button>
          <p className="text-xs text-muted-foreground mt-2">
            Read-only. We read which pages earn clicks so we can tell you what a bad
            redirect would cost — we never change anything on your site.
          </p>
        </div>
      )}

      {isTrafficSide && connected && (
        <div className="mt-3 pt-3 border-t border-border/60">
          {!domain.trim() ? (
            <p className="text-xs text-muted-foreground flex items-center gap-1.5">
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
              Search Console connected — we&rsquo;ll weight these pages by real traffic.
            </p>
          ) : propertiesQuery.isLoading ? (
            <p className="text-xs text-muted-foreground">Checking your properties…</p>
          ) : effectiveProperty ? (
            <div className="flex items-start justify-between gap-2">
              <p className="text-xs text-muted-foreground flex items-start gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600 flex-shrink-0 mt-px" />
                <span>
                  Traffic from{' '}
                  <span className="text-foreground">{formatProperty(effectiveProperty)}</span>
                </span>
              </p>
              {properties.length > 0 && (
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground flex-shrink-0"
                  onClick={() => setPickingProperty((v) => !v)}
                >
                  {pickingProperty ? 'Done' : 'Change'}
                </button>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              {chosenProperty === null ? 'Search Console is off for this scan.' : <>No Search Console property covers this domain
              {properties.length > 0 && ' — pick one below if it should'}.</>}
            </p>
          )}

          {(pickingProperty || (!!domain.trim() && !effectiveProperty && properties.length > 0)) && (
            <select
              className="mt-2 w-full text-base sm:text-xs bg-background border border-border rounded px-2 py-1.5 text-foreground"
              value={effectiveProperty ?? ''}
              onChange={(e) => {
                const value = e.target.value || null;
                setChosenProperty(value);
                posthog?.capture('gsc_property_overridden', { property: value ?? undefined });
              }}
              aria-label="Search Console property"
              disabled={isLoading}
            >
              <option value="">Don&rsquo;t use Search Console</option>
              {properties.map((p) => (
                <option key={p.site_url} value={p.site_url}>
                  {formatProperty(p.site_url)}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
    </div>
  );
}
