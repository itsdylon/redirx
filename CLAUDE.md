# RedirX engineering guide

## Start here

The current path is MCP → owned migration/inventories → URL-first Jev proposals →
explicit review → approved artifacts. The browser is a companion. Read `README.md`
and trace the current entry points before treating an older architecture proposal
as an implementation description.

The previous root guide described the legacy cosine/content engine as the primary
architecture. It was replaced during the October 1 cleanup; old guide text remains
in Git at `3ddece1:CLAUDE.md`. Historical rollout receipts describe their recorded
moment, not live service state. Verify deployed SHAs before a release. `render.yaml`
is not a reliable inventory of the current deployment.

## Boundaries that should survive a harness change

- `src/redirx/jev/data.py`, `retrieve.py`, `examples.py`, `pipeline.py` and
  `questions.py` implement URL retrieval and two-pass judgment. They depend on the
  small `JudgmentProvider` / `EmbeddingCache` protocols in `ports.py`, not Flask,
  the database or the concrete model client.
- `src/redirx/jev/jev.py` owns the pinned provider protocol, request validation,
  cache identity, pacing and billable-call accounting. Swapping a provider must
  preserve those guarantees and explicitly version incompatible prompts/caches.
- `backend/services/jev_pipeline_service.py` owns admission, refinement and review
  state. `jev_store.py` owns run-scoped persistence and lease-fenced budgets;
  `jev_runner.py` adapts jobs, audited seeds and embeddings to the algorithm.
  Blocking work stays off the async worker loop so lease heartbeats continue.
- `backend/worker.py` owns dispatch and completion. `migration_*` services own
  admission, ownership, review, artifacts and historical workflows. Keep these
  product authorities independent of model decisions and MCP/browser transport.

Prefer concrete modules and these existing boundaries over a generic agent
framework, plugin registry or parallel implementation of the same policy.

## Non-negotiable behavior

- A Jev suggestion is never approval. Only explicit audited decisions become
  confirmed examples or exportable redirects. Corrected examples invalidate older
  proposals. Preserve optimistic revisions and idempotency keys across retries.
- `confidence` is a model estimate, not a calibrated guarantee. URL-only refusal
  stays unresolved for review; no automatic 410. In ambiguous redirect policy,
  consult current Google guidance before changing behavior or labels.
- Keep raw URL identity, case and encoding intact. Retrieval tokenization is not
  URL identity. Export membership, origin and loop validation remain authoritative.
- Look up the durable `jev_runs` marker before selecting an engine. A disabled Jev
  switch pauses marked jobs; it must never send them into the old content engine.
- Every paid provider attempt reserves budget first. Preserve the global $1/day
  cap (including embeddings), unknown-outcome reservations and lease fencing.
  No hidden SDK retry or automatic provider fallback. See `contracts/jev-mvp-v1.json`.
- Current limits: 500 old / 2000 new URLs, 2 MiB raw URLs, five new runs per rolling
  24 hours, three passes, at most 100 initial confirmed pairs. Database authority
  wins over UI hints. One concurrent run is the measured worker envelope.
  The October billing PR adds an explicitly owner-requested, opt-in paid path
  (`REDIRX_CURRENT_BILLING_ENABLED`): one separate 500-URL trial, paid capacity
  15,000 old / 20,000 new URLs and 16 MiB, pooled monthly allowances and approved
  extras. This is an admission ceiling, not a measured throughput claim. The
  global provider budget and worker concurrency remain unchanged. See
  `docs/billing/current-checkout.md`; do not activate production from this guide.
- `jev_database_transport.py` may retry bounded reads once; never extend that to
  RPC mutations or model calls. Preserve finite diagnostic allowlists; no provider
  payloads, credentials or arbitrary exception text in telemetry.
- OAuth issuer/audience/resource checks, delegated ownership and server-owned user
  identity remain strict. A health response alone is not authenticated acceptance.
- Applied SQL migrations are history: do not rewrite/drop them during cleanup.
  Keep Jev migration 062 and markers; do not enable deferred billing 059–061.
  Current catalog migrations 063–064 are additive and do not require 059–061.
  Disabling new admissions does not authorize rolling back the worker to pre-Jev.

## Historical/shared code

Read the [pipeline inventory](docs/architecture/jev-pipeline-keep-manifest.md) and
[companion inventory](docs/architecture/jev-companion-keep-manifest.md) before
removing a legacy-looking module. Import reachability alone does not account for
SQL triggers/RPCs, environment-selected backends, templates or external clients.
The [October cleanup](docs/architecture/jev-cleanup-20261001.md) names the narrow
experimental and unreferenced pieces independently verified for removal.

- Old content matching, Match Repair, previews, billing, verification and Watch
  still serve historical/shared paths. Do not silently retire public endpoints.
- Match Repair is advisory until an audited review changes the mapping.
- Watch uses separate URL comparison and loop identities, revalidates every hop
  for SSRF, and runs in the worker. Only re-probed URLs can close issues; transient
  failures need two sightings; preserve pre-deploy grace and alert delivery guards.
- Legacy browser/v1 entitlement gates must agree. Keep API key management available
  to signed-in free users and preserve historical auth/download return paths.
- Keep Redis available for environment-selected rate-limit storage and scikit-learn
  for historical matching/preview paths. No direct import is not proof of no use.

## Verification

The required CI workflow is `.github/workflows/ci.yml`. See
[`docs/testing.md`](docs/testing.md) for the same local commands and coverage limits.
It checks product authority, transport, SQL, UI behavior and builds independently of
model choice or retrieval strategy. Model-quality evaluation is deliberately not a
CI gate (owner decision, October 1, 2026); do not add one during a harness migration.

Use an isolated checkout/environment. Never copy production credentials into tests.
`python scripts/ci/backend.py` requires disposable loopback PostgreSQL via
`PREFLIGHT_TEST_DATABASE_URL` and fails on skipped or empty required suites.
Frontend Vite build is not TypeScript checking; run `npm run typecheck --prefix frontend` separately. Existing model-specific and capacity
tests remain available for targeted work; they are not required CI gates.

The legacy `dev.py` owns isolated POSIX process groups and uses fixed ports. Broad
process-name killing was deliberately removed: never restore it to clear ports.
Use separate services/ports for concurrent worktrees. Windows cleanup covers direct
children; descendant cleanup is not verified. Account/project settings remain owner-applied.
This guide authorizes no deployment, billing activation or production schema change.
