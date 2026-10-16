import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { billingRedirectUrl, createCurrentCheckout, createCurrentPortal, getCurrentPricing, getCurrentQuote, getCurrentUsage,
  type CurrentPricing, type CurrentQuote, type CurrentSku, type CurrentUsage } from '../api/currentBilling';
import { ToolLayout } from './ToolLayout';
import { Button } from './ui/button';
import { useRequestScope } from './pivot/useRequestScope';

const money = (cents: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(cents / 100);
export function CurrentBillingPage() {
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const migration = search.get('migration_id') || '';
  const old = search.get('old_inventory_id') || '', next = search.get('new_inventory_id') || '';
  const scope = useRequestScope(`billing:${migration}:${old}:${next}`);
  const [pricing, setPricing] = useState<CurrentPricing | null>(null);
  const [usage, setUsage] = useState<CurrentUsage | null>(null);
  const [quote, setQuote] = useState<CurrentQuote | null>(null);
  const [consent, setConsent] = useState<CurrentSku | null>(null);
  const [error, setError] = useState('');
  const [usageError, setUsageError] = useState('');
  const [quoteError, setQuoteError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(), signal = controller.signal;
    setLoading(true); setError(''); setPricing(null); setQuote(null); setQuoteError(''); setUsageError(''); setUsage(null);
    void getCurrentPricing(signal).then(async result => {
      if (signal.aborted) return;
      setPricing(result);
      if (!result.enabled) return;
      const tasks = [getCurrentUsage(signal).then(value => { if (!signal.aborted) setUsage(value); })
        .catch(() => { if (!signal.aborted) setUsageError('Usage could not load. Refresh billing to retry.'); })];
      if (migration && old && next) tasks.push(getCurrentQuote(migration, old, next, signal).then(value => {
        if (!Number.isSafeInteger(value.old_urls) || value.old_urls < 1 || !Number.isSafeInteger(value.amount_cents) || value.amount_cents < 1000)
          throw new Error('The migration price could not be verified.');
        if (!signal.aborted) setQuote(value);
      }).catch(e => { if (!signal.aborted) setQuoteError(e instanceof Error ? e.message : 'The migration price could not load.'); }));
      await Promise.all(tasks);
    }).catch(e => { if (!signal.aborted) setError(e instanceof Error ? e.message : 'Billing could not load.'); })
      .finally(() => { if (!signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [migration, old, next, revision]);
  async function purchase(sku: CurrentSku) {
    if (lock.current || !pricing?.enabled || (sku === 'payg' ? !quote : consent !== sku)) return;
    lock.current = true; setBusy(true); setError('');
    const signal = scope.signal();
    try {
      const result = await createCurrentCheckout({ sku, recurring_consent: sku !== 'payg',
        ...(sku === 'payg' ? { migration_id: migration, inventory_ids: { old, new: next } } : {}),
        idempotency_key: scope.key(`checkout:${sku}`) }, signal);
      if (signal.aborted) return;
      if (result.status === 'open' && result.checkout_url) window.location.assign(billingRedirectUrl(result.checkout_url));
      else navigate(`/billing/current/return?checkout_id=${encodeURIComponent(result.checkout_id)}`);
    } catch (e) { if (!signal.aborted) setError(e instanceof Error ? e.message : 'Checkout could not start. Try again.'); }
    finally { lock.current = false; if (!signal.aborted) setBusy(false); }
  }
  async function portal() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    const signal = scope.signal();
    try { const url = await createCurrentPortal(signal); if (!signal.aborted) window.location.assign(url); }
    catch (e) { if (!signal.aborted) setError(e instanceof Error ? e.message : 'Billing portal could not open.'); }
    finally { lock.current = false; if (!signal.aborted) setBusy(false); }
  }
  return <ToolLayout title="Plans and billing"><main className="mx-auto max-w-4xl space-y-6 py-4">
    <header className="space-y-3">
      <Link to={migration ? `/migrations/${encodeURIComponent(migration)}` : '/companion'} className="text-sm underline">{migration ? 'Back to migration' : 'All migrations'}</Link>
      <h1 className="text-3xl font-semibold">Plans and billing</h1>
      <p className="text-sm text-muted-foreground">Free trial: 500 old URLs. Paid plans are separate from the trial.</p>
    </header>
    {loading && <p role="status">Loading billing…</p>}
    {error && <p role="alert">{error}</p>}
    {pricing && !pricing.enabled && <p role="status">Paid checkout is not available yet.</p>}
    {pricing && <section aria-label="Plans" className="divide-y border-y">
      <div className="space-y-3 py-4"><h2 className="text-lg font-semibold">Pay as you go</h2>
        <p className="text-sm">$1 per 50 old URLs, rounded up. $10 minimum per migration.</p>
        {quote && <p className="font-medium">{quote.old_urls.toLocaleString()} old URLs · {money(quote.amount_cents)} once</p>}
        {quoteError && <p role="alert">{quoteError}</p>}
        {pricing.enabled && (!migration || !old || !next) && <p className="text-sm text-muted-foreground">Open billing from a migration with both URL inventories selected. Your connected agent can provide that link.</p>}
        {pricing.enabled && quote && <Button disabled={busy || loading} onClick={() => void purchase('payg')}>Pay {money(quote.amount_cents)} once</Button>}
      </div>
      {pricing.plans.filter(plan => ['studio', 'agency'].includes(plan.sku)).map(plan => <div key={plan.sku} className="space-y-3 py-4">
        <h2 className="text-lg font-semibold">{plan.name}</h2>
        <p>{money(plan.monthly_amount_cents ?? 0)} / month · {plan.included_old_urls.toLocaleString()} old URLs included</p>
        {pricing.enabled && <>
          <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={consent === plan.sku} disabled={busy}
            onChange={e => setConsent(e.target.checked ? plan.sku : null)} />
            I agree to {plan.name} at {money(plan.monthly_amount_cents ?? 0)} per month until canceled.</label>
          <Button disabled={busy || loading || consent !== plan.sku} onClick={() => void purchase(plan.sku)}>Continue with {plan.name}</Button>
        </>}
      </div>)}
    </section>}
    {pricing && <p className="text-sm text-muted-foreground">Subscription extras cost $1 per 50 old URLs, rounded once across the monthly total. Extra URLs require approval before a run. Starting a subscription does not approve extra charges.</p>}
    {usageError && <p role="alert">{usageError}</p>}
    {usage && <section className="space-y-3" aria-labelledby="usage-heading"><h2 id="usage-heading" className="text-lg font-semibold">Current usage</h2>
      {usage.sku && usage.sku !== 'free' ? <><p className="text-sm">{usage.used_old_urls.toLocaleString()} of {usage.included_old_urls.toLocaleString()} included old URLs used.</p>
        <p className="text-sm">{usage.extra_old_urls.toLocaleString()} extra old URLs · {money(usage.extra_amount_cents)}</p>
        {usage.period_start && usage.period_end && <p className="text-sm text-muted-foreground">Period: {usage.period_start.slice(0, 10)} – {usage.period_end.slice(0, 10)}</p>}
      </> : <p className="text-sm">No current subscription.</p>}
      {usage.portal_available && <Button variant="outline" disabled={busy} onClick={() => void portal()}>Manage subscription</Button>}
    </section>}
    <Button variant="outline" disabled={busy || loading} onClick={() => setRevision(n => n + 1)}>Refresh billing</Button>
  </main></ToolLayout>;
}
