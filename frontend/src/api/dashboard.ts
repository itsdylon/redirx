import { API_BASE_URL, getAuthHeaders } from './config';
import { handleApiError } from '../utils/errorHandler';

export interface DashboardData {
  success: boolean;
  total_redirects: number;
  total_sessions: number;
  approval_progress: number;
  average_confidence: number;
  confidence_breakdown?: {
    high: number;
    medium: number;
    low: number;
    exact: number;
  };
  recent_sessions: Array<{
    id: string;
    project_name: string;
    created_at: string;
    total_mappings: number;
    approved_mappings: number;
    status: string;
    pipeline_type?: 'content' | 'url_only' | string;
    source_session_id?: string | null;
  }>;
}

export async function fetchDashboardData(signal?: AbortSignal): Promise<DashboardData> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/user/dashboard`, {
      method: 'GET', signal,
      headers: getAuthHeaders(),
    });

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
    }

    const data = await response.json();
    if (data.success !== true || !Array.isArray(data.recent_sessions)) throw new Error('Dashboard data was incomplete. Try loading it again.');
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
