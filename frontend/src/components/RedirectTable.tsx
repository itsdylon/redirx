import React, { useState, useEffect } from 'react';
import { ChevronDown, ChevronRight, Edit2, AlertTriangle, CheckCircle, Circle, Search, FileQuestion, Link2, Wand2, X } from 'lucide-react';
import { Checkbox } from './ui/checkbox';
import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Skeleton } from './ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui/table';
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from './ui/tooltip';
import { RedirectMapping } from './ReviewInterface';

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

function AnimatedBar({ value, delay = 0 }: { value: number; delay?: number }) {
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const timeout = setTimeout(() => setWidth(value), delay + 50);
    return () => clearTimeout(timeout);
  }, [value, delay]);

  return (
    <div className="mt-2 h-2 bg-muted rounded-full overflow-hidden">
      <div
        className="h-full bg-primary"
        style={{
          width: `${width}%`,
          transition: 'width 700ms ease-out',
        }}
      />
    </div>
  );
}

function AnimateIn({ delay = 0, children, className = '' }: { delay?: number; children: React.ReactNode; className?: string }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setVisible(true), delay + 10);
    return () => clearTimeout(timeout);
  }, [delay]);

  return (
    <div
      className={className}
      style={{
        opacity: visible ? 1 : 0,
        transform: visible ? 'translateY(0)' : 'translateY(8px)',
        transition: `opacity 300ms ease-out, transform 300ms ease-out`,
      }}
    >
      {children}
    </div>
  );
}

/**
 * A better target proposed from the site's own rename conventions.
 *
 * Shown directly beneath the current target so the comparison needs no
 * clicking, and always with the evidence: a suggestion a reviewer cannot check
 * is one they should not accept. Taking it is an ordinary edit — the row is
 * repointed and approved only after the server confirms the explicit acceptance.
 */
function RepairSuggestion({
  redirect,
  onAccept,
  onDismiss,
}: {
  redirect: RedirectMapping;
  onAccept?: (id: string) => void;
  onDismiss?: (id: string) => void;
}) {
  const repair = redirect.repair;
  if (!repair) return null;

  return (
    <div className="mt-2 rounded-md border border-primary/30 bg-primary/5 p-2">
      <div className="flex items-start gap-1.5">
        <Wand2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="font-mono text-sm break-all text-foreground">{repair.url}</div>
          <div className="mt-1 text-[11px] leading-snug text-muted-foreground">
            {repair.evidence}
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <Button
              size="sm"
              variant="default"
              className="h-6 px-2 text-xs"
              disabled={!onAccept}
              onClick={() => onAccept?.(redirect.id)}
            >
              Use this
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              disabled={!onDismiss}
              onClick={() => onDismiss?.(redirect.id)}
            >
              <X className="mr-1 h-3 w-3" />
              Dismiss
            </Button>
            <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">
              {repair.confidence}%
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

interface RedirectTableProps {
  redirects: RedirectMapping[];
  selectedRows: Set<string>;
  expandedRow: string | null;
  onToggleSelect: (id: string) => void;
  onToggleExpand: (id: string) => void;
  onEdit: (redirect: RedirectMapping) => void;
  onApprove: (id: string) => void;
  onAcceptRepair?: (id: string) => void;
  onDismissRepair?: (id: string) => void;
  hasActiveFilters?: boolean;
  onClearFilters?: () => void;
  totalRedirectsCount?: number;
  isLoading?: boolean;
  pipelineType?: string;
  showTraffic?: boolean;
  actionsDisabled?: boolean;
}

export function RedirectTable({
  redirects,
  selectedRows,
  expandedRow,
  onToggleSelect,
  onToggleExpand,
  onEdit,
  onApprove,
  onAcceptRepair,
  onDismissRepair,
  hasActiveFilters = false,
  onClearFilters,
  totalRedirectsCount = 0,
  isLoading = false,
  pipelineType = 'content',
  showTraffic = false,
  actionsDisabled = false
}: RedirectTableProps) {
  const columnCount = showTraffic ? 10 : 9;
  const isExactMatch = (redirect: RedirectMapping) =>
    redirect.matchType === 'exact_url';

  const getConfidenceBadge = (redirect: RedirectMapping) => {
    if (isExactMatch(redirect)) {
      return (
        <Badge className="bg-blue-100 text-blue-800 border-blue-300 dark:bg-blue-900/30 dark:text-blue-400 dark:border-blue-700">
          <Link2 className="h-3 w-3 mr-1" />
          Exact
        </Badge>
      );
    }
    switch (redirect.confidenceBand) {
      case 'high':
        return <Badge className="bg-green-100 text-green-800 border-green-300 dark:bg-green-900/30 dark:text-green-400 dark:border-green-700">High</Badge>;
      case 'medium':
        return <Badge className="bg-yellow-100 text-yellow-800 border-yellow-300 dark:bg-yellow-900/30 dark:text-yellow-400 dark:border-yellow-700">Medium</Badge>;
      case 'low':
        return <Badge className="bg-red-100 text-red-800 border-red-300 dark:bg-red-900/30 dark:text-red-400 dark:border-red-700">Low</Badge>;
      default:
        return null;
    }
  };

  const getWarningIcons = (warnings: string[]) => {
    return warnings.map((warning, index) => {
      let color = 'text-yellow-600';
      let title = warning;

      if (warning === 'duplicate-target') {
        color = 'text-red-600';
        title = 'This URL is already assigned to another redirect';
      } else if (warning === 'invalid-target') {
        color = 'text-orange-600';
        title = 'Target URL does not exist in new site';
      } else if (warning === 'near-tie') {
        color = 'text-yellow-600';
        title = 'Multiple URLs have similar confidence scores';
      }

      return (
        <Tooltip key={index}>
          <TooltipTrigger asChild>
            <AlertTriangle className={`h-4 w-4 ${color} cursor-help`} />
          </TooltipTrigger>
          <TooltipContent>
            {title}
          </TooltipContent>
        </Tooltip>
      );
    });
  };

  const renderSkeletonRow = (index: number) => (
    <TableRow key={`skeleton-${index}`}>
      {/* Expand button */}
      <TableCell>
        <Skeleton className="h-8 w-8 rounded" />
      </TableCell>
      {/* Checkbox */}
      <TableCell>
        <Skeleton className="h-4 w-4 rounded" />
      </TableCell>
      {/* Old URL */}
      <TableCell>
        <Skeleton className="h-4 w-full max-w-md" />
      </TableCell>
      {/* New URL */}
      <TableCell>
        <Skeleton className="h-4 w-full max-w-md" />
      </TableCell>
      {/* Clicks */}
      {showTraffic && (
        <TableCell className="text-right">
          <Skeleton className="h-4 w-12 ml-auto" />
        </TableCell>
      )}
      {/* Confidence Badge */}
      <TableCell>
        <Skeleton className="h-6 w-20 rounded-full" />
      </TableCell>
      {/* Score */}
      <TableCell className="text-center">
        <Skeleton className="h-4 w-12 mx-auto" />
      </TableCell>
      {/* Status */}
      <TableCell className="text-center">
        <Skeleton className="h-5 w-16 mx-auto" />
      </TableCell>
      {/* Warnings */}
      <TableCell className="text-center">
        <Skeleton className="h-4 w-4 mx-auto" />
      </TableCell>
      {/* Actions */}
      <TableCell>
        <Skeleton className="h-8 w-8 rounded" />
      </TableCell>
    </TableRow>
  );

  const getEmptyStateContent = () => {
    if (totalRedirectsCount === 0) {
      // No redirects at all
      return {
        icon: <FileQuestion className="h-12 w-12 text-muted-foreground mb-4" />,
        title: "No redirect mappings found",
        description: "Upload CSV files to get started.",
        showClearButton: false
      };
    } else if (hasActiveFilters) {
      // Active filters with no results
      return {
        icon: <Search className="h-12 w-12 text-muted-foreground mb-4" />,
        title: "No redirects match your filters",
        description: "Try adjusting your search or confidence level.",
        showClearButton: true
      };
    } else {
      // All filtered (shouldn't normally happen, but fallback)
      return {
        icon: <Search className="h-12 w-12 text-muted-foreground mb-4" />,
        title: "All redirects are hidden by current filters",
        description: "",
        showClearButton: true
      };
    }
  };

  return (
    <div className="border border-border bg-card overflow-hidden">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted">
            <TableHead className="w-12"><span className="sr-only">Details</span></TableHead>
            <TableHead className="w-12">
              <span className="sr-only">Selection</span>
              <Checkbox
                aria-label="Select all redirects on this page"
                checked={
                  redirects.length > 0 &&
                  redirects.every((r) => selectedRows.has(r.id))
                }
                onCheckedChange={(checked) => {
                  const shouldSelect = !!checked;
                  redirects.forEach((r) => {
                    const isSelected = selectedRows.has(r.id);
                    if (shouldSelect && !isSelected) {
                      onToggleSelect(r.id);
                    } else if (!shouldSelect && isSelected) {
                      onToggleSelect(r.id);
                    }
                  });
                }}
              />
            </TableHead>
            <TableHead className="text-foreground w-[30%]">Old URL</TableHead>
            <TableHead className="text-foreground w-[30%]">Suggested New URL</TableHead>
            {showTraffic && (
              <TableHead className="text-foreground w-24 text-right">Clicks</TableHead>
            )}
            <TableHead className="text-foreground w-32">Confidence</TableHead>
            <TableHead className="text-foreground w-24 text-center">Score</TableHead>
            <TableHead className="w-20 text-foreground text-center">Status</TableHead>
            <TableHead className="w-16 text-foreground text-center">Warnings</TableHead>
            <TableHead className="w-16"><span className="sr-only">Actions</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading ? (
            // Show skeleton rows while loading
            Array.from({ length: 10 }).map((_, index) => renderSkeletonRow(index))
          ) : redirects.length === 0 ? (
            <TableRow>
              <TableCell colSpan={columnCount} className="h-96">
                <div className="flex flex-col items-center justify-center text-center">
                  {(() => {
                    const emptyState = getEmptyStateContent();
                    return (
                      <>
                        {emptyState.icon}
                        <h3 className="text-lg font-semibold text-foreground mb-2">
                          {emptyState.title}
                        </h3>
                        <p className="text-sm text-muted-foreground mb-4">
                          {emptyState.description}
                        </p>
                        {emptyState.showClearButton && onClearFilters && (
                          <Button
                            variant="outline"
                            onClick={onClearFilters}
                          >
                            Clear Filters
                          </Button>
                        )}
                      </>
                    );
                  })()}
                </div>
              </TableCell>
            </TableRow>
          ) : (
            redirects.map((redirect) => (
            <React.Fragment key={redirect.id}>
              <TableRow
                className={`
                  ${isExactMatch(redirect) ? 'border-l-4 border-l-blue-500' : ''}
                  ${!isExactMatch(redirect) && redirect.confidenceBand === 'high' ? 'border-l-4 border-l-green-500' : ''}
                  ${!isExactMatch(redirect) && redirect.confidenceBand === 'medium' ? 'border-l-4 border-l-yellow-500' : ''}
                  ${!isExactMatch(redirect) && redirect.confidenceBand === 'low' ? 'border-l-4 border-l-red-500' : ''}
                `}
              >
                <TableCell>
                  <Button
                    aria-label={`${expandedRow === redirect.id ? 'Collapse' : 'Expand'} details for ${redirect.oldUrl}`}
                    aria-expanded={expandedRow === redirect.id}
                    aria-controls={expandedRow === redirect.id ? `redirect-details-${redirect.id}` : undefined}
                    variant="ghost"
                    size="sm"
                    onClick={() => onToggleExpand(redirect.id)}
                  >
                    {expandedRow === redirect.id ? (
                      <ChevronDown className="h-4 w-4" />
                    ) : (
                      <ChevronRight className="h-4 w-4" />
                    )}
                  </Button>
                </TableCell>
                <TableCell>
                  <Checkbox
                    aria-label={`Select redirect from ${redirect.oldUrl}`}
                    checked={selectedRows.has(redirect.id)}
                    onCheckedChange={() => onToggleSelect(redirect.id)}
                  />
                </TableCell>
                <TableCell className="max-w-0" title={redirect.oldUrl}>
                  <div className="text-foreground font-mono text-sm line-clamp-2 break-all">
                    {redirect.oldUrl}
                  </div>
                </TableCell>
                <TableCell className="max-w-0" title={redirect.newUrl}>
                  <div className="text-foreground font-mono text-sm line-clamp-2 break-all">
                    {redirect.newUrl}
                  </div>
                  {redirect.repair && (
                    <RepairSuggestion
                      redirect={redirect}
                      onAccept={actionsDisabled ? undefined : onAcceptRepair}
                      onDismiss={actionsDisabled ? undefined : onDismissRepair}
                    />
                  )}
                </TableCell>
                {showTraffic && (
                  <TableCell className="text-right">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className={`tabular-nums ${(redirect.gscClicks ?? 0) > 0 ? 'text-foreground' : 'text-muted-foreground'}`}>
                          {(redirect.gscClicks ?? 0).toLocaleString()}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent>
                        {(redirect.gscImpressions ?? 0).toLocaleString()} impressions (last 90 days)
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                )}
                <TableCell>
                  {getConfidenceBadge(redirect)}
                </TableCell>
                <TableCell className="text-center">
                  <span className="text-foreground">{redirect.matchScore}%</span>
                </TableCell>
                <TableCell className="text-center">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        aria-label={`${redirect.approved ? "Remove approval for" : "Approve"} ${redirect.oldUrl}`}
                        disabled={actionsDisabled}
                        onClick={() => onApprove(redirect.id)}
                        className={`mx-auto flex items-center justify-center rounded-md p-1 transition-colors ${
                          redirect.approved
                            ? 'text-green-600 hover:text-muted-foreground'
                            : 'text-muted-foreground hover:text-green-600'
                        }`}
                      >
                        {redirect.approved ? (
                          <CheckCircle className="h-5 w-5" />
                        ) : (
                          <Circle className="h-5 w-5" />
                        )}
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>
                      {redirect.approved ? 'Click to unapprove' : 'Click to approve'}
                    </TooltipContent>
                  </Tooltip>
                </TableCell>
                <TableCell>
                  <div className="flex items-center justify-center gap-1">
                    {getWarningIcons(redirect.warnings)}
                  </div>
                </TableCell>
                <TableCell>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Edit redirect for ${redirect.oldUrl}`}
                    disabled={actionsDisabled}
                    onClick={() => onEdit(redirect)}
                  >
                    <Edit2 className="h-4 w-4" />
                  </Button>
                </TableCell>
              </TableRow>

              {/* Expanded Row Details */}
              {expandedRow === redirect.id && (
                <TableRow id={`redirect-details-${redirect.id}`} className="bg-muted">
                  <TableCell colSpan={columnCount} className="p-6">
                    <AnimateIn>
                      {/* Full URLs */}
                      <div className="grid grid-cols-2 gap-4 mb-4">
                        <div>
                          <div className="text-xs text-muted-foreground mb-1">Old URL</div>
                          <div className="text-sm font-mono text-foreground break-all">{redirect.oldUrl}</div>
                        </div>
                        <div>
                          <div className="text-xs text-muted-foreground mb-1">New URL</div>
                          <div className="text-sm font-mono text-foreground break-all">{redirect.newUrl}</div>
                        </div>
                      </div>

                      {isExactMatch(redirect) ? (
                        <>
                          <div className="flex items-center gap-3 mb-4">
                            <Link2 className="h-5 w-5 text-blue-600" />
                            <h3 className="text-foreground">Exact URL Match</h3>
                          </div>
                          <AnimateIn delay={100} className="border border-blue-500/30 bg-blue-500/5 p-4 rounded-md">
                            <p className="text-sm text-muted-foreground">
                              The URL paths on the old and new sites are identical. No redirect rule is needed since the content will be served at the same path.
                            </p>
                          </AnimateIn>
                          <AnimateIn delay={200} className="mt-4 flex gap-3">
                            <Button
                              variant={redirect.approved ? "outline" : "default"}
                              size="sm"
                              aria-label={`${redirect.approved ? "Remove approval for" : "Approve"} ${redirect.oldUrl}`}
                        disabled={actionsDisabled}
                        onClick={() => onApprove(redirect.id)}
                            >
                              {redirect.approved ? 'Unapprove Match' : 'Approve Match'}
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              aria-label={`Edit redirect for ${redirect.oldUrl}`}
                    disabled={actionsDisabled}
                    onClick={() => onEdit(redirect)}
                            >
                              Edit Mapping
                            </Button>
                          </AnimateIn>
                        </>
                      ) : (
                        <>
                          <h3 className="text-foreground mb-4">Matching Details</h3>
                          <div className={`grid gap-6 ${pipelineType === 'url_only' ? 'grid-cols-2' : 'grid-cols-3'}`}>
                            {[
                              { label: 'Path Similarity', value: redirect.pathSimilarity ?? 0, delay: 0, show: true },
                              { label: 'Match Method', value: redirect.matchScore ?? 0, delay: 100, show: pipelineType === 'url_only' },
                              { label: 'Title Similarity', value: redirect.titleSimilarity ?? 0, delay: 100, show: pipelineType !== 'url_only' },
                              { label: 'Content Similarity', value: redirect.contentSimilarity ?? 0, delay: 200, show: pipelineType !== 'url_only' },
                            ].filter(card => card.show).map((card) => (
                              <AnimateIn
                                key={card.label}
                                delay={card.delay}
                                className="border border-border bg-card p-4"
                              >
                                <div className="text-muted-foreground text-sm mb-2">{card.label}</div>
                                <div className="flex items-end gap-1">
                                  <span className="text-foreground text-2xl tabular-nums">
                                    <AnimatedNumber value={card.value} delay={card.delay} />
                                  </span>
                                  <span className="text-foreground text-lg mb-px">%</span>
                                </div>
                                <AnimatedBar value={card.value} delay={card.delay} />
                              </AnimateIn>
                            ))}
                          </div>

                          {redirect.warnings.length > 0 && (
                            <AnimateIn delay={300} className="mt-4 p-4 border border-yellow-500/50 bg-yellow-500/10">
                              <h4 className="text-foreground text-sm mb-2">Warnings</h4>
                              <ul className="text-sm text-muted-foreground space-y-1">
                                {redirect.warnings
                                  .map((warning) => {
                                    const messages: Record<string, string> = {
                                      'duplicate-target': 'This URL is already assigned to another redirect',
                                      'invalid-target': 'Target URL does not exist in new site',
                                      'near-tie': 'Multiple URLs have similar confidence scores',
                                      'needs-review': 'This mapping requires manual review',
                                      'low-confidence': 'Low confidence match — review recommended',
                                    };
                                    return messages[warning] || null;
                                  })
                                  .filter(Boolean)
                                  .map((message, index) => (
                                    <li key={index} className="list-disc list-inside">
                                      {message}
                                    </li>
                                  ))}
                              </ul>
                            </AnimateIn>
                          )}

                          <AnimateIn delay={350} className="mt-4 flex gap-3">
                            <Button
                              variant={redirect.approved ? "outline" : "default"}
                              size="sm"
                              aria-label={`${redirect.approved ? "Remove approval for" : "Approve"} ${redirect.oldUrl}`}
                        disabled={actionsDisabled}
                        onClick={() => onApprove(redirect.id)}
                            >
                              {redirect.approved ? 'Unapprove Match' : 'Approve Match'}
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              aria-label={`Edit redirect for ${redirect.oldUrl}`}
                    disabled={actionsDisabled}
                    onClick={() => onEdit(redirect)}
                            >
                              Edit Mapping
                            </Button>
                            {pipelineType !== 'url_only' && (
                              <Button
                                variant="outline"
                                size="sm"
                                aria-label={`Edit redirect for ${redirect.oldUrl}`}
                    disabled={actionsDisabled}
                    onClick={() => onEdit(redirect)}
                              >
                                View Alternatives
                              </Button>
                            )}
                          </AnimateIn>
                        </>
                      )}
                    </AnimateIn>
                  </TableCell>
                </TableRow>
              )}
            </React.Fragment>
          )))}
        </TableBody>
      </Table>
    </div>
  );
}
