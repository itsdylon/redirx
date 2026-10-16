# Historical history pages and whole-account search

The browser history screen now reads 50 summaries per page; the account screen reads
only its five recent summaries. Search runs against all eligible history instead
of filtering whichever rows happened to survive a database response cap. Requests
use owner/mode/search/cursor/limit-specific cache keys under the existing owner
invalidation prefix. Raw session IDs and opaque cursors are passed unchanged.

## API contract

`GET /api/user/sessions?pagination=cursor&limit=50&q=literal&cursor=opaque`

- `pagination=cursor` is explicit. Limit defaults to 50 and accepts 1–100.
- `q` is a trimmed, case-insensitive literal substring over project name, raw status
  and raw pipeline type. Empty/null names match the existing “Untitled Session”
  display fallback. `%`, `_` and backslash are ordinary characters, not patterns.
  Search is limited to 200 Unicode characters.
- Response: `success`, `pagination: "cursor"`, `sessions`, `total_count`,
  `next_cursor` and normalized `search`. Sessions contain the same eight summary
  fields as the dashboard. Total count covers all matching owned history, not
  merely this page. It includes MCP-linked sessions and excludes tutorials/previews,
  matching the existing list population.
- One SQL statement computes the count and bounded page. Ordering is
  `created_at DESC NULLS FIRST,id DESC`, preserving historical descending-date
  behavior and adding an unambiguous UUID tie-break. NULL timestamps are handled
  explicitly on both sides of the cursor. Microseconds never pass through JS Date.
- Cursor is an opaque versioned continuation bound to the verified owner and
  normalized search. Invalid/oversized/mismatched tokens return 400. The token is
  not an authorization capability; database owner predicates remain authoritative.
- Separate page requests are live, not a frozen snapshot. Newer inserts may require
  a fresh first-page read; renames/deletes can alter search membership and count.
  Deleted cursor anchors do not prevent continuation. UI shows current count/page,
  not invented fixed row ranges, and offers “Reload first page.”
- Database/acknowledgment failure returns 503. Missing or unlabeled page results do
  not become a successful empty list. Pagination never falls back to a full download.

`GET /api/user/sessions` without the mode retains `{success,sessions}` and original
SELECT* extras for older callers. It now drains ascending-ID reads of at most 500
rows, with a fixed initial upper bound, then sorts the result into historical date
order. Smaller transport caps do not truncate it. Every row must advance the ID;
repeated/malformed/wrong-owner pages fail instead of looping or returning partial
success. **This compatibility response remains unbounded in total memory/bytes.**
It is also live across reads, so inserts behind its cursor require another request.
All known current UI consumers now use bounded mode. The compatibility helper
remains exported for older bundles/clients; no public endpoint was silently retired.

## Rollout and recovery

Apply additive migration 065 before deploying the paginated API, then deploy the
frontend. No applied migration changed; deferred 059–061 remain excluded. 065 adds
only a `STABLE SECURITY DEFINER` read function with fixed search path and execute
permission restricted to service_role. It does not create rights, mutate sessions,
change billing, or loosen existing deletion/ownership guards.

New frontend against an older API rejects its unlabeled full-list result with
reload guidance. New API without 065 returns 503 for cursor mode while legacy full
mode remains available. Older frontend against the new API retains its old response
shape. No production migration or deployment was performed.

## Verification and limits

Native PostgreSQL 18.4 fixtures cover 1205 rows through an actual SQL transport capped
at 37 rows (below the requested legacy page size),
exact literal search outside the first page, another owner, tutorial/preview
exclusion, explicit MCP-session inclusion in both modes, empty/no-results, tied and NULL dates, sub-millisecond cursor precision,
interleaved insertion/rename/deleted anchors, Unicode query continuation,
malformed/oversized/scope-mismatched cursors, and service-role privileges. The
legacy compatibility test asserts all 1205 IDs and preserved URL-array extras.
A separate transport test positively rejects repeated/malformed pages.

Frontend interaction tests cover owner changes with late query and mutation
responses, page/search failure and retry, opaque token transmission, malformed
responses, and whole-history search. Production-bundle Chromium journeys at 1280 px
and 390 px verify 50 rendered rows, a failed next page followed by successful retry,
searching a fixture row beyond 1000, search membership updating after rename,
horizontal keyboard scrolling on mobile, and no document-width overflow.
All browser traffic is fixture-intercepted; no hosted API or customer files.

Counts and search still scan matching account history. No substring-search index,
constant-time query claim, cross-request snapshot, physical-device or multi-engine
browser claim is made. Deep lifetime-scale cost should be measured before adding
an index or changing search semantics.


Final checks: 13 backend tests passed (7 native history tests, 1 legacy progress test,
5 existing route tests), zero skips; 19 focused frontend tests passed; TypeScript
checking and the production build passed; 2 fixture Chromium journeys passed.
The impeccable mechanical detector returned no findings. Desktop/mobile renders
were inspected in two bounded rounds. The existing Vite large-chunk warning remains;
no claim is made that this package fixes bundle size. Root owns required-CI ledger
updates for `backend.tests.test_history_pages` and the new browser fixture.

Captured fixtures: [mobile history](history-390.png),
[page recovery](history-retry-1280.png), [search after rename](history-search-1280.png).

To reproduce frontend checks:

```sh
npm run typecheck --prefix frontend
npm run test:run --prefix frontend -- src/api/sessions.test.ts src/components/history/History.test.tsx src/components/AccountPage.test.tsx
npm run test:e2e --prefix frontend -- --config playwright.history.config.ts
```

Run `backend.tests.test_history_pages` and `backend.tests.test_user_session_filters`
through the disposable-loopback backend CI harness with dotenv loading patched off.
No environment files or credentials belong in these fixtures.
