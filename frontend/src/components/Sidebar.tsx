import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { LayoutDashboard, FolderOpen, Settings, ChevronLeft, ChevronRight, LogOut, CreditCard } from 'lucide-react';
import faviconImg from '@/assets/favicon.png';
import { useAuth } from '../contexts/AuthContext';
import { isAgencyPlan } from '../lib/plans';
import { MCP_PIVOT_ENABLED } from '../api/config';

interface SidebarProps {
  onLogout: () => void;
}

export function Sidebar({ onLogout }: SidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const [isCollapsed, setIsCollapsed] = useState(false);
  const agencyUser = isAgencyPlan(user?.plan);

  // Load collapsed state from localStorage
  useEffect(() => {
    let saved: string | null = null;
    try { saved = localStorage.getItem('sidebar-collapsed'); } catch { /* Navigation works without storage. */ }
    if (saved) {
      setIsCollapsed(saved === 'true');
    }
  }, []);

  // Save collapsed state to localStorage
  const toggleCollapse = () => {
    const newState = !isCollapsed;
    setIsCollapsed(newState);
    try { localStorage.setItem('sidebar-collapsed', String(newState)); } catch { /* Keep the in-memory preference. */ }
  };

  const navItems = MCP_PIVOT_ENABLED
    ? [
        { icon: LayoutDashboard, label: 'Migration companion', path: '/companion' },
        { icon: FolderOpen, label: 'Project History', path: '/projects' },
        { icon: CreditCard, label: 'Account', path: '/account' },
        { icon: Settings, label: 'Settings', path: '/settings' },
      ]
    : agencyUser
    ? [
        { icon: LayoutDashboard, label: 'Dashboard', path: '/dashboard' },
        { icon: FolderOpen, label: 'All Projects', path: '/projects' },
        { icon: Settings, label: 'Settings', path: '/settings' },
      ]
    : [
        { icon: LayoutDashboard, label: 'Quick Match', path: '/quick-match' },
        { icon: FolderOpen, label: 'Project History', path: '/projects' },
        { icon: CreditCard, label: 'Pricing', path: '/pricing' },
        { icon: Settings, label: 'Settings', path: '/settings' },
      ];

  return (
    <div
      className={`relative shrink-0 flex flex-col bg-card border-r border-border transition-all duration-300 ${
        isCollapsed ? 'w-16' : 'w-16 md:w-64'
      }`}
    >
      {/* Logo/Brand */}
      <div className="flex items-center justify-between p-4 border-b border-border">
        {!isCollapsed && (
          <div className="hidden md:flex items-center gap-2">
            <img src={faviconImg} alt="RedirX" className="w-8 h-8 rounded object-contain" />
            <span className="font-semibold text-foreground">RedirX</span>
          </div>
        )}
        {isCollapsed && (
          <img src={faviconImg} alt="RedirX" className="w-8 h-8 rounded object-contain mx-auto" />
        )}
      </div>

      {!isCollapsed && <img src={faviconImg} alt="RedirX" className="md:hidden w-8 h-8 mx-auto my-3" />}

      {/* Navigation */}
      <nav aria-label="Primary" className="flex-1 p-3">
        <div className="space-y-1">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = location.pathname === item.path;
            return (
              <button
                key={item.path}
                onClick={() => navigate(item.path)}
                className={`w-full flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${
                  isActive
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                } ${isCollapsed ? 'justify-center' : ''}`}
                title={item.label}
                aria-label={item.label}
                aria-current={isActive ? 'page' : undefined}
              >
                <Icon className="h-5 w-5 flex-shrink-0" />
                {!isCollapsed && <span className="hidden md:inline text-sm font-medium">{item.label}</span>}
              </button>
            );
          })}
        </div>
      </nav>

      {/* Bottom Actions */}
      <div className="p-3 border-t border-border">
        <button
          onClick={onLogout}
          className={`w-full flex items-center gap-3 px-3 py-2 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors ${
            isCollapsed ? 'justify-center' : ''
          }`}
          title="Logout" aria-label="Logout"
        >
          <LogOut className="h-5 w-5 flex-shrink-0" />
          {!isCollapsed && <span className="hidden md:inline text-sm font-medium">Logout</span>}
        </button>
      </div>

      {/* Toggle Button */}
      <button
        onClick={toggleCollapse}
        aria-expanded={!isCollapsed}
        aria-label={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="hidden md:flex absolute -right-3 top-20 w-6 h-6 rounded-full border border-border bg-background shadow-sm flex items-center justify-center hover:bg-muted transition-colors"
        title={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        {isCollapsed ? (
          <ChevronRight className="h-3 w-3" />
        ) : (
          <ChevronLeft className="h-3 w-3" />
        )}
      </button>
    </div>
  );
}
