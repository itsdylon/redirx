# Migration history does not establish current RPC authority

The existing atomic application runner is correctly pinned to 032 and 034–056.
It validates source hashes and the recorded migration source, stops on uncertain
commits, and does not numerically walk into deferred billing 059–061. It remains
unchanged. Its `already_applied` result establishes ledger identity, not the
current function definition or permissions.

A disposable PostgreSQL reproduction recorded the exact 064 source, replaced
`legacy_dashboard_snapshot` with a different body, and observed
`history_state(...) == ('already_applied', original_version)`. That is expected
ledger behavior but insufficient evidence for shipping a dependent reader.

## New narrowly scoped read-only check

`scripts/release/check_history_schema.py` checks only the independently staged
063 deletion, 064 dashboard and 065 history-page RPCs. Its explicit allowlist pins
the three source SHA-256 values. It neither discovers nor applies migrations,
changes grants, invokes RPCs, reads customer rows, sends schema reload notices,
or retries. There is no `--apply` flag or capture-live-definitions-as-expected mode.

The checker compares actual catalog evidence against those reviewed sources:

- Exact `public` signatures, one function per name, argument names/types/defaults
  and JSONB return type. A same-named function in another schema is insufficient;
  another public overload is rejected rather than assuming PostgREST resolution.
- Exact stored function body, language, volatility, SECURITY DEFINER, strictness,
  leakproof/parallel flags, ordinary function kind, no planner support hook, and
  exact fixed `search_path=public, pg_temp` configuration.
- An **explicitly supplied reviewed owner**. Local fixtures use `postgres`; that
  is not a claim that every hosted migration was applied by the same role. Verify
  the intended deployment owner before running the check; do not copy an observed
  unexpected owner into the expected input merely to make it pass.
- Service-role schema usage and effective function execution; effective denial to
  anon/authenticated, including inherited permissions and PUBLIC. Unexpected
  direct grantees, PUBLIC execution and service-role grant options also fail.

The connection must be idle and autocommit. Inspection runs in an explicit
repeatable-read **READ ONLY** transaction with a ten-second statement timeout and
a fixed catalog search path. Output contains reviewed signatures/source hashes,
finite failure codes and server version; it never prints observed function bodies,
DSNs, role names from unexpected ACLs or raw database error messages. A mismatch
or unknown failure exits nonzero. CLI connections retain verified TLS, a private
credential file and an exact reviewed connection target; no environment file is
loaded. The command is available for a separately authorized release check:

```sh
python scripts/release/check_history_schema.py \
  --dsn-file /private/path/connection.txt \
  --ca-file /private/path/ca.pem \
  --target-file /private/path/reviewed-target.json \
  --expected-owner REVIEWED_FUNCTION_OWNER
```

The target JSON uses the same `connection_target` object as the historical runner:
exact `host`, `port`, `user` and `dbname` strings. Host-address/service overrides
are rejected. The script does not authorize connecting to or changing production.

## Evidence and limits

23 combined tests passed on disposable native PostgreSQL 18.4 with Python 3.12.15:
8 new source/catalog/CLI tests plus 15 existing atomic release tests, zero skips.
The positive catalog test runs as a fresh unprivileged auditor with no execution
rights to any of these RPCs. All three match; the read-only setting is observed
as on; fixture canary rows, tuple identity and transaction IDs remain unchanged.
Adversarial tests positively detect body drift despite intact ledger, moved and
overloaded functions, changed defaults/owner/security/search path, revoked service
grants, PUBLIC/browser grants, inherited service-role grants and grant options.
The source test rejects a modified migration and pins both explicit file lists,
so adding a numeric migration to the directory cannot expand either packet.
The CLI failure test supplies a deliberately invalid fixture DSN and verifies a
nonzero result without leaking its secret or the parser's raw error text.

This is **not whole-product readiness**. It does not verify 062 or earlier schema,
table/RLS/trigger/index contracts, owner-role capabilities, arbitrary role membership,
PostgREST schema-cache freshness, API credentials/ownership paths, or worker version.
Existing native behavioral tests remain necessary. Source/definition matching is
deliberately strict; legitimate future function changes require a reviewed updated
contract. Catalog rendering is verified on the stated native version, not every
PostgreSQL release or hosted configuration. Root separately runs the integrated
suite against PostgreSQL 17, the CI major.

The check takes no deployment lock. Concurrent DDL, grants or later changes can
invalidate its observation; it is evidence at inspection time, not a durable
authorization token. Re-run it in the separately reviewed release sequence.
Missing staged functions are a failed prerequisite, never a request to apply every
numeric migration. Apply only the independently reviewed 065–067 prerequisites
before their dependent application release. Deferred 059–061 remain inactive.
No production connection, schema application, account setting or deployment was
performed during this work.

To reproduce, run `backend.tests.test_history_schema_readiness` and
`backend.tests.test_pivot_migration_release` through the disposable-loopback backend
test harness with dotenv loading disabled. The new module creates and drops its
own random fixture database; it rejects non-loopback fixture hosts through the
shared native fixture. Required-CI enrollment is coordinated with the root agent.

Root integration verification at `ba2c638`: all 23 new/existing release tests passed
on native PostgreSQL 17.10, and the combined required backend suite passed 428 tests
with zero skips. The CLI hosted TLS connection remains unverified.

A separately named [066 GSC disconnect check](gsc-disconnect-066.md) now reuses the
same catalog comparator. It does not expand this history check or the historical
application packet. Run it separately when releasing the dependent disconnect service.

The shared release CLI now also [rejects inherited libpq routing](release-target-validation.md)
before connecting. Review the explicit environment requirements before a separately
authorized invocation; it no longer accepts ambient host/service routing inputs.
