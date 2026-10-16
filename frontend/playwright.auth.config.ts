import { defineConfig } from '@playwright/test';
import smoke from './playwright.smoke.config';

// Separate loopback port permits concurrent isolated product workstreams.
export default defineConfig({
  ...smoke,
  testMatch: 'auth-recovery.spec.ts',
  webServer: {
    ...smoke.webServer,
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4275 --strictPort',
    url: 'http://127.0.0.1:4275',
  },
  use: { ...smoke.use, baseURL: 'http://127.0.0.1:4275' },
});
