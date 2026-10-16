import { useState, useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { useHotkeys } from 'react-hotkeys-hook';
import { DashboardLayout } from './DashboardLayout';
import { ToolLayout } from './ToolLayout';
import { StatsBar } from './StatsBar';
import { ReviewToolbar } from './ReviewToolbar';
import { RedirectTable } from './RedirectTable';
import { InlineEditDialog } from './InlineEditDialog';
import { ExportModal } from './ExportModal';
import { WatchPrompt } from './WatchPrompt';
import { KeyboardShortcutsDialog } from './KeyboardShortcutsDialog';
import { Button } from './ui/button';
import { Keyboard, Loader2 } from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from './ui/tooltip';
import { toast } from 'sonner';
import { Info } from 'lucide-react';
import { getResults, getDeepPreview, persistReviewDecision, type DeepPreviewResponse, type ReviewDecision } from '../api/pipeline';
import { getProjectUnlockStatus } from '../api/billing';
import { isMac } from '../lib/keyboard';
import { isEnterprisePlan } from '../lib/plans';
import { useOnboarding } from '../contexts/OnboardingContext';
import { useAuth } from '../contexts/AuthContext';
import { DeepMatchPreviewCard } from './DeepMatchPreviewCard';
import { GscTrafficCard } from './GscTrafficCard';
import type { GscResultsMeta } from '../api/gsc';
import { TrafficRiskPanel } from './TrafficRiskPanel';
import { MCP_PIVOT_ENABLED, RISK_PLACEMENT } from '../api/config';
import type { TrafficRisk } from '../lib/riskSummary';
import { queryKeys } from '../queries/queryKeys';
import { handleUnauthorizedAndRedirect } from '../queries/auth';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from './ui/pagination';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select';

export interface RedirectMapping {
  id: string;
  oldUrl: string;
  newUrl: string;
  /** Authoritative saved target for review preconditions; null differs from empty. */
  persistedNewUrl?: string | null;
  confidence: number;
  confidenceBand: 'high' | 'medium' | 'low';
  matchScore: number;
  matchType?: string;
  approved: boolean;
  warnings: string[];
  pathSimilarity: number;
  titleSimilarity: number;
  contentSimilarity: number;
  gscClicks?: number;
  gscImpressions?: number;
  /**
   * A better target proposed from this site's own rename conventions, present
   * only on rows the matcher flagged. Advisory — `newUrl` above is still what
   * the matcher chose until someone accepts this.
   */
  repair?: {
    url: string;
    method: 'exact' | 'section';
    confidence: number;
    support: number;
    evidence: string;
  };
}

interface ReviewInterfaceProps {
  layoutVariant?: 'dashboard' | 'tool';
  pivotEnabled?: boolean;
}

export function ReviewInterface(props: ReviewInterfaceProps = {}) {
  const { user } = useAuth();
  const { sessionId } = useParams<{ sessionId: string }>();
  // Every local draft, selection and dialog belongs to this owner and result.
  return <OwnerReviewInterface key={`${user?.id ?? 'signed-out'}:${sessionId ?? ''}`} {...props} />;
}

function OwnerReviewInterface({ layoutVariant = 'dashboard', pivotEnabled = MCP_PIVOT_ENABLED }: ReviewInterfaceProps) {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const posthog = usePostHog();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const active = useRef(true);
  const saveLock = useRef(false);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const [searchParams] = useSearchParams();
  const {
    onboarding,
    completeStep,
    completeOnboarding,
    isStepCompleted,
  } = useOnboarding();
  const [redirects, setRedirects] = useState<RedirectMapping[]>([]);
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set());
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [editingRow, setEditingRow] = useState<RedirectMapping | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [confidenceFilter, setConfidenceFilter] = useState<string>('all');
  const [currentPage, setCurrentPage] = useState(1);
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [keyboardShortcutsOpen, setKeyboardShortcutsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortOption, setSortOption] = useState<string>('confidence-desc');
  const [showExactMatches, setShowExactMatches] = useState(true);
  // Pages with no recorded traffic start collapsed once Search Console data
  // is in — they carry no measurable risk.
  const [showZeroTraffic, setShowZeroTraffic] = useState(false);
  const [pipelineType, setPipelineType] = useState<string>('content');
  const [gscMeta, setGscMeta] = useState<GscResultsMeta | null>(null);
  const [showTutorialSuccess, setShowTutorialSuccess] = useState(false);
  const [showCoachmarks, setShowCoachmarks] = useState(false);
  const [samplePreparationActive, setSamplePreparationActive] = useState(false);
  const PAGE_SIZE = 25;
  const Layout = layoutVariant === 'tool' ? ToolLayout : DashboardLayout;
  const layoutTitle = 'Review Redirects';
  const enterpriseUser = isEnterprisePlan(user?.plan);
  const isFreeUser = (user?.plan || 'free') === 'free';

  // Refs for keyboard shortcuts
  const searchInputRef = useRef<HTMLInputElement>(null);
  const sampleGenerateMarkedRef = useRef(false);
  const tutorialReviewMarkedRef = useRef(false);
  const hydratedSessionRef = useRef<string | null>(null);
  const resultsViewedTrackedRef = useRef(false);
  const quickMatchCompletedTrackedRef = useRef(false);
  const tutorialFromQuery = searchParams.get('tutorial') === '1';
  const tutorialActive = !!onboarding &&
    onboarding.onboarding_status === 'in_progress' &&
    (tutorialFromQuery || onboarding.onboarding_state.path !== null);
  const isSampleTutorial = tutorialActive && onboarding?.onboarding_state.path === 'sample';
  const sampleTutorialBanner = tutorialFromQuery && onboarding?.onboarding_state.path === 'sample';

  const resultsQuery = useQuery({
    queryKey: queryKeys.results.forOwner(user?.id || '', sessionId || ''),
    queryFn: ({ signal }) => getResults(sessionId!, signal),
    enabled: !!sessionId && !!user?.id,
    retry: false,
    refetchOnMount: 'always',
  });

  const deepPreviewEnabled = !pivotEnabled && !!user?.id && !!sessionId && pipelineType === 'url_only' && isFreeUser;
  const deepPreviewQuery = useQuery({
    queryKey: queryKeys.results.deepPreviewForOwner(user?.id || '', sessionId || ''),
    queryFn: ({ signal }) => getDeepPreview(sessionId!, signal),
    retry: false,
    enabled: deepPreviewEnabled,
    refetchInterval: (query) => {
      const data = query.state.data as DeepPreviewResponse | undefined;
      if (data?.status === 'queued' || data?.status === 'processing') {
        return 4000;
      }
      return false;
    },
  });

  const deepPreview = deepPreviewEnabled ? deepPreviewQuery.data ?? null : null;
  const deepPreviewError =
    deepPreviewEnabled && deepPreviewQuery.error
      ? (deepPreviewQuery.error instanceof Error
          ? deepPreviewQuery.error.message
          : 'Unable to load Deep Match preview.')
      : null;

  const unlockStatusEnabled = !!user?.id && !!sessionId && pipelineType === 'url_only';
  const unlockStatusQuery = useQuery({
    queryKey: queryKeys.billing.unlockStatusForOwner(user?.id || '', sessionId || ''),
    queryFn: ({ signal }) => getProjectUnlockStatus(sessionId!, signal),
    retry: false,
    enabled: unlockStatusEnabled,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      if (data.quote_status === 'checkout_created') return 4000;
      if (
        data.is_unlocked &&
        (data.deep_session_status === 'pending' || data.deep_session_status === 'processing')
      ) {
        return 4000;
      }
      return false;
    },
  });
  const isDeepMatchUnlocked = !!unlockStatusQuery.data?.is_unlocked;
  // Inline fallback for the risk beat. Same component as the standalone
  // screen; the numbers come from the results payload instead of client-side
  // discovery, so no extra request is needed.
  const inlineRisk: TrafficRisk | null =
    RISK_PLACEMENT === 'inline' && gscMeta?.synced && gscMeta.riskSummary
      ? {
          domain: gscMeta.property || 'this site',
          total: redirects.length,
          withTraffic: gscMeta.riskSummary.urlsWithTraffic,
          noRecordedTraffic: Math.max(0, redirects.length - gscMeta.riskSummary.urlsWithTraffic),
          lowPriority: 0,
          topUrlCount: gscMeta.riskSummary.topUrlCount,
          totalClicks: gscMeta.riskSummary.totalClicks,
          totalImpressions: gscMeta.riskSummary.totalImpressions,
          shareTarget: gscMeta.riskSummary.trafficShareTarget,
          weightMetric: gscMeta.riskSummary.weightMetric,
          hasGsc: true,
          topUrls: [...redirects]
            .sort((a, b) => (b.gscClicks ?? 0) - (a.gscClicks ?? 0))
            .slice(0, 5)
            .map((r) => ({ url: r.oldUrl, clicks: r.gscClicks ?? 0, impressions: r.gscImpressions ?? 0 })),
        }
      : null;

  const showDeepMatchPreview = pipelineType === 'url_only' && isFreeUser && !!deepPreview && !isDeepMatchUnlocked;
  const showDeepMatchPreviewError = pipelineType === 'url_only' && isFreeUser && !!deepPreviewError && !isDeepMatchUnlocked;

  const isLoading =
    resultsQuery.isLoading ||
    (resultsQuery.isFetching && hydratedSessionRef.current !== sessionId);

  useEffect(() => {
    hydratedSessionRef.current = null;
    resultsViewedTrackedRef.current = false;
    quickMatchCompletedTrackedRef.current = false;
    setRedirects([]);
    setPipelineType('content');
    setGscMeta(null);
    setSortOption('confidence-desc');
    setError(null);
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) {
      setError('No session ID provided');
      return;
    }

    if (!resultsQuery.data || resultsQuery.isFetching || hydratedSessionRef.current === sessionId) {
      return;
    }

    if (resultsQuery.data.success && resultsQuery.data.mappings) {
      setRedirects(resultsQuery.data.mappings);
      if (resultsQuery.data.session?.pipeline_type) {
        setPipelineType(resultsQuery.data.session.pipeline_type);
      }
      setError(null);
    } else {
      setError('Failed to load results');
    }

    hydratedSessionRef.current = sessionId;

    if (!resultsViewedTrackedRef.current) {
      resultsViewedTrackedRef.current = true;
      posthog?.capture('results_page_viewed', {
        session_id: sessionId,
        pipeline_type: resultsQuery.data.session?.pipeline_type || 'content',
        total_mappings: resultsQuery.data.mappings?.length ?? 0,
      });
    }
  }, [resultsQuery.data, resultsQuery.isFetching, sessionId, posthog]);

  // GSC metadata updates on every refetch (unlike redirects, which hydrate
  // once per session to preserve local edits). After a traffic sync, merge the
  // new click counts into existing rows and surface the traffic sort.
  const gscSortAppliedRef = useRef(false);
  useEffect(() => {
    const data = resultsQuery.data;
    if (!data?.success) return;

    const meta = (data.gsc as GscResultsMeta | undefined) ?? null;
    setGscMeta(meta);

    if (meta?.synced && Array.isArray(data.mappings) && hydratedSessionRef.current === sessionId) {
      const byId = new Map<string, RedirectMapping>(
        data.mappings.map((m: RedirectMapping) => [m.id, m])
      );
      setRedirects(prev =>
        prev.map(r => {
          const fresh = byId.get(r.id);
          return fresh
            ? { ...r, gscClicks: fresh.gscClicks, gscImpressions: fresh.gscImpressions }
            : r;
        })
      );
    }

    if (meta?.synced && !gscSortAppliedRef.current) {
      gscSortAppliedRef.current = true;
      setSortOption('traffic-desc');
    }
    if (!meta?.synced) {
      gscSortAppliedRef.current = false;
    }
  }, [resultsQuery.data, sessionId]);

  useEffect(() => {
    if (!resultsQuery.error) return;
    if (handleUnauthorizedAndRedirect(resultsQuery.error, navigate)) return;
    setError(
      resultsQuery.error instanceof Error
        ? resultsQuery.error.message
        : 'Failed to fetch results'
    );
  }, [resultsQuery.error, navigate]);

  useEffect(() => {
    if (!sessionId || pipelineType !== 'url_only' || quickMatchCompletedTrackedRef.current) {
      return;
    }

    quickMatchCompletedTrackedRef.current = true;
    posthog?.capture('quick_match_completed', {
      session_id: sessionId,
      pipeline_type: pipelineType,
      source: searchParams.get('source') || 'review',
      plan: user?.plan || 'free',
    });
  }, [pipelineType, posthog, searchParams, sessionId, user?.plan]);

  useEffect(() => {
    if (!deepPreviewQuery.error) return;
    handleUnauthorizedAndRedirect(deepPreviewQuery.error, navigate);
  }, [deepPreviewQuery.error, navigate]);

  useEffect(() => {
    if (!unlockStatusQuery.error) return;
    handleUnauthorizedAndRedirect(unlockStatusQuery.error, navigate);
  }, [unlockStatusQuery.error, navigate]);

  useEffect(() => {
    if (!tutorialActive) {
      setShowCoachmarks(false);
      return;
    }
    setShowCoachmarks(true);
  }, [tutorialActive]);

  useEffect(() => {
    if (!isSampleTutorial || sampleGenerateMarkedRef.current || isStepCompleted('generate_mappings')) return;
    sampleGenerateMarkedRef.current = true;
    setSamplePreparationActive(true);

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const updated = await completeStep('generate_mappings');
      if (cancelled) return;
      if (!updated) {
        sampleGenerateMarkedRef.current = false;
      }
      setSamplePreparationActive(false);
    }, 1200);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [completeStep, isSampleTutorial, isStepCompleted]);

  useEffect(() => {
    if (!tutorialActive || tutorialReviewMarkedRef.current) return;

    // For sample-path users, wait until mappings are marked generated so step
    // transitions feel sequential instead of instant.
    if (isSampleTutorial && !isStepCompleted('generate_mappings')) return;

    let cancelled = false;
    const delayMs = isSampleTutorial ? 900 : 0;
    const timer = window.setTimeout(async () => {
      if (cancelled) return;
      tutorialReviewMarkedRef.current = true;
      await completeStep('open_review');
    }, delayMs);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [completeStep, isSampleTutorial, isStepCompleted, tutorialActive]);

  useEffect(() => {
    if (!tutorialActive || !showCoachmarks) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setShowCoachmarks(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [showCoachmarks, tutorialActive]);

  const handleToggleSelect = (id: string) => {
    const newSelected = new Set(selectedRows);
    if (newSelected.has(id)) {
      newSelected.delete(id);
    } else {
      newSelected.add(id);
    }
    setSelectedRows(newSelected);
  };

  const handleToggleExpand = (id: string) => {
    setExpandedRow(expandedRow === id ? null : id);
  };

  const handleEdit = (redirect: RedirectMapping) => {
    if (saveLock.current || saveError) return;
    setEditingRow(redirect);
  };

  const reloadResults = async () => {
    if (saveLock.current) return;
    const result = await resultsQuery.refetch();
    if (!active.current) return;
    if (result.data?.success && !result.error) {
      setRedirects(result.data.mappings);
      setSaveError(null);
      setError(null);
      setEditingRow(null);
      setSelectedRows(new Set());
    }
  };

  // Explicit setters are sent once. A failed response is an unknown outcome,
  // not proof of rollback; require readback before exporting the local snapshot.
  const saveDecisions = async (changes: { row: RedirectMapping; decision: ReviewDecision }[]) => {
    if (saveLock.current || saveError || !sessionId || !user?.id) return false;
    saveLock.current = true;
    setIsSaving(true);
    const failed = new Set<string>();
    let confirmed = 0;
    let cursor = 0;
    await Promise.all(Array.from({ length: Math.min(4, changes.length) }, async () => {
      while (active.current && cursor < changes.length) {
        const { row, decision } = changes[cursor++];
        try {
          const original = redirects.find(current => current.id === row.id);
          if (!original) throw new Error('The reviewed row is no longer available.');
          const expectedTarget = original.persistedNewUrl !== undefined ? original.persistedNewUrl : original.newUrl;
          await persistReviewDecision(sessionId, row.id, { ...decision, expected_new_url: expectedTarget });
          if (!active.current) return;
          confirmed++;
          // The legacy API trims edited targets. Retain the exact acknowledged
          // value as the precondition for the next approval, not the old target.
          const savedTarget = decision.new_url !== undefined ? decision.new_url.trim() : expectedTarget;
          const savedRow = { ...row, newUrl: savedTarget ?? '', persistedNewUrl: savedTarget };
          setRedirects(current => current.map(old => old.id === row.id ? savedRow : old));
          queryClient.setQueryData(queryKeys.results.forOwner(user.id, sessionId), (data: any) => data ? {
            ...data, mappings: data.mappings.map((old: RedirectMapping) => old.id === row.id ? savedRow : old),
          } : data);
        } catch {
          failed.add(row.id);
        }
      }
    }));
    if (!active.current) return false;
    saveLock.current = false;
    setIsSaving(false);
    if (failed.size) {
      setSaveError(`${failed.size} save${failed.size === 1 ? ' was' : 's were'} not confirmed. Reload results to check the server state before exporting. Your last confirmed results are still shown.`);
      setSelectedRows(failed);
    }
    if (confirmed) toast.success(`${confirmed} review change${confirmed === 1 ? '' : 's'} saved`);
    return failed.size === 0;
  };

  const handleSaveEdit = async (updated: RedirectMapping) => {
    const previous = redirects.find(row => row.id === updated.id);
    if (!previous || saveLock.current) return;
    if (previous.newUrl === updated.newUrl) { setEditingRow(null); return; }
    // A changed target needs its own approval. Model confidence remains evidence,
    // not an editable review label.
    const saved = await saveDecisions([{ row: { ...previous, newUrl: updated.newUrl, matchType: 'manual', approved: false },
      decision: { new_url: updated.newUrl, match_type: 'manual', approved: false } }]);
    if (saved) setEditingRow(null);
    else throw new Error('Save not confirmed. Close this editor and reload results to check the server state before trying again.');
  };

  const handleBulkAction = async (action: string) => {
    if (action === 'clear-selection') { setSelectedRows(new Set()); return; }
    if (saveLock.current) return;
    const approved = action !== 'reject-selected';
    const rows = redirects.filter(row =>
      (action === 'approve-all-high' ? row.confidenceBand === 'high' : selectedRows.has(row.id)) && row.approved !== approved);
    if (!rows.length) { toast.info('No approval changes needed.'); return; }
    const saved = await saveDecisions(rows.map(row => ({ row: { ...row, approved }, decision: { approved } })));
    if (saved && active.current) setSelectedRows(new Set());
  };

  const handleExport = async (_format: string, _confidenceLevels: string[]) => {
    if (tutorialActive && !isStepCompleted('export_redirects')) {
      const updated = await completeStep('export_redirects');
      if (active.current && updated?.onboarding_status === 'completed') setShowTutorialSuccess(true);
    }
  };

  const handleAcceptRepair = async (id: string) => {
    const target = redirects.find(row => row.id === id);
    if (!target?.repair) return;
    const repair = target.repair;
    await saveDecisions([{ row: { ...target, newUrl: repair.url, confidence: repair.confidence,
      matchScore: repair.confidence, confidenceBand: repair.confidence >= 85 ? 'high' : repair.confidence >= 65 ? 'medium' : 'low', matchType: 'rule_repaired', approved: true,
      warnings: target.warnings.filter(warning => warning !== 'needs-review'), repair: undefined },
      decision: { new_url: repair.url, approved: true, match_type: 'rule_repaired', confidence: repair.confidence / 100, clear_repair: true } }]);
  };

  const handleDismissRepair = (id: string) => {
    if (!saveLock.current) setRedirects(current => current.map(row => row.id === id ? { ...row, repair: undefined } : row));
  };

  const handleApproveRow = async (id: string) => {
    const row = redirects.find(row => row.id === id);
    if (row) await saveDecisions([{ row: { ...row, approved: !row.approved }, decision: { approved: !row.approved } }]);
  };

  const handleDeepUnlockClick = () => {
    if (!sessionId) return;
    posthog?.capture('deep_unlock_clicked_from_review', {
      session_id: sessionId,
      pipeline_type: pipelineType,
      source: 'review',
      plan: user?.plan || 'free',
    });
    navigate(`/pricing?source_session_id=${sessionId}`);
  };

  const filteredRedirects = redirects.filter((r) => {
    // Hide exact URL matches when toggle is off
    if (!showExactMatches && r.matchType === 'exact_url') return false;

    const q = searchQuery.trim().toLowerCase();

    const matchesSearch =
      q.length === 0 ||
      r.oldUrl.toLowerCase().includes(q) ||
      r.newUrl.toLowerCase().includes(q);

    const matchesConfidence =
      confidenceFilter === 'all' || r.confidenceBand === confidenceFilter;

    return matchesSearch && matchesConfidence;
  });

  // Determine if filters are active
  const hasActiveFilters = searchQuery.trim().length > 0 || confidenceFilter !== 'all' || !showExactMatches;

  // Handler to clear all filters
  const handleClearFilters = () => {
    setSearchQuery('');
    setConfidenceFilter('all');
    setShowExactMatches(true);
    setCurrentPage(1);
  };

  /**
   * Pages with no recorded traffic collapse out of the way.
   *
   * Once Search Console has spoken, a page with no clicks and no impressions
   * carries no measurable risk — reviewing it costs attention that belongs on
   * the pages that do. They are hidden rather than dropped: "no recorded
   * traffic" is not "no traffic", since Google withholds low-volume queries.
   *
   * Only collapses when the split is real. If nothing has traffic — an
   * unindexed site, or a property that matched no URLs — hiding everything
   * would leave an empty table with a confusing explanation.
   */
  const trafficKnown = !!gscMeta?.synced;
  const hasTraffic = (r: RedirectMapping) =>
    (r.gscClicks ?? 0) > 0 || (r.gscImpressions ?? 0) > 0;
  const zeroTrafficCount = trafficKnown
    ? filteredRedirects.filter((r) => !hasTraffic(r)).length
    : 0;
  const canCollapseZeroTraffic =
    trafficKnown && zeroTrafficCount > 0 && zeroTrafficCount < filteredRedirects.length;
  const visibleRedirects =
    canCollapseZeroTraffic && !showZeroTraffic
      ? filteredRedirects.filter(hasTraffic)
      : filteredRedirects;

  // Sort according to toolbar selection
  const sortedRedirects = [...visibleRedirects].sort((a, b) => {
    switch (sortOption) {
      case 'traffic-desc':
        return (
          (b.gscClicks ?? 0) - (a.gscClicks ?? 0) ||
          (b.gscImpressions ?? 0) - (a.gscImpressions ?? 0) ||
          a.matchScore - b.matchScore
        );
      case 'confidence-asc':
        return a.matchScore - b.matchScore;
      case 'url-asc':
        return a.oldUrl.localeCompare(b.oldUrl);
      case 'warnings':
        return (b.warnings?.length ?? 0) - (a.warnings?.length ?? 0);
      case 'confidence-desc':
      default:
        return b.matchScore - a.matchScore;
    }
  });

  // Pagination
  const totalPages = Math.max(1, Math.ceil(sortedRedirects.length / PAGE_SIZE));
  const currentPageSafe = Math.min(currentPage, totalPages);
  const startIndex = (currentPageSafe - 1) * PAGE_SIZE;
  const pageRedirects = sortedRedirects.slice(startIndex, startIndex + PAGE_SIZE);

  const stats = {
    total: redirects.length,
    exact: redirects.filter(r => r.matchType === 'exact_url').length,
    high: redirects.filter(r => r.confidenceBand === 'high' && r.matchType !== 'exact_url').length,
    medium: redirects.filter(r => r.confidenceBand === 'medium').length,
    low: redirects.filter(r => r.confidenceBand === 'low').length,
    approved: redirects.filter(r => r.approved).length,
    approvalProgress: redirects.length > 0 ? Math.round((redirects.filter(r => r.approved).length / redirects.length) * 100) : 0,
  };

  // Keyboard shortcuts
  const modKey = isMac() ? 'meta' : 'ctrl';

  const dialogOpen = exportModalOpen || keyboardShortcutsOpen || !!editingRow || showTutorialSuccess;
  const canExport = !isSaving && !saveError && !isLoading;
  useHotkeys(`${modKey}+k`, event => {
    event.preventDefault(); searchInputRef.current?.focus();
  }, { enabled: !dialogOpen, enableOnFormTags: true, preventDefault: true }, [dialogOpen]);
  useHotkeys(`${modKey}+e`, event => {
    event.preventDefault(); setExportModalOpen(true);
  }, { enabled: !dialogOpen && canExport }, [dialogOpen, canExport]);
  useHotkeys(`${modKey}+a`, event => {
    event.preventDefault(); setSelectedRows(new Set(pageRedirects.map(row => row.id)));
  }, { enabled: !dialogOpen && !isSaving }, [dialogOpen, isSaving, pageRedirects]);
  useHotkeys('shift+/', () => setKeyboardShortcutsOpen(true), { enabled: !dialogOpen }, [dialogOpen]);

  // Show error state
  if (error) {
    return (
      <Layout title={layoutTitle}>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <div className="text-lg font-medium text-destructive mb-2">Error Loading Results</div>
            <div className="text-sm text-muted-foreground mb-4">{error}</div>
            <Button onClick={() => void reloadResults()} disabled={resultsQuery.isFetching}>Reload results</Button>
            <Button variant="outline" onClick={() => navigate('/')}>
              Go to Dashboard
            </Button>
          </div>
        </div>
      </Layout>
    );
  }

  return (
      <Layout title={layoutTitle}>
          {layoutVariant === 'tool' && <h1 className="sr-only">{layoutTitle}</h1>}
          {isSampleTutorial && samplePreparationActive && (
            <div className="mb-4 border border-[#26D99D] bg-[#26D99D]/16 dark:bg-[#26D99D]/24 p-3">
              <div className="flex items-center gap-2 text-sm font-medium text-[#064731] dark:text-[#E9FFF8]">
                <Loader2 className="h-4 w-4 animate-spin" />
                Generating mappings from sample CSVs...
              </div>
            </div>
          )}

          {sampleTutorialBanner && (
            <div className="mb-4 border border-[#26D99D] bg-[#26D99D]/16 dark:bg-[#26D99D]/24 p-3">
              <p className="text-sm font-medium text-[#064731] dark:text-[#E9FFF8]">
                Sample data for learning. Start a real job anytime.
              </p>
            </div>
          )}

          {/* URL-only pricing banner */}
          {!pivotEnabled && pipelineType === 'url_only' && (
            <div className="mb-4 border border-blue-500/30 bg-blue-500/5 p-3 flex items-center gap-3">
              <Info className="h-4 w-4 text-blue-500 flex-shrink-0" />
              <p className="text-sm text-muted-foreground">
                These results use Quick Match. Unlock Deep Match for semantic content analysis and alternative suggestions.
              </p>
            </div>
          )}

          {pipelineType === 'url_only' && unlockStatusEnabled && (!pivotEnabled ||
            isDeepMatchUnlocked || unlockStatusQuery.data?.quote_status === 'checkout_created') && (
            <div
              className={`mb-4 border p-4 ${
                isDeepMatchUnlocked
                  ? 'border-emerald-500/40 bg-emerald-500/10'
                  : 'border-border bg-card'
              }`}
            >
              {unlockStatusQuery.isLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading project unlock status...
                </div>
              ) : unlockStatusQuery.data ? (
                <div className="space-y-3">
                  {!pivotEnabled && !unlockStatusQuery.data.has_quote && (
                    <>
                      <p className="text-sm text-muted-foreground">
                        Get a one-time Deep Match quote for this project.
                      </p>
                      <Button onClick={handleDeepUnlockClick}>
                        View Project Pricing
                      </Button>
                    </>
                  )}

                  {!pivotEnabled && unlockStatusQuery.data.has_quote && !unlockStatusQuery.data.is_unlocked && !unlockStatusQuery.data.contact_required && (
                    <>
                      <p className="text-sm text-muted-foreground">
                        Deep Match is locked for this project.
                      </p>
                      <Button onClick={handleDeepUnlockClick}>
                        Unlock Deep Match
                      </Button>
                    </>
                  )}

                  {unlockStatusQuery.data.quote_status === 'checkout_created' && !unlockStatusQuery.data.is_unlocked && (
                    <p className="text-sm text-muted-foreground">
                      Payment submitted. Waiting for confirmation and deep-run queueing...
                    </p>
                  )}

                  {!pivotEnabled && unlockStatusQuery.data.contact_required && (
                    <p className="text-sm text-muted-foreground">
                      This project requires enterprise pricing. Contact sales from the pricing page.
                    </p>
                  )}

                  {unlockStatusQuery.data.is_unlocked && (
                    <>
                      <p className="text-sm font-medium text-emerald-700 dark:text-emerald-300">Deep Match unlocked.</p>
                      {unlockStatusQuery.data.deep_session_status === 'completed' &&
                        unlockStatusQuery.data.deep_session_id && (
                          <Button
                            onClick={() => navigate(`/review/${unlockStatusQuery.data.deep_session_id}`)}
                          >
                            Open Deep Match Results
                          </Button>
                        )}
                      {(unlockStatusQuery.data.deep_session_status === 'queued' ||
                        unlockStatusQuery.data.deep_session_status === 'pending' ||
                        unlockStatusQuery.data.deep_session_status === 'processing') && (
                        <p className="text-sm text-emerald-700 dark:text-emerald-300">
                          Deep Match is processing in the background. This page will refresh status automatically.
                        </p>
                      )}
                    </>
                  )}
                </div>
              ) : null}
            </div>
          )}

          {showDeepMatchPreview && (
            <DeepMatchPreviewCard preview={deepPreview} />
          )}
          {showDeepMatchPreviewError && (
            <div className="mb-4 text-xs text-muted-foreground">{deepPreviewError}</div>
          )}

          {/* Search Console traffic triage */}
          {inlineRisk && (
            <div className="mb-4">
              <TrafficRiskPanel risk={inlineRisk} variant="inline" />
            </div>
          )}

          {sessionId && <GscTrafficCard sessionId={sessionId} gscMeta={gscMeta} />}

          {/* Stats Bar */}
          <StatsBar stats={stats} />

          {/* Toolbar */}
          {tutorialActive && showCoachmarks && (
            <div className="mt-4 mb-2 border border-[#26D99D] bg-[#26D99D]/16 dark:bg-[#26D99D]/24 p-3 text-xs font-medium text-[#064731] dark:text-[#E9FFF8]">
              Step 3-4 of 4: Review results, then export redirect rules. Press <kbd className="mx-1 rounded border border-[#26D99D]/70 px-1 py-0.5 font-semibold">Esc</kbd> to hide hints.
            </div>
          )}
          {isSaving && <p role="status" className="mb-3 text-sm text-muted-foreground">Saving review changes. Export will be available when saves are confirmed.</p>}
          {saveError && <div role="alert" className="mb-3 rounded-md border border-destructive p-3 text-sm">
            <p>{saveError}</p>
            <Button variant="outline" className="mt-2" onClick={() => void reloadResults()} disabled={isSaving || resultsQuery.isFetching}>Reload results</Button>
          </div>}
          <ReviewToolbar
            exportDisabled={!canExport}
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            confidenceFilter={confidenceFilter}
            onConfidenceFilterChange={setConfidenceFilter}
            sortOption={sortOption}
            onSortChange={setSortOption}
            showExactMatches={showExactMatches}
            onShowExactMatchesChange={setShowExactMatches}
            onExportClick={() => { if (canExport) setExportModalOpen(true); }}
            totalCount={redirects.length}
            filteredCount={sortedRedirects.length}
            searchInputRef={searchInputRef}
            tutorialExportHighlight={tutorialActive && showCoachmarks && !isStepCompleted('export_redirects')}
            showTrafficSort={!!gscMeta?.synced}
          />

          {/* Table */}
          <div className="mt-6">
          <RedirectTable
            actionsDisabled={isSaving || !!saveError}
            redirects={pageRedirects}
            selectedRows={selectedRows}
            expandedRow={expandedRow}
            onToggleSelect={handleToggleSelect}
            onToggleExpand={handleToggleExpand}
            onEdit={handleEdit}
            onApprove={handleApproveRow}
            onAcceptRepair={handleAcceptRepair}
            onDismissRepair={handleDismissRepair}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={handleClearFilters}
            totalRedirectsCount={redirects.length}
            isLoading={isLoading}
            pipelineType={pipelineType}
            showTraffic={!!gscMeta?.synced}
          />

          {canCollapseZeroTraffic && (
            <button
              type="button"
              onClick={() => {
                setShowZeroTraffic((v) => !v);
                setCurrentPage(1);
                posthog?.capture('review_zero_traffic_toggled', {
                  session_id: sessionId,
                  shown: !showZeroTraffic,
                  count: zeroTrafficCount,
                });
              }}
              className="mt-3 w-full flex items-center justify-between gap-3 rounded-lg border border-dashed border-border bg-muted/30 px-4 py-3 text-left hover:bg-muted/50 transition-colors"
              aria-expanded={showZeroTraffic}
            >
              <span className="text-sm text-muted-foreground">
                <span className="text-foreground font-medium">
                  {zeroTrafficCount.toLocaleString()}
                </span>{' '}
                page{zeroTrafficCount !== 1 ? 's' : ''} with no recorded traffic
                <span className="hidden sm:inline">
                  {' '}— still redirected, just not worth your review time
                </span>
              </span>
              <span className="text-sm text-muted-foreground underline underline-offset-2 flex-shrink-0">
                {showZeroTraffic ? 'Hide' : 'Show'}
              </span>
            </button>
          )}

          </div>

          {/* Bottom Controls */}
          <div className="mt-6 flex flex-wrap gap-4 items-center justify-between border-t border-border pt-6 bg-card px-6 py-4">
            <div className="flex items-center gap-4">
              <Select value="" onValueChange={handleBulkAction} disabled={isSaving}>
                <SelectTrigger aria-label="Bulk Actions" className="w-[200px] data-[placeholder]:text-foreground">
                  <SelectValue placeholder="Bulk Actions" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="approve-all-high" disabled={!!saveError}>Approve All High</SelectItem>
                  <SelectItem value="approve-selected" disabled={!!saveError}>Approve Selected</SelectItem>
                  <SelectItem value="reject-selected" disabled={!!saveError}>Reject Selected</SelectItem>
                  <SelectItem value="clear-selection">Clear Selection</SelectItem>
                </SelectContent>
              </Select>
              {selectedRows.size > 0 && (
                <span className="text-sm text-muted-foreground">
                  {selectedRows.size} row{selectedRows.size !== 1 ? 's' : ''} selected
                </span>
              )}
            </div>

            <Pagination>
              <PaginationContent>
                {/* Previous */}
                <PaginationItem>
                  <PaginationPrevious
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      if (currentPageSafe > 1) {
                        setCurrentPage(currentPageSafe - 1);
                      }
                    }}
                  />
                </PaginationItem>

                {/* Page numbers */}
                {Array.from({ length: totalPages }, (_, i) => i + 1).map((page) => (
                  <PaginationItem key={page}>
                    <PaginationLink
                      href="#"
                      isActive={page === currentPageSafe}
                      onClick={(e) => {
                        e.preventDefault();
                        setCurrentPage(page);
                      }}
                    >
                      {page}
                    </PaginationLink>
                  </PaginationItem>
                ))}

                {/* Next */}
                <PaginationItem>
                  <PaginationNext
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      if (currentPageSafe < totalPages) {
                        setCurrentPage(currentPageSafe + 1);
                      }
                    }}
                  />
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          </div>

      {/* Post-cutover monitoring: the export is a prediction until the live
          site confirms it. */}
      {sessionId && <WatchPrompt sessionId={sessionId} pivotEnabled={pivotEnabled} />}

      {/* Floating Keyboard Shortcuts Button */}
      <div className="fixed bottom-6 right-6 z-40">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="outline"
              size="icon"
              aria-label="Keyboard shortcuts"
              onClick={() => setKeyboardShortcutsOpen(true)}
              className="h-10 w-10 rounded-full shadow-lg border-2"
            >
              <Keyboard className="h-5 w-5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="left">
            <p>Keyboard shortcuts (?)</p>
          </TooltipContent>
        </Tooltip>
      </div>

      {/* Inline Edit Dialog */}
      {editingRow && (
        <InlineEditDialog
          redirect={editingRow}
          sessionId={sessionId!}
          onSave={handleSaveEdit}
          onCancel={() => { if (!saveLock.current) setEditingRow(null); }}
          pipelineType={pipelineType}
        />
      )}

      {/* Export Modal */}
      <ExportModal
        sessionId={sessionId!}
        open={exportModalOpen && canExport}
        onOpenChange={setExportModalOpen}
        onExport={handleExport}
        redirects={redirects}
      />

      {/* Keyboard Shortcuts Dialog */}
      <KeyboardShortcutsDialog
        open={keyboardShortcutsOpen}
        onOpenChange={setKeyboardShortcutsOpen}
      />

      <Dialog open={showTutorialSuccess} onOpenChange={setShowTutorialSuccess}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Tutorial complete</DialogTitle>
            <DialogDescription>
              You exported redirect rules successfully. You can now run a real project with your own CSVs.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="sm:justify-between gap-2">
            <Button
              variant="outline"
              onClick={async () => {
                await completeOnboarding();
                setShowTutorialSuccess(false);
              }}
            >
              Close
            </Button>
            <Button
              onClick={async () => {
                await completeOnboarding();
                setShowTutorialSuccess(false);
                navigate(enterpriseUser ? '/upload' : '/quick-match');
              }}
            >
              Start Real Job Now
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Layout>
  );
}
