import { uploadCSVs, UploadNotConfirmedError, type ContentUrlCapError } from "../api/pipeline";
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { useAuth } from '../contexts/AuthContext';
import { useOnboarding } from '../contexts/OnboardingContext';
import { DashboardLayout } from './DashboardLayout';
import { ToolLayout } from './ToolLayout';
import { FileUploadZone } from './FileUploadZone';
import { DomainDiscoveryPanel } from './DomainDiscoveryPanel';
import type { DiscoveryResponse } from '../api/discovery';
import { TrafficRiskPanel } from './TrafficRiskPanel';
import { computeRisk } from '../lib/riskSummary';
import { RISK_PLACEMENT } from '../api/config';
import { LoadingScreen } from './LoadingScreen';
import { Button } from './ui/button';
import { AlertTriangle, Loader2, Zap, Search, Info, ShieldAlert } from 'lucide-react';
import { validateFile, FileValidationResult } from '../utils/validation';
import { appQueryClient } from '../queries/queryClient';
import { queryKeys } from '../queries/queryKeys';
import { CONTENT_MAX_URLS_PER_SITE } from '../api/config';
import { useRequestScope } from './pivot/useRequestScope';

interface FileData {
  name: string;
  rowCount: number;
  file: File;
}

interface BeginMatchingOptions {
  force?: boolean;
  skipWarningCheck?: boolean;
  skipScrapingWarning?: boolean;
}

const SAMPLE_OLD_URLS = [
  "https://legacy-example.com/about",
  "https://legacy-example.com/services/seo-audit",
  "https://legacy-example.com/case-studies/retail-growth",
  "https://legacy-example.com/resources/redirect-guide",
  "https://legacy-example.com/blog/platform-migration-checklist",
  "https://legacy-example.com/pricing/agency",
  "https://legacy-example.com/integrations/hubspot",
  "https://legacy-example.com/docs/import",
];

const SAMPLE_NEW_URLS = [
  "https://new-example.com/about",
  "https://new-example.com/services/technical-seo-audit",
  "https://new-example.com/case-studies/retail-traffic-growth",
  "https://new-example.com/resources/301-redirect-guide",
  "https://new-example.com/blog/website-migration-checklist",
  "https://new-example.com/pricing/partners",
  "https://new-example.com/integrations/crm",
  "https://new-example.com/help/uploading-csvs",
];

function buildSampleCsv(urls: string[]): string {
  return ["url", ...urls].join("\n");
}

const LARGE_DATASET_WARNING_PREFIX = 'Large dataset detected';

interface UploadPageProps {
  quickOnly?: boolean;
  flowSource?: string;
  layoutVariant?: 'dashboard' | 'tool';
}

export function UploadPage(props: UploadPageProps = {}) {
  const { user } = useAuth();
  if (!user) return null;
  return <UploadPageContent key={user.id} {...props} />;
}

function UploadPageContent({
  quickOnly = false,
  flowSource = 'upload',
  layoutVariant = 'dashboard',
}: UploadPageProps = {}) {
  const navigate = useNavigate();
  const posthog = usePostHog();
  const [searchParams] = useSearchParams();
  const { user } = useAuth();
  const requestScope = useRequestScope(`upload:${user?.id}`);
  const {
    onboarding,
    startOnboarding,
    completeStep,
    createSampleSession,
  } = useOnboarding();
  const userPlan = user?.plan || 'free';
  const isFreeUser = userPlan === 'free';
  const isQuickOnlyMode = quickOnly;
  const [pipelineType, setPipelineType] = useState<'content' | 'url_only'>((
    isFreeUser || isQuickOnlyMode
  ) ? 'url_only' : 'content');
  const [isLoading, setIsLoading] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [oldSiteFile, setOldSiteFile] = useState<FileData | null>(null);
  const [newSiteFile, setNewSiteFile] = useState<FileData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadNotConfirmed, setUploadNotConfirmed] = useState(false);
  const uploadRequest = useRef<AbortController | null>(null);
  const [contentCapApiError, setContentCapApiError] = useState<ContentUrlCapError | null>(null);
  const [duplicateSessionId, setDuplicateSessionId] = useState<string | null>(null);

  // Raw file objects needed for API
  const [oldCsvFile, setOldCsvFile] = useState<File | null>(null);
  const [newCsvFile, setNewCsvFile] = useState<File | null>(null);

  // Validation state
  const [oldFileValidation, setOldFileValidation] = useState<FileValidationResult | null>(null);
  const [newFileValidation, setNewFileValidation] = useState<FileValidationResult | null>(null);
  const fileRevision = useRef({ old: 0, new: 0 });
  const fileReads = useRef<{ old?: AbortController; new?: AbortController }>({});
  const [checkingFiles, setCheckingFiles] = useState<{ old: string | null; new: string | null }>({ old: null, new: null });
  useEffect(() => () => {
    fileReads.current.old?.abort();
    fileReads.current.new?.abort();
    fileRevision.current.old += 1;
    fileRevision.current.new += 1;
  }, []);
  const [pendingWarnings, setPendingWarnings] = useState<{ old: string[], new: string[] } | null>(null);
  const [showCoachmarks, setShowCoachmarks] = useState(false);
  const [ingestMode, setIngestMode] = useState<'domains' | 'csv'>('domains');
  // Discovery results kept so the risk number can be shown before matching.
  const [discovery, setDiscovery] = useState<{ old: DiscoveryResponse | null; new: DiscoveryResponse | null }>({ old: null, new: null });
  const [showRisk, setShowRisk] = useState(false);

  const tutorialFromQuery = searchParams.get('tutorial') === '1';
  const sampleFromQuery = searchParams.get('sample') === '1';
  const tutorialPath = onboarding?.onboarding_state.path;
  const tutorialIntent = tutorialFromQuery || tutorialPath === 'real' || tutorialPath === 'sample';
  const tutorialActive = !!onboarding &&
    tutorialIntent &&
    (onboarding.onboarding_status === 'in_progress' || onboarding.onboarding_status === 'not_started');
  const sampleTutorialActive = tutorialActive && (sampleFromQuery || tutorialPath === 'sample');
  const effectivePipelineType = (isFreeUser || isQuickOnlyMode) ? 'url_only' : pipelineType;
  const Layout = layoutVariant === 'tool' ? ToolLayout : DashboardLayout;
  const layoutTitle = isQuickOnlyMode ? 'Quick Match' : 'Upload CSV Files';
  const oldRowCount = oldFileValidation?.rowCount ?? oldSiteFile?.rowCount ?? 0;
  const newRowCount = newFileValidation?.rowCount ?? newSiteFile?.rowCount ?? 0;
  const oldContentCapExceeded = effectivePipelineType === 'content' && oldRowCount > CONTENT_MAX_URLS_PER_SITE;
  const newContentCapExceeded = effectivePipelineType === 'content' && newRowCount > CONTENT_MAX_URLS_PER_SITE;
  const hasLocalContentCapViolation = oldContentCapExceeded || newContentCapExceeded;

  useEffect(() => {
    if (isFreeUser || isQuickOnlyMode) {
      setPipelineType('url_only');
    }
  }, [isFreeUser, isQuickOnlyMode]);

  useEffect(() => {
    if (!tutorialActive) {
      setShowCoachmarks(false);
      return;
    }
    setShowCoachmarks(true);
  }, [tutorialActive]);

  // The tutorial walks through the CSV flow, so keep its copy accurate.
  useEffect(() => {
    if (tutorialActive) {
      setIngestMode('csv');
    }
  }, [tutorialActive]);

  useEffect(() => {
    if (!tutorialActive || onboarding?.onboarding_status !== 'not_started') return;
    startOnboarding();
  }, [onboarding?.onboarding_status, startOnboarding, tutorialActive]);

  useEffect(() => {
    if (!tutorialActive || !showCoachmarks) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setShowCoachmarks(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [showCoachmarks, tutorialActive]);

  useEffect(() => {
    if (!sampleTutorialActive) return;
    if (oldCsvFile && newCsvFile && oldSiteFile && newSiteFile) return;

    const oldSampleFile = new File(
      [buildSampleCsv(SAMPLE_OLD_URLS)],
      "sample-old-urls.csv",
      { type: "text/csv" }
    );
    const newSampleFile = new File(
      [buildSampleCsv(SAMPLE_NEW_URLS)],
      "sample-new-urls.csv",
      { type: "text/csv" }
    );

    setOldCsvFile(oldSampleFile);
    setNewCsvFile(newSampleFile);
    setOldSiteFile({
      name: oldSampleFile.name,
      rowCount: SAMPLE_OLD_URLS.length,
      file: oldSampleFile,
    });
    setNewSiteFile({
      name: newSampleFile.name,
      rowCount: SAMPLE_NEW_URLS.length,
      file: newSampleFile,
    });
    setOldFileValidation({
      valid: true,
      warnings: [],
      errors: [],
      rowCount: SAMPLE_OLD_URLS.length,
    });
    setNewFileValidation({
      valid: true,
      warnings: [],
      errors: [],
      rowCount: SAMPLE_NEW_URLS.length,
    });
  }, [newCsvFile, newSiteFile, oldCsvFile, oldSiteFile, sampleTutorialActive]);

  // Scraping warning state (Deep Match only)
  const [showScrapingWarning, setShowScrapingWarning] = useState(false);
  const [scrapingWarningAcknowledged, setScrapingWarningAcknowledged] = useState(false);

  const handleFileUpload = async (file: File, type: 'old' | 'new') => {
    const revision = ++fileRevision.current[type];
    fileReads.current[type]?.abort();
    const controller = new AbortController();
    fileReads.current[type] = controller;
    setCheckingFiles(previous => ({ ...previous, [type]: file.name }));
    setContentCapApiError(null);
    setPendingWarnings(null);
    setError(null);

    // Clear previous errors for this file type
    if (type === 'old') {
      setOldFileValidation(null);
      setOldSiteFile(null);
      setOldCsvFile(null);
    } else {
      setNewFileValidation(null);
      setNewSiteFile(null);
      setNewCsvFile(null);
    }

    // Validate the file (CSV, TXT, XML, or XLSX)
    let validationResult: FileValidationResult;
    try {
      validationResult = await validateFile(file, controller.signal);
    } catch {
      if (controller.signal.aborted || revision !== fileRevision.current[type]) return;
      validationResult = { valid: false, rowCount: 0, warnings: [], errors: ['Could not read this file. Choose it again or export a new copy.'] };
    }
    // A newer selection, removal, mode switch or unmount owns this side now.
    if (revision !== fileRevision.current[type] || controller.signal.aborted) return;
    setCheckingFiles(previous => ({ ...previous, [type]: null }));
    delete fileReads.current[type];

    if (type === 'old') {
      setOldFileValidation(validationResult);
    } else {
      setNewFileValidation(validationResult);
    }

    // If validation failed, don't proceed with file upload
    if (!validationResult.valid) {
      return;
    }

    // Use the converted file for XML/XLSX, otherwise use the original
    const fileToSend = validationResult.convertedFile || file;

    const fileData: FileData = {
      name: file.name,
      rowCount: validationResult.rowCount,
      file: fileToSend,
    };

    if (type === 'old') {
      setOldCsvFile(fileToSend);
      setOldSiteFile(fileData);
    } else {
      setNewCsvFile(fileToSend);
      setNewSiteFile(fileData);
    }
  };

  const handleDomainDiscovered = (side: 'old' | 'new', result: DiscoveryResponse | null) => {
    fileRevision.current[side] += 1;
    fileReads.current[side]?.abort();
    setCheckingFiles(previous => ({ ...previous, [side]: null }));
    setContentCapApiError(null);
    setPendingWarnings(null);
    setError(null);
    setDuplicateSessionId(null);

    setDiscovery((prev) => ({ ...prev, [side]: result }));

    if (!result) {
      handleFileRemove(side);
      return;
    }

    // Bridge discovered URLs into the existing file-based submit flow.
    const host = new URL(result.root_url).hostname;
    const file = new File([result.urls.join('\n')], `${host}-discovered.csv`, {
      type: 'text/csv',
    });
    const fileData: FileData = {
      name: `${host} — ${result.count.toLocaleString()} pages`,
      rowCount: result.count,
      file,
    };
    const validation: FileValidationResult = {
      valid: true,
      warnings: [],
      errors: [],
      rowCount: result.count,
    };

    if (side === 'old') {
      setOldCsvFile(file);
      setOldSiteFile(fileData);
      setOldFileValidation(validation);
    } else {
      setNewCsvFile(file);
      setNewSiteFile(fileData);
      setNewFileValidation(validation);
    }
  };

  const handleIngestModeChange = (mode: 'domains' | 'csv') => {
    if (mode === ingestMode) return;
    setIngestMode(mode);
    setDiscovery({ old: null, new: null });
    setShowRisk(false);
    handleFileRemove('old');
    handleFileRemove('new');
  };

  const handleFileRemove = (type: 'old' | 'new') => {
    fileRevision.current[type] += 1;
    fileReads.current[type]?.abort();
    delete fileReads.current[type];
    setCheckingFiles(previous => ({ ...previous, [type]: null }));
    setContentCapApiError(null);
    setPendingWarnings(null);
    setError(null);
    setDuplicateSessionId(null);
    setShowScrapingWarning(false);
    setScrapingWarningAcknowledged(false);

    if (type === 'old') {
      setOldSiteFile(null);
      setOldCsvFile(null);
      setOldFileValidation(null);
      return;
    }

    setNewSiteFile(null);
    setNewCsvFile(null);
    setNewFileValidation(null);
  };

  const showUnconfirmedUpload = () => {
    setIsUploading(false);
    setIsLoading(false);
    setCurrentSessionId(null);
    setUploadNotConfirmed(true);
    setError(new UploadNotConfirmedError().message);
    if (user?.id) {
      // A stopped wait can still mean a saved job. Read current owned history.
      void appQueryClient.invalidateQueries({ queryKey: queryKeys.sessions.forOwner(user.id) });
      void appQueryClient.invalidateQueries({ queryKey: queryKeys.dashboard.forOwner(user.id) });
    }
  };

  const stopWaitingForUpload = () => {
    const request = uploadRequest.current;
    if (!request || requestScope.signal().aborted) return;
    uploadRequest.current = null;
    request.abort();
    // Do not depend on fetch rejecting: even an abort-ignoring transport must
    // release this UI. The abandoned attempt cannot publish a late response.
    showUnconfirmedUpload();
  };

  const handleBeginMatching = async ({
    force = false,
    skipWarningCheck = false,
    skipScrapingWarning = false,
  }: BeginMatchingOptions = {}) => {
    const signal = requestScope.signal();
    if (signal.aborted || uploadRequest.current) return;
    setUploadNotConfirmed(false);
    if (sampleTutorialActive) {
      setError(null);
      setDuplicateSessionId(null);
      setPendingWarnings(null);
      setIsUploading(true);
      setIsLoading(true);

      try {
        const sample = await createSampleSession();
        if (signal.aborted) return;
        if (sample.session_id) {
          setCurrentSessionId(sample.session_id);
        }
        void appQueryClient.invalidateQueries({ queryKey: queryKeys.dashboard.summary });
        void appQueryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
        await completeStep('generate_mappings');
        if (signal.aborted) return;
        setIsUploading(false);
      } catch (err) {
        if (signal.aborted) return;
        console.error(err);
        setIsUploading(false);
        setIsLoading(false);
        setCurrentSessionId(null);
        setError(err instanceof Error ? err.message : "Failed to prepare sample tutorial session.");
      }
      return;
    }
    if (!oldCsvFile || !newCsvFile) {
      setError("Upload both CSV files first.");
      return;
    }

    if (effectivePipelineType === 'content' && hasLocalContentCapViolation) {
      return;
    }

    // Check for warnings if not already confirmed
    if (!skipWarningCheck) {
      const suppressLargeDatasetWarnings = effectivePipelineType === 'content' && hasLocalContentCapViolation;
      const oldWarnings = suppressLargeDatasetWarnings
        ? (oldFileValidation?.warnings || []).filter((w) => !w.startsWith(LARGE_DATASET_WARNING_PREFIX))
        : (oldFileValidation?.warnings || []);
      const newWarnings = suppressLargeDatasetWarnings
        ? (newFileValidation?.warnings || []).filter((w) => !w.startsWith(LARGE_DATASET_WARNING_PREFIX))
        : (newFileValidation?.warnings || []);

      if (oldWarnings.length > 0 || newWarnings.length > 0) {
        setPendingWarnings({ old: oldWarnings, new: newWarnings });
        return;
      }
    }

    // Show scraping warning for Deep Match (content pipeline)
    if (effectivePipelineType === 'content' && !skipScrapingWarning) {
      setShowScrapingWarning(true);
      setScrapingWarningAcknowledged(false);
      return;
    }

    // Clear previous errors
    setError(null);
    setContentCapApiError(null);
    setDuplicateSessionId(null);
    setPendingWarnings(null);
    setIsUploading(true);
    setIsLoading(true);

    const request = new AbortController();
    uploadRequest.current = request;
    const abortRequest = () => request.abort();
    signal.addEventListener('abort', abortRequest, { once: true });
    request.signal.addEventListener('abort', () => signal.removeEventListener('abort', abortRequest), { once: true });

    if (effectivePipelineType === 'url_only') {
      posthog?.capture('quick_match_upload_started', {
        plan: userPlan,
        source: flowSource,
        pipeline_type: effectivePipelineType,
        old_url_count: oldRowCount,
        new_url_count: newRowCount,
      });
    }

    try {
      const result = await uploadCSVs(oldCsvFile, newCsvFile, force, effectivePipelineType, request.signal);
      if (signal.aborted || request.signal.aborted) return;
      // Acknowledged uploads now belong to the session's processing lifecycle.
      uploadRequest.current = null;

      console.log("Pipeline Response:", result);

      // Check if this is a duplicate run
      if (result.is_duplicate && !force) {
        console.log("Duplicate detected, showing warning");
        setIsUploading(false);
        setIsLoading(false);
        setDuplicateSessionId(result.session_id);
        return;
      }

      // Store session ID - LoadingScreen will poll and navigate when complete
      if (result.session_id) {
        setCurrentSessionId(result.session_id);
      }
      void appQueryClient.invalidateQueries({ queryKey: queryKeys.dashboard.summary });
      void appQueryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
      if (tutorialActive) {
        await completeStep('generate_mappings');
        if (signal.aborted || request.signal.aborted) return;
      }
      setIsUploading(false);
    } catch (err) {
      if (signal.aborted || request.signal.aborted) return;
      console.error(err);
      setIsUploading(false);
      setIsLoading(false);
      setCurrentSessionId(null);

      if (err && typeof err === 'object' && 'type' in err && (err as ContentUrlCapError).type === 'content_url_cap_exceeded') {
        setContentCapApiError(err as ContentUrlCapError);
        setError(null);
      } else if (err instanceof Error) {
        if (err instanceof UploadNotConfirmedError) showUnconfirmedUpload();
        else setError(err.message);
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      signal.removeEventListener('abort', abortRequest);
      if (uploadRequest.current === request) uploadRequest.current = null;
    }
  };

  const handleProceedAnyway = () => {
    handleBeginMatching({ force: true, skipWarningCheck: true, skipScrapingWarning: true });
  };

  const handleProceedWithWarnings = () => {
    handleBeginMatching({ skipWarningCheck: true });
  };

  const handleCancelWarnings = () => {
    setPendingWarnings(null);
  };

  const handleConfirmScrapingWarning = () => {
    setShowScrapingWarning(false);
    handleBeginMatching({ skipWarningCheck: true, skipScrapingWarning: true });
  };

  const handleCancelScrapingWarning = () => {
    setShowScrapingWarning(false);
    setScrapingWarningAcknowledged(false);
  };

  const handlePipelineTypeChange = (nextType: 'content' | 'url_only') => {
    if (isQuickOnlyMode) {
      return;
    }
    setPipelineType(nextType);
    setPendingWarnings(null);
    setContentCapApiError(null);
    setError(null);
  };

  const handleSwitchToQuickMatch = () => {
    if (!isFreeUser) {
      setPipelineType('url_only');
    }
    setPendingWarnings(null);
    setContentCapApiError(null);
    setError(null);
  };

  // The risk number comes from the old side — the new site isn't indexed, so
  // it has no traffic to lose.
  const oldDiscovery = discovery.old;
  const risk = oldDiscovery
    ? computeRisk(
        new URL(oldDiscovery.root_url).hostname,
        oldDiscovery.entries ?? [],
        oldDiscovery.summary
      )
    : null;
  // Only worth its own beat when we actually have traffic data to show.
  const riskBeatAvailable =
    RISK_PLACEMENT === 'screen' &&
    ingestMode === 'domains' &&
    !!risk?.hasGsc &&
    !tutorialActive;

  const bothFilesUploaded = oldSiteFile && newSiteFile;
  const hasValidationErrors =
    (oldFileValidation && !oldFileValidation.valid) ||
    (newFileValidation && !newFileValidation.valid) ||
    hasLocalContentCapViolation;
  const defaultContentCapMessage =
    `Deep Match has a per-file limit of ${CONTENT_MAX_URLS_PER_SITE.toLocaleString()} URLs. ` +
    'Split your CSV or switch to Quick Match.';
  const contentCapPanelData = hasLocalContentCapViolation
    ? {
        message: defaultContentCapMessage,
        oldCount: oldRowCount,
        newCount: newRowCount,
        maxOld: CONTENT_MAX_URLS_PER_SITE,
        maxNew: CONTENT_MAX_URLS_PER_SITE,
        showOld: oldContentCapExceeded,
        showNew: newContentCapExceeded,
      }
    : contentCapApiError
      ? {
          message: contentCapApiError.message || defaultContentCapMessage,
          oldCount: contentCapApiError.old_url_count,
          newCount: contentCapApiError.new_url_count,
          maxOld: contentCapApiError.max_old_urls,
          maxNew: contentCapApiError.max_new_urls,
          showOld: contentCapApiError.affected_file === 'old' || contentCapApiError.affected_file === 'both',
          showNew: contentCapApiError.affected_file === 'new' || contentCapApiError.affected_file === 'both',
        }
      : null;

  // Show loading screen when processing
  if (isLoading) {
    return (
      <Layout title="Processing">
        {!sampleTutorialActive && !currentSessionId ? (
          <section className="mx-auto max-w-xl py-12 space-y-6" aria-labelledby="upload-wait-title">
            <Loader2 className="h-8 w-8 animate-spin motion-reduce:animate-none text-primary" aria-hidden="true" />
            <div className="space-y-3" role="status">
              <h1 id="upload-wait-title" className="text-2xl font-semibold">Waiting for upload confirmation</h1>
              <p className="text-muted-foreground">We haven’t received confirmation for this upload. Stopping here does not cancel a job the server may have accepted.</p>
            </div>
            <Button type="button" variant="outline" onClick={stopWaitingForUpload}>Stop waiting</Button>
          </section>
        ) : <LoadingScreen
          sessionId={currentSessionId}
          tutorialMode={sampleTutorialActive}
          minDisplayMs={sampleTutorialActive ? 4500 : 0}
        />}
      </Layout>
    );
  }

  // The risk number gets its own beat: what's at stake, before we spend time
  // producing a map of it. Rendered from the same component used inline on
  // review, so this can be folded back with a config change.
  if (showRisk && risk) {
    return (
      <Layout title="Traffic at risk">
        <div className="max-w-3xl">
          <p className="text-sm text-muted-foreground mb-6">
            Before we map anything — here&rsquo;s what this migration is putting at risk.
          </p>
          <TrafficRiskPanel
            risk={risk}
            variant="screen"
            isBusy={isUploading}
            onContinue={() => handleBeginMatching()}
            continueLabel={
              effectivePipelineType === 'content'
                ? 'Map these to the new site'
                : 'Match these to the new site'
            }
          />
          <button
            type="button"
            onClick={() => setShowRisk(false)}
            className="mt-6 text-sm text-muted-foreground underline-offset-4 hover:underline"
          >
            Back to domains
          </button>
        </div>
      </Layout>
    );
  }

  return (
    <Layout title={layoutTitle}>
      <div className="max-w-5xl">
          {/* Headline / Subtitle */}
          {isQuickOnlyMode ? (
            <div className="mb-8">
              <h1 className="text-3xl font-semibold tracking-tight text-foreground md:text-4xl">
                Free Redirect Map Generator
              </h1>
              <p className="mt-3 text-muted-foreground">
                Paste two domains. Get matched redirects in seconds.
              </p>
            </div>
          ) : (
            <div className="mb-8">
              <p className="text-muted-foreground">
                {sampleTutorialActive
                  ? 'Sample CSV files are preloaded. Click Begin Matching to run the tutorial sample and continue to review.'
                  : 'Upload URL lists from your old and new site to begin the redirect mapping process.'}
              </p>
            </div>
          )}

          {/* Pipeline Type Selector / Tier Banner */}
          {isQuickOnlyMode ? null : isFreeUser ? (
            <div className="mb-6 border border-blue-500/30 bg-blue-500/5 p-4 flex items-start gap-3">
              <Info className="h-5 w-5 text-blue-500 flex-shrink-0 mt-0.5" />
              <div>
                <div className="font-medium text-blue-600 dark:text-blue-400">Free Plan: Quick Match</div>
                <p className="text-sm text-muted-foreground mt-1">
                  Quick Match is always free. Unlock Deep Match per project or choose the Agency plan for recurring migrations.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => navigate('/pricing')}
                >
                  View Pricing
                </Button>
              </div>
            </div>
          ) : (
            <div className="mb-6 grid grid-cols-2 gap-4">
              <button
                onClick={() => handlePipelineTypeChange('content')}
                className={`border p-4 text-left transition-colors ${
                  pipelineType === 'content'
                    ? 'border-primary bg-primary/5'
                    : 'border-border bg-card hover:bg-accent'
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <Zap className="h-4 w-4 text-primary" />
                  <span className="font-medium text-foreground">Deep Match</span>
                </div>
                <p className="text-sm text-muted-foreground">
                  Scrapes page content and uses AI embeddings for semantic matching. Most accurate.
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Available for Agency plans and unlocked project runs.
                </p>
              </button>
              <button
                onClick={() => handlePipelineTypeChange('url_only')}
                className={`border p-4 text-left transition-colors ${
                  pipelineType === 'url_only'
                    ? 'border-primary bg-primary/5'
                    : 'border-border bg-card hover:bg-accent'
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <Search className="h-4 w-4 text-primary" />
                  <span className="font-medium text-foreground">Quick Match</span>
                  <span className="text-xs bg-green-500/10 text-green-600 dark:text-green-400 px-1.5 py-0.5 rounded font-medium">Free</span>
                </div>
                <p className="text-sm text-muted-foreground">
                  URL pattern matching only. Fastest, no scraping required.
                </p>
              </button>
            </div>
          )}

          {/* Content URL Cap Error */}
          {contentCapPanelData && (
            <div className="mb-6 border border-destructive bg-destructive/10 p-4 flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium text-destructive">Deep Match URL Limit Reached</div>
                <p className="text-sm text-muted-foreground mt-1">{contentCapPanelData.message}</p>

                {contentCapPanelData.showOld && (
                  <p className="text-sm text-foreground mt-2">
                    Old Site CSV has {contentCapPanelData.oldCount.toLocaleString()} URLs; Deep Match allows up to {contentCapPanelData.maxOld.toLocaleString()}.
                  </p>
                )}
                {contentCapPanelData.showNew && (
                  <p className="text-sm text-foreground mt-1">
                    New Site CSV has {contentCapPanelData.newCount.toLocaleString()} URLs; Deep Match allows up to {contentCapPanelData.maxNew.toLocaleString()}.
                  </p>
                )}

                {!isFreeUser && effectivePipelineType === 'content' && (
                  <div className="flex gap-3 mt-3">
                    <Button
                      variant="default"
                      size="sm"
                      onClick={handleSwitchToQuickMatch}
                    >
                      Switch to Quick Match
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* General Error */}
          {error && (
            <div role="alert" className="mb-6 border border-destructive bg-destructive/10 p-4 text-destructive">
              <p>{error}</p>
              {uploadNotConfirmed && (
                <Link to="/projects" className="mt-3 inline-block underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4">
                  Check Project History
                </Link>
              )}
            </div>
          )}

          {/* Duplicate Warning */}
          {duplicateSessionId && (
            <div className="mb-6 border border-yellow-500 bg-yellow-500/10 p-4 flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-yellow-500 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium text-yellow-600 dark:text-yellow-400">Duplicate Request Detected</div>
                <p className="text-sm text-muted-foreground mt-1">
                  You've already uploaded these exact files before. You can view the existing results or force a new run.
                </p>
                <div className="flex gap-3 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => navigate(`/review/${duplicateSessionId}`)}
                  >
                    View Existing Results
                  </Button>
                  <Button
                    variant="default"
                    size="sm"
                    onClick={handleProceedAnyway}
                  >
                    Proceed Anyway
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Validation Warnings */}
          {pendingWarnings && (
            <div className="mb-6 border border-yellow-500 bg-yellow-500/10 p-4 flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-yellow-500 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium text-yellow-600 dark:text-yellow-400">File Warnings Detected</div>
                <p className="text-sm text-muted-foreground mt-1">
                  The following non-blocking warnings were found in your files. You can continue, but processing may take longer or encounter issues.
                </p>
                {pendingWarnings.old.length > 0 && (
                  <div className="mt-3">
                    <div className="text-sm font-medium text-yellow-600 dark:text-yellow-400">Old Site CSV:</div>
                    <ul className="list-disc list-inside text-sm text-muted-foreground mt-1">
                      {pendingWarnings.old.map((warning, idx) => (
                        <li key={idx}>{warning}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {pendingWarnings.new.length > 0 && (
                  <div className="mt-3">
                    <div className="text-sm font-medium text-yellow-600 dark:text-yellow-400">New Site CSV:</div>
                    <ul className="list-disc list-inside text-sm text-muted-foreground mt-1">
                      {pendingWarnings.new.map((warning, idx) => (
                        <li key={idx}>{warning}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <div className="flex gap-3 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleCancelWarnings}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="default"
                    size="sm"
                    onClick={handleProceedWithWarnings}
                  >
                    Proceed Anyway
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Scraping Warning (Deep Match only) */}
          {showScrapingWarning && (
            <div className="mb-6 border border-orange-500 bg-orange-500/10 p-4 flex items-start gap-3">
              <ShieldAlert className="h-5 w-5 text-orange-500 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium text-orange-600 dark:text-orange-400">
                  We&rsquo;ll read content from your pages
                </div>
                <p className="text-sm text-muted-foreground mt-1">
                  Deep Match needs the text on each page. Where we can, we read it in bulk
                  through your CMS — on WordPress that&rsquo;s a few API calls instead of one
                  request per page. Otherwise we fetch pages directly, about one per second,
                  backing off if your server pushes back.
                </p>
                <div className="mt-3 text-sm text-muted-foreground">
                  <p className="font-medium text-foreground mb-2">
                    If your site blocks bots, allowlist us rather than turning protection off:
                  </p>
                  <ul className="list-disc list-inside space-y-1">
                    <li>
                      Wordfence → All Options → Whitelisted Services, or allow the{' '}
                      <code className="text-xs">RedirxBot</code> user agent
                    </li>
                    <li>Cloudflare → WAF → add a skip rule for <code className="text-xs">RedirxBot</code></li>
                  </ul>
                  <p className="mt-2 text-xs text-muted-foreground">
                    If we do get blocked we&rsquo;ll stop rather than hammer your server, fall
                    back to archived copies where they exist, and tell you exactly which pages
                    we couldn&rsquo;t read.
                  </p>
                </div>
                <label className="flex items-center gap-2 mt-4 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={scrapingWarningAcknowledged}
                    onChange={(e) => setScrapingWarningAcknowledged(e.target.checked)}
                    className="h-4 w-4 rounded border-border accent-primary"
                  />
                  <span className="text-sm text-foreground">
                    Got it — read content from my pages
                  </span>
                </label>
                <div className="flex gap-3 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleCancelScrapingWarning}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="default"
                    size="sm"
                    disabled={!scrapingWarningAcknowledged}
                    onClick={handleConfirmScrapingWarning}
                  >
                    Proceed with Deep Match
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Upload Zones */}
          {tutorialActive && showCoachmarks && (
            <div className="mb-4 border border-[#26D99D] bg-[#26D99D]/16 dark:bg-[#26D99D]/24 p-3 text-sm text-[#064731] dark:text-[#E9FFF8] font-medium">
              {sampleTutorialActive
                ? <>Step 2 of 4: Sample CSV files are ready. Click Begin Matching to run the sample job. Press <kbd className="mx-1 rounded border border-[#26D99D]/70 px-1 py-0.5 text-xs font-semibold">Esc</kbd> to hide these hints.</>
                : <>Step 2 of 4: Upload both CSV files, then run matching. Press <kbd className="mx-1 rounded border border-[#26D99D]/70 px-1 py-0.5 text-xs font-semibold">Esc</kbd> to hide these hints.</>
              }
            </div>
          )}
          {ingestMode === 'domains' && (
            <div className="mb-8">
              {/* The two sides do different jobs and shouldn't look identical:
                  the old site is where the traffic to protect lives, and the
                  new one isn't indexed yet so Search Console has nothing to
                  say about it. */}
              <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
                <DomainDiscoveryPanel
                  side="old"
                  label="Old site — what you're replacing"
                  hint="Import pages that get traffic. Read-only Search Console access, finds only your ranking pages."
                  emphasis
                  onDiscovered={handleDomainDiscovered}
                />
                <DomainDiscoveryPanel
                  side="new"
                  label="New site — where it's moving"
                  hint="Read from your sitemap. Search Console can't help here yet — the new pages aren't indexed."
                  onDiscovered={handleDomainDiscovered}
                />
              </div>
              <p className="mt-4 text-xs text-muted-foreground">
                Already have a URL list?{' '}
                <button
                  type="button"
                  onClick={() => handleIngestModeChange('csv')}
                  className="underline underline-offset-4 hover:text-foreground"
                >
                  Import a CSV or sitemap file
                </button>
                .
              </p>
            </div>
          )}

          {ingestMode === 'csv' && (
            <p className="mb-4 text-xs text-muted-foreground">
              Importing a file.{' '}
              <button
                type="button"
                onClick={() => handleIngestModeChange('domains')}
                className="underline underline-offset-4 hover:text-foreground"
              >
                Paste domains instead
              </button>
              .
            </p>
          )}

          {ingestMode === 'csv' && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <div className={tutorialActive && showCoachmarks ? 'rounded-md ring-2 ring-[#26D99D]/75 p-2 bg-[#26D99D]/8 dark:bg-[#26D99D]/14' : ''}>
              <FileUploadZone
                label="Old Site CSV"
                onFileUpload={(file) => handleFileUpload(file, 'old')}
                onFileRemove={() => handleFileRemove('old')}
                file={oldSiteFile}
                checkingFileName={checkingFiles.old}
                validationError={oldFileValidation && !oldFileValidation.valid ? oldFileValidation.errors.join(', ') : null}
              />
              {contentCapPanelData?.showOld && effectivePipelineType === 'content' && (
                <p className="mt-2 text-xs text-destructive">
                  Old Site CSV has {contentCapPanelData.oldCount.toLocaleString()} URLs; Deep Match allows up to {contentCapPanelData.maxOld.toLocaleString()}.
                </p>
              )}
            </div>
            <div className={tutorialActive && showCoachmarks ? 'rounded-md ring-2 ring-[#26D99D]/75 p-2 bg-[#26D99D]/8 dark:bg-[#26D99D]/14' : ''}>
              <FileUploadZone
                label="New Site CSV"
                onFileUpload={(file) => handleFileUpload(file, 'new')}
                onFileRemove={() => handleFileRemove('new')}
                file={newSiteFile}
                checkingFileName={checkingFiles.new}
                validationError={newFileValidation && !newFileValidation.valid ? newFileValidation.errors.join(', ') : null}
              />
              {contentCapPanelData?.showNew && effectivePipelineType === 'content' && (
                <p className="mt-2 text-xs text-destructive">
                  New Site CSV has {contentCapPanelData.newCount.toLocaleString()} URLs; Deep Match allows up to {contentCapPanelData.maxNew.toLocaleString()}.
                </p>
              )}
            </div>
          </div>
          )}

          {/* File Status */}
          {bothFilesUploaded && (
            <div className="mb-8 border border-border bg-card p-6">
              <div className="grid grid-cols-2 gap-6">
                <div>
                  <div className="text-sm text-muted-foreground mb-1">Old Site</div>
                  <div className="text-foreground">{oldSiteFile.name}</div>
                  <div className="text-sm text-muted-foreground">{oldSiteFile.rowCount} rows</div>
                </div>
                <div>
                  <div className="text-sm text-muted-foreground mb-1">New Site</div>
                  <div className="text-foreground">{newSiteFile.name}</div>
                  <div className="text-sm text-muted-foreground">{newSiteFile.rowCount} rows</div>
                </div>
              </div>
            </div>
          )}

          {/* Begin Matching Button */}
          <div>
            {tutorialActive && showCoachmarks && (
              <p className="text-xs text-[#0B6B4C] dark:text-[#D8FFF2] mb-2 font-medium">
                {sampleTutorialActive
                  ? 'This runs the preloaded sample files and moves you into a short processing step before review.'
                  : 'This button moves you to processing and marks the “Generate mappings” tutorial step complete.'}
              </p>
            )}
            <Button
              onClick={() => {
                if (riskBeatAvailable && !showRisk) {
                  setShowRisk(true);
                  posthog?.capture('traffic_risk_viewed', {
                    domain: risk?.domain,
                    top_url_count: risk?.topUrlCount,
                    total: risk?.total,
                    total_clicks: risk?.totalClicks,
                  });
                  return;
                }
                handleBeginMatching();
              }}
              disabled={!bothFilesUploaded || hasValidationErrors || isUploading}
              size="lg"
              className={`w-full ${tutorialActive && showCoachmarks ? 'ring-2 ring-[#26D99D]/85 ring-offset-2 ring-offset-background' : ''}`}
            >
              {isUploading && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
              {isUploading
                ? 'Processing...'
                : (isFreeUser || isQuickOnlyMode)
                  ? 'Begin Quick Match →'
                  : pipelineType === 'content'
                    ? 'Begin Deep Match →'
                    : 'Begin Quick Match →'
              }
            </Button>
            {hasValidationErrors && (
              <p className="text-sm text-destructive mt-2 text-center">
                {hasLocalContentCapViolation && effectivePipelineType === 'content'
                  ? 'Deep Match URL limit exceeded. Split your CSVs or switch to Quick Match.'
                  : 'Please fix validation errors before proceeding'}
              </p>
            )}
          </div>
      </div>
    </Layout>
  );
}
