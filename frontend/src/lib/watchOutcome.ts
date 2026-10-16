import type { WatchCheck } from '../api/watch';

/** A clean issue list is not coverage evidence. Sweeps may be capped or fail. */
export function watchOutcome(check: WatchCheck | undefined, openIssues: number) {
  if (!check) return { healthy: false, title: 'No completed check yet', detail: 'Coverage is not available yet. An active monitor checks on its schedule; you can also request a check.' };
  if (check.status === 'running') return { healthy: false, title: 'Check in progress', detail: 'Results are not final. This page refreshes automatically.' };
  if (check.status === 'failed') return { healthy: false, title: 'The latest check failed', detail: 'This attempt did not verify your redirects. Review the error below and request another check when the issue is resolved.' };
  if (!Number.isFinite(check.urls_checked) || !Number.isFinite(check.urls_ok) || check.urls_ok < 0 || check.urls_ok > check.urls_checked) return { healthy: false, title: 'Check coverage is unavailable', detail: 'Refresh the monitor to load valid check results.' };
  if (check.urls_checked === 0) return { healthy: false, title: 'No URLs were checked', detail: 'This completed attempt verified no redirects. Confirm that this migration has approved mappings, then request another check.' };
  if (check.urls_ok < check.urls_checked || check.issues_open > 0 || openIssues > 0) return { healthy: false, title: 'Redirects need attention', detail: `${check.urls_ok.toLocaleString()} of ${check.urls_checked.toLocaleString()} checked URLs passed. Open issues can include earlier findings that have not been rechecked.` };
  return { healthy: true, title: `All ${check.urls_checked.toLocaleString()} checked URLs passed`, detail: 'This result covers the URLs included in the latest sweep. Unchecked URLs have not been verified.' };
}
