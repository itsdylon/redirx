import { StrictMode, type ReactNode } from 'react';
import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useRequestScope } from './useRequestScope';

describe('stable request scope callbacks', () => {
  it('retains method and key identity across ordinary rerenders, while a resource change aborts old work and resets keys', () => {
    const view = renderHook(({ scope }) => useRequestScope(scope), { initialProps: { scope: 'owner-a:run-1' } });
    const first = view.result.current, signal = first.signal(), key = first.key('set-target');
    view.rerender({ scope: 'owner-a:run-1' });
    expect(view.result.current).toBe(first); expect(view.result.current.key('set-target')).toBe(key);
    expect(view.result.current.signal()).toBe(signal); expect(signal.aborted).toBe(false);
    view.rerender({ scope: 'owner-b:run-1' });
    expect(signal.aborted).toBe(true); expect(view.result.current).not.toBe(first);
    expect(view.result.current.signal().aborted).toBe(false); expect(view.result.current.key('set-target')).not.toBe(key);
    const currentSignal = view.result.current.signal(); view.unmount(); expect(currentSignal.aborted).toBe(true);
  });
  it('keeps the live controller usable after StrictMode cleanup/setup and aborts it on unmount', () => {
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const view = renderHook(() => useRequestScope('owner:run'), { wrapper });
    const scope = view.result.current, signal = scope.signal(), key = scope.key('retry');
    expect(signal.aborted).toBe(false); view.rerender();
    expect(view.result.current).toBe(scope); expect(scope.key('retry')).toBe(key);
    expect(scope.signal()).toBe(signal); view.unmount(); expect(signal.aborted).toBe(true);
  });
});
