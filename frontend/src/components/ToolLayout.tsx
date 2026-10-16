import { ReactNode, useState, useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { CircleUserRound, Menu } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { Button } from './ui/button';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from './ui/breadcrumb';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from './ui/sheet';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { cn } from './ui/utils';
import { isAgencyPlan } from '../lib/plans';
import { MCP_PIVOT_ENABLED } from '../api/config';

interface ToolLayoutProps {
  title: string;
  children: ReactNode;
}

export function ToolLayout({ title, children }: ToolLayoutProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  useEffect(() => { setMobileNavOpen(false); }, [user?.id, location.pathname]);
  const agencyUser = isAgencyPlan(user?.plan);
  const hasSourceSessionId = new URLSearchParams(location.search).has('source_session_id');
  const isQuickMatchActive =
    !MCP_PIVOT_ENABLED && (location.pathname === '/quick-match' ||
    location.pathname.startsWith('/review/') ||
    location.pathname.startsWith('/pricing'));
  const isCompanionActive = ['/companion', '/migrations/', '/billing/subscriptions/'].some(path => location.pathname.startsWith(path));
  const isProjectHistoryActive = location.pathname === '/projects';
  const showBreadcrumb =
    location.pathname.startsWith('/review/') ||
    (location.pathname === '/pricing' && hasSourceSessionId);
  const breadcrumbLabel = location.pathname.startsWith('/review/')
    ? 'Review Redirects'
    : 'Project Pricing';

  const handleLogout = async () => {
    await logout();
    setMobileNavOpen(false);
    navigate('/login');
  };

  const handlePrimaryNavigate = (path: '/quick-match' | '/projects' | '/companion') => {
    setMobileNavOpen(false);
    navigate(path);
  };

  const navButtonClass = (active: boolean) =>
    cn(
      'text-muted-foreground hover:text-foreground',
      active && 'bg-muted text-foreground',
    );

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur">
        <div className="mx-auto w-full max-w-6xl px-6">
          <div className="flex items-center justify-between py-3">
            <div className="flex min-w-0 items-center gap-3">
              <a
                href="https://redirx.dev"
                target="_blank"
                rel="noreferrer"
                className="text-sm font-semibold text-foreground"
              >
                RedirX
              </a>
              <span className="min-w-0 break-words text-sm text-muted-foreground">{title}</span>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              {user ? (
                <>
                  <nav aria-label="Primary" className="hidden items-center gap-1 md:flex">
                    {!MCP_PIVOT_ENABLED && <Button
                      variant="ghost"
                      size="sm"
                      className={navButtonClass(isQuickMatchActive)}
                      aria-current={isQuickMatchActive ? 'page' : undefined}
                      onClick={() => handlePrimaryNavigate('/quick-match')}
                    >
                      Quick Match
                    </Button>}
                    {MCP_PIVOT_ENABLED && <Button variant="ghost" size="sm" className={navButtonClass(isCompanionActive)} aria-current={isCompanionActive ? 'page' : undefined} onClick={() => handlePrimaryNavigate('/companion')}>
                      Migration companion
                    </Button>}
                    <Button
                      variant="ghost"
                      size="sm"
                      className={navButtonClass(isProjectHistoryActive)}
                      aria-current={isProjectHistoryActive ? 'page' : undefined}
                      onClick={() => handlePrimaryNavigate('/projects')}
                    >
                      Project History
                    </Button>
                  </nav>
                  {agencyUser && !MCP_PIVOT_ENABLED && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="hidden md:inline-flex"
                      onClick={() => navigate('/dashboard')}
                    >
                      Dashboard
                    </Button>
                  )}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon" aria-label="Open profile menu">
                        <CircleUserRound className="h-5 w-5" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-60">
                      <DropdownMenuLabel className="font-normal">
                        <div className="text-xs text-muted-foreground">Signed in as</div>
                        <div className="truncate text-sm text-foreground">{user.email}</div>
                      </DropdownMenuLabel>
                      <DropdownMenuSeparator />
                      {MCP_PIVOT_ENABLED && <DropdownMenuItem onClick={() => navigate('/account')}>Account</DropdownMenuItem>}
                      {MCP_PIVOT_ENABLED && <DropdownMenuItem onClick={() => navigate('/settings')}>Settings</DropdownMenuItem>}
                      <DropdownMenuItem onClick={handleLogout}>Logout</DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>

                  <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
                    <SheetTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="md:hidden"
                        aria-label="Open navigation menu"
                      >
                        <Menu className="h-5 w-5" />
                      </Button>
                    </SheetTrigger>
                    <SheetContent side="right">
                      <SheetHeader>
                        <SheetTitle>Navigation</SheetTitle>
                        <SheetDescription>
                          {MCP_PIVOT_ENABLED
                            ? 'Move between your migration companion and project history.'
                            : 'Move between Quick Match and your project history.'}
                        </SheetDescription>
                      </SheetHeader>
                      <nav aria-label="Primary mobile" className="flex flex-col gap-1 px-4">
                        {MCP_PIVOT_ENABLED && <Button variant="ghost" className={cn('justify-start', navButtonClass(isCompanionActive))}
                          aria-current={isCompanionActive ? 'page' : undefined} onClick={() => handlePrimaryNavigate('/companion')}>
                          Migration companion
                        </Button>}
                        {!MCP_PIVOT_ENABLED && <Button
                          variant="ghost"
                          className={cn('justify-start', navButtonClass(isQuickMatchActive))}
                          aria-current={isQuickMatchActive ? 'page' : undefined}
                          onClick={() => handlePrimaryNavigate('/quick-match')}
                        >
                          Quick Match
                        </Button>}
                        <Button
                          variant="ghost"
                          className={cn('justify-start', navButtonClass(isProjectHistoryActive))}
                          aria-current={isProjectHistoryActive ? 'page' : undefined}
                          onClick={() => handlePrimaryNavigate('/projects')}
                        >
                          Project History
                        </Button>
                      </nav>
                      {agencyUser && !MCP_PIVOT_ENABLED && (
                        <div className="mt-auto px-4 pb-4">
                          <Button
                            variant="outline"
                            className="w-full justify-start"
                            onClick={() => {
                              setMobileNavOpen(false);
                              navigate('/dashboard');
                            }}
                          >
                            Dashboard
                          </Button>
                        </div>
                      )}
                    </SheetContent>
                  </Sheet>
                </>
              ) : (
                <>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => navigate(MCP_PIVOT_ENABLED ? '/login?redirect=%2Fcompanion' : '/login?redirect=%2Fquick-match&source=quick-match')}
                  >
                    Log in
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => navigate(MCP_PIVOT_ENABLED ? '/signup?redirect=%2Fcompanion' : '/signup?redirect=%2Fquick-match&source=quick-match')}
                  >
                    Sign up
                  </Button>
                </>
              )}
            </div>
          </div>

          {user && showBreadcrumb && (
            <div className="border-t border-border/60 py-2">
              <Breadcrumb aria-label="Breadcrumb">
                <BreadcrumbList>
                  <BreadcrumbItem>
                    <BreadcrumbLink asChild>
                      <Link to={MCP_PIVOT_ENABLED ? '/projects' : '/quick-match'}>
                        {MCP_PIVOT_ENABLED ? 'Project History' : 'Quick Match'}
                      </Link>
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbPage>{breadcrumbLabel}</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl px-6 py-10">
        {children}
      </main>
    </div>
  );
}
