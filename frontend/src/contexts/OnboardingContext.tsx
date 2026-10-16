import { useRequestScope } from '../components/pivot/useRequestScope';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useRef,
  ReactNode,
} from 'react';
import {
  createOnboardingSampleSession,
  getOnboarding,
  OnboardingPath,
  OnboardingResponse,
  OnboardingStep,
  SampleSessionResponse,
  updateOnboarding,
} from '../api/onboarding';
import { useAuth } from './AuthContext';

interface OnboardingContextType {
  onboarding: OnboardingResponse | null;
  loading: boolean;
  error: string | null;
  entryModalOpen: boolean;
  setEntryModalOpen: (open: boolean) => void;
  refreshOnboarding: () => Promise<OnboardingResponse | null>;
  startOnboarding: () => Promise<OnboardingResponse | null>;
  selectPath: (path: OnboardingPath) => Promise<OnboardingResponse | null>;
  completeStep: (step: OnboardingStep) => Promise<OnboardingResponse | null>;
  dismissOnboarding: () => Promise<OnboardingResponse | null>;
  completeOnboarding: () => Promise<OnboardingResponse | null>;
  resetOnboarding: () => Promise<OnboardingResponse | null>;
  createSampleSession: () => Promise<SampleSessionResponse>;
  isStepCompleted: (step: OnboardingStep) => boolean;
}

const OnboardingContext = createContext<OnboardingContextType | undefined>(undefined);

export function OnboardingProvider({ children }: { children: ReactNode }) {
  const { user, loading: authLoading } = useAuth();
  return <OwnedOnboardingProvider ownerId={user?.id} authLoading={authLoading}>{children}</OwnedOnboardingProvider>;
}

function OwnedOnboardingProvider({ children, ownerId, authLoading }: { children: ReactNode; ownerId?: string; authLoading: boolean }) {
  const scope = useRequestScope(ownerId || 'signed-out');
  const revision = useRef(0);
  const generation = useRef(0);
  const [stateOwner, setStateOwner] = useState(ownerId);
  const [onboarding, setOnboarding] = useState<OnboardingResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [entryModalOpen, setEntryModalOpen] = useState(false);
  // Reset only onboarding state, never the App/AuthCallback subtree. React
  // restarts this render before children receive another owner's state.
  if (stateOwner !== ownerId) {
    generation.current++; revision.current++;
    setStateOwner(ownerId);
    setOnboarding(null); setLoading(true); setError(null); setEntryModalOpen(false);
  }
  const activeGeneration = generation.current;

  const refreshOnboarding = useCallback(async (): Promise<OnboardingResponse | null> => {
    if (activeGeneration !== generation.current || scope.signal().aborted) return null;
    if (!ownerId) {
      setOnboarding(null);
      setLoading(false);
      return null;
    }

    const request = ++revision.current;
    const signal = scope.signal();
    setLoading(true);
    setError(null);

    try {
      const data = await getOnboarding(signal);
      if (signal.aborted || request !== revision.current) return null;
      setOnboarding(data);
      return data;
    } catch (err) {
      if (!signal.aborted && request === revision.current) setError(err instanceof Error ? err.message : 'Failed to load onboarding state');
      return null;
    } finally {
      if (!signal.aborted && request === revision.current) setLoading(false);
    }
  }, [ownerId, activeGeneration]);

  const applyUpdate = useCallback(async (payload: Parameters<typeof updateOnboarding>[0]) => {
    if (!ownerId || activeGeneration !== generation.current || scope.signal().aborted) return null;
    const request = ++revision.current;
    const signal = scope.signal();
    setError(null);
    try {
      const data = await updateOnboarding(payload, signal);
      if (signal.aborted) return null;
      if (request === revision.current) { setOnboarding(data); setLoading(false); }
      return data;
    } catch (err) {
      if (!signal.aborted && request === revision.current) { setError(err instanceof Error ? err.message : 'Failed to update onboarding state'); setLoading(false); }
      return null;
    }
  }, [ownerId, activeGeneration]);

  const createSampleSession = useCallback(async (): Promise<SampleSessionResponse> => {
    const signal = scope.signal();
    if (!ownerId || activeGeneration !== generation.current || signal.aborted) throw new DOMException('Onboarding account changed', 'AbortError');
    setError(null);
    try {
      const result = await createOnboardingSampleSession(signal);
      if (signal.aborted) throw new DOMException('Onboarding account changed', 'AbortError');
      return result;
    } catch (err) {
      if (!signal.aborted) setError(err instanceof Error ? err.message : 'Failed to create sample tutorial session');
      throw err;
    }
  }, [ownerId, activeGeneration]);

  useEffect(() => {
    if (authLoading) return;
    if (!ownerId || scope.signal().aborted) {
      setOnboarding(null);
      setEntryModalOpen(false);
      setError(null);
      setLoading(false);
      return;
    }
    refreshOnboarding();
  }, [authLoading, ownerId, refreshOnboarding]);

  const contextValue = useMemo<OnboardingContextType>(() => {
    return {
      onboarding,
      loading,
      error,
      entryModalOpen,
      setEntryModalOpen: open => { if (activeGeneration === generation.current) setEntryModalOpen(open); },
      refreshOnboarding,
      startOnboarding: () => applyUpdate({ action: 'start' }),
      selectPath: (path) => applyUpdate({ action: 'select_path', path }),
      completeStep: (step) => applyUpdate({ action: 'complete_step', step }),
      dismissOnboarding: () => applyUpdate({ action: 'dismiss' }),
      completeOnboarding: () => applyUpdate({ action: 'complete' }),
      resetOnboarding: () => applyUpdate({ action: 'reset' }),
      createSampleSession,
      isStepCompleted: (step) => !!onboarding?.onboarding_state?.steps?.[step]?.completed,
    };
  }, [activeGeneration, applyUpdate, createSampleSession, entryModalOpen, error, loading, onboarding, refreshOnboarding]);

  return (
    <OnboardingContext.Provider value={contextValue}>
      {children}
    </OnboardingContext.Provider>
  );
}

export function useOnboarding() {
  const context = useContext(OnboardingContext);
  if (!context) {
    throw new Error('useOnboarding must be used within an OnboardingProvider');
  }
  return context;
}
