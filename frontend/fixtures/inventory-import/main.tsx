import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FileUploadZone } from '../../src/components/FileUploadZone';
import { DomainDiscoveryPanel } from '../../src/components/DomainDiscoveryPanel';
import { validateFile } from '../../src/utils/validation';
import '../../src/styles/globals.css';
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function Fixture() {
  const [file, setFile] = useState<{ name: string; rowCount: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  return <main className="max-w-3xl mx-auto p-5 space-y-8">
    <h1 className="text-2xl font-semibold">Inventory import fixture</h1>
    <FileUploadZone label="Old site URL file" file={file} validationError={error}
      onFileRemove={() => { setFile(null); setError(null); }}
      onFileUpload={async value => { const result = await validateFile(value); setError(result.errors[0] ?? null); setFile(result.valid ? { name: value.name, rowCount: result.rowCount } : null); }} />
    <DomainDiscoveryPanel side="old" label="Old site" hint="Discover the pages that need a redirect." onDiscovered={(side, result) => { (window as any).__discovered = result; }} />
  </main>;
}
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><Fixture /></QueryClientProvider>);
