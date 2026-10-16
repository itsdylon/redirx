import type { ReactNode } from 'react';
export function DashboardLayout({ children, title }: { children: ReactNode; title: string }) {
  return <main className="mx-auto max-w-5xl p-5"><h1 className="mb-6 text-2xl font-semibold">{title}</h1>{children}</main>;
}
export const ToolLayout = DashboardLayout;
