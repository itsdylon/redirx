import { defineConfig } from '@playwright/test';
import smoke from './playwright.smoke.config';
export default defineConfig({
  ...smoke,
  testMatch: 'history-pagination.spec.ts',
  webServer: {
    ...smoke.webServer,
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4292 --strictPort',
    url: 'http://127.0.0.1:4292',
  },
  use: { ...smoke.use, baseURL: 'http://127.0.0.1:4292' },
});
