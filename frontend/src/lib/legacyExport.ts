export const legacyExportFormats = ['apache', 'nginx', 'wordpress', 'vercel', 'cloudflare', 'shopify', 'csv', 'json'] as const;
export function normalizeLegacyExportFormat(value: string | null): string {
  const format = value === 'htaccess' ? 'apache' : value;
  return legacyExportFormats.includes(format as typeof legacyExportFormats[number]) ? format! : '';
}

/** Browser initiation is observable; completion or installation is not. */
export function startTextDownload(content: string, fileName: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  try {
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    // Let the browser consume the object URL before releasing it.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
