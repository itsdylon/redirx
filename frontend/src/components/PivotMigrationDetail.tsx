import { useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { checkout, downloadArtifact, getPivotMigration, listPivotMatches, monitoring, resolvePivotMatch, refinePivotMatches,
  type MigrationDetail, type MonitoringData, type MonitoringIssue, type PivotEnvelope, type PivotMatch, type PivotMatchFilter } from '../api/pivot';
import { FrontendEvent, safeCapture } from '../lib/analyticsEvents';
import { ToolLayout } from './ToolLayout';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { SubscriptionCheckoutPanel } from './SubscriptionCheckoutPanel';
import { PivotBillingNotice } from './PivotBillingNotice';
import { SearchConsolePanel } from './pivot/SearchConsolePanel';
import { useRequestScope } from './pivot/useRequestScope';

function MonitoringPanel({ migrationId, monitoringId }: { migrationId: string; monitoringId?: string | null }) {
  const scope = useRequestScope(`monitor:${migrationId}:${monitoringId || ''}`);
  const [data, setData] = useState<MonitoringData | null>(null);
  const [issues, setIssues] = useState<MonitoringIssue[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!monitoringId) return;
    const controller = new AbortController();
    setBusy(true); setError('');
    void Promise.all([monitoring(migrationId, false, monitoringId, -1, controller.signal),
      monitoring(migrationId, true, monitoringId, -1, controller.signal)]).then(([status, fixes]) => {
      if (controller.signal.aborted) return;
      setData(status.data); setIssues(fixes.data.items || []); setNext(fixes.data.next_cursor ?? null);
    }).catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Monitoring could not load. Refresh to retry.'); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [migrationId, monitoringId, refresh]);
  async function more() {
    if (next === null) return;
    const signal = scope.signal();
    setBusy(true);
    try {
      const result = await monitoring(migrationId, true, monitoringId, next, signal);
      if (!signal.aborted) { setIssues(current => [...current, ...(result.data.items || [])]); setNext(result.data.next_cursor ?? null); }
    } catch (e) { if (!signal.aborted) setError(e instanceof Error ? e.message : 'Corrections could not load. Try again.'); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  return <section className="space-y-3 border-t pt-6" aria-labelledby="monitoring-heading">
    <h2 id="monitoring-heading" className="text-lg font-semibold">Monitoring</h2>
    {!monitoringId ? <p className="text-sm text-muted-foreground">Monitoring has not started for this migration. Ask your agent to confirm installation and check eligibility.</p> : <>
      <Button variant="outline" disabled={busy} onClick={() => setRefresh(n => n + 1)}>Refresh monitoring</Button>
      {busy && <p role="status">Loading monitoring…</p>}
      {error && <p role="alert">{error}</p>}
      {data && <><p className="text-sm">Monitoring state: {data.state}</p><p className="text-sm">{data.summary}</p>
        <p className="text-sm">Coverage: {data.coverage?.checked ?? 0} of {data.coverage?.total ?? 'unknown'} URLs checked. {data.coverage?.unchecked ?? 'Unknown'} unchecked.</p>
      </>}
      <ul className="divide-y">{issues.map(issue => <li className="space-y-1 py-3 text-sm" key={issue.issue_id}>
        <p className="break-all font-medium">{issue.source_url}</p>
        <p>{(issue.issue || issue.evidence?.issue || 'Needs review').replaceAll('_', ' ')}</p>
        {issue.expected_url && <p className="break-all">Expected destination: {issue.expected_url}</p>}
        <p>{issue.suggested_action === 'retry_check' ? 'Retry the check when the origin is available.' : 'Restore the saved artifact rule, deploy it, then verify again.'}</p>
      </li>)}</ul>
      {data && !busy && !error && !issues.length && <p className="text-sm">No recorded correction items. Coverage above shows what has actually been checked.</p>}
      {next !== null && <Button variant="outline" disabled={busy} onClick={() => void more()}>Load more corrections</Button>}
    </>}
  </section>;
}

export function PivotMigrationDetail() {
  const { migrationId = '' } = useParams();
  const [search] = useSearchParams();
  const posthog = usePostHog();
  const scope = useRequestScope(migrationId);
  const [summary, setSummary] = useState<PivotEnvelope<MigrationDetail> | null>(null);
  const [rows, setRows] = useState<PivotMatch[]>([]);
  const [matchFilter, setMatchFilter] = useState<PivotMatchFilter>('all');
  const [pageLoading, setPageLoading] = useState(false);
  const [next, setNext] = useState<string | null>(null);
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [matchesLoaded, setMatchesLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const mappingScope = useRequestScope(`${migrationId}:${summary?.data.run_id || ''}:${matchFilter}:${refresh}`);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true); setMessage(''); setMatchesLoaded(false); setPageLoading(false);
    setRows([]); setNext(null);
    void (async () => {
      try {
        const result = await getPivotMigration(migrationId, controller.signal);
        if (controller.signal.aborted) return;
        setSummary(result);
        if (result.data.run_id && !['queued', 'running'].includes(result.data.run?.status || '')) {
          const matches = await listPivotMatches(migrationId, result.data.run_id, undefined, controller.signal, matchFilter);
          if (controller.signal.aborted) return;
          setRows(matches.data.items); setNext(matches.data.next_cursor || null); setMatchesLoaded(true);
        } else { setRows([]); setNext(null); }
        if (result.next_action === 'poll') timer = setTimeout(() => setRefresh(n => n + 1),
          Math.min(30, Math.max(3, result.retry_after_seconds || 10)) * 1000);
      } catch (e) { if (!controller.signal.aborted) setMessage(e instanceof Error ? e.message : 'Migration could not load. Refresh to retry.'); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    })();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [migrationId, refresh, matchFilter]);
  async function perform(action: (signal: AbortSignal) => Promise<void>) {
    const signal = scope.signal();
    setBusy(true); setMessage('');
    try { await action(signal); }
    catch (e) { if (!signal.aborted) setMessage(e instanceof Error ? e.message : 'The action could not finish. Try again.'); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function decide(row: PivotMatch, action: string, confirmedTarget?: string) {
    // Fired on the user's initiated click, before the request resolves. The
    // backend's own mapping_decisions_applied event (migration_mapping_routes.py)
    // is the one that says the decision was actually persisted — this one says
    // a person chose to try.
    safeCapture(posthog, FrontendEvent.PIVOT_REVIEW_DECISION_INITIATED, {
      migration_id: migrationId, run_id: summary?.data.run_id, action,
    });
    await perform(async signal => {
      const decision = { mapping_id: row.mapping_id, expected_revision: row.revision, action,
        ...(action === 'set_target' ? { target_url: confirmedTarget ?? targets[row.mapping_id] } : {}) };
      const result = await resolvePivotMatch(migrationId, summary!.data.run_id!, decision,
        scope.key(JSON.stringify(decision)), signal);
      if (signal.aborted) return;
      const outcome = result.data.outcomes?.[0];
      if (!outcome || outcome.code !== 'ok') throw new Error(outcome?.message ||
        `Decision was not saved (${outcome?.code || 'missing result'}). Refresh the mappings and retry.`);
      setRefresh(n => n + 1);
    });
  }
  async function refine() {
    const run = summary?.data.run_id, jev = summary?.data.jev;
    if (!run || !jev) return;
    safeCapture(posthog, FrontendEvent.PIVOT_REFINE_INITIATED, {
      migration_id: migrationId, run_id: run, pass: jev.pass,
    });
    await perform(async signal => {
      await refinePivotMatches(migrationId, run, jev.seed_revision,
        scope.key(`refine:${run}:${jev.pass}:${jev.seed_revision}`), signal);
      if (!signal.aborted) setRefresh(n => n + 1);
    });
  }
  async function more() {
    if (!next || !summary?.data.run_id || pageLoading) return;
    const signal = mappingScope.signal();
    setPageLoading(true); setMessage('');
    try {
      const result = await listPivotMatches(migrationId, summary.data.run_id, next, signal, matchFilter);
      if (!signal.aborted) { setRows(current => [...current, ...result.data.items]); setNext(result.data.next_cursor || null); }
    } catch (e) {
      if (!signal.aborted) setMessage(e instanceof Error ? e.message : 'More mappings could not load. Try again.');
    } finally { if (!signal.aborted) setPageLoading(false); }
  }
  async function pay() {
    await perform(async signal => {
      const operationId = summary?.operation_id;
      if (!operationId) throw new Error('Payment details are incomplete. Refresh the migration before trying checkout again.');
      const result = await checkout(migrationId, summary!.data.quote_id!, operationId,
        scope.key(`checkout:${summary!.data.quote_id}:${operationId}`), signal);
      if (signal.aborted) return;
      if (result.data.checkout_url) window.location.assign(result.data.checkout_url);
      else { setMessage(`Checkout status: ${result.data.state || result.status}. Refresh to check payment.`); }
    });
  }
  async function download() {
    safeCapture(posthog, FrontendEvent.PIVOT_EXPORT_INITIATED, {
      migration_id: migrationId, artifact_id: summary?.data.artifact?.artifact_id,
    });
    await perform(async signal => {
      const artifact = summary!.data.artifact!;
      const result = await downloadArtifact(migrationId, artifact.artifact_id, signal);
      if (signal.aborted) return;
      const url = URL.createObjectURL(new Blob([result.data.content], { type: 'text/plain;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url; link.download = `redirects-${artifact.artifact_id}.${artifact.format === 'json' ? 'json' : artifact.format === 'csv' ? 'csv' : 'txt'}`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  const data = summary?.data;
  const isJev = data?.jev?.engine === 'jev-url-v1';
  const paymentRequired = !isJev && (summary?.status === 'payment_required' || summary?.next_action === 'complete_payment');
  const monitorId = search.get('monitoring_id') || data?.monitoring_id;
  const historicalServices = !isJev && !!(data?.quote_id || monitorId || data?.deployment_id);
  return <ToolLayout title="Migration detail"><main className="mx-auto max-w-4xl space-y-8 py-4">
    <header className="space-y-3">
      <Link to="/companion" className="text-sm underline">All migrations</Link>
      <Link to={`/billing/current?${new URLSearchParams({ migration_id: migrationId, ...(data?.run?.inventory_ids ? { old_inventory_id: data.run.inventory_ids.old, new_inventory_id: data.run.inventory_ids.new } : {}) })}`} className="block text-sm underline">Plans and billing for this migration</Link>
      <h1 className="break-words text-3xl font-semibold">{data?.migration?.name || 'Migration detail'}</h1>
      <p className="break-all text-sm text-muted-foreground">{migrationId}</p>
      <Button variant="outline" disabled={busy || loading} onClick={() => setRefresh(n => n + 1)}>Refresh status</Button>
      {loading && <p role="status">Loading migration status…</p>}
      {message && <p role="alert">{message}</p>}
      {summary && <><p className="font-medium">Status: {summary.status.replaceAll('_', ' ')}</p><p>{data?.summary}</p></>}
    </header>
    {paymentRequired && data?.quote_id && <section className="space-y-3 border-t pt-6" aria-labelledby="payment-heading">
      <h2 id="payment-heading" className="text-lg font-semibold">Payment required</h2>
      <PivotBillingNotice />
      {data.quote && <p>{new Intl.NumberFormat('en-US', { style: 'currency', currency: data.quote.currency }).format(data.quote.amount_cents / 100)} for this migration.</p>}
      <p className="text-sm text-muted-foreground">Payment is confirmed by the server. Returning from checkout does not start work by itself.</p>
      {!summary?.operation_id && <p role="alert">Payment details are incomplete. Refresh the migration before trying checkout again.</p>}
      <Button disabled={busy || loading || !summary?.operation_id} onClick={() => void pay()}>Continue to checkout</Button>
    </section>}
    {isJev && data?.jev && <section className="space-y-3 border-t pt-6" aria-labelledby="jev-heading">
      <h2 id="jev-heading" className="text-lg font-semibold">Free URL mapping</h2>
      <p className="text-sm">Pass {data.jev.pass} of {data.jev.limits.passes}. Confirmed-example revision {data.jev.seed_revision}. Model confidence is an estimate, not a verified match.</p>
      <p className="text-sm text-muted-foreground">Confirm known destinations to guide another pass over unresolved pages. Existing confirmed decisions remain saved. URL text is matched without fetching page content.</p>
      <Button variant="outline" disabled={busy || loading || ['queued', 'running'].includes(data.run?.status || '') ||
        (data.jev.pass >= data.jev.limits.passes && data.run?.status !== 'failed')}
        onClick={() => void refine()}>{data.run?.status === 'failed' ? 'Resume matching pass' : 'Refine with confirmed examples'}</Button>
      {data.jev.pass >= data.jev.limits.passes && data.run?.status !== 'failed' && <p className="text-sm">All three passes are used. Continue reviewing and exporting this run.</p>}
    </section>}
    {data?.run_id && <section className="space-y-3 border-t pt-6" aria-labelledby="mappings-heading">
      <h2 id="mappings-heading" className="text-lg font-semibold">Mapping review</h2>
      <p className="text-sm text-muted-foreground">Approve only a relevant destination. Leave uncertain decisions for review with your agent.</p>
      <label className="block space-y-2 text-sm">Show mappings
        <select className="block rounded-md border border-input bg-background px-3 py-2" value={matchFilter}
          disabled={busy || loading} onChange={event => setMatchFilter(event.target.value as PivotMatchFilter)}>
          <option value="all">All mappings</option>
          <option value="needs_review">Needs review or deferred</option>
          <option value="unmatched">Unmatched</option>
          <option value="rejected">Rejected or intentionally removed</option>
          <option value="approved">Approved</option>
        </select>
      </label>
      {data.run?.status !== 'succeeded' && <p className="text-sm text-muted-foreground">This run has not completed successfully. Saved mappings may be incomplete.</p>}
      {!loading && matchesLoaded && <p className="text-sm text-muted-foreground">{rows.length} saved {rows.length === 1 ? 'mapping' : 'mappings'} loaded for this filter{next ? '; more available' : ''}.</p>}
      {!loading && matchesLoaded && !rows.length && <p className="text-sm">No saved mappings match this filter.</p>}
      <ul className="divide-y">{rows.map(row => <li key={row.mapping_id} className="space-y-3 py-4">
        <p className="break-all text-sm font-medium">{row.old_url} → {row.decision_target || row.new_url || 'No destination'}</p>
        {row.jev_proposal && <div className="space-y-1 text-sm">
          <p className="break-all">Proposed destination: {row.jev_proposal.target_url || 'No confident destination'}</p>
          {typeof row.jev_proposal.confidence === 'number' && Number.isFinite(row.jev_proposal.confidence) &&
            <p>Model estimate: {(row.jev_proposal.confidence * 100).toFixed(1)}% · pass {row.jev_proposal.pass}</p>}
          {row.jev_proposal.stale && <p>This proposal predates the latest confirmed examples. Review it or refine again.</p>}
        </div>}
        <p className="text-sm text-muted-foreground">{row.review_status.replaceAll('_', ' ')}. {row.traffic_observed ? `${row.traffic_clicks ?? 0} observed search clicks` : 'Search traffic not measured'}</p>
        <div className="flex flex-wrap gap-2">
          {isJev || row.jev_proposal ? <Button disabled={busy || loading || !row.jev_proposal?.target_url || row.jev_proposal.stale || row.review_status === 'approved'}
            onClick={() => void decide(row, 'set_target', row.jev_proposal?.target_url || undefined)}>Confirm proposed destination</Button> :
            <Button disabled={busy || loading || !row.new_url} onClick={() => void decide(row, 'approve')}>Approve</Button>}
          <Button variant="outline" disabled={busy || loading} onClick={() => void decide(row, 'reject')}>Reject</Button>
          <Button variant="outline" disabled={busy || loading} onClick={() => void decide(row, 'defer')}>Defer</Button>
        </div>
        <label className="block space-y-2 text-sm">Destination for {row.old_url}
          <Input type="url" maxLength={8192} value={targets[row.mapping_id] || ''}
            onChange={e => setTargets(current => ({ ...current, [row.mapping_id]: e.target.value }))} />
        </label>
        <Button variant="outline" disabled={busy || loading || !targets[row.mapping_id]} onClick={() => void decide(row, 'set_target')}>Set destination</Button>
      </li>)}</ul>
      {next && <Button variant="outline" disabled={busy || loading || pageLoading} onClick={() => void more()}>{pageLoading ? 'Loading mappings…' : 'Load more mappings'}</Button>}
    </section>}
    {data?.artifact && <section className="space-y-3 border-t pt-6">
      <h2 className="text-lg font-semibold">Redirect artifact</h2>
      <p className="text-sm">{data.artifact.included_count} redirects included; {data.artifact.excluded_count} excluded. Format: {data.artifact.format}.</p>
      <Button disabled={busy} onClick={() => void download()}>Download redirects</Button>
      <p className="text-sm text-muted-foreground">Download the reviewed redirect map for installation on your platform.</p>
      {data.verification && <p>Verification: {data.verification.outcome}. {data.verification.checked} of {data.verification.total} checked.</p>}
    </section>}
    {historicalServices && <><MonitoringPanel key={`${migrationId}:${monitorId || ''}`} migrationId={migrationId} monitoringId={monitorId} />
    <SubscriptionCheckoutPanel deploymentId={data?.deployment_id} /></>}
    {!isJev && <SearchConsolePanel key={migrationId} migrationId={migrationId} />}
  </main></ToolLayout>;
}
