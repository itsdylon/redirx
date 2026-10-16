import { LogOut, Settings as SettingsIcon, Plus, Moon, Sun } from 'lucide-react';
import { Button } from './ui/button';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useTheme } from 'next-themes';
import { MCP_PIVOT_ENABLED } from '../api/config';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from './ui/dropdown-menu';

interface TopBarProps { title: string; onCreateJob: () => void; }

export function TopBar({ title, onCreateJob }: TopBarProps) {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const initials = user?.full_name?.trim().split(/\s+/).map(n => n[0]).join('').toUpperCase().slice(0, 2) || user?.email?.[0]?.toUpperCase() || 'U';
  return <header className="shrink-0 border-b border-border bg-background flex flex-wrap gap-3 items-center justify-between px-4 py-3 md:px-6">
    <h1 className="min-w-0 break-words text-2xl font-semibold text-foreground">{title}</h1>
    <div className="flex items-center gap-3">
      <Button onClick={onCreateJob} size="default"><Plus aria-hidden="true" className="h-4 w-4 mr-2" />{MCP_PIVOT_ENABLED ? 'Migration companion' : 'New Redirect Job'}</Button>
      <DropdownMenu key={user?.id}>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Open profile menu"><span className="w-9 h-9 rounded-full bg-primary text-primary-foreground flex items-center justify-center text-sm">{initials}</span></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => navigate('/settings')}><SettingsIcon className="h-4 w-4" />Settings</DropdownMenuItem>
          <DropdownMenuItem onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</DropdownMenuItem>
          <DropdownMenuItem onClick={async () => { await logout(); navigate('/login'); }}><LogOut className="h-4 w-4" />Logout</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </header>;
}
