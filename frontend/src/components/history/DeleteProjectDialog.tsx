import { useRef } from 'react';
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogCancel } from '../ui/alert-dialog';
import { Button } from '../ui/button';

export function DeleteProjectDialog({ open, name, busy, error, onCancel, onConfirm }: {
  open: boolean; name: string; busy: boolean; error: string | null; onCancel: () => void; onConfirm: () => void;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return <AlertDialog open={open} onOpenChange={next => { if (!next && !busy) onCancel(); }}>
    <AlertDialogContent
      onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement; }}
      onCloseAutoFocus={event => { if (returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus(); } }}
      onEscapeKeyDown={event => { if (busy) event.preventDefault(); }}>
      <AlertDialogHeader>
        <AlertDialogTitle>Delete project permanently?</AlertDialogTitle>
        <AlertDialogDescription className="break-words">
          This removes “{name}”, its redirect mappings and related previews from RedirX. It cannot be undone. Download any results you need before deleting.
        </AlertDialogDescription>
      </AlertDialogHeader>
      {error && <p role="alert" className="text-sm text-destructive">{error} The deletion was not confirmed. Refresh project history before retrying if the request was interrupted.</p>}
      <AlertDialogFooter>
        <AlertDialogCancel disabled={busy}>Keep project</AlertDialogCancel>
        <Button variant="destructive" disabled={busy} onClick={onConfirm}>{busy ? 'Deleting…' : 'Delete permanently'}</Button>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}
