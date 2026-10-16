# Exact historical dashboard snapshot

Apply 064 before releasing the new `/api/user/dashboard` reader. It adds the
service-role-only `legacy_dashboard_snapshot(UUID)` function and does not alter
tables, existing RPCs, approval policy or billing. It uses historical columns from
001/018/020; deferred migrations 059–061 are not prerequisites. Migration 063 is a
separate delete-route dependency in the same staged release.

The function returns one owner-scoped aggregate snapshot and five projected recent
sessions, avoiding PostgREST row-cap truncation and transferring no URL arrays.
Tutorial/preview exclusions, MCP-linked session inclusion and confidence thresholds
match the prior dashboard. Nullable counts/scores are treated as zero. Recent-session
objects preserve the browser's declared fields; undocumented SELECT-star extras are
intentionally omitted. See the [native evidence](../../docs/research/read-paths/README.md).

An unavailable or malformed RPC returns a recoverable 503; the reader never falls
back to truncated success. Applying the unused function before the reader is
compatible with the old reader. Roll back the application before removing its RPC
dependency. Do not edit applied migration files or execute this change without a
reviewed release. No production SQL or deployment has been performed by this package.
