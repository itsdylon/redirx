import { createRoot } from 'react-dom/client';
import { MigrationHandoff } from '../../src/components/pivot/MigrationHandoff';
import type { MigrationDetail } from '../../src/api/pivot';
import '../../src/styles/globals.css';
const params = new URLSearchParams(location.search);
const state = params.get('state');
const data: MigrationDetail = {
  artifact: { artifact_id: 'fb9284b0-9e62-4ad8-a172-67363b4ee3a1', format: params.get('format') || 'cloudflare', included_count: 498, excluded_count: 2 },
  ...(state === 'installed' ? { deployment_id: 'ab9284b0-9e62-4ad8-a172-67363b4ee3a1' } : {}),
  ...(state === 'passed' ? { verification: { outcome: 'passed', checked: 498, total: 498, unchecked: 0 } } : {}),
  ...(state === 'partial' ? { verification: { outcome: 'unverifiable', checked: 490, total: 498, unchecked: 8 } } : {}),
};
createRoot(document.getElementById('root')!).render(<main className="mx-auto max-w-3xl space-y-5 p-5 sm:p-8">
  <h1 className="text-2xl font-semibold">Redirect artifact</h1>
  <p className="text-sm">498 redirects included; 2 excluded. Fixture data.</p>
  <MigrationHandoff migrationId="eb9284b0-9e62-4ad8-a172-67363b4ee3a1" data={data} fresh={state !== 'stale'} />
</main>);
