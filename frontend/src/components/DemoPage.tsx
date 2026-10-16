import { useState, useRef, useCallback, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Card } from './ui/card';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Badge } from './ui/badge';
import { Progress } from './ui/progress';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from './ui/table';
import {
  Globe,
  Search,
  FileText,
  AlertTriangle,
  Layers,
  Loader2,
  ChevronDown,
  ChevronRight,
  ArrowRight,
  Sparkles,
} from 'lucide-react';
import {
  startSiteAudit,
  DemoProgressEvent,
  DemoPageEvent,
  DemoCompleteEvent,
} from '../api/demo';

type AuditState = 'idle' | 'running' | 'complete' | 'error';

const CATEGORY_LABELS: Record<string, string> = {
  landing_page: 'Landing Page',
  blog_post: 'Blog Post',
  blog_landing: 'Blog Landing',
  product_page: 'Product Page',
  asset: 'Asset',
};

const CATEGORY_COLORS: Record<string, string> = {
  landing_page: 'bg-blue-500',
  blog_post: 'bg-purple-500',
  blog_landing: 'bg-indigo-500',
  product_page: 'bg-emerald-500',
  asset: 'bg-gray-400',
};

const ISSUE_LABELS: Record<string, string> = {
  missing_title: 'Missing Title',
  thin_content: 'Thin Content',
  duplicate_content: 'Duplicate Content',
  scrape_failed: 'Scrape Failed',
};

const PAGES_PER_PAGE = 20;

export function DemoPage() {
  const navigate = useNavigate();
  const [url, setUrl] = useState('');
  const [state, setState] = useState<AuditState>('idle');
  const [progress, setProgress] = useState<DemoProgressEvent | null>(null);
  const [pages, setPages] = useState<DemoPageEvent[]>([]);
  const [result, setResult] = useState<DemoCompleteEvent | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [expandedIssues, setExpandedIssues] = useState<Set<string>>(new Set());
  const [tablePage, setTablePage] = useState(0);
  const controllerRef = useRef<AbortController | null>(null);

  const requestId = useRef(0);
  useEffect(() => () => { requestId.current++; controllerRef.current?.abort(); }, []);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const trimmed = url.trim();
      if (!trimmed) return;

      // Prepend https:// if missing
      const finalUrl = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;

      controllerRef.current?.abort();
      const current = ++requestId.current;
      // Reset state
      setPages([]);
      setResult(null);
      setErrorMsg('');
      setProgress(null);
      setTablePage(0);
      setExpandedIssues(new Set());
      setState('running');

      controllerRef.current = startSiteAudit(finalUrl, {
        onProgress: (data: DemoProgressEvent) => { if (current === requestId.current) setProgress(data); },
        onPage: (data: DemoPageEvent) => { if (current === requestId.current) setPages((prev) => [...prev, data]); },
        onComplete: (data: DemoCompleteEvent) => {
          if (current !== requestId.current) return;
          setResult(data);
          setState('complete');
        },
        onError: (data) => {
          if (current !== requestId.current) return;
          setErrorMsg(data.message);
          setState('error');
        },
      });
    },
    [url],
  );

  const handleCancel = () => {
    requestId.current++;
    controllerRef.current?.abort();
    setState('idle');
  };

  const toggleIssue = (issue: string) => {
    setExpandedIssues((prev) => {
      const next = new Set(prev);
      if (next.has(issue)) next.delete(issue);
      else next.add(issue);
      return next;
    });
  };

  // Paginated pages for table
  const paginatedPages = (result?.pages || pages).slice(
    tablePage * PAGES_PER_PAGE,
    (tablePage + 1) * PAGES_PER_PAGE,
  );
  const totalTablePages = Math.ceil((result?.pages || pages).length / PAGES_PER_PAGE);

  // --- Landing State ---
  if (state === 'idle') {
    return (
      <div className="min-h-screen bg-background">
        {/* Nav */}
        <nav className="border-b border-border">
          <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
            <span className="text-lg font-bold text-foreground">Redirx</span>
            <Link to="/login" className="text-sm text-muted-foreground hover:text-foreground">
              Log in
            </Link>
          </div>
        </nav>

        <div className="max-w-3xl mx-auto px-6 pt-24 pb-16 text-center">
          <h1 className="text-4xl font-bold text-foreground tracking-tight">
            Free Site Audit
          </h1>
          <p className="mt-4 text-lg text-muted-foreground max-w-xl mx-auto">
            Enter your website URL and preview discovered pages, content quality, and detected issues. No sign-up required.
          </p>

          <p className="mt-3 text-sm text-muted-foreground">This site audit is separate from URL-only migration matching. Review migration proposals in your companion before exporting.</p>

          <form onSubmit={handleSubmit} className="mt-10 flex gap-3 max-w-lg mx-auto">
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              aria-label="Website to audit"
              placeholder="example.com"
              className="flex-1"
              autoFocus
            />
            <Button type="submit" disabled={!url.trim()}>
              Audit My Site
            </Button>
          </form>
        </div>

        {/* Feature cards */}
        <div className="max-w-4xl mx-auto px-6 pb-24 grid grid-cols-1 md:grid-cols-3 gap-6">
          <Card className="p-6">
            <FileText className="h-8 w-8 text-muted-foreground mb-3" />
            <h3 className="font-semibold text-foreground mb-1">Page Inventory</h3>
            <p className="text-sm text-muted-foreground">
              Discover pages through available sitemaps and crawling. This preview may not include every URL.
            </p>
          </Card>
          <Card className="p-6">
            <Search className="h-8 w-8 text-muted-foreground mb-3" />
            <h3 className="font-semibold text-foreground mb-1">Content Quality</h3>
            <p className="text-sm text-muted-foreground">
              Detect thin content, missing titles, and duplicate pages.
            </p>
          </Card>
          <Card className="p-6">
            <Layers className="h-8 w-8 text-muted-foreground mb-3" />
            <h3 className="font-semibold text-foreground mb-1">Site Structure</h3>
            <p className="text-sm text-muted-foreground">
              See URL depth, page categories, and how your site is organized.
            </p>
          </Card>
        </div>

        <div className="text-center pb-12 text-sm text-muted-foreground">
          Already have an account?{' '}
          <Link to="/login" className="text-primary hover:underline font-medium">
            Log in
          </Link>
        </div>
      </div>
    );
  }

  // --- Running / Complete / Error States ---
  const showResults = state === 'complete' && result;
  const totalPagesDisplay = result?.pages || pages;

  return (
    <div className="min-h-screen bg-background">
      {/* Nav */}
      <nav className="border-b border-border">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link to="/demo" onClick={handleCancel} className="text-lg font-bold text-foreground">
            Redirx
          </Link>
          <Link to="/login" className="text-sm text-muted-foreground hover:text-foreground">
            Log in
          </Link>
        </div>
      </nav>

      <div className="max-w-6xl mx-auto px-6 py-8">
        {/* Error */}
        {state === 'error' && (
          <Card className="p-6 mb-6 border-destructive/50">
            <div className="flex items-center gap-3">
              <AlertTriangle className="h-5 w-5 text-destructive" />
              <div>
                <p className="font-medium text-foreground">Audit Failed</p>
                <p className="text-sm text-muted-foreground">{errorMsg}</p>
              </div>
            </div>
            <Button variant="outline" className="mt-4" onClick={() => setState('idle')}>
              Try Another URL
            </Button>
          </Card>
        )}

        {/* Progress (while running) */}
        {state === 'running' && (
          <Card className="p-6 mb-6">
            <div className="flex items-center gap-3 mb-3">
              <Loader2 className="h-5 w-5 animate-spin text-primary" />
              <span role="status" className="font-medium text-foreground">{progress?.message || 'Connecting to the site audit…'}</span>
            </div>
            {progress?.phase === 'scraping' && progress.total_pages && (
              <Progress
                value={((progress.pages_scraped || 0) / progress.total_pages) * 100}
                className="h-2"
              />
            )}
            <div className="mt-3 flex justify-end">
              <Button variant="ghost" size="sm" onClick={handleCancel}>
                Cancel
              </Button>
            </div>
          </Card>
        )}

        {/* Live page count while running */}
        {state === 'running' && pages.length > 0 && (
          <p className="text-sm text-muted-foreground mb-4">
            {pages.length} page{pages.length !== 1 ? 's' : ''} analyzed so far...
          </p>
        )}

        {/* Results */}
        {showResults && (
          <>
            {/* Summary Cards */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
              <Card className="!gap-0 p-5">
                <span className="text-sm text-muted-foreground">Total Pages</span>
                <div className="text-3xl font-semibold text-foreground mt-2">
                  {result.summary.pages_audited}
                </div>
                {result.summary.total_pages > result.summary.pages_audited && (
                  <p className="text-xs text-muted-foreground mt-1">
                    of {result.summary.total_pages} discovered
                  </p>
                )}
              </Card>
              <Card className="!gap-0 p-5">
                <span className="text-sm text-muted-foreground">Content Issues</span>
                <div className="text-3xl font-semibold text-foreground mt-2">
                  {result.summary.total_issues}
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  {Object.keys(result.issues).length} issue type{Object.keys(result.issues).length !== 1 ? 's' : ''}
                </p>
              </Card>
              <Card className="!gap-0 p-5">
                <span className="text-sm text-muted-foreground">Site Depth</span>
                <div className="text-3xl font-semibold text-foreground mt-2">
                  {result.summary.max_depth}
                </div>
                <p className="text-xs text-muted-foreground mt-1">max levels deep</p>
              </Card>
              <Card className="!gap-0 p-5">
                <span className="text-sm text-muted-foreground">Discovery</span>
                <div className="text-xl font-semibold text-foreground mt-2 capitalize">
                  {result.summary.discovery_method.replace('_', ' ')}
                </div>
                <p className="text-xs text-muted-foreground mt-1">URL discovery method</p>
              </Card>
            </div>

            {/* Category Breakdown */}
            {Object.keys(result.categories).length > 0 && (
              <Card className="!gap-0 p-5 mb-6">
                <span className="text-sm text-muted-foreground mb-4 block">
                  Page Categories
                </span>
                {/* Stacked bar */}
                <div className="flex h-4 rounded-full overflow-hidden">
                  {Object.entries(result.categories).map(([cat, count]) => (
                    <div
                      key={cat}
                      className={`${CATEGORY_COLORS[cat] || 'bg-gray-400'} transition-all`}
                      style={{
                        width: `${(count / result.summary.pages_audited) * 100}%`,
                      }}
                    />
                  ))}
                </div>
                {/* Legend */}
                <div className="flex flex-wrap items-center gap-x-6 gap-y-2 mt-4 text-sm">
                  {Object.entries(result.categories).map(([cat, count]) => (
                    <div key={cat} className="flex items-center gap-1.5">
                      <div
                        className={`w-2.5 h-2.5 rounded-full ${CATEGORY_COLORS[cat] || 'bg-gray-400'}`}
                      />
                      <span className="text-muted-foreground">
                        {CATEGORY_LABELS[cat] || cat}
                      </span>
                      <span className="font-medium text-foreground">{count}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}

            {/* Issues Panel */}
            {Object.keys(result.issues).length > 0 && (
              <Card className="!gap-0 p-5 mb-6">
                <span className="text-sm text-muted-foreground mb-3 block">Issues Found</span>
                <div className="space-y-2">
                  {Object.entries(result.issues).map(([issue, info]) => (
                    <div key={issue} className="border border-border rounded-md">
                      <button
                        className="w-full flex items-center justify-between p-3 text-left hover:bg-muted/50 transition-colors"
                        onClick={() => toggleIssue(issue)}
                      >
                        <div className="flex items-center gap-2">
                          {expandedIssues.has(issue) ? (
                            <ChevronDown className="h-4 w-4 text-muted-foreground" />
                          ) : (
                            <ChevronRight className="h-4 w-4 text-muted-foreground" />
                          )}
                          <span className="text-sm font-medium text-foreground">
                            {ISSUE_LABELS[issue] || issue}
                          </span>
                        </div>
                        <Badge variant="secondary">{info.count}</Badge>
                      </button>
                      {expandedIssues.has(issue) && (
                        <div className="px-3 pb-3 pt-1 border-t border-border">
                          <ul className="space-y-1">
                            {info.urls.map((u) => (
                              <li
                                key={u}
                                className="text-xs text-muted-foreground font-mono truncate"
                              >
                                {new URL(u).pathname}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Card>
            )}

            {/* Pages Table */}
            <Card className="!gap-0 mb-6">
              <div className="p-5 pb-0">
                <span className="text-sm text-muted-foreground">
                  All Pages ({totalPagesDisplay.length})
                </span>
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5">URL Path</TableHead>
                    <TableHead>Title</TableHead>
                    <TableHead>Category</TableHead>
                    <TableHead>Length</TableHead>
                    <TableHead>Issues</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginatedPages.map((page) => (
                    <TableRow key={page.url}>
                      <TableCell className="pl-5 font-mono text-xs max-w-[200px] truncate">
                        {(() => {
                          try {
                            return new URL(page.url).pathname;
                          } catch {
                            return page.url;
                          }
                        })()}
                      </TableCell>
                      <TableCell className="max-w-[200px] truncate text-sm">
                        {page.title || <span className="text-muted-foreground italic">No title</span>}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          {CATEGORY_LABELS[page.category] || page.category}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {page.content_length.toLocaleString()}
                      </TableCell>
                      <TableCell>
                        {page.issues.length > 0 ? (
                          <div className="flex flex-wrap gap-1">
                            {page.issues.map((iss) => (
                              <Badge key={iss} variant="destructive" className="text-xs">
                                {ISSUE_LABELS[iss] || iss}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">None</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {/* Pagination */}
              {totalTablePages > 1 && (
                <div className="flex items-center justify-between px-5 py-3 border-t border-border">
                  <span className="text-xs text-muted-foreground">
                    Page {tablePage + 1} of {totalTablePages}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={tablePage === 0}
                      onClick={() => setTablePage((p) => p - 1)}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={tablePage >= totalTablePages - 1}
                      onClick={() => setTablePage((p) => p + 1)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              )}
            </Card>

            {/* Conversion CTA */}
            <Card className="p-6 mb-8 border-primary/30 bg-primary/5">
              <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
                <div className="flex items-start gap-3">
                  <Sparkles className="h-6 w-6 text-primary mt-0.5" />
                  <div>
                    <h3 className="font-semibold text-foreground">
                      Your site has {result.summary.pages_audited} pages. Ready to migrate?
                    </h3>
                    <p className="text-sm text-muted-foreground mt-1">
                      Redirx automatically generates 301 redirect mappings when you move to a new domain.
                      Upload your old and new site URLs to get started.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3 shrink-0">
                  <Button onClick={() => navigate('/signup')}>
                    Sign Up Free
                    <ArrowRight className="ml-2 h-4 w-4" />
                  </Button>
                </div>
              </div>
            </Card>
          </>
        )}

        {/* Running: show pages streaming in as a minimal table */}
        {state === 'running' && pages.length > 0 && (
          <Card className="!gap-0">
            <div className="p-5 pb-0">
              <span className="text-sm text-muted-foreground">Pages Found ({pages.length})</span>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-5">URL Path</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Issues</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pages.slice(-10).map((page) => (
                  <TableRow key={page.url}>
                    <TableCell className="pl-5 font-mono text-xs max-w-[200px] truncate">
                      {(() => {
                        try {
                          return new URL(page.url).pathname;
                        } catch {
                          return page.url;
                        }
                      })()}
                    </TableCell>
                    <TableCell className="max-w-[200px] truncate text-sm">
                      {page.title || <span className="text-muted-foreground italic">No title</span>}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {CATEGORY_LABELS[page.category] || page.category}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {page.issues.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {page.issues.map((iss) => (
                            <Badge key={iss} variant="destructive" className="text-xs">
                              {ISSUE_LABELS[iss] || iss}
                            </Badge>
                          ))}
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">None</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </div>
    </div>
  );
}
