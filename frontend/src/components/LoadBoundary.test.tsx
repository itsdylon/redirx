import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { LoadBoundary } from './LoadBoundary';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function Draft() { const [value, setValue] = useState(''); return <input aria-label="Draft" value={value} onChange={event => setValue(event.target.value)} />; }
function Broken(): never { throw new Error('secret transport detail must not appear'); }

describe('load failure boundary', () => {
  it('does not remount successful children when route metadata changes', () => {
    const view = render(<LoadBoundary resetKey="first"><Draft /></LoadBoundary>);
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'unsaved input' } });
    view.rerender(<LoadBoundary resetKey="second"><Draft /></LoadBoundary>);
    expect(screen.getByLabelText('Draft')).toHaveValue('unsaved input');
  });
  it('contains optional failures without removing a surrounding draft or displaying raw errors', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const view = render(<><Draft /><LoadBoundary section="Optional panel"><p>Ready</p></LoadBoundary></>);
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'unsaved input' } });
    view.rerender(<><Draft /><LoadBoundary section="Optional panel"><Broken /></LoadBoundary></>);
    expect(screen.getByRole('heading', { name: 'Optional panel could not load' })).toBeInTheDocument();
    expect(screen.getByLabelText('Draft')).toHaveValue('unsaved input');
    expect(screen.queryByText(/secret transport detail/)).not.toBeInTheDocument();
    expect(screen.getByText(/Reloading discards unsaved edits/)).toBeInTheDocument();
  });
  it('can show a different healthy route after an error without resetting the auth provider', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const view = render(<LoadBoundary resetKey="broken" homeHref="/companion"><Broken /></LoadBoundary>);
    expect(screen.getByRole('link', { name: 'Return to your workspace' })).toHaveAttribute('href', '/companion');
    view.rerender(<LoadBoundary resetKey="healthy" homeHref="/companion"><p>Healthy route</p></LoadBoundary>);
    expect(screen.getByText('Healthy route')).toBeInTheDocument();
  });
});
