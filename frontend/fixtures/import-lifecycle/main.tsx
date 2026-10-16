import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { UploadPage } from '../../src/components/UploadPage';
import { FixtureOwner } from './auth';
import '../../src/styles/globals.css';
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function Fixture() {
  const [owner, setOwner] = useState('fixture-owner-a');
  const [mounted, setMounted] = useState(true);
  return <FixtureOwner.Provider value={owner}>
    <div className="flex gap-4 p-4"><button onClick={() => setOwner('fixture-owner-b')}>Switch fixture owner</button><button onClick={() => setMounted(value => !value)}>Toggle fixture surface</button></div>
    {mounted && <UploadPage quickOnly />}
  </FixtureOwner.Provider>;
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><QueryClientProvider client={client}><Fixture /></QueryClientProvider></BrowserRouter>);
