import { useState } from 'react';
import { gscAction, type GscData } from '../../api/pivot';
import { Button } from '../ui/button';
import { useRequestScope } from './useRequestScope';
import { useAuth } from '../../contexts/AuthContext';

export function SearchConsolePanel({ migrationId }: { migrationId?: string }) {
  const { user } = useAuth();
  if (!user) return null;
  return <SearchConsoleContent key={`${user.id}:${migrationId || 'account'}`} ownerId={user.id} migrationId={migrationId} />;
}

function SearchConsoleContent({ ownerId, migrationId }: { ownerId: string; migrationId?: string }) {
  const scope = useRequestScope(`gsc:${ownerId}:${migrationId || 'account'}`);
  const [data, setData] = useState<GscData>({});
  const [property, setProperty] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  async function act(action: string) {
    const signal = scope.signal();
    setBusy(true); setMessage('');
    try {
      const response = await gscAction({ action, ...(migrationId ? { migration_id: migrationId } : {}),
        ...(action === 'sync' ? { property } : {}) }, scope.key(`${action}:${property}`), signal);
      if (signal.aborted) return;
      if (response.data.authorization_url) { window.location.assign(response.data.authorization_url); return; }
      if (response.data.properties && !response.data.properties.some(item => item.site_url === property)) setProperty('');
      setData(current => ({ ...current, ...response.data }));
      setMessage(response.data.summary || `Search Console: ${response.data.connection_state || 'updated'}.`);
    } catch (error) {
      if (!signal.aborted) setMessage(error instanceof Error ? error.message : 'Search Console could not connect. Try again.');
    } finally { if (!signal.aborted) setBusy(false); }
  }
  return <section aria-labelledby="search-console-heading" className="space-y-3 border-t pt-6">
    <h2 id="search-console-heading" className="text-lg font-semibold">Search Console</h2>
    <p className="max-w-prose text-sm text-muted-foreground">Optional historical clicks and impressions help prioritize review. Authorize through Google, then select a property.</p>
    <p className="text-sm">{data.connection_state || 'Connection not checked'}</p>
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" disabled={busy} onClick={() => void act('connect')}>Connect Search Console</Button>
      <Button variant="outline" disabled={busy} onClick={() => void act('properties')}>Load properties</Button>
    </div>
    {data.properties && <label className="block max-w-lg space-y-2 text-sm">Search Console property
      <select className="block w-full rounded-md border bg-background p-2" value={property} disabled={busy} onChange={e => setProperty(e.target.value)}>
        <option value="">Select a property</option>
        {data.properties.map(item => <option key={item.site_url} value={item.site_url}>{item.site_url}</option>)}
      </select>
    </label>}
    {migrationId && <Button disabled={busy || !property} onClick={() => void act('sync')}>Sync selected property</Button>}
    {message && <p role="status" className="text-sm">{message}</p>}
  </section>;
}
