import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';

export const FIRST_MIGRATION_URI = 'redirx://guides/first-migration';
export const FIRST_MIGRATION_INSTRUCTIONS = `For a first migration, read ${FIRST_MIGRATION_URI}. Import actual old source URLs and available new destination URLs; complete means syntax/origin checks passed, not full site coverage. Preserve URL spelling. Keep mutation inputs, idempotency keys, migration_id and run_id for recovery. A timeout does not prove failure: read status, then replay only the same input and key if needed. Proposals require explicit user approval before resolve_matches and export. Export does not install redirects.`;

export const FIRST_MIGRATION_GUIDE = `# Your first RedirX migration

1. Prepare two inventories. Old URLs are the existing source pages that need a migration decision; new URLs are the available destination pages. Use absolute HTTP(S) page URLs from existing exports or reviewed inventories. A sitemap file URL is not its list of page URLs: extract and review the page entries before import. Do not guess destinations, expand patterns, lowercase paths, decode escapes, or remove query strings. Ask the user to confirm the intended scope and any missing pages.
2. Call plan_migration with old_site and new_site as origins, for example https://old.example and https://new.example, without page paths, query strings, fragments or credentials. Declare any additional old/new origins in site_aliases when planning; www and staging hosts are not inferred aliases. Save the returned migration_id and the exact request and idempotency key.
3. Call import_inventory once per side with the full intended URL list. An inventory is immutable. Only status=complete can be used for a run. Complete means the supplied list passed syntax and declared-origin checks; it is not a claim of full site coverage, reachable pages, or correct destinations. Partial imports expose input_issues with finite reason counts and at most ten sanitized URL hints; hints omit queries/fragments and may be absent. Correct the original list and submit it with a new key. Do not silently discard rejected URLs. If the declared origins were wrong, create a corrected plan with the user's intended origins and import both sides there.
4. Call run_migration with the two complete inventory IDs. Current free Jev limits are 500 unique old URLs, 2000 new URLs, 2 MiB total URL text, and five new runs per rolling 24 hours. No page content is fetched by this URL workflow. Optional confirmed_pairs must be explicitly verified examples from these exact inventories. Save run_id and poll get_migration after retry_after_seconds. A finished run contains proposals, not approved redirects.
5. Follow list_matches pagination until next_cursor is null with the same run and filter. Retain row revisions and selection_revision. Present uncertain or missing matches to the user; model confidence is not evidence of approval or a reason to remove a page. After explicit user approval, use resolve_matches with set_target, the exact target_url and expected_revision. Inspect every outcome, including partial failures. After corrections, reread affected proposals before further decisions. refine_matches can resume a paused pass or use confirmed examples, within three total passes; first read data.jev.seed_revision and honor pause reasons/retry timing.
6. Export explicitly approved decisions using export_redirects and the latest selection_revision. Partial export requires explicit agreement to excluded rows. Read the returned owned artifact resource if provided. Export creates a file; the user must install it using the target platform's instructions. Only then confirm installation for verify_redirects. HTTP verification does not establish Google indexing or full SEO correctness; incomplete/unchecked results need attention.

## If a response is lost or a connection stops

Keep the same input and idempotency key for an uncertain mutation. A timeout or disconnect does not roll back work. If migration_id is known, use get_migration (and run_id or operation_id when known) to read current state. If a planning response was lost before its ID arrived, replay the exact plan with its original key. Replay an uncertain run with its original input/key only when needed to recover its result; never create a new key or a new run merely because the response was lost. A deliberate corrected request needs a new key. For a paused Jev run, resolve the reported pause reason and use refine_matches with the current seed revision instead of starting another migration. Do not invent success if status cannot be read; retain the IDs and ask the user to reconnect or inspect their owned migration history.
`;

/** Public static guidance only; artifact reads retain their separate owner checks. */
export function registerFirstMigrationResource(server: McpServer): void {
  server.registerResource('first-migration', FIRST_MIGRATION_URI, {
    title: 'Start and recover a RedirX migration',
    description: 'Prepare inventories, review proposals, recover interrupted work and export approved redirects.',
    mimeType: 'text/markdown',
  }, async uri => ({ contents: [{ uri: uri.href, mimeType: 'text/markdown', text: FIRST_MIGRATION_GUIDE }] }));
}
