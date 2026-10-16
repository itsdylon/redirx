import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { getCurrentCheckout, type CurrentCheckout } from '../api/currentBilling';
import { ToolLayout } from './ToolLayout';
import { Button } from './ui/button';

export function CurrentBillingReturn() {
  const [search] = useSearchParams();
  const id = search.get('checkout_id') || '';
  const [checkout, setCheckout] = useState<CurrentCheckout | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setCheckout(null); setError('');
    if (!id) { setError('The checkout link is missing its ID. Open billing to continue.'); return; }
    let attempts = 0;
    async function verify() {
      setBusy(true);
      try {
        const result = await getCurrentCheckout(id, controller.signal);
        if (controller.signal.aborted) return;
        setCheckout(result);
        if (['open', 'reserved'].includes(result.status) && ++attempts < 6) timer = setTimeout(() => void verify(), 3000);
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Payment could not be verified. Try again.'); }
      finally { if (!controller.signal.aborted) setBusy(false); }
    }
    void verify();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [id, revision]);
  return <ToolLayout title="Checkout status"><main className="mx-auto max-w-4xl space-y-6 py-4">
    <h1 className="text-3xl font-semibold">Checkout status</h1>
    {busy && <p role="status">Checking payment with the server…</p>}
    {error && <p role="alert">{error}</p>}
    {!error && checkout?.status === 'paid' && <p role="status">Payment confirmed. Return to your migration to continue.</p>}
    {!error && checkout && ['open', 'reserved'].includes(checkout.status) && <p role="status">Payment has not been confirmed yet. If you completed checkout, check again shortly.</p>}
    {!error && checkout?.status === 'expired' && <p role="status">This checkout expired. Open billing to start again.</p>}
    {!error && checkout?.status === 'revoked' && <p role="status">This checkout was revoked. Open billing to review your account.</p>}
    {id && <Button variant="outline" disabled={busy} onClick={() => setRevision(n => n + 1)}>Check payment again</Button>}
    <div className="flex flex-wrap gap-4 text-sm">{checkout?.migration_id && <Link to={`/migrations/${encodeURIComponent(checkout.migration_id)}`} className="underline">Return to migration</Link>}<Link to="/billing/current" className="underline">Plans and billing</Link><Link to="/companion" className="underline">All migrations</Link></div>
  </main></ToolLayout>;
}
