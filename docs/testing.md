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
| Frontend | Strict TypeScript checking, all Vitest tests, production Vite build, Chromium smoke cases covering protected deep links/reload, consent recovery, and fixture-authenticated account/settings journeys at desktop/mobile widths |
| Backend contracts | Auth isolation/delegation, inventory validation/import and explicit Search Console opt-out, atomic historical deletion, ownership, safe fetching, worker recovery, real PostgreSQL admission/idempotency/concurrent retries/lease fencing and audited decision-to-artifact publication |
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
npm run typecheck --prefix frontend
npm run test:run --prefix frontend
npm run test:smoke --prefix frontend
```

The smoke command builds the app with fixture settings, then launches its production
preview server on loopback port 4173. It cannot reuse an unrelated running server.
The deep-link and incomplete-consent cases are unauthenticated. Account/settings
cases use fixture authentication and intercepted API responses; they verify the
production browser bundle, not live Supabase or backend integration. All nonlocal
requests are blocked in the added fixtures. Component tests cover authenticated
review interactions; the OAuth suite separately exercises the real protocol over HTTP.
The older mocked pricing Playwright suite remains separate (`test:e2e`).

Backend requires a **disposable loopback PostgreSQL instance** (CI uses PostgreSQL
17) whose user may create databases. Tests create random databases, apply real SQL
and drop them afterward:

```sh
PREFLIGHT_TEST_DATABASE_URL=postgresql://postgres:fixture-only@127.0.0.1:5432/postgres \
  python scripts/ci/backend.py
```

Fixture destination validation requires an explicit `postgresql://` or `postgres://`
URI with numeric `127.0.0.1` or `[::1]`, a port and a database. `localhost`, socket
paths, multiple hosts and routing query overrides are refused. Unset ambient
`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGSERVICE` and `PGSERVICEFILE` for
these fixtures, even if they name another local server. The runner validates before
test collection; native setup methods validate again before connecting. The two
historical subscription Python probes accept only their generated five-field
connection configuration and apply the same destination policy. No production
connection policy is changed. See [fixture guard inventory](research/native-companion/fixture-destination-guards.md).

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

- A Vite build does not type-check the frontend. `npm run typecheck --prefix frontend` separately checks all frontend TypeScript/TSX, including application, tests, browser fixtures and build configuration, with strict checking and no emit. Third-party declarations are checked too. Existing explicit `any` annotations remain outside what static checking can guarantee.
- Atomic historical deletion tests apply the actual schema through058 plus062–063
  and check rollback and positive completion through the authenticated Flask route.
  Deploy migration063 before the updated DELETE endpoint; absent RPCs fail closed.
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

## Native companion browser journey (opt-in)

`JOURNEY_PYTHON=/path/to/venv/bin/python node scripts/testing/companion-journey.mjs` builds the fixture UI and runs real browser→Flask→native PostgreSQL review/export recovery checks at desktop and mobile widths. It uses disposable loopback PostgreSQL and explicit external auth/model fixtures; empty or skipped suites fail. See [native companion acceptance](acceptance/native-companion-review.md) for installed dependencies, PostgreSQL17/custom disposable DSN options, exact boundaries and retained evidence. It does not run from the ordinary frontend unit or backend contract commands.
