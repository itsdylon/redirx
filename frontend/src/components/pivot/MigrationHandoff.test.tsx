import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { MigrationDetail } from '../../api/pivot';
import { handoffState, MigrationHandoff } from './MigrationHandoff';

const artifact = { artifact_id: 'artifact-1', format: 'cloudflare', included_count: 8, excluded_count: 2 };
const data: MigrationDetail = { artifact };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe('migration installation handoff', () => {
  it('does not mistake an export or installation report for verification', () => {
    expect(handoffState(data).title).toBe('Export saved; installation not reported');
    expect(handoffState({ ...data, deployment_id: 'deployment-1' }).title).toBe('Installation reported; HTTP checks pending');
  });
  it('requires complete artifact coverage before showing passed', () => {
    const verified = { ...data, verification: { outcome: 'passed', total: 8, checked: 8, unchecked: 0 } };
    expect(handoffState(verified).title).toBe('HTTP redirect checks passed');
    for (const change of [{ unchecked: 1 }, { checked: 7 }, { total: 7, checked: 7 }, { total: 0, checked: 0 }]) {
      expect(handoffState({ ...verified, verification: { ...verified.verification, ...change } }).title).toBe('HTTP verification is incomplete');
    }
  });
  it('distinguishes actual failure outcomes and unknown outcomes from success', () => {
    expect(handoffState({ ...data, verification: { outcome: 'issues_found', checked: 8, total: 8, unchecked: 0 } }).title).toBe('Redirect issues need attention');
    for (const outcome of ['in_progress', 'unverifiable', 'unknown']) {
      expect(handoffState({ ...data, verification: { outcome, checked: 5, total: 8, unchecked: 3 } }).title).toBe('HTTP verification is incomplete');
    }
  });
  it('gives exact artifact references, correct Cloudflare product, exclusions and no deployment side effects', () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    render(<MigrationHandoff migrationId="migration-1" data={data} fresh />);
    expect(screen.getByText('Install on Cloudflare Pages')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Cloudflare Pages installation documentation', hidden: true })).toHaveAttribute('href', 'https://developers.cloudflare.com/pages/configuration/redirects/');
    expect(screen.getByText(/not a Bulk Redirects CSV/)).toBeInTheDocument();
    expect(screen.getByText(/2 mappings are excluded/)).toBeInTheDocument();
    expect(screen.getByText('artifact-1')).toBeInTheDocument();
    expect(screen.getByText('migration-1')).toBeInTheDocument();
    expect(screen.getByText(/does not establish that Google has indexed/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('keeps stale data from inviting verification with outdated references', () => {
    render(<MigrationHandoff migrationId="migration-1" data={data} fresh={false} />);
    expect(screen.getByText('Refresh status before continuing')).toBeInTheDocument();
    expect(screen.queryByText('artifact-1')).not.toBeInTheDocument();
    expect(screen.queryByText('Export saved; installation not reported')).not.toBeInTheDocument();
  });
  it.each(['csv', 'json'])('does not imply generic %s can be installed directly', format => {
    render(<MigrationHandoff migrationId="migration-1" data={{ artifact: { ...artifact, format } }} fresh />);
    expect(screen.getByText(/choose a supported platform export or adapt/)).toBeInTheDocument();
  });
  it.each(['apache', 'nginx', 'wordpress', 'vercel', 'shopify'])('provides official installation guidance for %s', format => {
    render(<MigrationHandoff migrationId="migration-1" data={{ artifact: { ...artifact, format } }} fresh />);
    expect(screen.getByRole('link', { name: /installation documentation/, hidden: true })).toHaveAttribute('href', expect.stringMatching(/^https:\/\//));
  });
  it('safely handles unknown formats and absent artifacts', () => {
    const { rerender } = render(<MigrationHandoff migrationId="migration-1" data={{ artifact: { ...artifact, format: '__proto__' } }} fresh />);
    expect(screen.getByText('Install on Platform export')).toBeInTheDocument();
    rerender(<MigrationHandoff migrationId="migration-1" data={{}} fresh />);
    expect(screen.queryByText('Install on Platform export')).not.toBeInTheDocument();
  });
});
