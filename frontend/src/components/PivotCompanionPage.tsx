import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { listPivotMigrations, type PivotMigration } from '../api/pivot';
import { FrontendEvent, safeCapture } from '../lib/analyticsEvents';
import { ToolLayout } from './ToolLayout';
import { Button } from './ui/button';
import { useRequestScope } from './pivot/useRequestScope';

const MCP_URL = 'https://redirx-mcp-server.onrender.com/mcp';
export function PivotCompanionPage() {
  const posthog = usePostHog();
  const scope = useRequestScope('companion');
  const [items, setItems] = useState<PivotMigration[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  async function load(cursor?: string) {
    const signal = scope.signal();
    setBusy(true); setError('');
    try {
      const result = await listPivotMigrations(cursor, signal);
      if (signal.aborted) return;
      setItems(current => cursor ? [...current, ...result.data.items] : result.data.items);
      setNext(result.data.next_cursor || null); setLoaded(true);
    } catch (e) { if (!signal.aborted) setError(e instanceof Error ? e.message : 'History could not load. Try again.'); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  useEffect(() => { void load(); }, []);
  return <ToolLayout title="Migration companion"><main className="mx-auto max-w-4xl space-y-8 py-4">
    <header className="space-y-3">
      <h1 className="text-3xl font-semibold">Your migrations</h1>
      <Link to="/billing/current" className="text-sm underline">Plans and billing</Link>
      <p className="max-w-prose text-muted-foreground">Start a free URL migration in your MCP client. Import old and new URL lists, review proposed destinations here, then export the reviewed map.</p>
    </header>
    <section className="space-y-3 border-t pt-6" aria-labelledby="connect-heading">
      <h2 id="connect-heading" className="text-lg font-semibold">Connect your agent</h2>
      <p className="text-sm">Add this Streamable HTTP server in your MCP client, then choose its sign-in action and approve RedirX access.</p>
      <code className="block break-all rounded-md bg-muted p-3 text-sm">{MCP_URL}</code>
      <p className="text-sm text-muted-foreground">The client handles OAuth and refreshes access automatically. Ask it to plan the migration, import both URL inventories, then start matching.</p>
    </section>
    <p className="text-sm text-muted-foreground">Free while we build: up to 500 old URLs, 2,000 new URLs, 2 MiB of URL text, five new runs per 24 hours and three passes per run. No page-content scraping. Known, verified URL pairs can guide matching.</p>
    <section aria-labelledby="history-heading" className="space-y-3 border-t pt-6">
      <div className="flex items-center justify-between gap-3">
        <h2 id="history-heading" className="text-lg font-semibold">Migration history</h2>
        <Button variant="outline" disabled={busy} onClick={() => void load()}>Refresh history</Button>
      </div>
      {busy && <p role="status">Loading migration history…</p>}
      {error && <p role="alert">{error}</p>}
      {!error && loaded && !items.length && <p className="text-sm text-muted-foreground">No migrations yet. Ask your connected agent to plan a migration from your old site to your new site.</p>}
      <ul className="divide-y">
        {items.map(item => <li key={item.id}><Link to={`/migrations/${item.id}`} className="block space-y-1 py-4 hover:underline"
          onClick={() => safeCapture(posthog, FrontendEvent.PIVOT_MIGRATION_OPENED, { migration_id: item.id, status: item.status })}>
          <span className="block break-words font-medium">{item.name || item.old_origin || 'Untitled migration'}</span>
          <span className="block break-all text-sm text-muted-foreground">{item.old_origin} → {item.new_origin}</span>
          <span className="text-sm">{item.status || 'Open migration'}</span>
        </Link></li>)}
      </ul>
      {next && <Button variant="outline" disabled={busy} onClick={() => void load(next)}>Load more migrations</Button>}
    </section>

  </main></ToolLayout>;
}
