import { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { OnboardingChecklistDock } from './OnboardingChecklistDock';
import { isAgencyPlan } from '../lib/plans';
import { MCP_PIVOT_ENABLED } from '../api/config';

interface DashboardLayoutProps {
  title: string;
  children: ReactNode;
}

export function DashboardLayout({ title, children }: DashboardLayoutProps) {
  const navigate = useNavigate();
  const { logout, user } = useAuth();
  const createJobPath = MCP_PIVOT_ENABLED ? '/companion' : (isAgencyPlan(user?.plan) ? '/upload' : '/quick-match');

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="flex h-screen">
      <Sidebar onLogout={handleLogout} />
      <div className="min-w-0 flex-1 flex flex-col overflow-hidden">
        <TopBar
          title={title}
          onCreateJob={() => navigate(createJobPath)}
        />
        <main className="flex-1 flex flex-col overflow-auto p-4 md:p-8 bg-muted/20">
          {children}
        </main>
        <OnboardingChecklistDock />
      </div>
    </div>
  );
}
