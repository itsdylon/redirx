import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { UploadPage } from '../../src/components/UploadPage';
import { AllProjects } from '../../src/components/AllProjects';
import { appQueryClient } from '../../src/queries/queryClient';
import { queryKeys } from '../../src/queries/queryKeys';
import { FixtureOwner } from './auth';
import '../../src/styles/globals.css';
// A recent history visit returned no projects before this upload attempt.
appQueryClient.setQueryData(queryKeys.sessions.page('fixture-owner-a', '', null, 50), {
  success: true, sessions: [], pagination: 'cursor', total_count: 0, next_cursor: null, search: '',
});
createRoot(document.getElementById('root')!).render(
  <BrowserRouter><QueryClientProvider client={appQueryClient}><FixtureOwner.Provider value="fixture-owner-a">
    <Routes>
      <Route path="/projects" element={<AllProjects />} />
      <Route path="*" element={<UploadPage quickOnly />} />
    </Routes>
  </FixtureOwner.Provider></QueryClientProvider></BrowserRouter>,
);
