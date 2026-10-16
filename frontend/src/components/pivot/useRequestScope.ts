import { useEffect, useMemo, useRef } from 'react';

/** Scope requests and retry keys to one mounted account resource. */
export function useRequestScope(scope: string) {
  const context = useRef({ scope, controller: new AbortController(), keys: new Map<string, string>() });
  if (context.current.scope !== scope) {
    context.current.controller.abort();
    context.current = { scope, controller: new AbortController(), keys: new Map() };
  }
  useEffect(() => {
    if (context.current.controller.signal.aborted) context.current.controller = new AbortController();
    const current = context.current;
    return () => current.controller.abort();
  }, [scope]);
  return useMemo(() => ({
    signal: () => context.current.controller.signal,
    key: (intent: string) => {
      const existing = context.current.keys.get(intent);
      if (existing) return existing;
      const created = crypto.randomUUID();
      context.current.keys.set(intent, created);
      return created;
    },
  }), [scope]);
}
