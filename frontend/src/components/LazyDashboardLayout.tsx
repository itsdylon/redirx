import { lazy, Suspense, type ComponentProps } from 'react';

const DashboardLayout = lazy(() => import('./DashboardLayout').then(module => ({ default: module.DashboardLayout })));

// The companion uses ToolLayout. Keep the legacy sidebar, top bar and onboarding
// dock out of companion account/settings loads while retaining historical routes.
export function LazyDashboardLayout(props: ComponentProps<typeof DashboardLayout>) {
  return <Suspense fallback={<p className="p-6 text-muted-foreground" role="status">Loading workspace…</p>}>
    <DashboardLayout {...props} />
  </Suspense>;
}
