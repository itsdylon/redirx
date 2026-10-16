// Preserve the legacy prefixes for broad invalidation, but never share owner data.
export const monitoringKeys = {
  watches: (owner: string) => ['watches', 'all', owner] as const,
  watch: (owner: string, watch: string) => ['watches', 'detail', owner, watch] as const,
  gscStatus: (owner: string) => ['gsc', 'status', owner] as const,
  gscProperties: (owner: string) => ['gsc', 'properties', owner] as const,
};
