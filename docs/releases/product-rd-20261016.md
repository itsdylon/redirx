# Product R&D release — October 16, 2026

Owner approved the reviewed product screenshots and technical figures for release.
The release applies the production changes from research checkpoint1a6b2caba783209b3acdc1cad8108eadd1b3121f onto current main3b6fa99a04a29b20ab4bfc0188101183f868129e. Raw benchmark corpora and full research receipts remain in the local handoff bundle; only production code, tests, supporting synthetic research helpers and release documentation are published.

The deployed current billing behavior remains authoritative. Paid proposal reads retain deterministic pagination through15000 rows, including the early completed-work check. Completion keeps current billing activation checks and the corrected telemetry replay semantics. Both paid inputs and structured MCP outputs survive the merge.

Previously staged migrations063–067 were never applied. They are renamed065–069 to follow applied063_current_billing and064_paid_jev_admission. SQL bytes and pinned source hashes are unchanged. Deferred059–061 remain untouched.

Apply only065_atomic_legacy_session_delete,066_legacy_dashboard_snapshot,067_legacy_session_pages,068_atomic_legacy_gsc_disconnect,069_jev_initial_seed_operation_scope. Verify function bodies, owners and effective privileges after application. Deploy the exact tested commit to the worker, then API, then gateway and frontend. Retain existing settings, billing activation, provider budget and concurrency. The OAuth broker has no code changes.

Marketing applies only the six reviewed changes afterddaad2d5 to production mainf160f5aa, leaving the separate pricing-preview commits out. Refresh instrumented MCP schemas against this release and describe the already-live current billing authority accurately. Publish marketing after the gateway.

Validation:654 backend contract tests;756 frontend unit tests; standalone TypeScript check;38 browser smoke tests (one optional audit skipped);106 MCP tests;26 OAuth tests;62 SQL compatibility tests (three native-only cases separately covered);14 native PostgreSQL paid tests, including501-URL replay/refinement/export with all five release migrations;33 Jev optimization/provider/native lifecycle tests. Production acceptance and deployment IDs are recorded separately in the local release receipt.

No research-only model, PageIndex, threading or labeling experiment becomes a production default in this release. Exact deployed SHA, health and authenticated acceptance still require post-deployment verification.
