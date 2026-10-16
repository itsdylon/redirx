# Application CI

`.github/workflows/ci.yml` runs on every pull request, pushes to `main`, and manual
dispatch. Three checks cover application behavior without prescribing a matching
model, prompts, RAG, tree retrieval or any other retrieval implementation.

**Decision, October 1, 2026:** model-quality evaluation is deliberately outside CI.
The owner evaluates harness releases. Do not add model scoring, paid provider calls
or a benchmark gate as part of a harness replacement. Existing focused engine tests
remain available for changes to those modules.

| Check | Required coverage |
| --- | --- |
| Frontend | All Vitest tests, production Vite build, three Chromium smoke cases covering protected deep links/reload and incomplete consent |
| Backend contracts | Auth isolation/delegation, inventory validation/import, ownership, safe fetching, worker recovery, real PostgreSQL admission/idempotency/concurrent retries/lease fencing and audited decision-to-artifact publication |
| MCP, OAuth and SQL | Tool contract, TypeScript compilation, all gateway/auth tests including the native SDK OAuth flow and concurrent token consumption, plus PGlite SQL permissions/invariants |

The workflow uses read-only repository permissions, pinned action revisions, job
timeouts and cancellation of superseded runs. No deployment step or service secrets.
Failed browser runs retain traces and screenshots for seven days.

## Run locally

Use an isolated checkout, Node 22, Python 3.12 and no production environment files.
Install the checked-in dependency declarations:

```sh
python -m pip install -r requirements.txt
npm ci --prefix frontend
npm ci --prefix mcp-server
npm ci --prefix mcp-auth-server
npm ci --prefix database/tests
```

Frontend (install Chromium once from `frontend` with `npx playwright install chromium`):

```sh
npm run test:run --prefix frontend
npm run test:smoke --prefix frontend
```

The smoke command builds the app with fixture settings, then launches its production
preview server on loopback port 4173. It cannot reuse an unrelated running server.
No API responses are faked; these are unauthenticated browser smoke checks, not a
complete signed-in product journey. Component tests cover authenticated review
interactions; the OAuth suite separately exercises the real protocol over HTTP.
The older mocked pricing Playwright suite remains separate (`test:e2e`).

Backend requires a **disposable loopback PostgreSQL instance** (CI uses PostgreSQL
17) whose user may create databases. Tests create random databases, apply real SQL
and drop them afterward:

```sh
PREFLIGHT_TEST_DATABASE_URL=postgresql://postgres:fixture-only@127.0.0.1:5432/postgres \
  python scripts/ci/backend.py
```

The explicit module list in `scripts/ci/backend.py` avoids accidental collection of
live-provider, historical billing and capacity suites. Add new product-contract
tests there. A missing database, import failure, empty module or skipped test fails
the command. The runner disables dotenv loading. Stored proposals are fixture data;
their generation method does not determine whether ownership, retries, decisions
and exports work.

Services (install both MCP packages before the OAuth suite, which uses the real SDK):

```sh
node scripts/check_pivot_contract.mjs
npm run build --prefix mcp-server
npm test --prefix mcp-server
npm test --prefix mcp-auth-server
npm test --prefix database/tests
```

OAuth tests launch and clean up embedded PostgreSQL. The SQL package runs its
`*.test.mjs` files with PGlite; the separate historical billing `.postgres.mjs`
acceptance scripts are not part of this lean gate.

## Limits and enforcement

- A Vite build does not type-check the frontend. The gateway's `tsc` build does.
- Native SQL fixtures adapt the database transport and apply scenario-specific
  migration chains. They do not test deployed Supabase/PostgREST configuration or
  prove that the entire production migration history applies to an empty database.
- This is not full repository test discovery, model evaluation, load testing or a
  deployment acceptance check. There is no claimed line-coverage percentage.
- After the workflow has run, the owner can require **Frontend**, **Backend
  contracts**, and **MCP, OAuth and SQL** in main's branch protection. Adding the
  workflow itself does not change branch-protection settings.

Current billing adds SDK-constrained Stripe adapter and route tests to the required
backend gate. The backend job also runs `current-billing.test.mjs` with native
PostgreSQL, two concurrent clients, and the Python paid worker fixture. That fixture
executes 501 URLs, retries saved progress, and verifies completed exports after the
work window expires; it uses a synthetic decision provider. The ordinary PGlite
command skips those three native-only cases. No test makes a Stripe purchase.
