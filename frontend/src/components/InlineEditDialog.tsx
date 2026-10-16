import { useState, useEffect, useRef } from 'react';
import { CheckCircle2, AlertCircle, Loader2 } from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from './ui/sheet';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Separator } from './ui/separator';
import { RedirectMapping } from './ReviewInterface';
import { validateUrl } from '../utils/validation';
import { getAlternatives, Alternative } from '../api/pipeline';

/**
 * Animated number that counts up from 0 to the target value with an ease-out curve.
 */
function AnimatedNumber({ value, duration = 500, delay = 0 }: { value: number; duration?: number; delay?: number }) {
  const [display, setDisplay] = useState(0);

  useEffect(() => {
    let rafId: number;
    const timeoutId = setTimeout(() => {
      const start = performance.now();
      const tick = () => {
        const elapsed = performance.now() - start;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        setDisplay(Math.round(value * eased));
        if (progress < 1) rafId = requestAnimationFrame(tick);
      };
      rafId = requestAnimationFrame(tick);
    }, delay);

    return () => {
      clearTimeout(timeoutId);
      if (rafId) cancelAnimationFrame(rafId);
    };
  }, [value, duration, delay]);

  return <>{display}</>;
}

interface InlineEditDialogProps {
  redirect: RedirectMapping;
  sessionId: string;
  onSave: (redirect: RedirectMapping) => void | Promise<void>;
  onCancel: () => void;
  pipelineType?: string;
}

export function InlineEditDialog({ redirect, sessionId, onSave, onCancel, pipelineType = 'content' }: InlineEditDialogProps) {
  const [editedRedirect, setEditedRedirect] = useState(redirect);
  const [urlError, setUrlError] = useState<string | null>(null);
  const [isUrlValid, setIsUrlValid] = useState<boolean>(true);
  const [isValidating, setIsValidating] = useState<boolean>(false);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [alternatives, setAlternatives] = useState<Alternative[]>([]);
  const [alternativesLoading, setAlternativesLoading] = useState<boolean>(true);
  const [alternativesError, setAlternativesError] = useState<string | null>(null);
  const [alternativesVisible, setAlternativesVisible] = useState(false);

  const [saveError, setSaveError] = useState<string | null>(null);
  const saveLock = useRef(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    setIsValidating(true);
    const timer = setTimeout(() => {
      const result = validateUrl(editedRedirect.newUrl);
      setUrlError(result.error);
      setIsUrlValid(result.valid);
      setIsValidating(false);
    }, 300);
    return () => clearTimeout(timer);
  }, [editedRedirect.newUrl]);

  useEffect(() => {
    if (pipelineType === 'url_only') {
      setAlternativesLoading(false);
      return;
    }
    let active = true;
    async function fetchAlternatives() {
      try {
        setAlternativesLoading(true);
        setAlternativesError(null);
        const data = await getAlternatives(sessionId, redirect.id);
        if (!active) return;
        setAlternatives(data.alternatives || []);
        if (data.message) setAlternativesError(data.message);
      } catch (err) {
        if (active) setAlternativesError(err instanceof Error ? err.message : 'Failed to load alternatives');
      } finally {
        if (active) setAlternativesLoading(false);
      }
    }
    fetchAlternatives();
    return () => { active = false; };
  }, [sessionId, redirect.id, pipelineType]);

  useEffect(() => {
    if (!alternativesLoading && alternatives.length > 0) {
      const timeout = setTimeout(() => setAlternativesVisible(true), 10);
      return () => clearTimeout(timeout);
    }
  }, [alternativesLoading, alternatives]);

  const handleNewUrlChange = (newUrl: string) => {
    setEditedRedirect({ ...editedRedirect, newUrl });
    setIsValidating(true);
    setSaveError(null);
  };

  const handleSave = async () => {
    if (saveLock.current) return;
    const result = validateUrl(editedRedirect.newUrl);
    if (result.valid) {
      saveLock.current = true;
      setIsSaving(true);
      setSaveError(null);
      try {
        await onSave(editedRedirect);
      } catch (error) {
        setSaveError(error instanceof Error ? error.message : 'Save not confirmed. Reload results before exporting.');
      } finally {
        saveLock.current = false;
        setIsSaving(false);
      }
    }
  };

  const scoreColor =
    redirect.matchScore >= 85
      ? 'text-green-500'
      : redirect.matchScore >= 65
      ? 'text-yellow-500'
      : 'text-red-500';

  return (
    <Sheet open={true} onOpenChange={() => { if (!saveLock.current) onCancel(); }}>
      <SheetContent side="right" className="w-full sm:max-w-lg"
        onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement; }}
        onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus(); }}>
        <SheetHeader>
          <SheetTitle>Edit Redirect</SheetTitle>
          <SheetDescription>
            Update the target URL or pick from alternative matches.
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto min-h-0 space-y-6 px-4 pb-4">
          {/* Source URL */}
          <div className="space-y-1.5">
            <Label className="text-xs uppercase tracking-wider text-muted-foreground">
              Source URL
            </Label>
            <div className="rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm text-muted-foreground break-all">
              {editedRedirect.oldUrl}
            </div>
          </div>

          {/* Target URL */}
          <div className="space-y-1.5">
            <Label htmlFor="new-url" className="text-xs uppercase tracking-wider text-muted-foreground">
              Target URL
            </Label>
            <div className="relative">
              <Input
                id="new-url"
                disabled={isSaving}
                aria-invalid={!isUrlValid}
                aria-describedby={urlError ? "target-url-error" : undefined}
                value={editedRedirect.newUrl}
                onChange={(e) => handleNewUrlChange(e.target.value)}
                className={`font-mono text-sm pr-10 ${
                  !isValidating && !isUrlValid
                    ? 'border-red-500 dark:border-red-400 focus-visible:ring-red-500'
                    : !isValidating && isUrlValid
                    ? 'border-green-500 dark:border-green-400 focus-visible:ring-green-500'
                    : ''
                }`}
                placeholder="/new/url/path"
              />
              {!isValidating && (
                <div className="absolute right-3 top-1/2 -translate-y-1/2">
                  {isUrlValid ? (
                    <CheckCircle2 className="h-4 w-4 text-green-500" />
                  ) : (
                    <AlertCircle className="h-4 w-4 text-red-500" />
                  )}
                </div>
              )}
            </div>
            {urlError && !isValidating && (
              <p id="target-url-error" className="text-xs text-red-500">{urlError}</p>
            )}
          </div>

          <Separator />

          {/* Match Details */}
          <div className="flex items-center gap-6">
            <div className="space-y-1">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Match Score
              </Label>
              <div className={`text-3xl font-semibold tabular-nums ${scoreColor}`}>
                <AnimatedNumber value={redirect.matchScore} />
                <span className="text-lg">%</span>
              </div>
            </div>

            <Separator orientation="vertical" className="h-12" />

            <div className="flex-1 space-y-1.5">
              <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                Confidence
              </Label>
              <p className="text-sm capitalize">{redirect.confidenceBand}</p>
              <p className="text-xs text-muted-foreground">Matcher evidence. Editing a target requires a new approval.</p>
            </div>
          </div>

          <Separator />

          {/* Alternatives */}
          <div className="space-y-3">
            <Label className="text-xs uppercase tracking-wider text-muted-foreground">
              Alternative Matches
            </Label>

            {pipelineType === 'url_only' ? (
              <div className="text-center py-6 border border-dashed border-border rounded-md">
                <p className="text-sm text-muted-foreground">
                  Alternative suggestions require content-based matching.
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Upgrade your plan to access AI-powered alternatives.
                </p>
              </div>
            ) : alternativesLoading ? (
              <div className="flex items-center gap-2 py-8 justify-center text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span className="text-sm">Finding alternatives...</span>
              </div>
            ) : alternativesError && alternatives.length === 0 ? (
              <p className="text-sm text-muted-foreground py-6 text-center">
                {alternativesError}
              </p>
            ) : alternatives.length === 0 ? (
              <p className="text-sm text-muted-foreground py-6 text-center">
                No alternative matches found
              </p>
            ) : (
              <div className="space-y-1.5">
                {alternatives.map((alt, index) => (
                  <button
                    key={alt.url}
                    className="w-full flex items-center justify-between gap-3 rounded-md border border-border bg-card px-3 py-2.5 text-left hover:bg-accent"
                    style={{
                      opacity: alternativesVisible ? 1 : 0,
                      transform: alternativesVisible ? 'translateY(0)' : 'translateY(8px)',
                      transition: `opacity 200ms ease-out ${index * 75}ms, transform 200ms ease-out ${index * 75}ms, background-color 150ms`,
                    }}
                    disabled={isSaving}
                    onClick={() => handleNewUrlChange(alt.url)}
                  >
                    <span className="font-mono text-sm text-foreground break-all">
                      {alt.url}
                    </span>
                    <span className={`text-sm font-medium tabular-nums shrink-0 ${
                      alt.similarity >= 85 ? 'text-green-500' : alt.similarity >= 65 ? 'text-yellow-500' : 'text-red-500'
                    }`}>
                      <AnimatedNumber value={alt.similarity} delay={index * 75} />%
                    </span>
                  </button>
                ))}
                <p className="text-xs text-muted-foreground pt-1">
                  Click to use as target URL
                </p>
              </div>
            )}
          </div>
        </div>

        {saveError && <p role="alert" className="px-4 text-sm text-destructive">{saveError}</p>}
        <SheetFooter className="flex-row justify-end gap-2 border-t border-border pt-4">
          <Button variant="outline" onClick={onCancel} disabled={isSaving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!isUrlValid || isValidating || isSaving}>
            {isSaving && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
            {isSaving ? 'Saving...' : 'Save Changes'}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
