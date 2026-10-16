import type { MigrationDetail } from '../../api/pivot';

const GOOGLE_MOVE = 'https://developers.google.com/search/docs/crawling-indexing/site-move-with-url-changes';
const guides: Record<string, { name: string; instruction: string; href?: string }> = {
  apache: { name: 'Apache', instruction: 'Have your server administrator merge the RedirectMatch rules into the configuration serving the old URLs, or its permitted .htaccess file. Preserve existing rules and test the configuration before reloading.', href: 'https://httpd.apache.org/docs/2.4/mod/mod_alias.html#redirectmatch' },
  nginx: { name: 'Nginx', instruction: 'Have your server administrator include the location rules inside the server block serving the old URLs. Preserve existing configuration and test it before reloading.', href: 'https://nginx.org/en/docs/http/ngx_http_core_module.html#location' },
  vercel: { name: 'Vercel', instruction: 'Merge the redirects array into the existing vercel.json for the project serving the old URLs, then deploy that project. Keep the other project configuration.', href: 'https://vercel.com/docs/redirects' },
  cloudflare: { name: 'Cloudflare Pages', instruction: 'Include the _redirects file in the Pages static asset output, then deploy. These rules do not apply to requests handled by Pages Functions. This file is not a Bulk Redirects CSV.', href: 'https://developers.cloudflare.com/pages/configuration/redirects/' },
  wordpress: { name: 'WordPress', instruction: 'Confirm the CSV columns match your redirect plugin before importing. Preview the import and check how duplicates and existing rules will be handled.', href: 'https://redirection.me/support/import-export/' },
  shopify: { name: 'Shopify', instruction: 'In Shopify, open Content → Menus → URL redirects, import the CSV, review the preview, and confirm the import. Check platform restrictions in the guide.', href: 'https://help.shopify.com/en/manual/online-store/menus-and-links/url-redirect' },
  csv: { name: 'CSV mapping', instruction: 'Give this mapping to your site administrator. A generic CSV does not install redirects: choose a supported platform export or adapt it to your platform’s import schema.' },
  json: { name: 'JSON mapping', instruction: 'Give this mapping to your site administrator. A generic JSON mapping does not install redirects: choose a supported platform export or adapt it to your platform’s configuration.' },
};

export function handoffState(data: MigrationDetail) {
  const check = data.verification;
  if (check) {
    const full = Number.isInteger(check.total) && check.total > 0 && check.total === data.artifact?.included_count && check.checked === check.total && check.unchecked === 0;
    if (check.outcome === 'passed' && full) return { title: 'HTTP redirect checks passed', next: 'Continue with the manual site checks below and monitor the move in Search Console.' };
    if (check.outcome === 'issues_found') return { title: 'Redirect issues need attention', next: 'Ask your connected agent to retrieve the verification findings. Correct the affected rules, then repeat verification.' };
    return { title: 'HTTP verification is incomplete', next: 'Ask your connected agent to check the verification status and resolve unchecked URLs before treating the check as complete.' };
  }
  if (data.deployment_id) return { title: 'Installation reported; HTTP checks pending', next: 'Ask your connected agent to verify this artifact using the recorded deployment.' };
  return { title: 'Export saved; installation not reported', next: 'Download the redirects, then install them on the service that receives requests for the old URLs.' };
}

export function MigrationHandoff({ migrationId, data, fresh }: { migrationId: string; data: MigrationDetail; fresh: boolean }) {
  if (!data.artifact) return null;
  const guide = Object.prototype.hasOwnProperty.call(guides, data.artifact.format) ? guides[data.artifact.format] : { name: 'Platform export', instruction: 'Ask your site administrator to confirm the file format and installation procedure before applying this export.' };
  const state = handoffState(data);
  return <div className="max-w-3xl space-y-5 text-sm">
    <div className="space-y-2" aria-live="polite">
      <h3 className="font-semibold">{fresh ? state.title : 'Refresh status before continuing'}</h3>
      <p>{fresh ? state.next : 'The saved export is shown below. Refresh the migration to get its current installation and verification status.'}</p>
      {fresh && data.verification && <p>{data.verification.checked} of {data.verification.total} URLs checked; {data.verification.unchecked} unchecked. Checked URLs can include failed redirects.</p>}
      {data.artifact.excluded_count > 0 && <p>{data.artifact.excluded_count} mappings are excluded from this artifact. Review their disposition separately; this file and its verification do not cover them.</p>}
    </div>
    <details className="space-y-3">
      <summary className="cursor-pointer font-medium underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">Install on {guide.name}</summary>
      <p className="mt-3">{guide.instruction}</p>
      {guide.href && <p><a className="underline underline-offset-4" href={guide.href} target="_blank" rel="noopener noreferrer">{guide.name} installation documentation</a></p>}
      <p>Downloading or reporting installation does not deploy rules. Keep a backup of the existing configuration so your administrator can restore it if needed.</p>
    </details>
    {fresh && <details className="space-y-3">
      <summary className="cursor-pointer font-medium underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">Continue verification with your connected agent</summary>
      <p className="mt-3">Give your agent these references. After installation, confirm the live origin receiving the old URLs and any explicit destination-origin changes. Ask it to run <code>verify_redirects</code>; that check does not install rules.</p>
      <dl className="grid gap-1 break-all"><dt className="font-medium">Migration</dt><dd>{migrationId}</dd><dt className="font-medium">Artifact</dt><dd>{data.artifact.artifact_id}</dd>{data.deployment_id && <><dt className="font-medium">Recorded deployment</dt><dd>{data.deployment_id}</dd></>}</dl>
      <p>Keep destination URL spelling unchanged. If destinations still refer to staging, agree on the production origins before requesting verification.</p>
    </details>}
    <details className="space-y-3">
      <summary className="cursor-pointer font-medium underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">Complete the remaining site checks</summary>
      <p className="mt-3">RedirX checks the exported HTTP redirects. It does not establish that Google has indexed the new site, or check page canonicals, noindex rules, robots.txt, internal links, or sitemaps.</p>
      <ul className="list-disc space-y-2 pl-5">
        <li>Compare the imported URL lists with your CMS, sitemaps, analytics and server logs. A completed import does not prove every site URL was included.</li>
        <li>Review new-page canonicals and crawl/indexing permissions, update internal links, and submit the new sitemap.</li>
        <li>For a domain or subdomain move, check whether Search Console’s Change of Address applies. It is not needed for path-only, HTTP-to-HTTPS, or www/non-www moves.</li>
      </ul>
      <p><a className="underline underline-offset-4" href={GOOGLE_MOVE} target="_blank" rel="noopener noreferrer">Google’s site migration guidance</a>{' · '}<a className="underline underline-offset-4" href="https://support.google.com/webmasters/answer/9370220" target="_blank" rel="noopener noreferrer">Change of Address eligibility</a></p>
    </details>
  </div>;
}
