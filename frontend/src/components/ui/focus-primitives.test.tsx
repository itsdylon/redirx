import { createRef } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Input } from './input';
import { Dialog, DialogTrigger, DialogContent, DialogTitle, DialogDescription } from './dialog';
import { Sheet, SheetTrigger, SheetContent, SheetTitle, SheetDescription } from './sheet';
import { AlertDialog, AlertDialogTrigger, AlertDialogContent, AlertDialogTitle, AlertDialogDescription, AlertDialogCancel } from './alert-dialog';

describe('focus handles in React 18', () => {
  it('passes the search ref to the native input', () => {
    const ref = createRef<HTMLInputElement>();
    render(<Input aria-label="Search" ref={ref} />);
    expect(ref.current).toBe(screen.getByRole('textbox'));
    ref.current!.focus();
    expect(screen.getByRole('textbox')).toHaveFocus();
  });
  it.each([
    ['dialog', Dialog, DialogTrigger, DialogContent, DialogTitle, DialogDescription],
    ['sheet', Sheet, SheetTrigger, SheetContent, SheetTitle, SheetDescription],
  ] as const)('%s forwards trigger/content refs and restores keyboard focus', async (_name, Root, Trigger, Content, Title, Description) => {
    const trigger = createRef<HTMLButtonElement>(); const content = createRef<HTMLDivElement>();
    render(<Root><Trigger ref={trigger}>Open</Trigger><Content ref={content}><Title>Test</Title><Description>Fixture dialog</Description><Input aria-label="Name" /></Content></Root>);
    expect(trigger.current).toBe(screen.getByRole('button', {name: 'Open'}));
    await userEvent.click(trigger.current!);
    expect(content.current).toBe(screen.getByRole('dialog'));
    await userEvent.keyboard('{Escape}');
    expect(trigger.current).toHaveFocus();
  });
  it('forwards alert cancel ref and restores focus', async () => {
    const ref = createRef<HTMLButtonElement>();
    render(<AlertDialog><AlertDialogTrigger>Delete</AlertDialogTrigger><AlertDialogContent><AlertDialogTitle>Confirm</AlertDialogTitle><AlertDialogDescription>Fixture</AlertDialogDescription><AlertDialogCancel ref={ref}>Cancel</AlertDialogCancel></AlertDialogContent></AlertDialog>);
    await userEvent.click(screen.getByRole('button', {name: 'Delete'}));
    expect(ref.current).toBe(screen.getByRole('button', {name: 'Cancel'}));
    expect(ref.current).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByRole('button', {name: 'Delete'})).toHaveFocus();
  });
});
