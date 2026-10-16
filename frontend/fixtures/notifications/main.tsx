import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Settings } from '../../src/components/Settings';
import '../../src/styles/globals.css';
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')!).render(<BrowserRouter><QueryClientProvider client={client}><Settings pivotEnabled /></QueryClientProvider></BrowserRouter>);
