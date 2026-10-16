import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError, toApiError } from '../utils/errorHandler';

export interface SessionStatus {
  success: boolean;
  session_id: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  project_name: string;
  total_mappings: number;
  current_stage?: number | null;
  stage_name?: string | null;
  total_stages?: number | null;
}

export interface MigrationSession {
  id: string;
  project_name: string;
  created_at: string;
  total_mappings: number;
  approved_mappings: number;
  status: string;
  pipeline_type?: 'content' | 'url_only' | string;
  source_session_id?: string | null;
}

export interface SessionListResponse {
  success: boolean;
  sessions: MigrationSession[];
}

export async function getSessionStatus(sessionId: string): Promise<SessionStatus> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/user/sessions/${sessionId}/status`, {
      method: 'GET',
      headers: getAuthHeaders(),
    });

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    return await response.json();
  } catch (error: any) {
    // Handle network errors and other fetch failures
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }

    // Re-throw other errors
    throw error;
  }
}

export async function updateSessionName(sessionId: string, projectName: string, signal?: AbortSignal): Promise<any> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/user/sessions/${sessionId}`, {
      method: 'PUT', signal,
      headers: getAuthHeaders(),
      body: JSON.stringify({ project_name: projectName }),
    });

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    const data = await response.json();
    if (data.success !== true || data.session?.id !== sessionId || data.session?.project_name !== projectName) throw new Error('The server did not confirm the request. Refresh before trying again.');
    return data;
  } catch (error: any) {
    // Handle network errors and other fetch failures
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }

    // Re-throw other errors
    throw error;
  }
}

export async function deleteSession(sessionId: string, signal?: AbortSignal): Promise<any> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/user/sessions/${sessionId}`, {
      method: 'DELETE', signal,
      headers: getAuthHeaders(),
    });

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    const data = await response.json();
    if (data.success !== true) throw new Error('The server did not confirm the request. Refresh before trying again.');
    return data;
  } catch (error: any) {
    // Handle network errors and other fetch failures
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }

    // Re-throw other errors
    throw error;
  }
}

/** Compatibility only: current screens should use fetchSessionPage. */
export async function fetchAllSessions(signal?: AbortSignal): Promise<SessionListResponse> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/user/sessions`, {
      method: 'GET', signal,
      headers: getAuthHeaders(),
    });

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    const data = await response.json();
    if (data.success !== true || !Array.isArray(data.sessions)) throw new Error('The server did not confirm the request. Refresh before trying again.');
    return data;
  } catch (error: any) {
    // Handle network errors and other fetch failures
    if (error instanceof TypeError || error.name === 'AbortError') {
      const userError = await handleApiError(error);
      throw new Error(userError.message);
    }

    // Re-throw other errors
    throw error;
  }
}


export interface SessionPageResponse extends SessionListResponse {
  pagination: 'cursor';
  total_count: number;
  next_cursor: string | null;
  search: string;
}

/** Cursor is opaque: preserve its timestamp precision by passing it unchanged. */
export async function fetchSessionPage({ search = '', cursor = null, limit = 50 }: {
  search?: string; cursor?: string | null; limit?: number;
} = {}, signal?: AbortSignal): Promise<SessionPageResponse> {
  const parameters = new URLSearchParams({ pagination: 'cursor', limit: String(limit), q: search.trim() });
  if (cursor !== null) parameters.set('cursor', cursor);
  const response = await fetch(`${API_BASE_URL}/api/user/sessions?${parameters}`, {
    method: 'GET', signal, headers: getAuthHeaders(),
  });
  if (!response.ok) throw await toApiError(response);
  const data = await response.json();
  if (!data || typeof data !== 'object' || data.success !== true || data.pagination !== 'cursor' || data.search !== search.trim() ||
      !Array.isArray(data.sessions) || data.sessions.length > limit ||
      !Number.isSafeInteger(data.total_count) || data.total_count < data.sessions.length ||
      !(data.next_cursor === null || (typeof data.next_cursor === 'string' && data.next_cursor.length > 0)) ||
      (data.next_cursor !== null && data.sessions.length !== limit) ||
      data.sessions.some((row: MigrationSession) => !row || typeof row.id !== 'string' || typeof row.status !== 'string' ||
        !(row.project_name === null || typeof row.project_name === 'string') ||
        !(row.created_at === null || typeof row.created_at === 'string') ||
        !Number.isSafeInteger(row.total_mappings) || row.total_mappings < 0 ||
        !Number.isSafeInteger(row.approved_mappings) || row.approved_mappings < 0 ||
        typeof row.pipeline_type !== 'string' ||
        !(row.source_session_id === null || typeof row.source_session_id === 'string'))) {
    throw new Error('Project history was incomplete. Reload the first page to try again.');
  }
  return data;
}
