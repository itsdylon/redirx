import type { AuthInfo } from '@modelcontextprotocol/sdk/server/auth/types.js';
import { McpError } from '@modelcontextprotocol/sdk/types.js';
import { resolveIdentity } from '../auth/identity.js';
import type { VerifiedIdentity } from '../auth/types.js';
import { RedirxClient } from '../backend/redirxClient.js';

/**
 * Every tool callback receives this shape as its `extra` — see mcpServer.ts,
 * which is the only place `authInfo` is guaranteed present (requireBearerAuth
 * already ran ahead of the MCP endpoint in 'oauth' mode; in 'dev' mode the
 * same middleware runs with DevApiKeyAdapter). A tool never sees a request
 * that skipped auth entirely.
 */
export interface ToolExtra {
  authInfo?: AuthInfo & { extra?: VerifiedIdentity };
  _meta?: Record<string, unknown>;
  signal?: AbortSignal;
}

/** Resolves the caller's Redirx identity and hands back a client already carrying their API key. */
export async function clientForCall(extra: ToolExtra): Promise<{ client: RedirxClient; userId: string }> {
  const subject = extra.authInfo?.extra?.subject;
  if (!subject) {
    // Should be unreachable — requireBearerAuth rejects unauthenticated
    // requests before a tool callback ever runs. A thrown McpError here is
    // the honest failure mode if that assumption is ever violated, not a
    // silent empty result.
    throw new McpError(-32001, 'No verified identity on this request.');
  }

  const resolved = await resolveIdentity(
    { subject, email: extra.authInfo?.extra?.email },
    extra.authInfo?.token ?? '',
  );
  return { client: new RedirxClient(resolved.apiKey), userId: resolved.userId };
}
