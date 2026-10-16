# Database Migrations

This directory contains SQL migration scripts for the Redirx database.

## Current release dependencies

The current Jev contract requires migration 062 and preserves its durable run
markers. Deferred billing migrations 059–061 remain inactive; a numeric gap is
not an instruction to enable them. Verify the applied schema against the intended
release before executing SQL. Applied migrations are immutable history.

Applied current billing migrations063–064 remain byte-identical. The product release adds these independent service-role RPCs:

- [065: atomic historical deletion](065-atomic-legacy-session-delete-notes.md)
  must precede the corresponding delete-route release.
- [066: historical dashboard snapshot](066-legacy-dashboard-snapshot-notes.md)
  must precede the corresponding dashboard-reader release.
- [067: bounded historical project pages](../../docs/research/history-pagination/README.md)
  must precede the new cursor API and historical/account screen release.
- [068: atomic historical GSC disconnect](../../docs/research/historical-authority/gsc-disconnect.md)
  must precede the updated browser disconnect service; it requires024 and040.

- [069: run-scoped initial Jev seeds](069-jev-initial-seed-operation-scope-notes.md) fixes confirmation idempotency across runs.

These remain staged, with disposable-database verification only. Missing RPCs
produce recoverable errors rather than partial deletes or fabricated totals.
None activates deferred billing. Existing deployments are not established by
the presence of files in this directory.

The [staged history schema check](../../docs/research/schema-readiness/README.md)
verifies current 065–067 function definitions and effective grants without applying
SQL or invoking RPCs. A matching migration ledger alone does not establish that
those current definitions and grants remain intact. This check is narrower than
whole-product readiness and requires an explicitly reviewed function owner.

## Running Migrations

Migrations must be run manually in the Supabase SQL Editor:

1. Go to [Supabase Dashboard](https://app.supabase.com)
2. Navigate to your project
3. Click "SQL Editor" in the left sidebar
4. Apply only the missing migrations required by the reviewed release, respecting
   their individual prerequisites and the deferred migrations above.

## Migration Files

### 004_add_listen_notify_trigger.sql
**Purpose**: Add PostgreSQL LISTEN/NOTIFY infrastructure for push-based job notifications.

**Creates**:
- `notify_pending_job()` function - Sends notification when jobs become pending
- Trigger on `migration_sessions` table - Fires notification on INSERT/UPDATE
- Channel: `job_queue_events`

**Safe to run**: Yes, non-breaking addition

### 005_add_lease_columns.sql
**Purpose**: Add lease-based locking for concurrent worker support.

**Creates**:
- Columns: `locked_at`, `locked_by`, `lease_expires_at`, `attempt_count`, `last_error`
- Status enum value: `permanently_failed`
- RPC function: `claim_next_job()` - Atomically claim jobs with FOR UPDATE SKIP LOCKED
- RPC function: `reclaim_expired_leases()` - Automatically reset stale locks
- Indexes for efficient lease queries

**Safe to run**: Yes, backward compatible (new columns start NULL)

### 006_add_idempotency_keys.sql
**Purpose**: Prevent duplicate job creation for identical requests.

**Creates**:
- Column: `idempotency_key`
- Unique constraint: `(user_id, idempotency_key)` WHERE idempotency_key IS NOT NULL
- Index for fast lookups

**Safe to run**: Yes, backward compatible (optional column)

### 007_reset_progress_on_claim.sql
**Purpose**: Fix progress meter regression issue by resetting stale progress data.

**Updates**:
- `claim_next_job()` function - Now resets `current_stage`, `stage_name`, `total_stages` to NULL when claiming a job
- Fixes issue where retried jobs show old progress (e.g., stage 7) before updating to current progress

**Safe to run**: Yes, updates existing function (drop and recreate)

### 023_update_handle_new_user_oauth_name_fallback.sql
**Purpose**: Improve auth profile name hydration for OAuth signups.

**Creates/Updates**:
- Replaces `handle_new_user()` trigger function to populate `user_profiles.full_name`
  from fallback metadata keys (`full_name`, `name`, `user_name`, `preferred_username`)
- Backfills missing `user_profiles.full_name` values for existing users from `auth.users`

**Safe to run**: Yes, backward compatible

## Verification

After applying the migrations required by the reviewed release, verify their
individual contracts. The following checks cover only the older worker objects:

```sql
-- Check trigger exists
SELECT tgname, tgtype, tgenabled
FROM pg_trigger
WHERE tgname = 'migration_sessions_notify_trigger';

-- Check RPC functions exist
SELECT proname, pronargs
FROM pg_proc
WHERE proname IN ('claim_next_job', 'reclaim_expired_leases', 'notify_pending_job');

-- Check new columns exist
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'migration_sessions'
  AND column_name IN ('locked_at', 'locked_by', 'lease_expires_at', 'attempt_count', 'last_error', 'idempotency_key')
ORDER BY column_name;
```

## Historical worker rollback reference

The following older recipe concerns migrations 004–007. It is not a rollback plan
for a current Jev deployment: current worker and RPC dependencies must be reviewed
before removing these objects.

```sql
-- Remove trigger
DROP TRIGGER IF EXISTS migration_sessions_notify_trigger ON migration_sessions;

-- Remove functions
DROP FUNCTION IF EXISTS notify_pending_job();
DROP FUNCTION IF EXISTS claim_next_job(TEXT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS reclaim_expired_leases(INTEGER);

-- Remove columns (CAREFUL: this deletes data)
ALTER TABLE migration_sessions
  DROP COLUMN IF EXISTS locked_at,
  DROP COLUMN IF EXISTS locked_by,
  DROP COLUMN IF EXISTS lease_expires_at,
  DROP COLUMN IF EXISTS attempt_count,
  DROP COLUMN IF EXISTS last_error,
  DROP COLUMN IF EXISTS idempotency_key;

-- Remove permanently_failed status (can't remove enum values easily)
-- You would need to recreate the enum type without this value
```

## Notes

- The worker recipes above describe historical migrations 004–007 and 023.
- Re-runnability, lock behavior and compatibility depend on each migration; they
  are not directory-wide guarantees. For example, 066 creates a new function and
  is not intended to be applied twice.
- Review current function definitions before using historical troubleshooting or
  rollback SQL; later migrations may have replaced those definitions.
