import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import * as z from 'zod';
import { config } from '../config.js';
import {
  attachReceipt,
  buildExportPaymentChallenge,
  decodeOpaque,
  extractCredential,
  throwPaymentRequired,
} from '../payments/mpp.js';
import { clientForCall, type ToolExtra } from './context.js';

/**
 * The one paid tool. The description leads with "paid" and "full quality
 * already ran for free" precisely to reinforce deep_match's own description —
 * an agent should never conclude "export failing" means "the match was bad,"
 * only "payment is needed."
 */
const DESCRIPTION = `Get the deploy-ready redirect file for a completed deep_match run — the actual artifact you install on the old server (.htaccess, nginx config, vercel.json, etc.).

This is what's paid for; deep_match itself already ran free at full quality. If payment is required you'll get a structured Payment Required response (MPP, JSON-RPC -32042) with a checkout URL — that page must be opened by a human in a browser; you cannot complete payment yourself. Once paid, call export again with the SAME arguments (or with the "opaque" value echoed back) and it will succeed — no separate "confirm payment" step exists, this tool re-checks entitlement itself.

Formats: apache, nginx, wordpress, vercel, cloudflare, shopify, csv, json.`;

const FORMATS = ['apache', 'nginx', 'wordpress', 'vercel', 'cloudflare', 'shopify', 'csv', 'json'] as const;

export function registerExportTool(server: McpServer): void {
  server.registerTool(
    'export',
    {
      title: 'Export redirect file',
      description: DESCRIPTION,
      inputSchema: {
        migration_id: z.string().describe('The migration_id from deep_match.'),
        format: z.enum(FORMATS).default('csv').describe('Target format: wordpress is Redirection plugin CSV; cloudflare is Pages _redirects. Generic csv/json are review mappings that need adaptation before installation.'),
        url_format: z
          .enum(['paths', 'full'])
          .default('paths')
          .describe("'paths' (default) for platforms that match on request path — almost always what you want. 'full' only if your target explicitly requires absolute URLs."),
        min_confidence: z.number().min(0).max(1).default(0).describe('Drop matches below this confidence (0-1). Default 0 includes everything deep_match found.'),
        opaque: z
          .string()
          .optional()
          .describe('Echo back the "opaque" value from a prior Payment Required response, if you have one. Optional — retrying with the same migration_id works too.'),
      },
      annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: false },
    },
    async ({ migration_id, format, url_format, min_confidence, opaque }, extra) => {
      const { client } = await clientForCall(extra as ToolExtra);

      // A client that only echoes `opaque` back (rather than re-passing
      // format/url_format/min_confidence explicitly) still gets the exact
      // export it originally asked for — opaque is authoritative over the
      // schema's defaults whenever both are present, exactly what MPP's
      // "clients must echo opaque back unchanged" is for.
      const resumed = opaque ? decodeOpaque(opaque) : null;
      const effective = {
        format: resumed?.format ?? format,
        urlFormat: resumed?.urlFormat ?? url_format,
        minConfidence: resumed?.minConfidence ?? min_confidence,
      };

      const result = await client.getExport(migration_id, effective);

      if (result.ok) {
        // A Credential in _meta means this call is fulfilling a prior
        // Payment Required challenge (protocol-faithful client behavior).
        // We don't need it to decide anything — v1's own re-check is what
        // actually gated this response — but a client that did the work of
        // presenting one gets an explicit Receipt back, per MPP shape.
        const credential = extractCredential((extra as ToolExtra)._meta);
        const meta = credential
          ? attachReceipt(undefined, { status: 'paid', migrationId: migration_id })
          : undefined;
        return {
          content: [
            { type: 'text', text: `${result.redirectCount} redirects, ${result.filename}:` },
            { type: 'text', text: result.content },
          ],
          ...(meta ? { _meta: meta } : {}),
        };
      }

      if (result.error.status === 402) {
        const upgradeUrl = String(result.error.body.upgrade_url ?? `${config.backendBaseUrl}`);
        const challenge = buildExportPaymentChallenge({
          migrationId: migration_id,
          format: effective.format,
          urlFormat: effective.urlFormat,
          minConfidence: effective.minConfidence,
          realm: new URL(config.publicUrl).host,
          checkoutUrl: upgradeUrl,
          description: result.error.message,
        });
        // Never reached with a normal return — this throws an McpError the
        // SDK serializes as the JSON-RPC error envelope (code -32042), not a
        // CallToolResult with isError. See payments/mpp.ts for why that
        // matters: MPP's error must be a transport-level JSON-RPC error, not
        // content the agent has to parse out of a successful-looking result.
        throwPaymentRequired(challenge);
      }

      return {
        isError: true,
        content: [{ type: 'text', text: `${result.error.code}: ${result.error.message}` }],
      };
    },
  );
}
