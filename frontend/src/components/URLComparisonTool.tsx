'use client';

import React, { useState } from 'react';
import { AlertCircle } from 'lucide-react';

interface ComparisonResult {
  url: string | null;
  similarity: number;
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
      const response = await fetch('/compare-sites', {
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
      return `RewriteRule ^${oldPath.substring(1)}$ ${newPath} [R=301,L]`;
    } catch {
      return 'Invalid URL';
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      <h1 className="text-2xl font-bold mb-6">Website Migration Comparison Tool</h1>
      
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-2">
          <label htmlFor="oldSite" className="block font-medium">
            Original Site URL:
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
          <label htmlFor="newSite" className="block font-medium">
            New Site URL:
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

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-blue-500 text-white p-2 rounded hover:bg-blue-600 disabled:bg-blue-300"
        >
          {loading ? 'Comparing Sites...' : 'Compare Sites'}
        </button>
      </form>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-md mt-4">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4" />
            <p className="font-semibold">Error</p>
          </div>
          <p className="mt-1">{error}</p>
        </div>
      )}

      {results && (
        <div className="mt-6">
          <h2 className="text-xl font-semibold mb-4">Comparison Results</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full border-collapse border">
              <thead>
                <tr className="bg-gray-100">
                  <th className="border p-2">Original URL</th>
                  <th className="border p-2">Best Match URL</th>
                  <th className="border p-2">Similarity</th>
                  <th className="border p-2">Redirect Rule</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(results).map(([oldUrl, match]: [string, ComparisonResult]) => (
                  <tr key={oldUrl}>
                    <td className="border p-2 break-all">{oldUrl}</td>
                    <td className="border p-2 break-all">{match.url}</td>
                    <td className="border p-2 text-center">
                      {(match.similarity * 100).toFixed(1)}%
                    </td>
                    <td className="border p-2 font-mono text-sm break-all">
                      {match.url && generateRedirectRule(oldUrl, match.url)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};

export default URLComparisonTool;