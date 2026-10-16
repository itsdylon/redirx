import { defineConfig } from '@playwright/test';
import smoke from './playwright.smoke.config';
export default defineConfig({
  ...smoke, testMatch: 'review-export.spec.ts',
  webServer: { ...smoke.webServer, command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4277 --strictPort', url: 'http://127.0.0.1:4277' },
  use: { ...smoke.use, baseURL: 'http://127.0.0.1:4277' },
});
