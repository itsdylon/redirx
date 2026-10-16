import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ThemeProvider } from "next-themes";
import { PostHogProvider } from "@posthog/react";
import type { PostHogConfig } from "posthog-js";
import { QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./contexts/AuthContext";
import { AppWithToaster } from "./App";
import { OnboardingProvider } from "./contexts/OnboardingContext";
import { appQueryClient } from "./queries/queryClient";
import "./styles/globals.css";
import { scrubAuthSecrets } from "./lib/posthogScrub";

// The landing page (redirx-landing/components/analytics/posthog-provider.tsx)
// writes its PostHog cookie on `.redirx.dev` so a visitor carries one identity
// across the domain. Without the same two settings here, this app writes a
// host-only cookie under the same name, and the visitor becomes two people at
// exactly the moment they sign up — which is the one join the shared project
// exists to make. Left undefined off redirx.dev so localhost and Render
// preview hostnames still work.
const cookieDomain =
  typeof window !== "undefined" && window.location.hostname.endsWith("redirx.dev")
    ? ".redirx.dev"
    : undefined;

const posthogOptions = {
  api_host: import.meta.env.VITE_PUBLIC_POSTHOG_HOST,
  defaults: "2026-01-30",
  cross_subdomain_cookie: true,
  ...(cookieDomain ? { cookie_domain: cookieDomain } : {}),
  // Browser exceptions. Nothing captured errors anywhere in this product —
  // `sentry-sdk` sits in requirements.txt but is imported by nothing (see
  // docs/architecture/agentic-pivot.md) — so a React error that breaks signup
  // was visible only as a funnel that quietly stopped converting.
  capture_exceptions: true,
  // The auth callback URL carries sign-in credentials (tokens in the fragment
  // for implicit/email flows, `?code=` for PKCE), and the automatic pageview
  // records the full URL. Scrub them before anything leaves the browser.
  before_send: scrubAuthSecrets,
  loaded: (client) => {
    // Registered on every event, autocapture included. The landing registers
    // source_repo "landing"; without this the app's events were identifiable
    // only by the *absence* of that property, which breaks the moment a third
    // surface starts sending.
    client.register({ source_repo: "app", source_component: "frontend" });
  },
} satisfies Partial<PostHogConfig>;

createRoot(document.getElementById("root")!).render(
  <PostHogProvider
    apiKey={import.meta.env.VITE_PUBLIC_POSTHOG_KEY}
    options={posthogOptions}
  >
    <BrowserRouter>
      <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
        <QueryClientProvider client={appQueryClient}>
          <AuthProvider>
            <OnboardingProvider>
              <AppWithToaster />
            </OnboardingProvider>
          </AuthProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </BrowserRouter>
  </PostHogProvider>
);
