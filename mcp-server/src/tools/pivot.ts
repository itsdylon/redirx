import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import * as z from 'zod';
import { pivotEnvelopeSchema, pivotEnvelopeShape } from '../backend/pivotEnvelope.js';
import type { PivotEnvelope, RedirxApiError } from '../backend/redirxClient.js';
import { clientForCall, type ToolExtra } from './context.js';

const UUID = z.string().uuid();
const IDEMPOTENCY = z.string().min(1).max(200).regex(/^[^\x00-\x1f\x7f]+$/);
const PAGE = z.number().int().min(1).max(500).default(100);
const FORMATS = ['apache', 'nginx', 'wordpress', 'vercel', 'cloudflare', 'shopify', 'csv', 'json'] as const;
const ORIGIN = z.string().min(1).max(2048).refine(value => {
  try {
    const parsed = new URL(value);
    return value === value.trim() && ['http:', 'https:'].includes(parsed.protocol)
      && !parsed.username && !parsed.password && parsed.pathname === '/' && !parsed.search && !parsed.hash;
  } catch { return false; }
}, 'Use an HTTP(S) origin without credentials, path, query, or fragment.');
const ORIGIN_REWRITES = z.record(ORIGIN, ORIGIN).refine(value => Object.keys(value).length <= 20, 'Use at most 20 origin rewrites.');
const ACTIONS = ['accept_repair', 'set_target', 'approve', 'reject', 'defer', 'intentional_removal'] as const;

type ToolResult = { content: Array<{ type: 'text'; text: string }>; structuredContent: Record<string, unknown>; isError?: boolean };

function localError(code: string, message: string, nextAction = 'none'): PivotEnvelope {
  return {
    contract_version: '1.0.0', migration_id: null, operation_id: null,
    status: 'failed', next_action: nextAction, data: {},
    error: { code, message, retryable: false, next_action: nextAction },
  };
}

function invalidInput(message: string): ToolResult {
  return toolResult(localError('invalid_input', message), true);
}

function toolResult(envelope: PivotEnvelope, isError = envelope.status === 'failed'): ToolResult {
  // Keep the original JSON text for existing clients; structured clients receive
  // the identical canonical envelope without parsing prose or losing next steps.
  const structuredContent = {
    contract_version: envelope.contract_version,
    migration_id: envelope.migration_id ?? null,
    operation_id: envelope.operation_id ?? null,
    status: envelope.status,
    next_action: envelope.next_action,
    ...(envelope.retry_after_seconds === undefined ? {} : { retry_after_seconds: envelope.retry_after_seconds }),
    ...(envelope.progress === undefined ? {} : { progress: envelope.progress }),
    data: envelope.data,
    error: envelope.error,
  };
  return { content: [{ type: 'text', text: JSON.stringify(structuredContent) }], structuredContent,
    ...(isError ? { isError: true } : {}) };
}

function failure(error: RedirxApiError): ToolResult {
  // Do not reflect arbitrary error response bodies: the API contract's error
  // fields are the only safe cross-boundary error authority.
  const body = error.body;
  const nested = body.error;
  const candidate = nested && typeof nested === 'object' ? nested as Record<string, unknown> : body;
  const next = typeof candidate.next_action === 'string' ? candidate.next_action : 'none';
  const retryable = candidate.retryable === true;
  return toolResult({
    ...localError(error.code, error.message, next),
    error: { code: error.code, message: error.message, retryable, next_action: next },
  }, true);
}

async function call(
  extra: ToolExtra, method: 'GET' | 'POST' | 'PATCH', path: string, body?: Record<string, unknown>,
): Promise<ToolResult> {
  try {
    const { client } = await clientForCall(extra);
    const result = await client.pivot(method, path, body, extra.signal);
    if (!result.ok) return failure(result.error);
    const parsed = pivotEnvelopeSchema.safeParse(result.data);
    if (!parsed.success) return toolResult(localError('internal_error', 'Invalid backend response.'), true);
    return toolResult(parsed.data);
  } catch {
    return toolResult(localError('internal_error', 'Gateway request failed. No outcome was confirmed. Read migration status if known; retry a mutation only with the identical input and idempotency key.', 'retry'), true);
  }
}

function query(path: string, values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined) params.set(key, String(value));
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

/** The opt-in v2 surface; Jev additions are pinned in jev-mvp-v1.json. */
export function registerPivotTools(server: McpServer): void {
  server.registerTool('plan_migration', {
    title: 'Plan a migration', description: 'Create an owned old-site/new-site plan. For free Jev, next call import_inventory for old and new URL lists; planning does not crawl or run the model.',
    inputSchema: { old_site: z.string().min(1).max(8192).describe('Origin of existing source pages, e.g. https://old.example; no page path, query, fragment or credentials.'), new_site: z.string().min(1).max(8192).describe('Origin of available destination pages, e.g. https://new.example; no page path, query, fragment or credentials.'), name: z.string().max(200).optional(), site_aliases: z.object({ old: z.array(z.string().max(8192)).max(100).optional(), new: z.array(z.string().max(8192)).max(100).optional() }).optional().describe('Additional explicitly allowed origins per side. Declare www or staging origins here before importing; aliases are not inferred.'), idempotency_key: IDEMPOTENCY },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  }, (args, extra) => call(extra as ToolExtra, 'POST', '/migrations', args));

  server.registerTool('import_inventory', {
    title: 'Import a URL inventory', description: 'Import explicit HTTP(S) URL strings into an owned immutable old or new inventory. No crawl or page scraping. Use data.inventory.id only when data.inventory.status is complete for run_migration; partial imports need corrected input and a new key. The free trial allows 500 old / 2000 new URLs and 2 MiB total URL text. When paid billing is enabled, paid runs allow 15000 old / 20000 new URLs and 16 MiB total. Importing never authorizes a charge.',
    inputSchema: { migration_id: UUID, side: z.enum(['old', 'new']).describe('old = existing source pages; new = available destination pages.'), urls: z.array(z.string().url().max(8192).regex(/^https?:\/\//i)).min(1).max(20000).describe('Actual absolute page URLs, not a sitemap file URL. Preserve path case, escapes and query strings. Complete validates this supplied list, not full site coverage or reachability.'), idempotency_key: IDEMPOTENCY },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
  }, ({ migration_id, urls, ...args }, extra) => {
    if (urls.reduce((bytes, url) => bytes + Buffer.byteLength(url, 'utf8'), 0) > 16777216) {
      return invalidInput('URL text exceeds the 16 MiB paid input limit.');
    }
    return call(extra as ToolExtra, 'POST', `/migrations/${encodeURIComponent(migration_id)}/inventories`, { ...args, rows: urls });
  });

  server.registerTool('run_migration', {
    title: 'Run a migration', description: 'Run URL matching from immutable inventories. When current billing is enabled, the separate free trial allows 500 old URLs; paid purchases or monthly allowances support 15000 old / 20000 new URLs. Payment-required responses provide a browser billing link. Only a human completes checkout. Pass its verified checkout_id for a one-time purchase or select a subscription_id. Never approve extra URLs without explicit user consent to max_extra_amount_cents, the TOTAL extra-usage charge for this month after this run, not an incremental charge. Included retries and refinements do not buy URLs again. confirmed_pairs must be explicitly verified examples, never guesses.',
    inputSchema: { migration_id: UUID, old_inventory_id: UUID, new_inventory_id: UUID, quote_id: UUID.optional(), checkout_id: UUID.optional(), subscription_id: UUID.optional(), approve_extra_urls: z.boolean().optional(), max_extra_amount_cents: z.number().int().min(0).optional(), confirmed_pairs: z.array(z.object({ old_url: z.string().url().max(8192), new_url: z.string().url().max(8192) })).max(100).optional(), idempotency_key: IDEMPOTENCY },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  }, ({ migration_id, old_inventory_id, new_inventory_id, ...args }, extra) => call(extra as ToolExtra, 'POST', `/migrations/${encodeURIComponent(migration_id)}/runs`, { inventory_ids: { old: old_inventory_id, new: new_inventory_id }, ...args }));

  server.registerTool('refine_matches', {
    title: 'Refine Jev matches', description: 'Resume a paused Jev pass, or improve unresolved URL mappings with explicitly confirmed examples. Up to three total passes; does not consume another free migration. Read data.jev.seed_revision from get_migration first. If paused, honor retry_after_seconds and resolve the reported pause reason before resuming.',
    inputSchema: { migration_id: UUID, run_id: UUID, expected_seed_revision: z.number().int().min(0), idempotency_key: IDEMPOTENCY },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  }, ({ migration_id, run_id, ...args }, extra) => call(extra as ToolExtra, 'POST', `/migrations/${encodeURIComponent(migration_id)}/runs/${encodeURIComponent(run_id)}/refine`, args));

  server.registerTool('get_migration', {
    title: 'Get migration status', description: 'Read an owned migration summary and its next recoverable action. Retain migration_id and run_id across reconnects. Poll only after retry_after_seconds; a completed run still requires explicit review before export.',
    inputSchema: { migration_id: UUID, run_id: UUID.optional(), operation_id: UUID.optional() },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: true, idempotentHint: true },
  }, ({ migration_id, run_id, operation_id }, extra) => call(extra as ToolExtra, 'GET', query(`/migrations/${encodeURIComponent(migration_id)}`, { run_id, operation_id })));

  server.registerTool('list_matches', {
    title: 'List migration matches', description: 'Read owned match exceptions in stable traffic order. Follow data.next_cursor until null using the same run and filter; one page is not the whole run. Retain each mapping revision and data.selection_revision for review/export. Traffic absence is distinct from observed zero. Confidence is an uncalibrated model estimate, never approval.',
    inputSchema: { migration_id: UUID, run_id: UUID, filter: z.enum(['all', 'needs_review', 'unmatched', 'approved', 'rejected']).default('all'), cursor: z.string().max(4096).optional(), limit: PAGE },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: true, idempotentHint: true },
  }, ({ migration_id, run_id, filter, cursor, limit }, extra) => call(extra as ToolExtra, 'GET', query(`/migrations/${encodeURIComponent(migration_id)}/runs/${encodeURIComponent(run_id)}/matches`, { filter, cursor, limit })));

  server.registerTool('resolve_matches', {
    title: 'Resolve match exceptions', description: 'Apply audited, optimistic-concurrency decisions. For Jev, confirm a proposed target with set_target, target_url, mapping_id and expected_revision after explicit user approval. Inspect every returned outcome: a batch can partially fail. On revision conflicts, reread matches and reassess the decision. Model predictions are never automatically verified examples.',
    inputSchema: { migration_id: UUID, run_id: UUID, idempotency_key: IDEMPOTENCY, decisions: z.array(z.object({ mapping_id: UUID, expected_revision: z.number().int().min(0), action: z.enum(ACTIONS), target_url: z.string().min(1).max(8192).optional(), rationale: z.string().max(2000).optional() })).min(1).max(100) },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true },
  }, ({ migration_id, run_id, ...args }, extra) => call(extra as ToolExtra, 'PATCH', `/migrations/${encodeURIComponent(migration_id)}/runs/${encodeURIComponent(run_id)}/matches`, args));

  server.registerTool('export_redirects', {
    title: 'Export redirects', description: 'Create an immutable artifact from explicitly approved decisions. Use data.selection_revision from the latest review response as revision. allow_partial requires explicit agreement to exclusions. Export does not install redirects. Large downloads are returned only as backend-authorized artifact resources; this gateway never follows arbitrary URLs.',
    inputSchema: { migration_id: UUID, run_id: UUID, format: z.enum(FORMATS).describe('wordpress is Redirection plugin CSV; cloudflare is Pages _redirects, not Bulk Redirects CSV. csv and json are generic review mappings that need adaptation before installation.'), revision: z.number().int().min(0), allow_partial: z.boolean().default(false), idempotency_key: IDEMPOTENCY },
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true },
  }, ({ migration_id, ...args }, extra) => call(extra as ToolExtra, 'POST', `/migrations/${encodeURIComponent(migration_id)}/artifacts`, args));

  server.registerTool('verify_redirects', {
    title: 'Verify redirects', description: 'Start or resume an owned artifact HTTP check. Supply an existing deployment_id, or explicitly confirm installation with live_origin and origin_rewrites ({} preserves artifact destinations). This does not install rules or establish Google indexing, page canonicals, robots permissions, or sitemap correctness. Inspect incomplete and unchecked results before claiming success.',
    inputSchema: z.strictObject({ migration_id: UUID, artifact_id: UUID, deployment_id: UUID.optional(), deployment_confirmation: z.literal(true).optional(), live_origin: ORIGIN.optional(), origin_rewrites: ORIGIN_REWRITES.optional(), idempotency_key: IDEMPOTENCY }),
    outputSchema: pivotEnvelopeShape,
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  }, ({ migration_id, ...args }, extra) => {
    if (args.deployment_id !== undefined) {
      if (args.deployment_confirmation !== undefined || args.live_origin !== undefined || args.origin_rewrites !== undefined) {
        return invalidInput('Use deployment_id or explicit installation confirmation, not both.');
      }
    } else if (args.deployment_confirmation !== true || args.live_origin === undefined || args.origin_rewrites === undefined) {
      return invalidInput('Supply deployment_id, or deployment_confirmation=true with live_origin and explicit origin_rewrites.');
    }
    return call(extra as ToolExtra, 'POST', `/migrations/${encodeURIComponent(migration_id)}/verifications`, args);
  });
}
