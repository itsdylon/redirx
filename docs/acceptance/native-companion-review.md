# Native browser companion review acceptance

This closes a specific integration gap: previous companion Playwright journeys supplied every migration response with fixture JSON, while native Jev acceptance exercised SQL and Flask without the browser. The new journey drives the built companion through real loopback Flask HTTP, actual v2 envelopes/serializers, service-role native PostgreSQL queries and committed review/export migrations. No production code changed to make the test possible.

Entry points:

- `scripts/testing/companion-journey.mjs`: builds the fixture UI and starts disposable PostgreSQL, or accepts an explicit disposable loopback DSN.
- `scripts/testing/run_companion_journey.py`: runs exactly two required tests; empty/skipped suites fail.
- `backend/tests/test_companion_native_journey.py`: reuses the existing NativeJev schema and NativeClient transport, starts actual Flask blueprints and reads final SQL evidence.
- `frontend/tests/native/companion.mjs`: real Chromium interactions, API decisions and downloads. Its network handler only continues loopback requests or blocks external requests. It never fulfills v2 responses.

## Reproduce

Prerequisites: application Python environment, installed frontend lockfile and Playwright Chromium, Node 22, and the existing `mcp-auth-server` development dependencies for automatic disposable PostgreSQL. No new dependency or service account is required.

```sh
JOURNEY_PYTHON=/absolute/path/to/application-venv/bin/python \
COMPANION_JOURNEY_OUTPUT_DIR=/absolute/path/to/fixture-evidence \
node scripts/testing/companion-journey.mjs
```

The default uses the repository's existing embedded-postgres package (18.4 in the tested installation). To use a separately installed PostgreSQL 17 embedded package:

```sh
JOURNEY_PYTHON=/absolute/path/to/application-venv/bin/python \
JOURNEY_PG_MODULE=/absolute/path/to/pg17-tools/node_modules/embedded-postgres/dist/index.js \
COMPANION_JOURNEY_OUTPUT_DIR=/absolute/path/to/fixture-evidence \
node scripts/testing/companion-journey.mjs
```

Alternatively, set `PREFLIGHT_TEST_DATABASE_URL` to a **disposable loopback** server with database-creation privileges. The launcher then uses that server and does not own its process. The reused native fixture creates a random database and drops it with class cleanup. The single offline libpq guard runs before the frontend build or database initialization, then again before fixture execution. It requires an explicit numeric `127.0.0.1` or `[::1]` host, port and database. Hostnames (including `localhost`), socket paths, multiple hosts and query routing overrides (`host`, `hostaddr`, `port`, `dbname`, `service`, `servicefile`) are refused. Nonempty inherited `PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGSERVICE` or `PGSERVICEFILE` are also refused; unset them for this isolated fixture invocation. The automatic path follows the same policy. Parse failures are sanitized and never print the DSN. Do not point this at a valuable local database cluster: fixture roles are cluster-scoped. The automatic launcher binds PostgreSQL to loopback and removes its temporary directory on normal completion. Flask binds an ephemeral loopback port and serves the built UI itself; no separately running dev/preview server is required.

No `.env` is copied. Python dotenv loading is disabled before application imports, provider keys/analytics are cleared, and UI service URLs point at fixtures. Browser external traffic is blocked and asserted empty. Receipt files contain fixture URLs/UUIDs and status paths, not bearer tokens, control credentials or DSNs.

## Verified behavior

At 1280 and 390 pixels, each fresh migration traverses:

1. Real HTTP planning, explicit old/new inventory imports and current Jev run admission. A leased native proposal write and actual finalization make one unresolved row ready for review. SQL confirms no initial approval event.
2. Browser reads the actual row and types a destination containing uppercase text, encoded slashes, encoded spaces and a query. A full export before approval returns HTTP409.
3. A second browser tab explicitly rejects that mapping. The first tab's stale correction receives the native `revision_conflict` outcome in HTTP200/partial, disables stale controls and retains the draft. Keyboard refresh reads current authority; a new deliberate save succeeds.
4. SQL and HTTP readback preserve the exact source and target strings. An explicit HTTP export request (representing the connected agent's action) creates a one-row CSV. The browser refreshes and downloads the actual authorized file; bytes and SHA-256 match the immutable artifact response.
5. A later browser rejection cannot rewrite those bytes. Replaying the original export key returns the original artifact. A new full export at the new revision fails HTTP409. A second authenticated fixture owner receives HTTP404 for the artifact.
6. Revoking the fixture auth provider's verdict makes a correction fail through the real backend auth decorator with HTTP401. The draft remains editable, download is disabled, and restoring access plus explicit refresh enables the existing download. No approval is silently resubmitted.
7. Native SQL confirms exactly three decision events (`reject`, `set_target`, `reject`), the exact corrected target, one immutable artifact, original raw imported source, and the artifact's original verification input. No browser errors, external requests or horizontal overflow occurred.

Fixture URLs:

```text
https://old.example/Case%2FSource?utm_source=A%20B&X=%2FKeep
https://new.example/Case%2FKeep?x=A%20B&next=%2FHome
```

## Recorded checks

- Two journeys passed on PostgreSQL18.4 before adding the explicit pre-approval export assertion.
- Two final journeys passed on PostgreSQL17.10, including that assertion and exact revision-conflict outcome.
- The explicit DSN launcher path was also replayed against PostgreSQL17.10 with the current aiohttp3.14.3 environment; see the retained receipt.
- Node22.23.3; Python3.12.15; Chromium145.0.7632.6. Standalone frontend strict TypeScript passes. Production Vite build passes with the existing large-chunk warning.

The records under `docs/research/native-companion/` contain actual HTTP statuses, final SQL-event assertions, artifact hashes and desktop/mobile screenshots. These are small acceptance journeys, not timing/capacity measurements. The source/target/hash are equal across viewport and database-version replays.

## Explicit boundaries

- External Supabase token verification is a trusted allowlisted fixture verdict. The real v2 resolver and authenticated decorator still determine whether that verdict permits the request. Profile/onboarding are separate fixture responses. This does not test OAuth login, refresh, PKCE, cookies or real token expiry.
- Model output is a trusted fixed proposal inserted through the actual leased `save_jev_proposal` RPC after real admission/claim/dispatch authorization. No provider, embedding, retrieval or worker-loop execution is claimed. Semantic quality has separate benchmarks.
- NativeClient replaces only the PostgREST transport with parameterized native SQL, under service_role. Repository readers, business services, constraints, RPCs and serializers remain real. Relevant schema comes from the existing NativeJev fixture: committed migrations through056,057,058,062 plus its documented minimal earlier tables/JSONB embedding storage. This is not full production schema migration, pgvector or external PostgREST-server acceptance.
- The Flask app registers actual business blueprints, not the production process entry point. Auth/profile/onboarding fixtures and the secret-protected access-control route exist only inside the test module. There is no deployed auth bypass or automatic approval.
- The browser has no export-creation control in the pinned MCP-first product. Creation/replay is an explicit test HTTP client action; review and download occur in the actual companion. Installation, live-site probes, billing, mail and customer accounts are untouched.

Root independently replayed both final journeys from integration `7393ec84` on
PostgreSQL 17.10 / Node 22.23.3 / Python 3.12.15. Each recorded 36 real business HTTP
requests, the same CSV artifact hash, exactly three native decision events and no
browser external requests or page errors. JSON receipts are retained under
`docs/research/native-companion/root-pg17/`.
