import { useProjectActions } from './history/useProjectActions';
import { DeleteProjectDialog } from './history/DeleteProjectDialog';
import { MCP_PIVOT_ENABLED } from '../api/config';
import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { usePostHog } from '@posthog/react';
import { DashboardLayout } from './DashboardLayout';
import { ToolLayout } from './ToolLayout';
import { Card } from './ui/card';
import { Button } from './ui/button';
import { Clock, Pencil, Check, X, Loader2, Trash2, Search, ArrowLeft } from 'lucide-react';
import { fetchSessionPage } from '../api/sessions';
import { formatDate } from '../utils/date';
import { queryKeys } from '../queries/queryKeys';
import { handleUnauthorizedAndRedirect } from '../queries/auth';
import { useAuth } from '../contexts/AuthContext';
import { isAgencyPlan } from '../lib/plans';

export function AllProjects() {
  const { user } = useAuth();
  if (!user) return null;
  return <AllProjectsContent key={user.id} ownerId={user.id} />;
}

function AllProjectsContent({ ownerId }: { ownerId: string }) {
  const navigate = useNavigate();
  const posthog = usePostHog();
  const { user } = useAuth();
  const [searchQuery, setSearchQuery] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const cursor = cursors[cursors.length - 1];
  const pageSize = 50;
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null);
  const agencyUser = isAgencyPlan(user?.plan);
  const pageTitle = agencyUser && !MCP_PIVOT_ENABLED ? 'All Projects' : 'Project History';
  const Layout = agencyUser && !MCP_PIVOT_ENABLED ? DashboardLayout : ToolLayout;

  const sessionsQuery = useQuery({
    queryKey: queryKeys.sessions.page(ownerId, appliedSearch, cursor, pageSize),
    queryFn: ({ signal }) => fetchSessionPage({ search: appliedSearch, cursor, limit: pageSize }, signal),
    retry: false,
  });

  const sessions = sessionsQuery.data?.sessions || [];
  const loading = sessionsQuery.isLoading;
  const error = sessionsQuery.error instanceof Error ? sessionsQuery.error.message : '';

  const firstPage = () => {
    setCursors([null]);
    if (cursor === null) void sessionsQuery.refetch();
  };
  const searchHistory = () => {
    const nextSearch = searchQuery.trim();
    setAppliedSearch(nextSearch); setCursors([null]);
    if (nextSearch === appliedSearch && cursor === null) void sessionsQuery.refetch();
  };

  useEffect(() => {
    if (sessionsQuery.error) {
      handleUnauthorizedAndRedirect(sessionsQuery.error, navigate);
    }
  }, [navigate, sessionsQuery.error]);

  useEffect(() => {
    posthog?.capture('project_history_opened', {
      plan: user?.plan || 'free',
      source: 'projects',
    });
  }, [posthog, user?.plan]);

  const { rename: updateSessionNameMutation, remove: deleteSessionMutation } = useProjectActions(ownerId, {
    onRenamed: () => { setEditingSessionId(null); setEditingName(''); },
    onDeleted: () => setDeletingSessionId(null),
  });

  const isDeleting = deletingSessionId !== null && deleteSessionMutation.isPending;

  const handleStartEdit = (sessionId: string, currentName: string) => {
    setEditingSessionId(sessionId);
    setEditingName(currentName || 'Untitled Session');
  };

  const handleCancelEdit = () => {
    setEditingSessionId(null);
    setEditingName('');
  };

  const handleSaveEdit = (sessionId: string) => {
    if (!updateSessionNameMutation.isPending && editingName.trim()) updateSessionNameMutation.mutate({ sessionId, projectName: editingName.trim() });
  };

  const handleDeleteClick = (sessionId: string) => {
    deleteSessionMutation.reset();
    setDeletingSessionId(sessionId);
  };

  const handleConfirmDelete = () => {
    if (!deletingSessionId) return;
    deleteSessionMutation.mutate({ sessionId: deletingSessionId });
  };

  const handleCancelDelete = () => {
    setDeletingSessionId(null);
  };

  return (
    <Layout title={pageTitle}>
      <div className="mb-4">
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate(MCP_PIVOT_ENABLED ? '/companion' : agencyUser ? '/dashboard' : '/quick-match')}
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Go back
        </Button>
      </div>

      {/* Search Bar */}
      <form className="mb-6 space-y-3" onSubmit={event => { event.preventDefault(); searchHistory(); }}>
        <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-0 flex-1 basis-64">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            aria-label="Search project history"
            placeholder="Search projects by name, status, or match type..."
            maxLength={200}
            aria-describedby="history-search-hint"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-border bg-background text-foreground rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
          />
        </div>
        <Button type="submit" disabled={sessionsQuery.isFetching}>Search</Button>
        <Button type="button" variant="outline" onClick={firstPage} disabled={sessionsQuery.isFetching}>Reload first page</Button>
        </div>
        <p id="history-search-hint" className="text-sm text-muted-foreground">Search all project history by name, status, or match type. Reload the first page to see newer projects.</p>
      </form>
      <div role="status" aria-live="polite" className="mb-4 text-sm text-muted-foreground">
        {sessionsQuery.isFetching ? 'Loading projects…' : !error && sessionsQuery.data
          ? `${sessionsQuery.data.total_count.toLocaleString()} ${appliedSearch ? 'matching ' : ''}${sessionsQuery.data.total_count === 1 ? 'project' : 'projects'} · Page ${cursors.length}` : null}
      </div>
      {error && <div role="alert" className="mb-4 space-y-3">
        <p className="text-destructive">{error}</p>
        <Button onClick={() => { void sessionsQuery.refetch(); }} disabled={sessionsQuery.isFetching}>Retry</Button>
      </div>}

      {/* Projects Table */}
      <Card>
        <div className="overflow-x-auto" role="region" aria-label="Project history table" tabIndex={0}>
          <table className="w-full min-w-[48rem]">
            <thead className="bg-muted border-b border-border">
              <tr>
                <th className="text-left p-4 text-muted-foreground text-sm">Project Name</th>
                <th className="text-left p-4 text-muted-foreground text-sm">Date</th>
                <th className="text-left p-4 text-muted-foreground text-sm">Redirects</th>
                <th className="text-left p-4 text-muted-foreground text-sm">Match Type</th>
                <th className="text-left p-4 text-muted-foreground text-sm">Status</th>
                <th className="text-left p-4 text-muted-foreground text-sm">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(!error && !loading ? sessions : []).map((session, index) => (
                <tr
                  key={session.id}
                  className={index !== sessions.length - 1 ? 'border-b border-border' : ''}
                >
                  <td className="min-w-56 p-4 text-foreground">
                    {editingSessionId === session.id ? (
                      <div className="flex items-center gap-2">
                        <input
                          type="text"
                          aria-label="Project name"
                          disabled={updateSessionNameMutation.isPending}
                          value={editingName}
                          onChange={(e) => setEditingName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') handleSaveEdit(session.id);
                            if (e.key === 'Escape') handleCancelEdit();
                          }}
                          className="border border-border bg-background text-foreground rounded px-2 py-1 text-sm flex-1"
                          autoFocus
                        />
                        <button
                          onClick={() => handleSaveEdit(session.id)}
                          className="text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300"
                          title="Save"
                          disabled={updateSessionNameMutation.isPending || !editingName.trim()}
                        >
                          <Check className="h-4 w-4" />
                        </button>
                        <button
                          onClick={handleCancelEdit}
                          className="text-destructive hover:text-destructive/80"
                          title="Cancel"
                        >
                          <X className="h-4 w-4" />
                        </button>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <span className="break-words min-w-0">{session.project_name || 'Untitled Session'}</span>
                        <button
                          onClick={() => handleStartEdit(session.id, session.project_name)}
                          className="text-muted-foreground hover:text-foreground"
                          title="Edit project name"
                          disabled={updateSessionNameMutation.isPending}
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                      </div>
                    )}
                  </td>
                  <td className="p-4 text-muted-foreground">
                    <div className="flex items-center gap-2">
                      <Clock className="h-3 w-3" />
                      {formatDate(session.created_at)}
                    </div>
                  </td>
                  <td className="p-4 text-foreground">{session.total_mappings || 0}</td>
                  <td className="p-4">
                    <span className={`inline-flex items-center rounded border px-2 py-1 text-xs ${
                      session.pipeline_type === 'url_only'
                        ? 'border-blue-500/50 text-blue-700 dark:text-blue-300'
                        : 'border-[#8353c5]/50 bg-[#8353c5]/10 text-[#8353c5]'
                    }`}>
                      {session.pipeline_type === 'url_only' ? 'Quick Match' : 'Deep Match'}
                    </span>
                  </td>
                  <td className="p-4">
                    <span className={`inline-flex items-center gap-1 border px-2 py-1 text-xs ${
                      session.status === 'completed'
                        ? 'border-green-600 dark:border-green-400 text-green-700 dark:text-green-400'
                        : session.status === 'processing'
                          ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                          : session.status === 'pending'
                            ? 'border-yellow-500 text-yellow-600 dark:text-yellow-400'
                            : session.status === 'failed'
                              ? 'border-red-500 text-red-600 dark:text-red-400'
                              : 'border-border text-muted-foreground'
                    }`}>
                      {(session.status === 'processing' || session.status === 'pending') && (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      )}
                      {session.status === 'pending' ? 'Queued' : session.status}
                    </span>
                  </td>
                  <td className="p-4">
                    <div className="flex w-full items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => navigate(`/review/${session.id}`)}
                      >
                        Open Results
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleDeleteClick(session.id)}
                        className="ml-auto text-destructive hover:text-destructive hover:bg-destructive/10"
                        title="Delete project"
                        aria-label={`Delete ${session.project_name || 'Untitled Session'}`}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {!loading && !error && sessions.length === 0 && (
          <div className="p-8 text-center text-muted-foreground">
            {cursor ? 'No projects remain on this page. Go to the previous page or reload the first page.' : appliedSearch ? 'No projects match your search.' : (MCP_PIVOT_ENABLED ? 'No historical projects. New migrations appear in your migration companion.' : 'No projects found. Start by creating a new mapping!')}
          </div>
        )}
      </Card>
      <nav aria-label="Project history pages" className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" disabled={cursors.length === 1 || sessionsQuery.isFetching}
          onClick={() => setCursors(current => current.slice(0, -1))}>Previous page</Button>
        <span className="text-sm text-muted-foreground">{!error && sessionsQuery.data ? `${sessions.length} ${sessions.length === 1 ? 'project' : 'projects'} on this page` : ''}</span>
        <Button variant="outline" disabled={!sessionsQuery.data?.next_cursor || sessionsQuery.isFetching || !!error}
          onClick={() => { const next = sessionsQuery.data?.next_cursor; if (next) setCursors(current => [...current, next]); }}>Next page</Button>
      </nav>

      <DeleteProjectDialog
        open={deletingSessionId !== null}
        name={sessions.find(session => session.id === deletingSessionId)?.project_name || 'Untitled Session'}
        busy={isDeleting}
        error={deleteSessionMutation.error instanceof Error ? deleteSessionMutation.error.message : null}
        onCancel={handleCancelDelete}
        onConfirm={handleConfirmDelete}
      />

    </Layout>
  );
}
