import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ExportModal } from './ExportModal';
import type { RedirectMapping } from './ReviewInterface';

vi.mock('../api/exportAPI', () => ({ previewExport: vi.fn().mockResolvedValue({success:true,content:'fixture rules',filename:'rules.txt',included_count:1,excluded_count:0,excluded:[],warning:null}), fetchExportFile: vi.fn() }));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const baseRedirect: RedirectMapping = {
  id: '1',
  oldUrl: 'https://old.com/page',
  newUrl: 'https://new.com/page',
  confidence: 0.95,
  confidenceBand: 'high',
  matchScore: 0.95,
  matchType: 'semantic',
  approved: true,
  warnings: [],
  pathSimilarity: 0.9,
  titleSimilarity: 0.9,
  contentSimilarity: 0.9,
};

function makeRedirects(overrides: Partial<RedirectMapping>[] = []): RedirectMapping[] {
  return overrides.length > 0
    ? overrides.map((o, i) => ({ ...baseRedirect, id: String(i + 1), ...o }))
    : [baseRedirect];
}

function renderModal(redirects = makeRedirects()) {
  return render(
    <ExportModal
      sessionId="fixture-session"
      open={true}
      onOpenChange={vi.fn()}
      onExport={vi.fn()}
      redirects={redirects}
    />
  );
}

// ---------------------------------------------------------------------------
// Setup / Teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

// ===========================================================================
// localStorage Defaults — Export Format
// ===========================================================================

describe('ExportModal — localStorage format default', () => {
  it('defaults to no format selected when localStorage is empty', () => {
    renderModal();

    // The placeholder text should show when no format is selected
    expect(screen.getByText('Select Format')).toBeInTheDocument();
  });

  it('pre-selects format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'nginx');
    renderModal();

    // The select trigger should display the saved format
    expect(screen.getByText('Nginx server rules')).toBeInTheDocument();
  });

  it('pre-selects apache format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'apache');
    renderModal();

    expect(screen.getByText('Apache .htaccess')).toBeInTheDocument();
  });

  it('pre-selects wordpress format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'wordpress');
    renderModal();

    expect(screen.getByText('WordPress Redirection CSV')).toBeInTheDocument();
  });

  it('pre-selects vercel format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'vercel');
    renderModal();

    expect(screen.getByText('Vercel redirects (vercel.json)')).toBeInTheDocument();
  });

  it('pre-selects cloudflare format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'cloudflare');
    renderModal();

    expect(screen.getByText('Cloudflare Pages (_redirects)')).toBeInTheDocument();
  });

  it('pre-selects shopify format from localStorage', () => {
    localStorage.setItem('redirx_default_export_format', 'shopify');
    renderModal();

    expect(screen.getByText('Shopify CSV')).toBeInTheDocument();
  });

  it('falls back to empty when localStorage has an invalid format', () => {
    localStorage.setItem('redirx_default_export_format', 'nonexistent');
    renderModal();

    // Invalid value is treated as the select value, but since it doesn't match
    // any option, the component should still render without crashing
    expect(screen.getByText('Select Format')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download File' })).toBeDisabled();
  });
});

// ===========================================================================
// localStorage Defaults — Confidence Levels
// ===========================================================================

describe('ExportModal — localStorage confidence defaults', () => {
  it('defaults to high=ON, medium=ON, low=OFF when localStorage is empty', () => {
    renderModal();

    const highCheckbox = screen.getByRole('checkbox', { name: /high/i });
    const mediumCheckbox = screen.getByRole('checkbox', { name: /medium/i });
    const lowCheckbox = screen.getByRole('checkbox', { name: /low/i });

    expect(highCheckbox).toBeChecked();
    expect(mediumCheckbox).toBeChecked();
    expect(lowCheckbox).not.toBeChecked();
  });

  it('reads high=OFF from localStorage', () => {
    localStorage.setItem('redirx_default_confidence_high', 'false');
    renderModal();

    expect(screen.getByRole('checkbox', { name: /high/i })).not.toBeChecked();
  });

  it('reads medium=OFF from localStorage', () => {
    localStorage.setItem('redirx_default_confidence_medium', 'false');
    renderModal();

    expect(screen.getByRole('checkbox', { name: /medium/i })).not.toBeChecked();
  });

  it('reads low=ON from localStorage', () => {
    localStorage.setItem('redirx_default_confidence_low', 'true');
    renderModal();

    expect(screen.getByRole('checkbox', { name: /low/i })).toBeChecked();
  });

  it('applies all confidence overrides together', () => {
    localStorage.setItem('redirx_default_confidence_high', 'false');
    localStorage.setItem('redirx_default_confidence_medium', 'false');
    localStorage.setItem('redirx_default_confidence_low', 'true');
    renderModal();

    expect(screen.getByRole('checkbox', { name: /high/i })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: /medium/i })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: /low/i })).toBeChecked();
  });
});

// ===========================================================================
// localStorage Defaults — URL Format
// ===========================================================================

describe('ExportModal — localStorage URL format default', () => {
  it('defaults to "Paths only" when localStorage is empty', () => {
    renderModal();

    const pathsRadio = screen.getByLabelText(/Paths only/i) as HTMLInputElement;
    expect(pathsRadio.checked).toBe(true);
  });

  it('reads "full" URL format from localStorage', () => {
    localStorage.setItem('redirx_default_url_format', 'full');
    renderModal();

    const fullRadio = screen.getByLabelText(/Full URLs from CSV/i) as HTMLInputElement;
    expect(fullRadio.checked).toBe(true);
  });

  it('falls back to "paths" for invalid URL format in localStorage', () => {
    localStorage.setItem('redirx_default_url_format', 'invalid');
    renderModal();

    const pathsRadio = screen.getByLabelText(/Paths only/i) as HTMLInputElement;
    expect(pathsRadio.checked).toBe(true);
  });

  it('falls back to "paths" for "custom" URL format in localStorage', () => {
    // "custom" is a valid modal value but not saved from Settings defaults
    localStorage.setItem('redirx_default_url_format', 'custom');
    renderModal();

    const pathsRadio = screen.getByLabelText(/Paths only/i) as HTMLInputElement;
    expect(pathsRadio.checked).toBe(true);
  });
});
