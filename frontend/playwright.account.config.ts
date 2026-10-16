import { defineConfig } from '@playwright/test';
import smoke from './playwright.smoke.config';

export default defineConfig({
  ...smoke,
  testMatch: 'account-surfaces.spec.ts',
  webServer: {
    ...smoke.webServer,
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4276 --strictPort',
    url: 'http://127.0.0.1:4276',
  },
  use: { ...smoke.use, baseURL: 'http://127.0.0.1:4276' },
});
