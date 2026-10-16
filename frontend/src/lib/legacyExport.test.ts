import { afterEach, describe, expect, it, vi } from 'vitest';
import { normalizeLegacyExportFormat, startTextDownload } from './legacyExport';
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });
describe('historical export identity and download', () => {
  it('normalizes the historical settings alias and rejects unknown formats', () => {
    expect(normalizeLegacyExportFormat('htaccess')).toBe('apache');
    expect(normalizeLegacyExportFormat('not-real')).toBe('');
  });
  it('starts one download and cleans the anchor and blob URL after the browser can consume it', () => {
    vi.useFakeTimers();
    URL.createObjectURL = vi.fn().mockReturnValue('blob:fixture');
    URL.revokeObjectURL = vi.fn();
    let download = '';
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function(this: HTMLAnchorElement) { download = this.download; expect(this.isConnected).toBe(true); });
    startTextDownload('Redirect 301 /a /b', 'redirects.htaccess');
    expect(download).toBe('redirects.htaccess');
    expect(document.querySelector('a[download]')).toBeNull();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.runAllTimers(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fixture');
  });
  it('cleans up when the browser rejects the initiation and reports failure', () => {
    vi.useFakeTimers(); URL.createObjectURL = vi.fn().mockReturnValue('blob:failed'); URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new DOMException('Blocked', 'SecurityError'); });
    expect(() => startTextDownload('rules', 'rules.txt')).toThrow('Blocked');
    expect(document.querySelector('a[download]')).toBeNull();
    vi.runAllTimers(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:failed');
  });
});
