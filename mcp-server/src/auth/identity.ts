import { config } from '../config.js';
import type { VerifiedIdentity } from './types.js';

export interface ResolvedIdentity {
  userId: string;
  apiKey: string;
  plan: string;
  gscConnected: boolean;
}

interface CacheEntry {
  value: ResolvedIdentity;
  expiresAt: number;
}

const cache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<ResolvedIdentity>>();
const EXPIRY_SKEW_MS = 5_000;
export const IDENTITY_REQUEST_TIMEOUT_MS = 10_000;

async function identityJson(path: string, init: RequestInit): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), IDENTITY_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${config.backendBaseUrl}${path}`, { ...init, signal: controller.signal });
    if (!response.ok) throw new Error(`Identity resolution failed (${response.status}). Please retry the connection.`);
    const body: unknown = await response.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('Invalid identity response.');
    return body as Record<string, unknown>;
  } catch (error) {
    if (controller.signal.aborted) throw new Error('Identity resolution timed out. Please retry the connection.');
    // Never include upstream response bodies, URLs or transport exception text.
    if (error instanceof Error && /^Identity resolution failed \(\d+\)\./.test(error.message)) throw error;
    throw new Error('Identity resolution is unavailable. Please retry the connection.');
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Turn a verified identity into `{userId, apiKey, plan}` — the thing every
 * tool actually needs to call `/api/v1/*` on the caller's behalf.
 *
 * Two paths, matching config.authMode:
 *  - 'oauth': calls the backend's /api/internal/mcp/resolve (the seam
 *    described in agentic-pivot.md §5, Task 5) with the subject the
 *    AuthorizationServerAdapter verified. That endpoint mints a short-lived
 *    signed delegation accepted only by backend v1.
 *  - 'dev': the presented bearer token already IS a Redirx API key
 *    (DevApiKeyAdapter only accepts rdx_... tokens) — reusing it directly
 *    avoids silently revoking a developer's own key via /resolve's rotation,
 *    which would be a surprising side effect of just calling a tool.
 */
export async function resolveIdentity(
  identity: VerifiedIdentity,
  rawToken: string,
): Promise<ResolvedIdentity> {
  // In dev mode the verified subject is not enough: two different valid
  // developer keys may represent the same user, and caching by subject would
  // send one caller's key on another caller's request.
  const cacheKey = config.authMode === 'dev'
    ? `dev:${identity.subject}:${rawToken}`
    : `oauth:${identity.subject}`;
  const cached = cache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) {
    return cached.value;
  }
  cache.delete(cacheKey);

  const pending = inflight.get(cacheKey);
  if (pending) return pending;

  const resolution = (async () => {
    const result = config.authMode === 'dev'
      ? await resolveViaDevToken(identity.subject, rawToken)
      : await resolveViaBackend(identity);
    cache.set(cacheKey, result.entry);
    return result.value;
  })();
  inflight.set(cacheKey, resolution);
  try {
    return await resolution;
  } finally {
    inflight.delete(cacheKey);
  }
}

interface ResolutionWithCache {
  value: ResolvedIdentity;
  entry: CacheEntry;
}

async function resolveViaBackend(identity: VerifiedIdentity): Promise<ResolutionWithCache> {
  // Resolution is shared by same-subject callers, so a caller cancellation
  // must not cancel other tools. This shared request has its own finite deadline.
  const body = await identityJson('/api/internal/mcp/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Internal-Secret': config.internalSecret },
    body: JSON.stringify({ subject: identity.subject, email: identity.email }),
  });
  if (body.user_id !== identity.subject || typeof body.api_key !== 'string' || !body.api_key
      || typeof body.plan !== 'string' || typeof body.gsc_connected !== 'boolean'
      || typeof body.expires_at !== 'number') {
    throw new Error('Identity resolution returned an invalid delegation.');
  }

  const expiresAt = Number(body.expires_at) * 1000 - EXPIRY_SKEW_MS;
  if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    throw new Error('Identity resolution returned an expired delegation');
  }
  const value = {
    userId: body.user_id, apiKey: body.api_key, plan: body.plan, gscConnected: body.gsc_connected,
  };
  return { value, entry: { value, expiresAt } };
}

async function resolveViaDevToken(subject: string, rawToken: string): Promise<ResolutionWithCache> {
  const body = await identityJson('/api/v1/me', {
    headers: { Authorization: `Bearer ${rawToken}` },
  });
  if (typeof body.plan !== 'string') throw new Error('Invalid developer identity response.');
  const value = { userId: subject, apiKey: rawToken, plan: body.plan, gscConnected: false };
  // Dev keys do not carry an expiry. Cache only briefly, and never share a
  // cache entry between distinct caller-provided keys.
  return { value, entry: { value, expiresAt: Date.now() + 60_000 } };
}

/** Test-only: clears the module-level identity cache between test cases. */
export function _resetIdentityCacheForTests(): void {
  cache.clear();
  inflight.clear();
}
