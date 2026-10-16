import { API_BASE_URL, getAuthHeadersMultipart, getAuthHeaders } from './config';
import { handleApiError, toApiError, throwApiErrorFromResponse } from '../utils/errorHandler';

export interface ContentUrlCapError {
  type: 'content_url_cap_exceeded';
  message: string;
  reason_code: 'content_old_url_cap_exceeded' | 'content_new_url_cap_exceeded' | 'content_both_url_caps_exceeded';
  old_url_count: number;
  new_url_count: number;
  max_old_urls: number;
  max_new_urls: number;
  affected_file: 'old' | 'new' | 'both';
  next_action?: string;
  retryable?: boolean;
}

const CONTENT_CAP_REASON_CODES = new Set([
  'content_old_url_cap_exceeded',
  'content_new_url_cap_exceeded',
  'content_both_url_caps_exceeded',
]);

/** A POST may have committed even when its acknowledgment was lost or malformed. */
export class UploadNotConfirmedError extends Error {
  constructor() {
    super('The upload was not confirmed. Check Project History for a new project before submitting these files again.');
    this.name = 'UploadNotConfirmedError';
  }
}

export async function uploadCSVs(oldFile: File, newFile: File, force: boolean = false, pipelineType?: 'content' | 'url_only', signal?: AbortSignal) {
  const formData = new FormData();
  formData.append("old_csv", oldFile);
  formData.append("new_csv", newFile);
  if (force) {
    formData.append("force", "true");
  }
  if (pipelineType) {
    formData.append("pipeline_type", pipelineType);
  }

  try {
    const response = await fetch(`${API_BASE_URL}/api/process`, {
      method: "POST",
      headers: getAuthHeadersMultipart(),
      body: formData,
      signal,
    });

    if (!response.ok) {
      // A native Response body can only be consumed once. Keep the parsed
      // payload for cap handling and the same structured error for other failures.
      const apiError = await toApiError(response);
      const data = apiError.payload ?? {};

      // Handle content URL cap exceeded (422) with structured payload
      if (response.status === 422 && CONTENT_CAP_REASON_CODES.has(String(data.reason_code || ''))) {
        const reasonCode = String(data.reason_code) as ContentUrlCapError['reason_code'];
        const affectedFile =
          data.affected_file === 'old' || data.affected_file === 'new' || data.affected_file === 'both'
            ? data.affected_file
            : reasonCode === 'content_old_url_cap_exceeded'
              ? 'old'
              : reasonCode === 'content_new_url_cap_exceeded'
                ? 'new'
                : 'both';

        const error: ContentUrlCapError = {
          type: 'content_url_cap_exceeded',
          message:
            String(data.user_message || data.error || '').trim() ||
            'Deep Match has a per-file URL limit. Split your CSVs or switch to Quick Match.',
          reason_code: reasonCode,
          old_url_count: Number.isFinite(Number(data.old_url_count)) ? Number(data.old_url_count) : 0,
          new_url_count: Number.isFinite(Number(data.new_url_count)) ? Number(data.new_url_count) : 0,
          max_old_urls: Number.isFinite(Number(data.max_old_urls)) ? Number(data.max_old_urls) : 5000,
          max_new_urls: Number.isFinite(Number(data.max_new_urls)) ? Number(data.max_new_urls) : 5000,
          affected_file: affectedFile,
          next_action: typeof data.next_action === 'string' ? data.next_action : undefined,
          retryable: typeof data.retryable === 'boolean' ? data.retryable : false,
        };
        throw error;
      }

      throw apiError;
    }

    let data;
    try {
      data = await response.json();
    } catch {
      throw new UploadNotConfirmedError();
    }
    // These are the fields /api/process always acknowledges for new and duplicate
    // sessions. Keep the opaque ID unchanged; never infer success from HTTP alone.
    if (data?.success !== true || typeof data.session_id !== 'string' || !data.session_id.trim()
        || typeof data.is_duplicate !== 'boolean') {
      throw new UploadNotConfirmedError();
    }
    return data;
  } catch (error: any) {
    if (error.type === 'content_url_cap_exceeded') {
      throw error;
    }

    // Handle network errors and other fetch failures
    if (error instanceof TypeError || error.name === 'AbortError') {
      throw new UploadNotConfirmedError();
    }

    // Re-throw other errors (like Error objects from our own code)
    throw error;
  }
}

export interface Alternative {
  url: string;
  similarity: number;
  title: string;
  pathSimilarity: number;
}

export async function getAlternatives(sessionId: string, mappingId: string): Promise<{ success: boolean; alternatives: Alternative[]; message?: string }> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/results/${sessionId}/alternatives/${mappingId}`,
      {
        method: "GET",
        headers: getAuthHeaders()
      }
    );

    if (!response.ok) {
      const userError = await handleApiError(null, response);
      throw new Error(userError.message);
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

export async function getResults(sessionId: string, signal?: AbortSignal) {
  const response = await fetch(`${API_BASE_URL}/api/results/${encodeURIComponent(sessionId)}`, {
    headers: getAuthHeaders(), signal,
  });
  if (!response.ok) await throwApiErrorFromResponse(response, 'Unable to load review results.');
  const data = await response.json();
  if (data?.success !== true || !Array.isArray(data.mappings)) throw new Error('The server did not return valid review results.');
  return data;
}

export interface DeepPreviewVisibleItem {
  old_url: string;
  current_target_url: string;
  deep_target_url: string;
  quick_confidence: number;
  deep_confidence: number;
  confidence_gain: number;
  confidence_gain_points: number;
  deep_gap: number;
  quick_match_type: string;
  deep_match_type: string;
  conviction_score: number;
}

export interface DeepPreviewLockedTeaser {
  old_url: string;
  current_target_url: string;
  confidence_gain_points: number;
  deep_confidence: number;
  teaser: string;
}

export interface DeepPreviewResponse {
  success: boolean;
  status: 'queued' | 'processing' | 'completed' | 'failed' | 'skipped' | 'not_applicable';
  reason?: string;
  source_session_id: string;
  free_unlock_count: number;
  total_convincing_fixes: number;
  visible_items: DeepPreviewVisibleItem[];
  locked_teasers: DeepPreviewLockedTeaser[];
  error_message?: string | null;
  headline: string;
  subheadline: string;
  cta_primary: string;
  cta_secondary: string;
  lock_overlay?: string;
}

export async function getDeepPreview(sessionId: string, signal?: AbortSignal): Promise<DeepPreviewResponse> {
  const response = await fetch(`${API_BASE_URL}/api/results/${encodeURIComponent(sessionId)}/deep-preview`, {
    headers: getAuthHeaders(), signal,
  });
  if (!response.ok) await throwApiErrorFromResponse(response, 'Unable to load review results.');
  const data = await response.json();
  if (data?.success !== true) throw new Error('The server did not return valid review results.');
  return data;
}

export interface ReviewDecision {
  /** Exact saved target before changing a decision or target; null is unresolved. */
  expected_new_url?: string | null;
  approved?: boolean;
  new_url?: string;
  match_type?: string;
  /** 0..1, matching the backend's confidence_score scale. */
  confidence?: number;
  clear_repair?: boolean;
}

/**
 * Persist a review decision (approve, edit, accepted repair).
 *
 * Review used to live only in React state, which made needs_review in the
 * database a fiction: the watch monitored the wrong rows and the v1 export
 * could disagree with the file the browser built. The UI waits for acknowledgment before changing the exportable decision.
 */
export async function persistReviewDecision(
  sessionId: string,
  mappingId: string,
  decision: ReviewDecision
): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error('Save not confirmed after 20 seconds. Reload results to check the server state.')), 20000);
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/results/${encodeURIComponent(sessionId)}/mappings/${encodeURIComponent(mappingId)}`,
      {
        method: 'PATCH',
        signal: controller.signal,
        headers: getAuthHeaders(),
        body: JSON.stringify(decision),
      }
    );
    if (!response.ok) {
      await throwApiErrorFromResponse(response, 'The review save was not confirmed.');
    }
    const data = await response.json();
    if (data?.success !== true) throw new Error('The server did not confirm the review save. Reload results before exporting.');
  } finally {
    clearTimeout(timer);
  }
}
