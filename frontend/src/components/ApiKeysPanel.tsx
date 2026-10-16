import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, KeyRound, Loader2, Trash2 } from 'lucide-react';
import { toast } from 'sonner';

import { Card } from './ui/card';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from './ui/alert-dialog';
import { useAuth } from '../contexts/AuthContext';
import { queryKeys } from '../queries/queryKeys';
import { formatDate, formatDateTime } from '../utils/date';
import {
  createApiKey,
  isActive,
  listApiKeys,
  revokeApiKey,
  type ApiKey,
  type CreatedApiKey,
} from '../api/keys';

/**
 * The plaintext key, shown exactly once.
 *
 * Deliberately loud and deliberately not dismissible by accident: the server
 * stores only a hash, so if this is closed before the key is copied it is gone
 * and the only remedy is issuing another one. Says so plainly rather than
 * relying on the user knowing how API keys work.
 */
function NewKeyReveal({ created, onDone }: { created: CreatedApiKey; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    heading.current?.focus();
    return () => { alive.current = false; };
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(created.key);
      // The initiated clipboard write can finish after this reveal is gone.
      // Only the mounted reveal owns its completion feedback.
      if (!alive.current) return;
      setCopied(true);
      toast.success('Key copied');
    } catch {
      if (!alive.current) return;
      // Clipboard is unavailable over plain http and in some embedded views.
      // The key is selectable on screen, so this is a downgrade, not a failure.
      toast.error('Could not copy automatically — select the key and copy it');
    }
  };

  return (
    <Card className="border-primary/40 bg-primary/5 p-4">
      <h3 ref={heading} tabIndex={-1} className="text-sm font-medium text-foreground">
        Copy your key now — this is the only time it will be shown
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        We store a hash, not the key itself, so it cannot be shown again. If you
        lose it, delete it and create another.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <textarea aria-label="New API key" readOnly value={created.key} rows={3} spellCheck={false}
          className="ph-no-capture ph-mask w-full min-w-0 basis-full resize-none rounded-md border border-border bg-background px-3 py-2 font-mono text-sm text-foreground focus-visible:outline-2 focus-visible:outline-ring sm:flex-1 sm:basis-auto"
          onFocus={event => event.currentTarget.select()} />
        <Button size="sm" onClick={copy}>
          {copied ? <Check className="mr-1.5 h-4 w-4" /> : <Copy className="mr-1.5 h-4 w-4" />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>

      <Button size="sm" variant="ghost" className="mt-3" onClick={onDone}>
        I've saved it
      </Button>
    </Card>
  );
}

function KeyRow({ apiKey, onRevoke }: { apiKey: ApiKey; onRevoke: (key: ApiKey) => void }) {
  const active = isActive(apiKey);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-3 last:border-b-0">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground break-words">{apiKey.name}</span>
          {!active && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
              Revoked
            </span>
          )}
        </div>
        <code className="mt-0.5 block font-mono text-xs text-muted-foreground">
          {apiKey.key_prefix}…
        </code>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Created {formatDate(apiKey.created_at)}
          {' · '}
          {apiKey.last_used_at
            ? `last used ${formatDateTime(apiKey.last_used_at)}`
            : 'never used'}
        </p>
      </div>

      {active && (
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground hover:text-destructive"
          onClick={() => onRevoke(apiKey)}
        >
          <Trash2 className="mr-1.5 h-4 w-4" />
          Delete
        </Button>
      )}
    </div>
  );
}

/**
 * Issue and manage API keys.
 *
 * Self-contained so it can sit in Settings for agency accounts and on its own
 * route for everyone else — a free account can drive Quick Match over the API,
 * so key management cannot live only behind the plans that can reach Settings.
 */
export function ApiKeysPanel() {
  const { user } = useAuth();
  return user ? <OwnedApiKeysPanel key={user.id} owner={user.id} /> : null;
}

function OwnedApiKeysPanel({ owner }: { owner: string }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<ApiKey | null>(null);
  const [revokeError, setRevokeError] = useState('');
  const alive = useRef(true);
  const createLocked = useRef(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const panelHeading = useRef<HTMLHeadingElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const revealWasOpen = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (created) revealWasOpen.current = true;
    else if (revealWasOpen.current) { revealWasOpen.current = false; nameInput.current?.focus(); }
  }, [created]);

  const keysQuery = useQuery({
    queryKey: queryKeys.apiKeys.forOwner(owner),
    queryFn: listApiKeys,
    retry: false,
  });

  const createMutation = useMutation({
    retry: false,
    // Keep the plaintext solely in mounted reveal state, never in the global
    // React Query mutation result cache (including after acknowledgment).
    mutationFn: async (label: string) => {
      const key = await createApiKey(label);
      if (!alive.current) return;
      setCreated(key);
      setName('');
      void queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys.forOwner(owner) });
    },
    onError: (error: Error) => {
      createLocked.current = false;
      if (alive.current) toast.error(error.message);
    },
  });
  const create = () => {
    if (createLocked.current || created) return;
    createLocked.current = true;
    createMutation.mutate(name.trim() || 'API key');
  };
  const acknowledge = () => { setCreated(null); createLocked.current = false; createMutation.reset(); };

  const revokeMutation = useMutation({
    retry: false,
    mutationFn: (keyId: string) => revokeApiKey(keyId),
    onSuccess: (_, keyId) => {
      if (!alive.current) return;
      toast.success('Key deleted');
      queryClient.setQueryData<ApiKey[]>(queryKeys.apiKeys.forOwner(owner), current => current?.map(key =>
        key.id === keyId ? { ...key, revoked_at: new Date().toISOString() } : key));
      void queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys.forOwner(owner) });
      if (created?.id === keyId) acknowledge();
      setPendingRevoke(null);
      setRevokeError('');
    },
    onError: (error: Error) => { if (alive.current) setRevokeError(error.message); },
  });

  const keys = keysQuery.data ?? [];
  const activeKeys = keys.filter(isActive);

  return (
    <div className="space-y-4">
      <Card className="p-6 space-y-4">
        <div className="flex items-start gap-3">
          <KeyRound className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
          <div>
            <h2 ref={panelHeading} tabIndex={-1} className="text-base font-medium text-foreground">API keys</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Let an agent run a migration without a browser. Send the key as{' '}
              <code className="font-mono text-xs">Authorization: Bearer rdx_…</code>{' '}
              to <code className="font-mono text-xs">/api/v1</code>.
            </p>
          </div>
        </div>

        {created && (
          <NewKeyReveal key={created.id} created={created} onDone={acknowledge} />
        )}

        {created && <p className="text-sm text-muted-foreground">Save this key before creating another.</p>}
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[200px] flex-1 space-y-2">
            <Label htmlFor="key-name">Name</Label>
            <Input
              ref={nameInput}
              id="key-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Claude Code"
              maxLength={100}
              disabled={createMutation.isPending || !!created}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); create(); }
              }}
            />
          </div>
          <Button onClick={create} disabled={createMutation.isPending || !!created}>
            {createMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Create key
          </Button>
        </div>
      </Card>

      <Card className="p-6">
        {keysQuery.isLoading ? (
          <div className="flex justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : keysQuery.error ? (
          <div role="alert" className="space-y-3 py-4 text-sm text-muted-foreground">
            <p>{keysQuery.error instanceof Error
              ? keysQuery.error.message
              : 'Could not load your keys.'}</p>
            <Button variant="outline" disabled={keysQuery.isFetching} onClick={() => void keysQuery.refetch()}>Reload keys</Button>
          </div>
        ) : keys.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">
            No keys yet. Create one above to start driving Redirx from an agent.
          </p>
        ) : (
          <>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {activeKeys.length} active
            </p>
            {keys.map((key) => (
              <KeyRow key={key.id} apiKey={key} onRevoke={key => { setRevokeError(''); setPendingRevoke(key); }} />
            ))}
          </>
        )}
      </Card>

      <AlertDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => { if (!open && !revokeMutation.isPending) setPendingRevoke(null); }}
      >
        <AlertDialogContent
          onKeyDown={event => {
            if (event.key !== 'Escape') return;
            event.preventDefault(); event.stopPropagation();
            if (!revokeMutation.isPending) setPendingRevoke(null);
          }}
          onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement; }}
          onCloseAutoFocus={event => {
            event.preventDefault();
            const target = returnFocus.current?.isConnected ? returnFocus.current
              : nameInput.current?.disabled ? panelHeading.current : nameInput.current;
            target?.focus();
          }}>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete “{pendingRevoke?.name}”?</AlertDialogTitle>
            <AlertDialogDescription>
              Anything using this key stops working immediately. This cannot be
              undone — you would need to create a new key and update whatever
              was using it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {revokeError && <p role="alert" className="text-sm text-destructive">{revokeError} Reload the key list to check its current state before retrying if the connection was interrupted.</p>}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={revokeMutation.isPending}
              onClick={event => { event.preventDefault(); if (pendingRevoke && !revokeMutation.isPending) { setRevokeError(''); revokeMutation.mutate(pendingRevoke.id); } }}
            >
              {revokeMutation.isPending ? 'Deleting…' : 'Delete key'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
