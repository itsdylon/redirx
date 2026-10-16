import { useEffect, useMemo, useRef, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog';
import { Button } from './ui/button';
import { Checkbox } from './ui/checkbox';
import { Label } from './ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Alert, AlertDescription } from './ui/alert';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from './ui/collapsible';
import { AlertTriangle, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { previewExport, fetchExportFile, type ExportOptions, type ExportPreview } from '../api/exportAPI';
import { normalizeLegacyExportFormat, startTextDownload } from '../lib/legacyExport';
import type { RedirectMapping } from './ReviewInterface';

interface ExportModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onExport: (format: string, confidenceLevels: string[]) => void | Promise<void>;
  redirects: RedirectMapping[];
  sessionId: string;
}

export function ExportModal({ open, onOpenChange, onExport, redirects, sessionId }: ExportModalProps) {
  const [format, setFormat] = useState(() => normalizeLegacyExportFormat(localStorage.getItem('redirx_default_export_format')));
  const [includeHigh, setIncludeHigh] = useState(() => localStorage.getItem('redirx_default_confidence_high') !== 'false');
  const [includeMedium, setIncludeMedium] = useState(() => localStorage.getItem('redirx_default_confidence_medium') !== 'false');
  const [includeLow, setIncludeLow] = useState(() => localStorage.getItem('redirx_default_confidence_low') === 'true');
  const [urlFormat, setUrlFormat] = useState<'paths' | 'full' | 'custom'>(() => localStorage.getItem('redirx_default_url_format') === 'full' ? 'full' : 'paths');
  const [customOldDomain, setCustomOldDomain] = useState('');
  const [customNewDomain, setCustomNewDomain] = useState('');
  const [previewExpanded, setPreviewExpanded] = useState(false);
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const active = useRef(true);
  const downloadLock = useRef(false);
  const downloadController = useRef<AbortController | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const options = useMemo<ExportOptions>(() => ({ format, urlFormat,
    confidenceLevels: [...(includeHigh ? ['high'] : []), ...(includeMedium ? ['medium'] : []), ...(includeLow ? ['low'] : [])],
    oldDomain: customOldDomain, newDomain: customNewDomain,
  }), [format, urlFormat, includeHigh, includeMedium, includeLow, customOldDomain, customNewDomain]);
  useEffect(() => { active.current = true; return () => { active.current = false; downloadController.current?.abort(); }; }, []);
  useEffect(() => {
    setPreview(null); setError(null);
    if (!open || !format || !options.confidenceLevels.length) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(() => {
      void previewExport(sessionId, options, controller.signal).then(data => {
        if (!controller.signal.aborted) setPreview(data);
      }).catch(error => {
        if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Preview could not load. Try again.');
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [open, sessionId, options, refresh]);

  async function handleDownload() {
    if (!preview || !preview.included_count || downloadLock.current || loading) return;
    downloadLock.current = true; setIsDownloading(true); setError(null);
    const controller = new AbortController(); downloadController.current = controller;
    try {
      const content = await fetchExportFile(sessionId, options, controller.signal);
      if (!active.current || controller.signal.aborted) return;
      if (content !== preview.content) {
        setPreview(null);
        setError('The saved redirects changed after this preview. Refresh the preview and review it before downloading.');
        return;
      }
      startTextDownload(content, preview.filename);
      toast.success(`Download started: ${preview.filename}`);
      onOpenChange(false);
      void Promise.resolve().then(() => { if (active.current) return onExport(format, options.confidenceLevels); }).catch(() => {
        if (active.current) toast.info('Download started, but progress could not be updated.');
      });
    } catch (error) {
      if (active.current && !controller.signal.aborted) setError(error instanceof Error ? error.message : 'Download could not start. Try again.');
    } finally {
      downloadLock.current = false;
      if (active.current) setIsDownloading(false);
    }
  }
  const requestedRows = redirects.filter(row => options.confidenceLevels.includes(row.confidenceBand));
  const unapproved = requestedRows.filter(row => !row.approved).length;
  const duplicateTargets = new Set(requestedRows.filter((row,index,rows) => rows.findIndex(other => other.newUrl === row.newUrl) !== index).map(row => row.newUrl)).size;
  const validSelection = !!format && !!options.confidenceLevels.length;
  return <Dialog open={open} onOpenChange={value => { if (!downloadLock.current) onOpenChange(value); }}>
    <DialogContent className="sm:max-w-4xl max-h-[90vh] flex flex-col overflow-hidden"
      onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement; }}
      onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus(); }}>
      <div className="flex-1 overflow-y-auto min-h-0">
        <DialogHeader><DialogTitle>Export Redirects</DialogTitle><DialogDescription>Preview saved redirects after the server’s review and export checks, then download the same rules.</DialogDescription></DialogHeader>
        <fieldset disabled={isDownloading} className="space-y-6 py-4 min-w-0">
          <div><Label htmlFor="export-format" className="mb-2 block">Export Format</Label>
            <Select value={format} onValueChange={setFormat} disabled={isDownloading}><SelectTrigger id="export-format"><SelectValue placeholder="Select Format" /></SelectTrigger>
              <SelectContent><SelectItem value="apache">Apache .htaccess</SelectItem><SelectItem value="nginx">Nginx server rules</SelectItem><SelectItem value="wordpress">WordPress Redirection CSV</SelectItem><SelectItem value="vercel">Vercel redirects (vercel.json)</SelectItem><SelectItem value="cloudflare">Cloudflare Pages (_redirects)</SelectItem><SelectItem value="shopify">Shopify CSV</SelectItem><SelectItem value="csv">Review CSV</SelectItem><SelectItem value="json">Review JSON</SelectItem></SelectContent>
            </Select>
          </div>
          <fieldset className="space-y-3"><legend className="mb-3 text-sm font-medium">URL Format</legend>
            <label className="flex items-start gap-3 text-sm"><input type="radio" name="urlFormat" checked={urlFormat==='paths'} onChange={()=>setUrlFormat('paths')} className="mt-1" /><span>Paths only (Recommended)<span className="block text-xs text-muted-foreground">Sources use paths; destinations keep another origin when needed.</span></span></label>
            <label className="flex items-start gap-3 text-sm"><input type="radio" name="urlFormat" checked={urlFormat==='full'} onChange={()=>setUrlFormat('full')} className="mt-1" /><span>Full URLs from CSV<span className="block text-xs text-muted-foreground">Keep original URLs in supported review and import formats.</span></span></label>
            <label className="flex items-start gap-3 text-sm"><input type="radio" name="urlFormat" checked={urlFormat==='custom'} onChange={()=>setUrlFormat('custom')} className="mt-1" /><span>Custom domains</span></label>
            {urlFormat==='custom' && <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-1 text-sm">Old domain<input type="url" value={customOldDomain} onChange={event=>setCustomOldDomain(event.target.value)} placeholder="https://old.example" className="block w-full rounded-md border bg-background p-2" /></label>
              <label className="space-y-1 text-sm">New domain<input type="url" value={customNewDomain} onChange={event=>setCustomNewDomain(event.target.value)} placeholder="https://new.example" className="block w-full rounded-md border bg-background p-2" /></label>
            </div>}
          </fieldset>
          <fieldset className="space-y-3"><legend className="mb-3 text-sm font-medium">Confidence levels</legend>
            {([['high',includeHigh,setIncludeHigh],['medium',includeMedium,setIncludeMedium],['low',includeLow,setIncludeLow]] as const).map(([level,checked,setChecked]) => <div className="flex items-center gap-2" key={level}><Checkbox id={`export-${level}`} checked={checked} onCheckedChange={value=>setChecked(value===true)} /><Label htmlFor={`export-${level}`} className="capitalize">{level} ({redirects.filter(row=>row.confidenceBand===level).length})</Label></div>)}
          </fieldset>
          {unapproved>0 && <Alert><AlertTriangle className="h-4 w-4" /><AlertDescription>{unapproved} redirect{unapproved===1?' awaits':'s await'} review. Close export to review them; the server preview below shows which rows are excluded.</AlertDescription></Alert>}
          {duplicateTargets>0 && <p className="text-sm text-muted-foreground">{duplicateTargets} destination{duplicateTargets===1?' is':'s are'} shared by multiple selected sources. Check that each source has an appropriate destination.</p>}
          {preview && <div className="space-y-2 text-sm" role="status"><p>{preview.included_count} redirect{preview.included_count===1?'':'s'} included. {preview.excluded_count} excluded by export checks.</p>{!!preview.excluded.length && <details><summary className="cursor-pointer">View excluded rows</summary><ul className="mt-2 space-y-1">{preview.excluded.map((item,index)=><li key={index} className="break-all">{redirects.find(row=>row.id===item.mapping_id)?.oldUrl || item.mapping_id || 'Row'} — {item.reason.replaceAll('_',' ')}</li>)}</ul></details>}{preview.warning && <p>{preview.warning}</p>}</div>}
          {loading && <p role="status" className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />Preparing saved redirect preview…</p>}
          {!options.confidenceLevels.length && <p className="text-sm">Select at least one confidence level.</p>}
          <Collapsible open={previewExpanded} onOpenChange={setPreviewExpanded}>
            <CollapsibleTrigger className="flex w-full items-center gap-2 border border-border bg-card p-3 text-left">{previewExpanded?<ChevronDown className="h-4 w-4" />:<ChevronRight className="h-4 w-4" />}Preview Rules</CollapsibleTrigger>
            <CollapsibleContent className="border border-t-0 p-3"><pre tabIndex={0} role="region" aria-label="Redirect rules preview" className="max-h-[250px] overflow-auto text-xs font-mono whitespace-pre focus-visible:outline-2 focus-visible:outline-ring">{preview?.content.slice(0,12000) || (format?'No preview available.':'Select a format to preview rules')}</pre>{preview && <p className="mt-3 text-xs text-muted-foreground">{preview.content.length>12000?'Preview shortened; the file contains all included rules. ':''}Generated by the server. Test rules before installing.</p>}</CollapsibleContent>
          </Collapsible>
        </fieldset>
      </div>
      {error && <div role="alert" className="space-y-2 text-sm text-destructive"><p>{error}</p>{validSelection && <Button variant="outline" disabled={loading||isDownloading} onClick={()=>setRefresh(value=>value+1)}>Refresh preview</Button>}</div>}
      <DialogFooter className="gap-3 border-t pt-4"><Button variant="outline" disabled={isDownloading} onClick={()=>onOpenChange(false)}>Cancel</Button><Button disabled={!preview?.included_count||loading||isDownloading||!!error} onClick={()=>void handleDownload()}>{isDownloading?'Preparing download…':'Download File'}</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}
