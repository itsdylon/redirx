import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { deleteSession, updateSessionName, type MigrationSession, type SessionListResponse } from '../../api/sessions';
import type { DashboardData } from '../../api/dashboard';
import { queryKeys } from '../../queries/queryKeys';
import { useRequestScope } from '../pivot/useRequestScope';

/** Only a server acknowledgment changes the visible record; deletion has no undo API. */
export function useProjectActions(ownerId: string, callbacks: { onRenamed: () => void; onDeleted: () => void }) {
  const client = useQueryClient();
  const scope = useRequestScope(ownerId);
  const sessionsKey = queryKeys.sessions.forOwner(ownerId);
  const dashboardKey = queryKeys.dashboard.forOwner(ownerId);
  const update = (apply: (rows: MigrationSession[]) => MigrationSession[]) => {
    client.setQueryData<SessionListResponse>(sessionsKey, current => current ? { ...current, sessions: apply(current.sessions) } : current);
    client.setQueryData<DashboardData>(dashboardKey, current => current ? { ...current, recent_sessions: apply(current.recent_sessions) } : current);
  };
  const invalidate = () => {
    if (scope.signal().aborted) return;
    void client.invalidateQueries({ queryKey: sessionsKey });
    void client.invalidateQueries({ queryKey: dashboardKey });
  };
  const rename = useMutation({
    mutationFn: ({ sessionId, projectName }: { sessionId: string; projectName: string }) => updateSessionName(sessionId, projectName, scope.signal()),
    onSuccess: (_result, { sessionId, projectName }) => {
      if (scope.signal().aborted) return;
      update(rows => rows.map(row => row.id === sessionId ? { ...row, project_name: projectName } : row));
      callbacks.onRenamed();
      toast.success('Project name updated');
    },
    onError: (error: Error) => { if (!scope.signal().aborted) toast.error(error.message); },
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: ({ sessionId }: { sessionId: string }) => deleteSession(sessionId, scope.signal()),
    onSuccess: (_result, { sessionId }) => {
      if (scope.signal().aborted) return;
      update(rows => rows.filter(row => row.id !== sessionId && row.source_session_id !== sessionId));
      callbacks.onDeleted();
      toast.success('Project permanently deleted');
    },
    onSettled: invalidate,
  });
  return { rename, remove };
}
