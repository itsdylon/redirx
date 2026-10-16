import { useRef } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from './ui/dialog';
import { formatShortcut } from '../lib/keyboard';

interface KeyboardShortcutsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface ShortcutGroup {
  title: string;
  shortcuts: {
    keys: string;
    description: string;
  }[];
}

export function KeyboardShortcutsDialog({ open, onOpenChange }: KeyboardShortcutsDialogProps) {
  const returnFocus = useRef<HTMLElement | null>(null);
  const shortcutGroups: ShortcutGroup[] = [
    {
      title: 'Navigation',
      shortcuts: [
        { keys: formatShortcut('K'), description: 'Focus search input' },
        { keys: 'Esc', description: 'Close modals' },
      ],
    },
    {
      title: 'Actions',
      shortcuts: [
        { keys: formatShortcut('E'), description: 'Open export modal' },
        { keys: formatShortcut('A'), description: 'Select all visible redirects' },
      ],
    },
    {
      title: 'Help',
      shortcuts: [
        { keys: '?', description: 'Show keyboard shortcuts' },
      ],
    },
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto"
        onOpenAutoFocus={() => { returnFocus.current = document.activeElement as HTMLElement; }}
        onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus(); }}>
        <DialogHeader>
          <DialogTitle>Keyboard Shortcuts</DialogTitle>
          <DialogDescription>
            Use these keyboard shortcuts to navigate and perform actions faster
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 mt-4">
          {shortcutGroups.map((group) => (
            <div key={group.title}>
              <h3 className="text-sm font-semibold text-foreground mb-3">{group.title}</h3>
              <div className="space-y-2">
                {group.shortcuts.map((shortcut) => (
                  <div
                    key={shortcut.keys}
                    className="flex items-center justify-between py-2 px-3 rounded-md hover:bg-muted/50 transition-colors"
                  >
                    <span className="text-sm text-muted-foreground">{shortcut.description}</span>
                    <kbd className="inline-flex items-center gap-1 rounded border border-border bg-muted px-2 py-1 font-mono text-sm font-medium text-foreground">
                      {shortcut.keys}
                    </kbd>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="mt-6 pt-4 border-t border-border">
          <p className="text-xs text-muted-foreground text-center">
            Press <kbd className="inline-flex items-center rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-xs">Esc</kbd> to close this dialog
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
