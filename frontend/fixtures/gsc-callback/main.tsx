import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import { GscTrafficCard } from '../../src/components/GscTrafficCard';
import '../../src/styles/globals.css';
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')!).render(<BrowserRouter><QueryClientProvider client={client}><main className="p-4"><h1 className="text-xl mb-4">Search Console callback fixture</h1><GscTrafficCard sessionId="fixture-session" gscMeta={null}/></main><Toaster /></QueryClientProvider></BrowserRouter>);
