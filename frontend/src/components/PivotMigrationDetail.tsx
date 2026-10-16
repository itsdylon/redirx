import { useAuth } from '../contexts/AuthContext';
import { lazy, memo, Suspense, useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { checkout, downloadArtifact, getPivotMigration, listPivotMatches, monitoring, resolvePivotMatch, refinePivotMatches,
  PivotRequestError, type MigrationDetail, type MonitoringData, type MonitoringIssue, type PivotEnvelope, type PivotMatch, type PivotMatchFilter } from '../api/pivot';
import { FrontendEvent, safeCapture } from '../lib/analyticsEvents';
import { LoadBoundary } from './LoadBoundary';
import { ToolLayout } from './ToolLayout';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { SubscriptionCheckoutPanel } from './SubscriptionCheckoutPanel';
import { PivotBillingNotice } from './PivotBillingNotice';
import { useRequestScope } from './pivot/useRequestScope';
import { MigrationHandoff } from './pivot/MigrationHandoff';

const SearchConsolePanel = lazy(() => import('./pivot/SearchConsolePanel').then(module => ({ default: module.SearchConsolePanel })));

// These are the approved membership states of list_migration_matches (038).
const isApproved = (row: PivotMatch) => ['approved', 'approve', 'set_target', 'accept_repair'].includes(row.review_status);
const reviewLabel = (row: PivotMatch) => isApproved(row) ? 'Approved' : ({
  needs_review: 'Needs review', defer: 'Deferred for review', unmatched: 'Unmatched',
  reject: 'Rejected', intentional_removal: 'Intentionally removed',
}[row.review_status] || row.review_status.replaceAll('_', ' '));
type DecisionFeedback = { id: string; kind: 'saving' | 'error' | 'saved' | 'notice'; text: string; refreshRequired?: boolean };
const needsAccessRefresh = (error: unknown) => error instanceof PivotRequestError
  && ([401, 403, 404].includes(error.status) || ['reconnect_required', 'not_found'].includes(error.code));
const accessRecovery = 'Refresh status after confirming access. Copy unsaved destinations before leaving this page.';

const modelEstimate = (row: PivotMatch) => {
  const value = row.jev_proposal?.confidence;
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
};

const MappingReviewRow = memo(function MappingReviewRow({ row, draft, reviewDisabled, inputDisabled, isJev, feedback, feedbackRef, onDecide, onTargetChange, onRefresh }: {
  row: PivotMatch; draft: string; reviewDisabled: boolean; inputDisabled: boolean; isJev: boolean;
  feedback: DecisionFeedback | null; feedbackRef: RefObject<HTMLParagraphElement>;
  onDecide: (row: PivotMatch, action: string, target?: string) => Promise<void>;
  onTargetChange: (id: string, value: string) => void;
  onRefresh: () => void;
}) {
  return <li className="space-y-3 py-4">
        <dl className="grid gap-1 text-sm">
          <dt className="font-medium">Source URL</dt><dd className="break-all">{row.old_url}</dd>
          <dt className="mt-2 font-medium">{isApproved(row) ? 'Approved destination' : 'Saved destination'}</dt>
          <dd className="break-all">{row.decision_target || row.new_url || 'No destination'}</dd>
        </dl>
        {row.jev_proposal && <div className="space-y-1 text-sm">
          <p className="break-all">{isApproved(row) ? 'Model proposal (does not replace your decision)' : 'Proposed destination'}: {row.jev_proposal.target_url || 'No proposed destination'}</p>
          {typeof row.jev_proposal.confidence === 'number' && Number.isFinite(row.jev_proposal.confidence) &&
            <p>Model estimate: {(row.jev_proposal.confidence * 100).toFixed(1)}% · pass {row.jev_proposal.pass}</p>}
          {row.jev_proposal.stale && <p>This proposal predates the latest confirmed examples. Review it or refine again.</p>}
        </div>}
        <p className="text-sm text-muted-foreground">{reviewLabel(row)}. {row.traffic_observed ? `${row.traffic_clicks ?? 0} observed search clicks` : 'Search traffic not measured'}</p>
        {feedback?.id === row.mapping_id && <p ref={feedbackRef} tabIndex={-1} role={feedback.kind === 'error' ? 'alert' : 'status'} className="break-words text-sm">{feedback.text}</p>}
        {feedback?.refreshRequired && <Button variant="outline" disabled={inputDisabled} onClick={onRefresh}>Refresh mappings</Button>}
        <div className="flex flex-wrap gap-2">
          {isJev || row.jev_proposal ? <Button disabled={reviewDisabled || !row.jev_proposal?.target_url || row.jev_proposal.stale || isApproved(row)}
            onClick={() => void onDecide(row, 'set_target', row.jev_proposal?.target_url || undefined)}>Confirm proposed destination</Button> :
            <Button disabled={reviewDisabled || !row.new_url} onClick={() => void onDecide(row, 'approve')}>Approve</Button>}
          <Button variant="outline" disabled={reviewDisabled} onClick={() => void onDecide(row, 'reject')}>Reject</Button>
          <Button variant="outline" disabled={reviewDisabled} onClick={() => void onDecide(row, 'defer')}>Defer</Button>
        </div>
        <form className="space-y-3" onSubmit={event => { event.preventDefault(); if (!reviewDisabled) void onDecide(row, 'set_target', draft); }}>
        <label className="block break-all space-y-2 text-sm">Destination for {row.old_url}
          <Input type="url" required pattern="[Hh][Tt][Tt][Pp][Ss]?://.+" title="Enter a full http:// or https:// destination URL." autoComplete="off" spellCheck={false} disabled={inputDisabled} maxLength={8192} value={draft}
            onChange={e => onTargetChange(row.mapping_id, e.target.value)} />
        </label>
        <p className="text-sm text-muted-foreground">Setting a destination explicitly approves this redirect. Use a full URL from the new inventory.</p>
        <Button type="submit" variant="outline" disabled={reviewDisabled || !draft}>Set destination</Button>
        </form>
      </li>;
});

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
  const { user } = useAuth();
  if (!user) return <ToolLayout title="Migration companion"><p role="status">Sign in to review this migration.</p></ToolLayout>;
  return <MigrationDetailContent key={`${user.id}:${migrationId}`} migrationId={migrationId} />;
}

// A route change is a new review resource, including its drafts and retry keys.
function MigrationDetailContent({ migrationId }: { migrationId: string }) {
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
  const [downloadFeedback, setDownloadFeedback] = useState<{ kind: 'saving' | 'error' | 'saved'; text: string } | null>(null);
  const focusedDownloadFeedback = useRef<typeof downloadFeedback>(null);
  const downloadFeedbackRef = useRef<HTMLParagraphElement>(null);
  const downloadButtonRef = useRef<HTMLButtonElement>(null);
  const statusButtonRef = useRef<HTMLButtonElement>(null);
  const restoreStatusFocus = useRef(false);
  const [loading, setLoading] = useState(true);
  const [matchesLoaded, setMatchesLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [savedMessage, setSavedMessage] = useState('');
  const [reviewReady, setReviewReady] = useState(false);
  const [summaryFresh, setSummaryFresh] = useState(false);
  const [find, setFind] = useState('');
  const [order, setOrder] = useState('traffic');
  const [feedback, setFeedback] = useState<DecisionFeedback | null>(null);
  const feedbackRef = useRef<HTMLParagraphElement>(null);
  const savedRef = useRef<HTMLParagraphElement>(null);
  const focusedFeedback = useRef<typeof feedback>(null);
  const pageSize = useRef<20 | 500>(20);
  const selectionRevision = useRef<number | undefined>();
  const loadedPages = useRef(1);
  const loadedRun = useRef<string | undefined>();
  const mappingScope = useRequestScope(`${migrationId}:${summary?.data.run_id || ''}:${matchFilter}:${refresh}`);
  useEffect(() => {
    if (feedback !== focusedFeedback.current && (feedback?.kind === 'error' || (['saved', 'notice'].includes(feedback?.kind || '') && !loading && reviewReady))) {
      const destination = feedbackRef.current || savedRef.current;
      if (destination) { destination.focus(); focusedFeedback.current = feedback; }
    }
  }, [feedback, loading, reviewReady]);
  useEffect(() => {
    if (busy || !downloadFeedback || downloadFeedback === focusedDownloadFeedback.current) return;
    if (downloadFeedback.kind === 'error') {
      if (!downloadFeedbackRef.current) return;
      downloadFeedbackRef.current.focus();
    }
    if (downloadFeedback.kind === 'saved') {
      const button = downloadButtonRef.current;
      // A concurrent status poll can keep this control disabled after the
      // download finishes. Retain the focus request until it can be honored.
      if (loading || !summaryFresh || !button || button.disabled) return;
      button.focus();
    }
    focusedDownloadFeedback.current = downloadFeedback;
  }, [downloadFeedback, busy, loading, summaryFresh]);
  useEffect(() => {
    if (!loading && restoreStatusFocus.current) {
      restoreStatusFocus.current = false; statusButtonRef.current?.focus();
    }
  }, [loading]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true); setMessage(''); setDownloadFeedback(null); setReviewReady(false); setSummaryFresh(false); setPageLoading(false);
    void (async () => {
      try {
        const result = await getPivotMigration(migrationId, controller.signal);
        if (controller.signal.aborted) return;
        if (loadedRun.current !== result.data.run_id) {
          loadedPages.current = 1; pageSize.current = 20; loadedRun.current = result.data.run_id;
          setRows([]); setNext(null); setTargets({}); setSavedMessage(''); setFeedback(null);
        }
        setSummary(result); setSummaryFresh(true);
        if (result.data.run_id && !['queued', 'running'].includes(result.data.run?.status || '')) {
          // Decisions can change filter membership. Rebuild opened pages from
          // fresh server cursors instead of merging stale revisions or cursors.
          const refreshed: PivotMatch[] = [];
          let cursor: string | undefined;
          let pages = 0;
          let revision: number | undefined;
          do {
            const matches = await listPivotMatches(migrationId, result.data.run_id, cursor, controller.signal, matchFilter, pageSize.current);
            if (controller.signal.aborted) return;
            const incomingRevision = Number.isInteger(matches.data.selection_revision) ? matches.data.selection_revision : undefined;
            if (pages > 0 && incomingRevision !== revision) throw new Error('Mappings changed while pages were loading. Refresh status to load one current selection. Your typed destinations are preserved.');
            revision = incomingRevision;
            refreshed.push(...matches.data.items);
            cursor = matches.data.next_cursor || undefined;
            pages++;
          } while (cursor && pages < loadedPages.current);
          loadedPages.current = pages;
          selectionRevision.current = revision;
          setRows(refreshed); setNext(cursor || null); setMatchesLoaded(true); setReviewReady(true);
          setFeedback(current => current?.refreshRequired ? { id: current.id, kind: 'notice',
            text: 'Current mappings loaded. Review the latest saved decision before deciding again. Your typed destination is preserved.' } : current);
        } else { setRows([]); setNext(null); setMatchesLoaded(false); }
        if (result.next_action === 'poll') timer = setTimeout(() => setRefresh(n => n + 1),
          Math.min(30, Math.max(3, result.retry_after_seconds || 10)) * 1000);
      } catch (e) { if (!controller.signal.aborted) setMessage(e instanceof Error ? e.message : 'Migration could not load. Refresh to retry.'); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    })();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [migrationId, refresh, matchFilter]);
  const perform = useCallback(async (action: (signal: AbortSignal) => Promise<void>, reportError: (message: string) => void = setMessage) => {
    const signal = scope.signal();
    setBusy(true); setMessage('');
    try { await action(signal); }
    catch (e) {
      if (!signal.aborted) {
        const staleAccess = needsAccessRefresh(e);
        if (staleAccess) { setSummaryFresh(false); setReviewReady(false); }
        reportError(`${e instanceof Error ? e.message : 'The action could not finish. Try again.'}${staleAccess ? ` ${accessRecovery}` : ''}`);
      }
    }
    finally { if (!signal.aborted) setBusy(false); }
  }, [scope]);
  const refreshStatus = useCallback(() => {
    restoreStatusFocus.current = document.activeElement === statusButtonRef.current;
    setRefresh(n => n + 1);
  }, []);
  const changeTarget = useCallback((id: string, value: string) => setTargets(current => ({ ...current, [id]: value })), []);
  const decide = useCallback(async(row: PivotMatch, action: string, confirmedTarget?: string) => {
    // Fired on the user's initiated click, before the request resolves. The
    // backend's own mapping_decisions_applied event (migration_mapping_routes.py)
    // is the one that says the decision was actually persisted — this one says
    // a person chose to try.
    safeCapture(posthog, FrontendEvent.PIVOT_REVIEW_DECISION_INITIATED, {
      migration_id: migrationId, run_id: summary?.data.run_id, action,
    });
    setSavedMessage('');
    setFeedback({ id: row.mapping_id, kind: 'saving', text: 'Saving decision…' });
    await perform(async signal => {
      try {
        const decision = { mapping_id: row.mapping_id, expected_revision: row.revision, action,
          ...(action === 'set_target' ? { target_url: confirmedTarget } : {}) };
        const result = await resolvePivotMatch(migrationId, summary!.data.run_id!, decision,
          scope.key(JSON.stringify(decision)), signal);
        if (signal.aborted) return;
        const outcome = result.data.outcomes?.[0];
        if (outcome && ['revision_conflict', 'not_found'].includes(outcome.code)) {
          setReviewReady(false);
          setFeedback({ id: row.mapping_id, kind: 'error', refreshRequired: true,
            text: outcome.code === 'revision_conflict'
              ? 'This mapping changed since you loaded it. Refresh mappings before deciding again. Your typed destination is preserved.'
              : 'This mapping is no longer available in the loaded review. Refresh mappings before deciding again. Your typed destination is preserved.' });
          return;
        }
        if (!outcome || outcome.code !== 'ok') throw new Error(outcome?.message ||
          `Decision was not saved (${outcome?.code || 'missing result'}). Refresh the mappings and retry.`);
        setTargets(current => { const updated = { ...current }; delete updated[row.mapping_id]; return updated; });
        const text = `${action === 'set_target' || action === 'approve' ? 'Destination approved' : action === 'reject' ? 'Rejection saved' : 'Deferred for review'} for ${row.old_url}.`;
        setSavedMessage(text); setReviewReady(false); setFeedback({ id: row.mapping_id, kind: 'saved', text });
        setRefresh(n => n + 1);
      } catch (e) {
        if (!signal.aborted) {
          const staleAccess = needsAccessRefresh(e);
          if (staleAccess) { setSummaryFresh(false); setReviewReady(false); }
          setFeedback({ id: row.mapping_id, kind: 'error', refreshRequired: staleAccess,
            text: `${e instanceof Error ? e.message : 'The decision could not be saved. Try again.'}${staleAccess ? ` ${accessRecovery}` : ''}` });
        }
      }
    });
  }, [posthog, migrationId, summary?.data.run_id, scope, perform]);
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
  async function more(loadAll = false) {
    if (!next || !summary?.data.run_id || pageLoading) return;
    const signal = mappingScope.signal();
    setPageLoading(true); setMessage('');
    try {
      const result = await listPivotMatches(migrationId, summary.data.run_id, loadAll ? undefined : next, signal, matchFilter, loadAll ? 500 : pageSize.current);
      if (!signal.aborted) {
        const revision = Number.isInteger(result.data.selection_revision) ? result.data.selection_revision : undefined;
        if (!loadAll && revision !== selectionRevision.current) {
          setReviewReady(false);
          throw new Error('Mappings changed since the previous page. Refresh status before continuing. Your typed destinations are preserved.');
        }
        selectionRevision.current = revision;
        if (loadAll) { pageSize.current = 500; loadedPages.current = 1; setRows(result.data.items); }
        else { loadedPages.current++; setRows(current => [...current, ...result.data.items]); }
        setNext(result.data.next_cursor || null);
      }
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
    setDownloadFeedback({ kind: 'saving', text: 'Preparing download…' });
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
      setDownloadFeedback({ kind: 'saved', text: 'Download prepared. Check your browser’s downloads.' });
    }, text => setDownloadFeedback({ kind: 'error', text }));
  }
  const data = summary?.data;
  const visibleRows = useMemo(() => {
    const query = find.toLocaleLowerCase();
    const visible = rows.filter(row => [row.old_url, row.decision_target, row.new_url, row.jev_proposal?.target_url]
      .some(url => url?.toLocaleLowerCase().includes(query)));
    if (order === 'estimate') visible.sort((a, b) => (modelEstimate(a) ?? -1) - (modelEstimate(b) ?? -1));
    return visible;
  }, [rows, find, order]);
  const reviewStatusMessage = savedMessage || (feedback?.kind === 'notice' ? feedback.text : '');
  const reviewDisabled = busy || loading || pageLoading || !reviewReady;
  const isJev = data?.jev?.engine === 'jev-url-v1';
  const paymentRequired = !isJev && (summary?.status === 'payment_required' || summary?.next_action === 'complete_payment');
  const monitorId = search.get('monitoring_id') || data?.monitoring_id;
  const historicalServices = !isJev && !!(data?.quote_id || monitorId || data?.deployment_id);
  return <ToolLayout title="Migration detail"><div className="mx-auto max-w-4xl space-y-8 py-4">
    <header className="space-y-3">
      <Link to="/companion" className="text-sm underline">All migrations</Link>
      <Link to={`/billing/current?${new URLSearchParams({ migration_id: migrationId, ...(data?.run?.inventory_ids ? { old_inventory_id: data.run.inventory_ids.old, new_inventory_id: data.run.inventory_ids.new } : {}) })}`} className="block text-sm underline">Plans and billing for this migration</Link>
      <h1 className="break-words text-3xl font-semibold">{data?.migration?.name || 'Migration detail'}</h1>
      <p className="break-all text-sm text-muted-foreground">{migrationId}</p>
      <Button id="migration-status" ref={statusButtonRef} variant="outline" disabled={busy || loading} onClick={refreshStatus}>Refresh status</Button>
      {loading && <p role="status">Loading migration status…</p>}
      {message && <p role="alert">{message}</p>}
      {summary && <><p className="font-medium">{summaryFresh ? 'Status' : 'Last loaded status'}: {summary.status.replaceAll('_', ' ')}</p><p>{data?.summary}</p></>}
    </header>
    {paymentRequired && data?.quote_id && <section className="space-y-3 border-t pt-6" aria-labelledby="payment-heading">
      <h2 id="payment-heading" className="text-lg font-semibold">Payment required</h2>
      <PivotBillingNotice />
      {data.quote && <p>{new Intl.NumberFormat('en-US', { style: 'currency', currency: data.quote.currency }).format(data.quote.amount_cents / 100)} for this migration.</p>}
      <p className="text-sm text-muted-foreground">Payment is confirmed by the server. Returning from checkout does not start work by itself.</p>
      {!summary?.operation_id && <p role="alert">Payment details are incomplete. Refresh the migration before trying checkout again.</p>}
      <Button disabled={busy || loading || !summaryFresh || !summary?.operation_id} onClick={() => void pay()}>Continue to checkout</Button>
    </section>}
    {isJev && data?.jev && <section className="space-y-3 border-t pt-6" aria-labelledby="jev-heading">
      <h2 id="jev-heading" className="text-lg font-semibold">Free URL mapping</h2>
      <p className="text-sm">Pass {data.jev.pass} of {data.jev.limits.passes}. Confirmed-example revision {data.jev.seed_revision}. Model confidence is an estimate, not a verified match.</p>
      {['queued', 'running'].includes(data.run?.status || '') && <p role="status" className="text-sm">{data.run?.status === 'queued' ? 'Matching is queued.' : 'Matching is in progress.'} This page checks for updates automatically. You can leave and return to this migration.</p>}
      <p className="text-sm text-muted-foreground">Confirm known destinations to guide another pass over unresolved pages. Existing confirmed decisions remain saved. URL text is matched without fetching page content.</p>
      <Button variant="outline" disabled={busy || loading || !summaryFresh || ['queued', 'running'].includes(data.run?.status || '') ||
        (data.jev.pass >= data.jev.limits.passes && data.run?.status !== 'failed')}
        onClick={() => void refine()}>{data.run?.status === 'failed' ? 'Resume matching pass' : 'Refine with confirmed examples'}</Button>
      {data.jev.pass >= data.jev.limits.passes && data.run?.status !== 'failed' && <p className="text-sm">All three passes are used. Continue reviewing and exporting this run.</p>}
    </section>}
    {data?.run_id && <section className="space-y-3 border-t pt-6" aria-labelledby="mappings-heading">
      <h2 id="mappings-heading" className="text-lg font-semibold">Mapping review</h2>
      <a href="#migration-export" className="text-sm underline">Jump to export guidance</a>
      {reviewStatusMessage && !visibleRows.some(row => row.mapping_id === feedback?.id) && <p ref={savedRef} tabIndex={-1} role="status" className="break-all text-sm">{reviewStatusMessage}</p>}
      {!loading && !reviewReady && message && <p className="text-sm">Refresh status to load current mapping revisions before making another decision. Your typed destinations are preserved.</p>}
      <p className="text-sm text-muted-foreground">Approve only a relevant destination. Leave uncertain decisions for review with your agent.</p>
      <label className="block space-y-2 text-sm">Show mappings
        <select className="block rounded-md border border-input bg-background px-3 py-2" value={matchFilter}
          disabled={busy || loading} onChange={event => { loadedPages.current = 1; pageSize.current = 20; setRows([]); setNext(null); setMatchesLoaded(false); setSavedMessage(''); setFeedback(null); setFind(''); setOrder('traffic'); setMatchFilter(event.target.value as PivotMatchFilter); }}>
          <option value="all">All mappings</option>
          <option value="needs_review">Needs review or deferred</option>
          <option value="unmatched">Unmatched</option>
          <option value="rejected">Rejected or intentionally removed</option>
          <option value="approved">Approved</option>
        </select>
      </label>
      <p className="text-sm text-muted-foreground">Needs review includes deferred decisions. Check Unmatched separately for pages without a saved destination.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-2 text-sm">Find in loaded mappings
          <Input type="search" value={find} disabled={busy || loading || pageLoading} onChange={event => setFind(event.target.value)} placeholder="Source or destination URL" />
        </label>
        {isJev && <label className="space-y-2 text-sm">Order loaded mappings
          <select className="block w-full rounded-md border border-input bg-background px-3 py-2" value={order} disabled={busy || loading || pageLoading} onChange={event => setOrder(event.target.value)}>
            <option value="traffic">Server order (search traffic)</option>
            <option value="estimate">Lowest model estimate first</option>
          </select>
        </label>}
      </div>
      {isJev && order === 'estimate' && <p className="text-sm text-muted-foreground">Missing estimates appear first. Estimates help you choose what to inspect; they are not measured error probabilities or approval.</p>}
      {data.run?.status !== 'succeeded' && <p className="text-sm text-muted-foreground">This run has not completed successfully. Saved mappings may be incomplete.</p>}
      {!loading && matchesLoaded && <p className="text-sm text-muted-foreground">{rows.length} saved {rows.length === 1 ? 'mapping' : 'mappings'} loaded for this filter{next ? '; more available' : ''}.</p>}
      {!loading && matchesLoaded && reviewReady && <p role="status" className="text-sm">{visibleRows.length} shown{next ? '. Find and ordering cover loaded mappings only; more remain on the server.' : '. All saved mappings for this filter are loaded.'}</p>}
      {next && isJev && <Button variant="outline" disabled={reviewDisabled} onClick={() => void more(true)}>Load up to 500 mappings for this filter</Button>}
      {!loading && matchesLoaded && !rows.length && <p className="text-sm">No saved mappings match this filter.</p>}
      {!loading && matchesLoaded && rows.length > 0 && !visibleRows.length && <p className="text-sm">No loaded URLs match your search.{next ? ' Load more mappings or clear the search.' : ' Clear the search or choose another review filter.'}</p>}
      <ul className="divide-y" aria-label="Mapping decisions" aria-busy={loading || pageLoading}>{visibleRows.map(row => <MappingReviewRow key={row.mapping_id} row={row} draft={targets[row.mapping_id] || ''}
        reviewDisabled={reviewDisabled} inputDisabled={busy || loading} isJev={isJev} feedback={feedback?.id === row.mapping_id ? feedback : null}
        feedbackRef={feedbackRef} onDecide={decide} onTargetChange={changeTarget} onRefresh={refreshStatus} />)}</ul>
      {next && <Button variant="outline" disabled={reviewDisabled} onClick={() => void more()}>{pageLoading ? 'Loading mappings…' : 'Load more mappings'}</Button>}
    </section>}
    {data?.run_id && !data.artifact && <section id="migration-export" className="space-y-3 border-t pt-6" aria-labelledby="export-next-heading">
      <h2 id="export-next-heading" className="text-lg font-semibold">Prepare an export with your agent</h2>
      <p className="text-sm">Confirm or correct the destinations you want to include, then ask your connected agent to export approved redirects for this migration. The server checks inventory membership and redirect safety before producing an export.</p>
      <p className="text-sm text-muted-foreground">If unresolved or excluded mappings remain, decide whether to continue reviewing or explicitly allow a partial export with your agent. Rejected, deferred and unmatched rows are not automatically converted into redirects or 410 rules.</p>
    </section>}
    {data?.artifact && <section id="migration-export" className="space-y-3 border-t pt-6" aria-labelledby="artifact-heading">
      <h2 id="artifact-heading" className="text-lg font-semibold">Redirect artifact</h2>
      <p className="text-sm text-muted-foreground">This is a saved export snapshot. Later review changes do not update this file; ask your agent for a new export after changing decisions.</p>
      <p className="text-sm">{data.artifact.included_count} redirects included; {data.artifact.excluded_count} excluded. Format: {data.artifact.format}.</p>
      {downloadFeedback && <p ref={downloadFeedbackRef} tabIndex={-1} role={downloadFeedback.kind === 'error' ? 'alert' : 'status'}>{downloadFeedback.text}</p>}
      <Button ref={downloadButtonRef} disabled={busy || loading || !summaryFresh} onClick={() => void download()}>Download redirects</Button>
      {downloadFeedback?.kind === 'error' && !summaryFresh && <p><a className="text-sm underline" href="#migration-status">Go to migration status</a></p>}
      <MigrationHandoff migrationId={migrationId} data={data} fresh={summaryFresh && !loading} />
    </section>}
    {historicalServices && <><MonitoringPanel key={`${migrationId}:${monitorId || ''}`} migrationId={migrationId} monitoringId={monitorId} />
    <SubscriptionCheckoutPanel deploymentId={data?.deployment_id} /></>}
    {summary && !isJev && <LoadBoundary section="Search Console" resetKey={migrationId}><Suspense fallback={<p role="status">Loading Search Console…</p>}>
      <SearchConsolePanel key={migrationId} migrationId={migrationId} />
    </Suspense></LoadBoundary>}
  </div></ToolLayout>;
}
