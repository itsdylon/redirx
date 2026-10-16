import { mergeConfig } from 'vite';
import path from 'node:path';
import base from '../../vite.config';
export default mergeConfig(base, {
  resolve: { alias: {
    '../contexts/AuthContext': path.resolve(__dirname, 'auth.tsx'),
    '../contexts/OnboardingContext': path.resolve(__dirname, 'onboarding.ts'),
    './DashboardLayout': path.resolve(__dirname, 'layout.tsx'),
    './ToolLayout': path.resolve(__dirname, 'layout.tsx'),
  } },
  server: { port: 4286, host: '127.0.0.1', open: false, strictPort: true },
});
