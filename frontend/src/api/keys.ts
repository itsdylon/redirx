import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError, throwApiErrorFromResponse } from '../utils/errorHandler';

export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

/**
 * A newly minted key. `key` is the full plaintext and exists only in this
 * response — the server stores a hash and cannot show it again.
 */
export interface CreatedApiKey {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  key: string;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, init);
    if (!response.ok) {
      await throwApiErrorFromResponse(response, 'Unable to update API keys. Reload the list to check the current state.');
    }
    return await response.json();
  } catch (error: any) {
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }
    throw error;
  }
}

export async function listApiKeys(): Promise<ApiKey[]> {
  const data = await request<{ success: boolean; keys: ApiKey[] }>('/api/keys', {
    method: 'GET',
    headers: getAuthHeaders(),
  });
  if (data.success !== true || !Array.isArray(data.keys) || !data.keys.every(validMetadata)) {
    throw new Error('The key list could not be verified. Reload it to try again.');
  }
  return data.keys.map(({ id, name, key_prefix, created_at, last_used_at, revoked_at }) =>
    ({ id, name, key_prefix, created_at, last_used_at, revoked_at }));
}

export async function createApiKey(name: string): Promise<CreatedApiKey> {
  const data = await request<{ success: boolean; key: CreatedApiKey }>('/api/keys', {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify({ name }),
  });
  if (data.success !== true || !validMetadata(data.key) || typeof data.key.key !== 'string' || !data.key.key.startsWith('rdx_')) {
    throw new Error('Key creation returned an incomplete response. Reload the key list before creating another; the key may already exist.');
  }
  return data.key;
}

export async function revokeApiKey(keyId: string): Promise<void> {
  const result = await request<{ success: boolean }>(`/api/keys/${encodeURIComponent(keyId)}`, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });
  if (result.success !== true) throw new Error('Deletion was not confirmed. Reload the key list to check its current state.');
}

function validMetadata(key: unknown): key is ApiKey {
  if (!key || typeof key !== 'object') return false;
  const value = key as Record<string, unknown>;
  return ['id', 'name', 'key_prefix', 'created_at'].every(field => typeof value[field] === 'string' && !!value[field]);
}

/** Whether a key is still usable. Revoked rows are kept for their audit trail. */
export function isActive(key: ApiKey): boolean {
  return !key.revoked_at;
}
