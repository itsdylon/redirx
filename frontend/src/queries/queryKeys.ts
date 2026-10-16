export const queryKeys = {
  dashboard: {
    summary: ['dashboard', 'summary'] as const,
    forOwner: (owner: string) => ['dashboard', 'summary', owner] as const,
  },
  sessions: {
    all: ['sessions', 'all'] as const,
    forOwner: (owner: string) => ['sessions', 'all', owner] as const,
    page: (owner: string, search: string, cursor: string | null, limit: number) =>
      ['sessions', 'all', owner, 'page', search, cursor, limit] as const,
  },
  results: {
    forOwner: (owner: string, sessionId: string) => ['results', 'session', sessionId, owner] as const,
    deepPreviewForOwner: (owner: string, sessionId: string) => ['results', 'deep-preview', sessionId, owner] as const,
    bySession: (sessionId: string) => ['results', 'session', sessionId] as const,
    deepPreview: (sessionId: string) => ['results', 'deep-preview', sessionId] as const,
  },
  billing: {
    status: ['billing', 'status'] as const,
    forOwner: (owner: string) => ['billing', 'status', owner] as const,
    quoteForOwner: (owner: string, sessionId: string) => ['billing', 'quote', sessionId, owner] as const,
    quote: (sourceSessionId: string) => ['billing', 'quote', sourceSessionId] as const,
    unlockStatusForOwner: (owner: string, sessionId: string) => ['billing', 'unlock-status', sessionId, owner] as const,
    unlockStatus: (sourceSessionId: string) => ['billing', 'unlock-status', sourceSessionId] as const,
    estimate: (pageCount: number) => ['billing', 'estimate', pageCount] as const,
  },
  gsc: {
    status: ['gsc', 'status'] as const,
    properties: ['gsc', 'properties'] as const,
  },
  email: {
    preferences: ['email', 'preferences'] as const,
    forOwner: (owner: string) => ['email', 'preferences', owner] as const,
  },
  user: {
    profile: ['user', 'profile'] as const,
    forOwner: (owner: string) => ['user', 'profile', owner] as const,
  },
  apiKeys: {
    all: ['api-keys', 'all'] as const,
    forOwner: (owner: string) => ['api-keys', 'all', owner] as const,
  },
  watches: {
    all: ['watches', 'all'] as const,
    detail: (watchId: string) => ['watches', 'detail', watchId] as const,
  },
};
