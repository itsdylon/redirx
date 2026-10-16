import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Building, Mail, User, CreditCard, Clock } from 'lucide-react';

import { useAuth } from '../contexts/AuthContext';
import { MCP_PIVOT_ENABLED } from '../api/config';
import { ToolLayout } from './ToolLayout';
import { DashboardLayout } from './DashboardLayout';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Separator } from './ui/separator';
import { fetchSessionPage } from '../api/sessions';
import { getBillingStatus } from '../api/billing';
import { getUserProfile, updateUserProfile, type UserProfile } from '../api/user';
import { queryKeys } from '../queries/queryKeys';
import { handleUnauthorizedAndRedirect } from '../queries/auth';
import { formatDate } from '../utils/date';

interface MigrationSession {
  id: string;
  project_name: string;
  status: string;
  created_at: string;
}

export function AccountPage() {
  const { user } = useAuth();
  return user ? <OwnedAccountPage key={user.id} owner={user.id} /> : null;
}

function OwnedAccountPage({ owner }: { owner: string }) {
  const { user, refreshSession } = useAuth();
  const queryClient = useQueryClient();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const navigate = useNavigate();

  const [editMode, setEditMode] = useState(false);
  const [fullName, setFullName] = useState('');
  const [company, setCompany] = useState('');
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');

  const profileQuery = useQuery({
    queryKey: queryKeys.user.forOwner(owner),
    queryFn: ({ signal }) => getUserProfile(signal),
    retry: false,
  });

  const sessionsQuery = useQuery({
    queryKey: queryKeys.sessions.page(owner, '', null, 5),
    queryFn: ({ signal }) => fetchSessionPage({ limit: 5 }, signal),
    retry: false,
  });

  const billingQuery = useQuery({
    queryKey: queryKeys.billing.forOwner(owner),
    queryFn: ({ signal }) => getBillingStatus(signal),
    retry: false,
  });

  const fallbackProfile = useMemo<UserProfile>(() => ({
    id: user?.id || '',
    email: user?.email || '',
    full_name: user?.full_name || '',
    company: '',
    plan: user?.plan || 'free',
  }), [user]);

  const profile = profileQuery.data?.profile ?? fallbackProfile;
  const sessions = (sessionsQuery.data?.sessions as MigrationSession[] | undefined) || [];

  useEffect(() => {
    const queryErrors = [profileQuery.error, sessionsQuery.error, billingQuery.error];
    for (const queryError of queryErrors) {
      if (!queryError) continue;
      if (!handleUnauthorizedAndRedirect(queryError, navigate)) {
        setError('Failed to load account data.');
      }
    }
  }, [billingQuery.error, navigate, profileQuery.error, sessionsQuery.error]);

  useEffect(() => {
    if (editMode) return;
    setFullName(profile.full_name || '');
    setCompany(profile.company || '');
  }, [editMode, profile]);

  const updateProfileMutation = useMutation({
    mutationFn: (payload: { full_name: string; company: string }) => updateUserProfile(payload),
    onSuccess: async () => {
      if (!alive.current) return;
      setSuccessMessage('Profile updated successfully.');
      setEditMode(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.user.profile }),
        refreshSession(),
      ]);
    },
    onError: (mutationError) => {
      if (!alive.current) return;
      if (!handleUnauthorizedAndRedirect(mutationError, navigate)) {
        setError('Failed to update profile.');
      }
    },
  });

  const handleSave = async () => {
    setError('');
    setSuccessMessage('');
    try {
      await updateProfileMutation.mutateAsync({ full_name: fullName, company });
    } catch {
      // handled in onError
    }
  };

  const loading = profileQuery.isLoading || sessionsQuery.isLoading || billingQuery.isLoading;

  const Layout = MCP_PIVOT_ENABLED ? ToolLayout : DashboardLayout;
  return (
    <Layout title="Account">
      <div className={MCP_PIVOT_ENABLED ? "mx-auto w-full max-w-4xl space-y-6 px-4 py-8" : "max-w-4xl space-y-6"}>
        <div>
          <Button variant="outline" onClick={() => navigate('/dashboard')}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Dashboard
          </Button>
        </div>

        <div>
          <h1 className="text-2xl font-semibold text-foreground mb-2">Account</h1>
          <p className="text-muted-foreground">Manage profile and review billing status.</p>
        </div>

        {error && (
          <div role="alert" className="bg-destructive/10 border border-destructive/40 text-destructive px-4 py-3 rounded">
            {error}
            {(profileQuery.error || sessionsQuery.error || billingQuery.error) && <Button variant="outline" className="mt-3 block" onClick={() => {
              setError(''); void profileQuery.refetch(); void sessionsQuery.refetch(); void billingQuery.refetch();
            }}>Reload account data</Button>}
          </div>
        )}

        {successMessage && (
          <div role="status" className="bg-green-500/10 border border-green-500/40 text-green-700 dark:text-green-400 px-4 py-3 rounded">
            {successMessage}
          </div>
        )}

        {loading ? (
          <div className="text-muted-foreground">Loading...</div>
        ) : (
          <div className="space-y-6">
            <Card>
              <CardHeader className="border-b border-border">
                <CardTitle className="flex items-center gap-2">
                  <User className="h-5 w-5" />
                  Profile
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-6">
                {editMode ? (
                  <div className="space-y-4">
                    <div>
                      <label htmlFor="account-full-name" className="block text-sm font-medium text-foreground mb-2">Full Name</label>
                      <Input id="account-full-name" value={fullName} onChange={(e) => setFullName(e.target.value)} />
                    </div>
                    <div>
                      <label htmlFor="account-company" className="block text-sm font-medium text-foreground mb-2">Company</label>
                      <Input id="account-company" value={company} onChange={(e) => setCompany(e.target.value)} />
                    </div>
                    <div className="flex gap-2">
                      <Button onClick={handleSave} disabled={updateProfileMutation.isPending}>
                        {updateProfileMutation.isPending ? 'Saving...' : 'Save Changes'}
                      </Button>
                      <Button variant="outline" onClick={() => setEditMode(false)}>Cancel</Button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                          <Mail className="h-4 w-4" /> Email
                        </div>
                        <div className="text-foreground break-words">{profile.email || user?.email}</div>
                      </div>
                      <div>
                        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                          <User className="h-4 w-4" /> Full Name
                        </div>
                        <div className="text-foreground break-words">{profile.full_name || '-'}</div>
                      </div>
                      <div>
                        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                          <Building className="h-4 w-4" /> Company
                        </div>
                        <div className="text-foreground break-words">{profile.company || '-'}</div>
                      </div>
                      <div>
                        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                          <CreditCard className="h-4 w-4" /> Plan
                        </div>
                        <div className="text-foreground capitalize">{billingQuery.error ? 'Unavailable' : billingQuery.data?.plan || profile.plan || 'free'}</div>
                      </div>
                    </div>
                    <Separator />
                    <Button variant="outline" disabled={!!profileQuery.error} onClick={() => setEditMode(true)}>
                      Edit Profile
                    </Button>
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b border-border">
                <CardTitle className="flex items-center gap-2">
                  <Clock className="h-5 w-5" />
                  Recent Projects
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-6">
                {sessionsQuery.error ? <p role="alert" className="text-sm text-muted-foreground">Recent projects could not load. Reload account data to try again.</p> : sessions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No projects yet.</p>
                ) : (
                  <div className="space-y-3">
                    {sessions.slice(0, 5).map((session) => (
                      <div key={session.id} className="flex items-center justify-between border border-border p-3">
                        <div>
                          <p className="font-medium text-foreground">{session.project_name || 'Untitled Project'}</p>
                          <p className="text-sm text-muted-foreground">{formatDate(session.created_at)}</p>
                        </div>
                        <div className="text-sm text-muted-foreground capitalize">{session.status}</div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </Layout>
  );
}
