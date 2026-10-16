# Companion review continuity — October 15, 2026

The active browser surfaces are `/companion` (connection instructions and paged
migration history) and `/migrations/:migrationId` (status, Jev refinement,
filtered/paged review, explicit correction, and existing artifact download).
Historical details additionally retain payment, Search Console and monitoring.
This package changes the detail/review flow only; history, auth, backend policy,
provider calls, export authority and historical service contracts are preserved.

## Problems addressed

- Saving one decision previously discarded every page after the first. The
  detail now reloads as many pages as the reviewer opened, starting from the
  first page and following fresh server cursors. A changed filter starts over;
  a changed run clears its drafts and page count. No stale revision is merged.
- A failed reload now disables decisions until fresh revisions arrive. Typed
  corrections remain available. A route change mounts a fresh detail so a
  failed second migration cannot display the first migration's actions/data.
- A successful audited decision produces explicit, screen-reader-announced
  confirmation. Proposal confidence remains an estimate and setting a
  destination is explicitly described as approval.
- Corrections use a native form: Enter submits, invalid/non-HTTP(S) URLs are
  blocked locally, and valid raw case/encoding are sent unchanged. Backend URL
  membership and all other authority remain final.
- Source, saved/approved destination and proposal are visibly distinguished.
  Long URLs wrap on narrow screens. Queued/running states explain polling and
  that the reviewer can leave and return.

## Verification

- 48 focused frontend/API tests passed, including 4 new regressions for fresh
  cursor/revision reload, failed refresh/draft preservation, exact keyboard URL
  correction, and navigation resource isolation.
- `node scripts/check_pivot_contract.mjs` passed (9 tools, 9 shared pricing cases).
- Vite production build passed; existing large-chunk warning remains. This is
  not a complete TypeScript check; this frontend has no tsconfig/typecheck script.
- Impeccable mechanical detector returned no findings for the changed component.
- Fixture-only Chromium checks: 1440px desktop and 390px mobile had no horizontal
  overflow; mobile correction input computed to 16px; Enter saved an explicit
  correction; a javascript: URL was blocked; no browser page errors. External
  network was blocked. This exercised the real component and stylesheet with
  fixture responses and an unauthenticated shell, not production authentication.
- Browser evidence retained under `/private/tmp/redirx-product-rd-20261015/`:
  `companion-review-{1440,390}.png` and `companion-review-browser.json`.

## Tradeoffs and follow-up

Refreshing all opened pages costs additional bounded read requests after each
save; it preserves correct server filtering and revision authority. A dedicated
server cursor/review-position endpoint could avoid those reads later. Concurrent
edits remain guarded by expected revisions; this package adds no auto-retry of
mutations. Browser zoom, screen-reader software and physical iOS were not tested.
A failed per-row decision still announces the server error at the page header;
future work could place conflict recovery beside the affected mapping.
