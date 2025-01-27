'use client';

import React, { useState } from 'react';
import { AlertCircle, CheckCircle2, Trash2 } from 'lucide-react';
import SuggestionBox from './SuggestionBox.jsx';

interface ElementSimilarities {
  title?: number;
  h1?: number;
  h2?: number;
  h3?: number;
  meta_desc?: number;
  content?: number;
}

interface ComparisonResult {
  url: string | null;
  similarity: number;
  match_type: 'exact_url' | 'content' | null;
  redirect_needed: boolean;
  url_similarity?: number;
  content_similarity?: number;
  element_matches?: ElementSimilarities;
}

interface Results {
  [key: string]: ComparisonResult;
}

const URLComparisonTool = () => {
  const [step, setStep] = useState(1);
  const [oldSite, setOldSite] = useState('');
  const [newSite, setNewSite] = useState('');
  const [oldSiteLinks, setOldSiteLinks] = useState<string[]>([]);
  const [selectedLinks, setSelectedLinks] = useState<string[]>([]);
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  //set in vercel env vars
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://redirx-1069707477785.us-central1.run.app/compare-sites';

  const handleInitialSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
  


    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          old_site: oldSite,
          scan_only: true
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      setOldSiteLinks(Object.keys(data));
      setSelectedLinks(Object.keys(data));
      setStep(2);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const handleCompareLinks = async () => {
    setLoading(true);
    setError('');
    setResults(null);

    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          old_site: oldSite,
          new_site: newSite,
          selected_urls: selectedLinks,
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      setResults(data);
      setStep(3);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const toggleLink = (link: string) => {
    setSelectedLinks(prev => 
      prev.includes(link)
        ? prev.filter(l => l !== link)
        : [...prev, link]
    );
  };

  const generateRedirectRule = (oldUrl: string, newUrl: string): string => {
    try {
      const oldHostname = new URL(oldUrl).hostname;
      const oldPath = new URL(oldUrl).pathname;
      const newPath = new URL(newUrl).pathname;
      const newSlug = newPath.startsWith("/") ? newPath.slice(1) : newPath;
      const redirectTarget = `https://${oldHostname}/${newSlug}`;
      return `Redirect 301 ^${oldPath}/$ ${redirectTarget}`;
    } catch {
      return 'Invalid URL';
    }
  };
  

  const formatPercentage = (value: number): string => {
    return `${(value * 100).toFixed(1)}%`;
  };

  const renderStepOne = () => (
    <form onSubmit={handleInitialSubmit} className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="space-y-2">
          <label htmlFor="oldSite" className="block font-medium text-white">
            Original Site URL
          </label>
          <input
            id="oldSite"
            type="url"
            value={oldSite}
            onChange={(e) => setOldSite(e.target.value)}
            required
            className="input w-full"
            placeholder="https://old-site.com"
          />
        </div>
        
        <div className="space-y-2">
          <label htmlFor="newSite" className="block font-medium text-white">
            New Site URL
          </label>
          <input
            id="newSite"
            type="url"
            value={newSite}
            onChange={(e) => setNewSite(e.target.value)}
            required
            className="input w-full"
            placeholder="https://new-site.com"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={loading}
        className="btn btn-primary w-full"
      >
        {loading ? 'Scanning Old Site...' : 'Start Redirect Analysis'}
      </button>
    </form>
  );

  const renderStepTwo = () => (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold text-white">Select Links to Process</h2>
        <div className="text-sm text-[--text-secondary]">
          {selectedLinks.length} of {oldSiteLinks.length} selected
        </div>
      </div>

      <div className="card">
        <div className="max-h-96 overflow-y-auto">
          {oldSiteLinks.map((link) => (
            <div
              key={link}
              className="flex items-center justify-between p-3 border-b border-[--surface-hover] last:border-b-0 card-hover"
            >
              <div className="flex items-center space-x-3">
                <input
                  type="checkbox"
                  checked={selectedLinks.includes(link)}
                  onChange={() => toggleLink(link)}
                  className="h-4 w-4 bg-[--surface] border-[--surface-hover] text-[--primary] rounded"
                />
                <span className="text-sm break-all text-white">{link}</span>
              </div>
              <button
                onClick={() => toggleLink(link)}
                className="text-[--text-secondary] hover:text-[--error]"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>
      </div>

      <div className="flex justify-between gap-4">
        <button
          onClick={() => setStep(1)}
          className="btn btn-secondary flex-1"
        >
          Back
        </button>
        <button
          onClick={handleCompareLinks}
          disabled={loading || selectedLinks.length === 0}
          className="btn btn-primary flex-1"
        >
          {loading ? 'Analyzing Links...' : 'Generate Redirects'}
        </button>
      </div>
    </div>
  );

  const renderResults = () => (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-xl font-semibold text-white">Redirect Analysis Results</h2>
        <button
          onClick={() => setStep(2)}
          className="text-[--primary] hover:text-[--primary-hover]"
        >
          Back to Link Selection
        </button>
      </div>
      
      {results && Object.entries(results).map(([oldUrl, result]) => (
        <div key={oldUrl} className="card space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <h3 className="font-medium text-white">Original URL</h3>
              <p className="text-sm break-all text-[--text-secondary]">{oldUrl}</p>
            </div>
            <div>
              <h3 className="font-medium text-white">Matched URL</h3>
              <p className="text-sm break-all text-[--text-secondary]">{result.url}</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {result.match_type === 'exact_url' ? (
              <div className="badge badge-success">
                <CheckCircle2 className="h-4 w-4 mr-1" />
                Exact Match
              </div>
            ) : (
              <div className="badge badge-warning">
                <AlertCircle className="h-4 w-4 mr-1" />
                Content Match ({formatPercentage(result.similarity)})
              </div>
            )}
          </div>

          {result.redirect_needed && (
            <div className="mt-2">
              <div className="bg-[--surface] text-[--text-primary] p-3 rounded font-mono text-sm">
                {generateRedirectRule(oldUrl, result.url!)}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );

  return (
    <div className="max-w-7xl mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-[--primary]">RedirX</h1>
        <span className="text-sm text-[--text-secondary]">SEO-Optimized Redirect Generator</span>
      </div>
      <SuggestionBox />
      {error && (
        <div className="bg-[--error] bg-opacity-10 border border-[--error] text-[--background] p-4 rounded-md">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4" />
            <p className="font-semibold">Error</p>
          </div>
          <p className="mt-1">{error}</p>
        </div>
      )}

      {step === 1 && renderStepOne()}
      {step === 2 && renderStepTwo()}
      {step === 3 && renderResults()}
    </div>
  );
};

export default URLComparisonTool;