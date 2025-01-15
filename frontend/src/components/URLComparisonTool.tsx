'use client';

import React, { useState } from 'react';
import { AlertCircle, CheckCircle2, XCircle } from 'lucide-react';

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
  const [oldSite, setOldSite] = useState('');
  const [newSite, setNewSite] = useState('');
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    setResults(null);

    try {
      const response = await fetch('http://127.0.0.1:5000/compare-sites', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          old_site: oldSite,
          new_site: newSite,
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      setResults(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const generateRedirectRule = (oldUrl: string, newUrl: string): string => {
    try {
      const oldPath = new URL(oldUrl).pathname;
      const newPath = new URL(newUrl).pathname;
      return `Redirect 301 ${oldPath} ${newPath}`;
    } catch {
      return 'Invalid URL';
    }
  };

  const formatPercentage = (value: number): string => {
    return `${(value * 100).toFixed(1)}%`;
  };

  const getMatchTypeDisplay = (result: ComparisonResult) => {
    if (result.match_type === 'exact_url') {
      return (
        <div className="flex items-center text-green-600">
          <CheckCircle2 className="w-4 h-4 mr-1" />
          Exact URL Match
        </div>
      );
    }
    return (
      <div className="flex items-center text-blue-600">
        <AlertCircle className="w-4 h-4 mr-1" />
        Content Match
      </div>
    );
  };

  const getSimilarityColor = (value: number): string => {
    if (value >= 0.8) return 'bg-green-100 text-green-800';
    if (value >= 0.6) return 'bg-yellow-100 text-yellow-800';
    return 'bg-red-100 text-red-800';
  };

  return (
    <div className="max-w-7xl mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">RedirX</h1>
        <span className="text-sm text-gray-500">SEO-Optimized Redirect Generator</span>
      </div>
      
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <label htmlFor="oldSite" className="block font-medium text-gray-700">
              Original Site URL
            </label>
            <input
              id="oldSite"
              type="url"
              value={oldSite}
              onChange={(e) => setOldSite(e.target.value)}
              required
              className="w-full p-2 border rounded focus:ring-2 focus:ring-blue-500"
              placeholder="https://old-site.com"
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="newSite" className="block font-medium text-gray-700">
              New Site URL
            </label>
            <input
              id="newSite"
              type="url"
              value={newSite}
              onChange={(e) => setNewSite(e.target.value)}
              required
              className="w-full p-2 border rounded focus:ring-2 focus:ring-blue-500"
              placeholder="https://new-site.com"
            />
          </div>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-blue-500 text-white p-3 rounded hover:bg-blue-600 disabled:bg-blue-300 font-medium"
        >
          {loading ? 'Analyzing Sites...' : 'Generate Redirects'}
        </button>
      </form>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-md">
          <div className="flex items-center gap-2">
            <XCircle className="h-4 w-4" />
            <p className="font-semibold">Error</p>
          </div>
          <p className="mt-1">{error}</p>
        </div>
      )}

      {results && (
        <div className="space-y-6">
          <h2 className="text-xl font-semibold">Redirect Analysis Results</h2>
          
          {Object.entries(results).map(([oldUrl, result]) => (
            <div key={oldUrl} className="bg-white shadow rounded-lg p-4 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <h3 className="font-medium text-gray-700">Original URL</h3>
                  <p className="text-sm break-all">{oldUrl}</p>
                </div>
                <div>
                  <h3 className="font-medium text-gray-700">Matched URL</h3>
                  <p className="text-sm break-all">{result.url}</p>
                </div>
              </div>

              <div className="border-t pt-4">
                <div className="flex items-center justify-between">
                  <div>{getMatchTypeDisplay(result)}</div>
                  <div className={`px-2 py-1 rounded text-sm ${getSimilarityColor(result.similarity)}`}>
                    Overall Match: {formatPercentage(result.similarity)}
                  </div>
                </div>

                {result.match_type === 'content' && (
                  <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-sm font-medium text-gray-700">URL Similarity</h4>
                      <div className="mt-1 h-2 bg-gray-200 rounded">
                        <div
                          className="h-2 bg-blue-500 rounded"
                          style={{ width: `${(result.url_similarity || 0) * 100}%` }}
                        />
                      </div>
                      <p className="text-sm text-gray-600 mt-1">
                        {formatPercentage(result.url_similarity || 0)}
                      </p>
                    </div>
                    <div>
                      <h4 className="text-sm font-medium text-gray-700">Content Similarity</h4>
                      <div className="mt-1 h-2 bg-gray-200 rounded">
                        <div
                          className="h-2 bg-blue-500 rounded"
                          style={{ width: `${(result.content_similarity || 0) * 100}%` }}
                        />
                      </div>
                      <p className="text-sm text-gray-600 mt-1">
                        {formatPercentage(result.content_similarity || 0)}
                      </p>
                    </div>
                  </div>
                )}

                {result.element_matches && (
                  <div className="mt-4">
                    <h4 className="text-sm font-medium text-gray-700 mb-2">Element Matches</h4>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                      {Object.entries(result.element_matches).map(([element, similarity]) => (
                        <div key={element} className="text-sm">
                          <span className="text-gray-600">{element}:</span>{' '}
                          <span className={getSimilarityColor(similarity)}>
                            {formatPercentage(similarity)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {result.redirect_needed && (
                  <div className="mt-4">
                    <h4 className="text-sm font-medium text-gray-700">Redirect Rule</h4>
                    <code className="block p-2 bg-gray-50 rounded text-sm font-mono mt-1">
                      {result.url && generateRedirectRule(oldUrl, result.url)}
                    </code>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default URLComparisonTool;