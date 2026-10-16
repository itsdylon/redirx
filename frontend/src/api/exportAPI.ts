import { API_BASE_URL, getAuthHeaders } from './config';
import { throwApiErrorFromResponse } from '../utils/errorHandler';

export interface ExportOptions {
  format: string;
  confidenceLevels: string[];
  urlFormat: 'paths' | 'full' | 'custom';
  oldDomain?: string;
  newDomain?: string;
}
export interface ExportPreview {
  success: true;
  content: string;
  filename: string;
  content_type: string;
  included_count: number;
  excluded_count: number;
  excluded: {mapping_id: string | null; reason: string}[];
  warning: string | null;
}
async function requestExport(sessionId: string, options: ExportOptions, preview: boolean, signal?: AbortSignal) {
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, {once:true});
  const timer = setTimeout(() => controller.abort(new Error('Export preparation timed out. Try again.')), 20000);
  try {
    const params = new URLSearchParams({format:options.format, confidence_levels:options.confidenceLevels.join(','), url_format:options.urlFormat});
    if (preview) params.set('preview','true');
    if (options.urlFormat === 'custom') {
      if (options.oldDomain) params.set('old_domain',options.oldDomain);
      if (options.newDomain) params.set('new_domain',options.newDomain);
    }
    const response = await fetch(`${API_BASE_URL}/api/results/${encodeURIComponent(sessionId)}/export?${params}`, {headers:getAuthHeaders(),signal:controller.signal,cache:'no-store'});
    if (!response.ok) await throwApiErrorFromResponse(response, 'Export could not be prepared.');
    if (preview) {
      const data = await response.json();
      if (data?.success !== true || typeof data.content !== 'string' || typeof data.filename !== 'string' || !/^[\w.-]+$/.test(data.filename) || !Number.isInteger(data.included_count) || data.included_count < 0 || !Number.isInteger(data.excluded_count) || data.excluded_count < 0 || typeof data.content_type !== 'string' || !Array.isArray(data.excluded)) throw new Error('The server did not return a valid export preview.');
      return data as ExportPreview;
    }
    const type = response.headers.get('content-type') || '';
    if (!/^(text\/(plain|csv)|application\/json)\b/.test(type)) throw new Error('The server did not return an export file.');
    return await response.text();
  } finally {
    clearTimeout(timer); signal?.removeEventListener('abort',abort);
  }
}
export const previewExport = (sessionId:string, options:ExportOptions, signal?:AbortSignal) => requestExport(sessionId,options,true,signal) as Promise<ExportPreview>;
export const fetchExportFile = (sessionId:string, options:ExportOptions, signal?:AbortSignal) => requestExport(sessionId,options,false,signal) as Promise<string>;
