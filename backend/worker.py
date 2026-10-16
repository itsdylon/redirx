"""
Background worker for processing redirect mapping jobs.

This worker uses PostgreSQL LISTEN/NOTIFY for push-based job processing.
Run with: python -m backend.worker
"""

import asyncio
import traceback
import sys
import os
import socket
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import json
import time
from contextlib import ExitStack

# Add project root to path for imports
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE_DIR)
# ...and src/, so `redirx.x` resolves here the same way it does under the API.
# Services shared with the Flask app (which adds both) import that way, and a
# worker that only understood `src.redirx.x` would fail on them at runtime.
sys.path.insert(0, os.path.join(_BASE_DIR, "src"))

from src.redirx.database import MigrationSessionDB, SupabaseClient, URLMappingDB, UserQuotaDB
from src.redirx.lib import Pipeline
from src.redirx.rate_limit import close_limiter_pools
from uuid import UUID
from src.redirx.config import Config
from backend.services.deep_preview_service import DeepPreviewService
from backend.services.migration_run_service import MigrationRunService
from backend.services.migration_subscription_service import MigrationSubscriptionService
from backend.services.pivot_background import PivotBackgroundRunner
from backend.services.pivot_resource_budget import reserve_pivot_temporary_capacity
from backend.services.match_repair_service import MatchRepairService
from backend.services.job_limits import (
    ContentJobUrlCapExceeded,
    validate_content_job_url_counts,
)

# PostgreSQL direct connection for LISTEN/NOTIFY
try:
    import psycopg
except ImportError:
    print("[Worker] ERROR: psycopg not installed. Run: pip install 'psycopg[binary]>=3.1.0'")
    sys.exit(1)

# A dropped connection is ordinary for a socket that idles for hours; these
# mean "reconnect", not "exit".
TRANSIENT_DB_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)
# Releasing to 'pending' is a retry, not an ending.
TERMINAL_JOB_STATUSES = frozenset({"completed", "permanently_failed", "failed"})
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_MAX_ATTEMPTS = 8


# Worker configuration from environment
def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer env var and clamp it into [minimum, maximum]."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        print(
            f"[Worker] Invalid {name}={raw!r}; using default {default}",
            flush=True,
        )
        value = default

    if value < minimum:
        print(
            f"[Worker] {name}={value} below minimum {minimum}; clamping",
            flush=True,
        )
        return minimum
    if value > maximum:
        print(
            f"[Worker] {name}={value} above maximum {maximum}; clamping",
            flush=True,
        )
        return maximum
    return value


WORKER_LEASE_DURATION = int(os.getenv('WORKER_LEASE_DURATION', '600'))  # 10 minutes
WORKER_MAX_CONCURRENT = _bounded_env_int('WORKER_MAX_CONCURRENT', 1, 1, 32)
WORKER_FALLBACK_INTERVAL = int(os.getenv('WORKER_FALLBACK_INTERVAL', '60'))  # 60 seconds
WORKER_MAX_ATTEMPTS = int(os.getenv('WORKER_MAX_ATTEMPTS', '5'))  # Max retries

# Post-cutover monitoring. Sweeps ride along in this process because it is the
# only long-lived one we run: the API is a single sync gunicorn worker where a
# multi-minute crawl would block every other request.
WATCH_ENABLED = os.getenv('WATCH_ENABLED', 'true').strip().lower() not in (
    '0', 'false', 'no', 'off',
)
# How often to look for a due watch. Cheap — one indexed query returning
# nothing in the common case.
WATCH_POLL_INTERVAL = _bounded_env_int('WATCH_POLL_INTERVAL', 60, 10, 3600)
# Long by job standards, because a paced sweep of a few thousand URLs takes
# tens of minutes. Nothing is waiting on it, so a stale lease costs an hour of
# monitoring latency rather than a user-visible delay — which buys the right to
# skip a lease-extension loop entirely.
WATCH_LEASE_DURATION = _bounded_env_int('WATCH_LEASE_DURATION', 3600, 300, 21600)

# Worker identifier: hostname-pid-uuid
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{str(uuid_module.uuid4())[:8]}"


class RedirxWorker:
    """
    Push-based worker using PostgreSQL LISTEN/NOTIFY.
    """

    # Watch-sweep state, declared on the class rather than only in __init__.
    # The loops are exercised by tests that build an instance via __new__ to
    # isolate them from the database, so anything the loops touch has to exist
    # without __init__ having run. All immutable, so no shared-state hazard.
    #
    # At most one sweep runs at a time regardless of job concurrency: sweeps
    # are almost entirely idle time waiting on the host rate limiter, so one
    # alongside jobs costs nothing, while two would double the request rate a
    # customer's origin sees from us.
    watch_task: Optional[asyncio.Task] = None
    last_watch_poll: float = 0.0
    watches_swept: int = 0

    def __init__(self):
        self.worker_id = WORKER_ID
        self.session_db = MigrationSessionDB()
        self.running = False
        self.jobs_processed = 0
        self.max_concurrent = WORKER_MAX_CONCURRENT
        self.in_flight_tasks: set[asyncio.Task] = set()
        self.pg_conn = None
        self.pg_claim_conn = None

    def _apply_usage_accounting(
        self,
        *,
        user_id: Optional[str],
        session_id: UUID,
        pipeline_type: str,
        is_preview: bool,
        old_urls: list[str],
        new_urls: list[str],
    ) -> None:
        """
        Apply post-completion accounting for pricing v2.

        Rules:
        - Preview jobs are non-billable.
        - Quick Match (url_only) is non-metered.
        - Agency Deep Match emits one Stripe metered event keyed by session_id.
        """
        if not user_id:
            return

        if is_preview or pipeline_type != 'content':
            return

        quota_db = UserQuotaDB()
        if quota_db.get_plan(user_id) != 'agency':
            return

        billable_pages = max(len(old_urls), len(new_urls))
        if billable_pages <= 0:
            return

        try:
            from backend.services.stripe_service import StripeService

            result = StripeService().record_agency_usage(
                session_id=str(session_id),
                user_id=user_id,
                billable_pages=billable_pages,
                metadata={
                    'source': 'worker_completion',
                    'pipeline_type': pipeline_type,
                },
            )
            print(
                f"[Worker] Agency metering result for session {session_id}: {result}",
                flush=True,
            )
        except Exception as metering_error:
            print(
                f"[Worker] Failed to emit agency meter event for session {session_id}: "
                f"{metering_error}",
                flush=True,
            )

    def get_database_url(self) -> Optional[str]:
        """
        Get PostgreSQL connection string from environment.

        Returns:
            DATABASE_URL connection string, or None if not set.
        """
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            print("[Worker] WARNING: DATABASE_URL not set. Falling back to polling mode.")
            print("[Worker] For push-based mode, set DATABASE_URL in .env")
            print("[Worker] Get from: Supabase Dashboard → Connect → Direct connection")
            return None
        return database_url

    def connect_postgres(self, quiet: bool = False) -> Optional[psycopg.Connection]:
        """
        Establish PostgreSQL connection for LISTEN/NOTIFY.

        TCP keepalives are set so a dropped connection is detected by the
        socket layer within ~a minute instead of at the next read. The LISTEN
        connection is idle by design — it can sit for hours between jobs —
        which is exactly the shape NAT timeouts and pooler recycling kill.

        Returns:
            PostgreSQL connection, or None if connection fails.
        """
        database_url = self.get_database_url()
        if not database_url:
            return None

        try:
            if not quiet:
                print("[Worker] Connecting to PostgreSQL...", flush=True)
            conn = psycopg.connect(
                database_url,
                autocommit=True,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
            if not quiet:
                print("[Worker] PostgreSQL connection established", flush=True)
            return conn
        except Exception as e:
            print(f"[Worker] Failed to connect to PostgreSQL: {e}", flush=True)
            return None

    def _close_quietly(self, conn: Optional[psycopg.Connection]) -> None:
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    async def _recover_connections(self) -> bool:
        """
        Rebuild both PostgreSQL connections after a transient failure.

        A long-lived connection dropping is ordinary, not exceptional: idle
        LISTEN sockets get closed by NAT, poolers recycle, and databases
        restart. Treating it as fatal meant the process died and only recovered
        because the platform restarted it — during which push delivery silently
        stopped and jobs waited for the 60s fallback poll.

        Returns False when the database stays unreachable, so the caller can
        degrade to polling instead of exiting.
        """
        self._close_quietly(self.pg_conn)
        self._close_quietly(self.pg_claim_conn)
        self.pg_conn = None
        self.pg_claim_conn = None

        delay = RECONNECT_BASE_DELAY
        for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
            await asyncio.sleep(delay)
            conn = self.connect_postgres(quiet=attempt > 1)
            if conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("LISTEN job_queue_events")
                    self.pg_conn = conn
                    self.pg_claim_conn = self.connect_postgres(quiet=True)
                    print(
                        f"[Worker] Reconnected and re-subscribed after {attempt} "
                        f"attempt{'s' if attempt != 1 else ''}",
                        flush=True,
                    )
                    return True
                except Exception as e:
                    print(f"[Worker] Re-subscribe failed: {e}", flush=True)
                    self._close_quietly(conn)

            print(
                f"[Worker] Reconnect attempt {attempt}/{RECONNECT_MAX_ATTEMPTS} "
                f"failed; retrying in {min(delay * 2, RECONNECT_MAX_DELAY):.0f}s",
                flush=True,
            )
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

        print("[Worker] Could not restore PostgreSQL connection", flush=True)
        return False

    @staticmethod
    def _wait_for_notification(conn: psycopg.Connection, timeout: float) -> Optional[str]:
        """
        Block for one notification. Runs in a thread — psycopg's connection is
        synchronous, so calling this inline would stall the event loop for the
        whole timeout and freeze lease extension for any in-flight job.
        """
        try:
            for notify in conn.notifies(timeout=timeout):
                return notify.channel
        except TimeoutError:
            return None
        return None

    async def claim_job(self) -> Optional[Dict[str, Any]]:
        """
        Atomically claim the next available job using RPC function.

        Returns:
            Job data if claimed, None if no jobs available.
        """
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=WORKER_LEASE_DURATION)).isoformat()

        # Prefer direct PostgreSQL claim when available (LISTEN/NOTIFY mode).
        # This avoids PostgREST/RPC visibility edge cases.
        pg_claim = self._claim_job_via_postgres(lease_expires_at)
        if pg_claim:
            print(f"[Worker] Claimed job via PostgreSQL: {pg_claim['id']} (attempt {pg_claim['attempt_count']})", flush=True)
            return pg_claim

        try:
            client = SupabaseClient.get_client()

            result = client.rpc('claim_next_job', {
                'p_worker_id': self.worker_id,
                'p_lease_expires_at': lease_expires_at
            }).execute()

            if result.data and len(result.data) > 0:
                job = result.data[0]
                print(f"[Worker] Claimed job: {job['id']} (attempt {job['attempt_count']})", flush=True)
                return job
            # Fallback: if RPC returns nothing, try direct claim for environments
            # where claim_next_job is missing/misconfigured.
            return self._claim_job_fallback(client, lease_expires_at)

        except Exception as e:
            print(f"[Worker] Error claiming job: {e}")
            traceback.print_exc()
            try:
                client = SupabaseClient.get_client()
                return self._claim_job_fallback(client, lease_expires_at)
            except Exception as fallback_error:
                print(f"[Worker] Fallback claim also failed: {fallback_error}")
                return None

    def _claim_job_via_postgres(self, lease_expires_at: str) -> Optional[Dict[str, Any]]:
        """
        Claim a job directly through PostgreSQL to avoid RPC visibility/permission gaps.
        Keeps backward compatibility with older claim_next_job return signatures.
        """
        if not self.pg_claim_conn:
            # Rebuild lazily. Without this, one dropped claim connection sent
            # every future claim down the slower REST fallback for the life of
            # the process, with nothing in the logs to say why.
            self.pg_claim_conn = self.connect_postgres(quiet=True)
            if not self.pg_claim_conn:
                return None

        try:
            with self.pg_claim_conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM claim_next_job(%s, %s)",
                    (self.worker_id, lease_expires_at),
                )
                row = cursor.fetchone()
                if not row:
                    return None

                columns = [desc.name for desc in cursor.description]
                claimed = dict(zip(columns, row))
                claimed_id = claimed.get('id')
                if isinstance(claimed_id, UUID):
                    claimed_id = str(claimed_id)

                claimed_source = claimed.get('source_session_id')
                if isinstance(claimed_source, UUID):
                    claimed_source = str(claimed_source)

                job = {
                    'id': claimed_id,
                    'user_id': claimed.get('user_id'),
                    'project_name': claimed.get('project_name'),
                    'old_urls': claimed.get('old_urls') or [],
                    'new_urls': claimed.get('new_urls') or [],
                    'attempt_count': int(claimed.get('attempt_count') or 1),
                    'pipeline_type': claimed.get('pipeline_type') or 'content',
                    'is_preview': bool(claimed.get('is_preview', False)),
                    'source_session_id': claimed_source,
                    'mcp_run_id': str(claimed['mcp_run_id']) if claimed.get('mcp_run_id') else None,
                }
                return job
        except TRANSIENT_DB_ERRORS as e:
            # Drop the dead handle so the next claim rebuilds it rather than
            # failing the same way forever.
            print(f"[Worker] Claim connection lost, will rebuild: {e}", flush=True)
            self._close_quietly(self.pg_claim_conn)
            self.pg_claim_conn = None
            return None
        except Exception as e:
            print(f"[Worker] PostgreSQL claim failed: {e}", flush=True)
            return None

    def _claim_job_fallback(self, client, lease_expires_at: str) -> Optional[Dict[str, Any]]:
        """
        Direct table-based claim fallback when claim_next_job RPC returns no row.
        Uses optimistic status check for safe single-row claim.
        """
        pending = client.table('migration_sessions').select(
            '*'
        ).eq(
            'status', 'pending'
        ).order(
            # Mirror claim_next_job: priority first, oldest within a priority.
            'priority', desc=True
        ).order(
            'created_at', desc=False
        ).limit(1).execute()

        if not pending.data:
            return None

        candidate = pending.data[0]
        current_attempt = int(candidate.get('attempt_count') or 0)
        new_attempt = current_attempt + 1

        update_result = client.table('migration_sessions').update({
            'status': 'processing',
            'locked_at': datetime.now(timezone.utc).isoformat(),
            'locked_by': self.worker_id,
            'lease_expires_at': lease_expires_at,
            'attempt_count': new_attempt,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'completed_at': None,
            'current_stage': None,
            'stage_name': None,
            'total_stages': None,
        }).eq(
            'id', candidate['id']
        ).eq(
            'status', 'pending'
        ).execute()

        if not update_result.data:
            print("[Worker] Fallback claim saw pending row, but it was claimed concurrently", flush=True)
            return None

        job = {
            'id': candidate['id'],
            'user_id': candidate.get('user_id'),
            'project_name': candidate.get('project_name'),
            'old_urls': candidate.get('old_urls') or [],
            'new_urls': candidate.get('new_urls') or [],
            'attempt_count': new_attempt,
            'pipeline_type': candidate.get('pipeline_type') or 'content',
            'is_preview': bool(candidate.get('is_preview', False)),
            'source_session_id': candidate.get('source_session_id'),
            'mcp_run_id': candidate.get('mcp_run_id'),
        }
        print(
            f"[Worker] Claimed job via fallback: {job['id']} (attempt {job['attempt_count']})",
            flush=True,
        )
        return job

    @staticmethod
    def _session_update_acknowledged(result, session_id, attempt_count, expected):
        """A returned representation proves this exact update, not a zero-row no-op."""
        if getattr(result, 'error', None):
            return False
        rows = getattr(result, 'data', None)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return False
        row = rows[0]
        if (str(row.get('id')) != str(session_id)
                or type(row.get('attempt_count')) is not int or row['attempt_count'] != attempt_count):
            return False
        for key, value in expected.items():
            if key not in row:
                return False
            if key in {'completed_at', 'lease_expires_at'} and value is not None:
                try:
                    actual = datetime.fromisoformat(str(row[key]).replace('Z', '+00:00'))
                    if actual != datetime.fromisoformat(value):
                        return False
                except (TypeError, ValueError):
                    return False
            elif row[key] != value:
                return False
        return True

    async def extend_lease(self, session_id: UUID, *, attempt_count: int) -> bool:
        """Renew only this processing attempt; an empty response is not success.

        The expiry predicate uses a client-clock snapshot, not database now().
        Pivot dispatch/finalization RPCs retain their independent SQL clock guard.
        """
        if type(attempt_count) is not int or attempt_count < 1:
            return False
        try:
            client = SupabaseClient.get_client()
            now = datetime.now(timezone.utc)
            expiry = (now + timedelta(seconds=WORKER_LEASE_DURATION)).isoformat()
            result = client.table('migration_sessions').update({
                'lease_expires_at': expiry
            }).eq('id', str(session_id)).eq('locked_by', self.worker_id).eq(
                'attempt_count', attempt_count
            ).eq('status', 'processing').gt('lease_expires_at', now.isoformat()).execute()
            confirmed = self._session_update_acknowledged(result, session_id, attempt_count, {
                'lease_expires_at': expiry, 'locked_by': self.worker_id, 'status': 'processing',
            })
            if confirmed:
                print(f"[Worker] Extended lease for job {session_id}")
            else:
                print(f"[Worker] Lease extension not confirmed for job {session_id}")
            return confirmed
        except Exception:
            print(f"[Worker] Lease extension not confirmed for job {session_id}")
            return False

    async def release_lease(self, session_id: UUID, status: str, error_message: Optional[str] = None,
                            *, attempt_count: int) -> bool:
        """Finalize only an owned historical attempt, with a positive row receipt.

        An absent row, lost lease or unknown response returns False. Never retry
        this mutation or infer rollback; normal durable lease recovery remains.
        """
        if type(attempt_count) is not int or attempt_count < 1:
            return False
        try:
            client = SupabaseClient.get_client()
            now = datetime.now(timezone.utc)
            updates = {'status': status, 'locked_at': None, 'locked_by': None, 'lease_expires_at': None}
            if status in TERMINAL_JOB_STATUSES:
                updates['completed_at'] = now.isoformat()
            if error_message:
                updates['last_error'] = error_message[:5000]
            result = client.table('migration_sessions').update(updates).eq(
                'id', str(session_id)
            ).eq('locked_by', self.worker_id).eq('attempt_count', attempt_count).eq(
                'status', 'processing'
            ).is_('mcp_run_id', 'null').gt('lease_expires_at', now.isoformat()).execute()
            confirmed = self._session_update_acknowledged(result, session_id, attempt_count, updates)
            if confirmed:
                print(f"[Worker] Released lease for job {session_id}, status: {status}")
            else:
                print(f"[Worker] Finalization not confirmed for job {session_id}")
            return confirmed
        except Exception:
            print(f"[Worker] Finalization not confirmed for job {session_id}")
            return False

    def _notification_parent_current(self, job, status):
        """Suppress a late notification when its owned parent is already gone.

        This is a last read before external delivery, not atomic revocation of
        an email already being accepted by a provider.
        """
        try:
            row = self.session_db.get_session(UUID(job['id']))
            return (isinstance(row, dict) and str(row.get('id')) == str(job['id'])
                    and row.get('user_id') == job.get('user_id')
                    and row.get('status') == status
                    and row.get('attempt_count') == job.get('attempt_count', 1))
        except Exception:
            return False

    async def process_job(self, job: Dict[str, Any]) -> bool:
        """
        Process a single job with lease management.

        Args:
            job: Job data from claim_next_job.

        Returns:
            True if successful, False if failed.
        """
        session_id = UUID(job['id'])
        attempt_count = job.get('attempt_count', 1)
        pipeline_type = job.get('pipeline_type', 'content')
        is_preview = bool(job.get('is_preview', False))
        pivot_service = MigrationRunService() if job.get('mcp_run_id') else None
        pivot_migration_id = None
        jev_job = False

        def report_progress(progress_session_id, current_stage, stage_name, total_stages):
            # Never fall back to unfenced progress for malformed/missing claim context.
            return self.session_db.update_session_progress_for_attempt(
                progress_session_id, current_stage, stage_name, total_stages,
                worker_id=self.worker_id, attempt_count=job.get('attempt_count'))

        async def finish(status, error=None):
            if pivot_service is not None:
                result = pivot_service.finalize_session(job, self.worker_id, status, error)
                # Terminal RPC replay reports observed durable state, which may
                # belong to a newer attempt with a different outcome.
                expected = {'completed': 'succeeded', 'pending': 'queued', 'permanently_failed': 'failed'}
                confirmed = isinstance(result, dict) and result.get('status') == expected.get(status)
                if not confirmed:
                    print(f"[Worker] Requested pivot outcome not confirmed for job {session_id}")
                return confirmed
            return await self.release_lease(session_id, status, error, attempt_count=attempt_count)

        print(
            f"[Worker] Processing job {session_id} (attempt {attempt_count}, "
            f"pipeline={pipeline_type}, preview={is_preview})...",
            flush=True
        )

        # Start lease extension task
        lease_extension_task = asyncio.create_task(self._lease_extension_loop(session_id, attempt_count=attempt_count))
        preview_service = DeepPreviewService()
        resource_reservations = ExitStack()

        def mark_preview_failed(error):
            # Call only after an acknowledged transition of this attempt.
            if is_preview:
                try:
                    preview_service.mark_failed(session_id, error)
                except Exception:
                    print(f"[Worker] Failed to persist preview failure status for {session_id}")

        try:
            if pivot_service is not None:
                authorization = pivot_service.authorize_dispatch(job, self.worker_id)
                pivot_migration_id = authorization.get('migration_id')

            if pivot_service is not None:
                from backend.services.jev_pipeline_service import JevService, enabled as jev_enabled
                jev_service = JevService(pivot_service.repository)
                jev_job = jev_service.state(job['mcp_run_id']) is not None
                if jev_job:
                    if not jev_enabled():
                        from src.redirx.jev.jev import ProviderUnavailable
                        raise ProviderUnavailable('jev_paused')
                    from backend.services.jev_runner import JevPipelineRunner
                    from backend.services.jev_database_transport import jev_worker_database
                    with jev_worker_database() as jev_client:
                        await JevPipelineRunner(jev_client).run(job,self.worker_id,report_progress)
                    if not await finish('completed'):
                        return False
                    self.jobs_processed += 1
                    return True

            # Get URLs from job
            old_urls = job.get('old_urls', [])
            new_urls = job.get('new_urls', [])

            if not old_urls or not new_urls:
                print(f"[Worker] Job {session_id} has no URLs")
                if await finish('permanently_failed', 'No URLs provided'):
                    mark_preview_failed('No URLs provided')
                return False

            try:
                validate_content_job_url_counts(old_urls, new_urls, pipeline_type, pivot=pivot_service is not None)
            except ContentJobUrlCapExceeded as cap_error:
                fail_message = cap_error.to_worker_error_message()
                print(f"[Worker] Job {session_id} rejected before processing: {fail_message}")
                if await finish('permanently_failed', fail_message):
                    mark_preview_failed(fail_message)
                return False

            print(f"[Worker] Processing {len(old_urls)} old URLs and {len(new_urls)} new URLs (pipeline: {pipeline_type})")

            if pivot_service is not None:
                resource_reservations.enter_context(reserve_pivot_temporary_capacity(len(old_urls), len(new_urls)))

            # Only validate OpenAI key for content pipeline (url_only doesn't need it)
            if pipeline_type == 'content':
                Config.validate_embeddings()

            if is_preview:
                preview_service.mark_processing(session_id)

            # Run pipeline
            pipeline_options = {}
            if pivot_service is not None:
                pipeline_options = {'preserve_url_identity': True, 'engine_write_context': {
                    'run_id': job['mcp_run_id'], 'worker_id': self.worker_id,
                    'attempt_count': attempt_count,
                }}
            pipeline = Pipeline(input=(old_urls, new_urls), session_id=session_id,
                                pipeline_type=pipeline_type, **pipeline_options)
            total = pipeline.total_stages
            names = pipeline.stage_names

            print(f"[Worker] Pipeline has {total} stages: {names}", flush=True)

            # Report first stage starting (progress fields were reset to NULL when job was claimed)
            print(f"[Worker] Initial progress report: stage 1/{total}, name={names[0]}", flush=True)
            report_progress(session_id, 1, names[0], total)

            final_state = None
            async for step in pipeline.iterate():
                completed = pipeline.current_stage_index
                print(f"[Worker] After iteration: completed={completed}, total={total}", flush=True)
                if completed < total:
                    print(f"[Worker] Reporting progress: stage {completed + 1}/{total}, name={names[completed]}", flush=True)
                    report_progress(session_id, completed + 1, names[completed], total)
                else:
                    print(f"[Worker] Skipping progress report (completed={completed} >= total={total})", flush=True)
                final_state = step

            # Update user usage
            user_id = job.get('user_id')
            mapping_count = 0
            if user_id:
                mapping_db = URLMappingDB()
                mappings = mapping_db.get_mappings_by_session(session_id)
                mapping_count = len(mappings or [])

            # Propose repairs for the rows the matcher flagged, using the
            # rename conventions this session's own confident matches
            # demonstrate. Advisory only — new_url is untouched — so a failure
            # here costs suggestions, never the mappings themselves.
            if not is_preview and pivot_service is None:
                try:
                    repair_outcome = await asyncio.to_thread(
                        MatchRepairService().repair_session, str(session_id)
                    )
                    print(
                        f"[Worker] Match repair for {session_id}: "
                        f"{repair_outcome.as_dict()}",
                        flush=True,
                    )
                except Exception as repair_err:
                    print(
                        f"[Worker] Match repair failed for {session_id} "
                        f"(mappings are unaffected): {repair_err}",
                        flush=True,
                    )

            # Do not publish completion side effects from a deleted/stale job
            # or from an update whose commit could not be confirmed.
            if not await finish('completed'):
                return False
            print(f"[Worker] Job {session_id} completed successfully")
            if pivot_service is None:
                try:
                    self._apply_usage_accounting(
                        user_id=user_id, session_id=session_id, pipeline_type=pipeline_type,
                        is_preview=is_preview, old_urls=old_urls, new_urls=new_urls,
                    )
                except Exception:
                    print(f"[Worker] Post-completion accounting unavailable for job {session_id}")

            # Queue preview job after successful source url_only completion.
            if (not is_preview) and pipeline_type == 'url_only' and user_id:
                try:
                    preview_result = preview_service.maybe_queue_preview(
                        source_session_id=session_id,
                        user_id=user_id,
                        old_urls=old_urls,
                        new_urls=new_urls,
                    )
                    print(f"[Worker] Deep preview queue result for {session_id}: {preview_result}")
                except Exception as preview_err:
                    print(f"[Worker] Failed to queue deep preview for {session_id}: {preview_err}")

            # Finalize preview snapshot after preview content job completes.
            if is_preview:
                try:
                    finalize_result = preview_service.finalize_preview(session_id)
                    print(f"[Worker] Deep preview finalize result for {session_id}: {finalize_result}")
                except Exception as preview_err:
                    print(f"[Worker] Failed to finalize deep preview for {session_id}: {preview_err}")
                    try:
                        preview_service.mark_failed(session_id, str(preview_err))
                    except Exception:
                        print(f"[Worker] Preview failure status unavailable for job {session_id}")

            # Send completion email (fire-and-forget)
            if not is_preview:
                try:
                    from backend.services.email_service import EmailService
                    if user_id:
                        client = SupabaseClient.get_client()
                        profile = client.table('user_profiles').select(
                            'email'
                        ).eq('id', user_id).maybe_single().execute()
                        if profile.data and profile.data.get('email') and self._notification_parent_current(job, 'completed'):
                            project_name = job.get('project_name', 'Migration')
                            old_domain = ""
                            new_domain = ""
                            if old_urls:
                                try:
                                    from urllib.parse import urlparse
                                    old_domain = urlparse(old_urls[0]).netloc
                                except Exception:
                                    pass
                            if new_urls:
                                try:
                                    from urllib.parse import urlparse
                                    new_domain = urlparse(new_urls[0]).netloc
                                except Exception:
                                    pass
                            email_svc = EmailService()
                            email_svc.send_mapping_complete(
                                user_id=user_id,
                                to_email=profile.data['email'],
                                project_name=project_name,
                                total_mappings=mapping_count,
                                session_id=str(session_id),
                                old_site_domain=old_domain,
                                new_site_domain=new_domain,
                                migration_id=pivot_migration_id,
                            )
                except Exception:
                    print(f"[Worker] Completion email failed (non-blocking)")

            self.jobs_processed += 1
            return True

        except Exception as e:
            if jev_job:
                # Provider errors and budget exhaustion stop automatic retries.
                # refine_matches resumes the saved pass explicitly, at no new
                # free allowance. Never include provider payloads in job errors.
                from src.redirx.jev.jev import ProviderUnavailable
                safe = str(e) if isinstance(e,ProviderUnavailable) else 'jev_run_interrupted: saved progress can resume with refine_matches.'
                await finish('permanently_failed',safe)
                return False
            error_msg = f"Error: {str(e)}\n{traceback.format_exc()}"
            print(f"[Worker] Job {session_id} failed: {e}")
            traceback.print_exc()

            # Check if we should retry or permanently fail
            if attempt_count >= WORKER_MAX_ATTEMPTS:
                print(f"[Worker] Job {session_id} exceeded max attempts, marking as permanently failed")
                if pivot_service is not None:
                    # Only terminal failures may release reserved capacity, and
                    # classification uses the original exception, never its text.
                    MigrationSubscriptionService().finalize_worker_failure(job, self.worker_id, e)
                else:
                    if not await finish('permanently_failed', error_msg):
                        return False
                mark_preview_failed(str(e))

                # Send failure email (fire-and-forget)
                if not is_preview:
                    try:
                        from backend.services.email_service import EmailService
                        user_id = job.get('user_id')
                        if user_id:
                            client = SupabaseClient.get_client()
                            profile = client.table('user_profiles').select(
                                'email'
                            ).eq('id', user_id).maybe_single().execute()
                            if profile.data and profile.data.get('email') and self._notification_parent_current(job, 'permanently_failed'):
                                project_name = job.get('project_name', 'Migration')
                                email_svc = EmailService()
                                email_svc.send_mapping_failed(
                                    user_id=user_id,
                                    to_email=profile.data['email'],
                                    project_name=project_name,
                                    error_summary=str(e),
                                    session_id=str(session_id),
                                    migration_id=pivot_migration_id,
                                )
                    except Exception:
                        print(f"[Worker] Failure email failed (non-blocking)")
            else:
                if await finish('pending', error_msg):
                    print(f"[Worker] Job {session_id} queued for retry")
                    mark_preview_failed(str(e))

            return False

        finally:
            resource_reservations.close()
            # Cancel lease extension task
            lease_extension_task.cancel()
            try:
                await lease_extension_task
            except asyncio.CancelledError:
                pass

    async def _lease_extension_loop(self, session_id: UUID, *, attempt_count: int) -> None:
        """Renew before expiry, stopping on a lost or unconfirmed attempt.

        One atomic renewal replaces the old read-then-update heartbeat. The old
        300s poll / 120s threshold reached the 600s default expiry before renewal.
        This loop does not cancel an already running legacy pipeline.
        """
        interval = max(1.0, min(300.0, WORKER_LEASE_DURATION / 3))
        try:
            while True:
                await asyncio.sleep(interval)
                if not await self.extend_lease(session_id, attempt_count=attempt_count):
                    return
        except asyncio.CancelledError:
            pass

    async def reclaim_expired_leases(self) -> int:
        """
        Reclaim jobs with expired leases.

        Returns:
            Number of jobs reclaimed.
        """
        try:
            client = SupabaseClient.get_client()

            result = client.rpc('reclaim_expired_leases', {
                'p_max_attempts': WORKER_MAX_ATTEMPTS
            }).execute()

            count = result.data[0]['reclaimed_count'] if result.data else 0

            if count > 0:
                print(f"[Worker] Reclaimed {count} expired lease(s)")

            return count

        except Exception as e:
            print(f"[Worker] Error reclaiming expired leases: {e}")
            return 0

    # ------------------------------------------------------------------
    # Post-cutover watch sweeps
    # ------------------------------------------------------------------

    async def _run_watch_sweep(self, watch: Dict[str, Any]) -> None:
        """
        Probe one watch's URLs and release its lease. Never raises.

        A sweep failing is a monitoring outage, not a job failure: there is no
        user waiting on a result and nothing to mark failed, so the error is
        recorded on the watch and the next interval tries again.
        """
        from backend.services.watch_service import WatchService

        watch_id = watch['id']
        service = WatchService()
        try:
            outcome = await service.run_check(watch)
            self.watches_swept += 1
            print(
                f"[Worker] Watch {watch_id}: checked {outcome.urls_checked}, "
                f"{outcome.issues_open} open ({outcome.issues_new} new, "
                f"{outcome.issues_resolved} resolved), "
                f"{outcome.clicks_at_risk} clicks at risk, "
                f"{outcome.alerted} alerted",
                flush=True,
            )
            await asyncio.to_thread(service.release_watch, watch_id, None)
        except Exception as e:
            print(f"[Worker] Watch {watch_id} sweep failed: {e}", flush=True)
            traceback.print_exc()
            try:
                await asyncio.to_thread(service.release_watch, watch_id, str(e))
            except Exception as release_error:
                # The lease expiry in claim_next_watch is the backstop; without
                # it a watch orphaned here would never be picked up again.
                print(
                    f"[Worker] Could not release watch {watch_id}, "
                    f"leaving it to lease expiry: {release_error}",
                    flush=True,
                )

    async def _watch_tick(self) -> None:
        """Claim and start one due watch, if it is time to look and none is running."""
        if not WATCH_ENABLED:
            return

        if self.watch_task is not None:
            if not self.watch_task.done():
                return
            # Surface anything the task itself failed to swallow.
            try:
                self.watch_task.result()
            except Exception as e:
                print(f"[Worker] Watch task ended abnormally: {e}", flush=True)
            self.watch_task = None

        now = time.time()
        if now - self.last_watch_poll < WATCH_POLL_INTERVAL:
            return
        self.last_watch_poll = now

        from backend.services.watch_service import WatchService

        try:
            # Synchronous supabase-py client, so keep it off the event loop.
            watch = await asyncio.to_thread(
                WatchService().claim_next_watch, self.worker_id, WATCH_LEASE_DURATION
            )
        except Exception as e:
            print(f"[Worker] Could not claim a watch: {e}", flush=True)
            return

        if not watch:
            return

        print(
            f"[Worker] Claimed watch {watch['id']} for {watch.get('old_domain')}",
            flush=True,
        )
        self.watch_task = asyncio.create_task(self._run_watch_sweep(watch))

    async def _await_watch_task(self) -> None:
        """On shutdown, let an in-progress sweep finish releasing its lease."""
        if self.watch_task is not None and not self.watch_task.done():
            print("[Worker] Waiting for in-progress watch sweep...", flush=True)
            try:
                await self.watch_task
            except Exception:
                pass

    def _has_available_capacity(self) -> bool:
        """Return True when this worker can accept another in-flight job."""
        return len(self.in_flight_tasks) < self.max_concurrent

    def _start_in_flight_job(self, job: Dict[str, Any], source: str) -> None:
        """Start processing a claimed job as an in-flight task."""
        task = asyncio.create_task(self.process_job(job))
        self.in_flight_tasks.add(task)

        print(
            f"[Worker] Dispatched job {job.get('id')} via {source} "
            f"({len(self.in_flight_tasks)}/{self.max_concurrent} in-flight)",
            flush=True,
        )

    async def _reap_finished_jobs(
        self,
        wait_for_one: bool = False,
        timeout: Optional[float] = None,
    ) -> int:
        """
        Remove completed in-flight tasks and surface any unexpected exceptions.
        """
        if not self.in_flight_tasks:
            return 0

        if wait_for_one:
            done, _ = await asyncio.wait(
                self.in_flight_tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            done = {task for task in self.in_flight_tasks if task.done()}

        if not done:
            return 0

        for task in done:
            self.in_flight_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[Worker] In-flight job task raised unexpectedly: {e}", flush=True)

        return len(done)

    async def _dispatch_until_capacity(self, source: str) -> int:
        """
        Claim and dispatch jobs until the worker reaches max in-flight capacity.
        """
        claimed = 0
        while self.running and self._has_available_capacity():
            job = await self.claim_job()
            if not job:
                break
            self._start_in_flight_job(job, source)
            claimed += 1
        return claimed

    async def _wait_for_in_flight_jobs(self) -> None:
        """Drain in-flight jobs before shutdown."""
        while self.in_flight_tasks:
            await self._reap_finished_jobs(wait_for_one=True)

    async def polling_loop(self) -> None:
        """
        Fallback polling loop (when LISTEN/NOTIFY not available).
        """
        print("[Worker] Starting polling loop...")
        print(f"[Worker] Poll interval: {WORKER_FALLBACK_INTERVAL} seconds")

        self.running = True
        next_reclaim_at = time.monotonic()
        reclaim_interval = max(1.0, float(WORKER_FALLBACK_INTERVAL))

        try:
            while self.running:
                await self._reap_finished_jobs(wait_for_one=False)
                if time.monotonic() >= next_reclaim_at:
                    # Polling is also the recovery path when LISTEN is unavailable.
                    # Pending-only claims cannot see abandoned processing leases.
                    # An uncertain reclaim is not retried here; wait a full tick.
                    await self.reclaim_expired_leases()
                    next_reclaim_at = time.monotonic() + reclaim_interval
                await self._watch_tick()
                claimed = await self._dispatch_until_capacity("polling loop")

                if claimed > 0:
                    continue

                if self.in_flight_tasks:
                    await self._reap_finished_jobs(wait_for_one=True, timeout=1.0)
                else:
                    # No jobs, wait before polling again
                    print("[Worker] No pending jobs. Waiting...", end='\r')
                    await asyncio.sleep(WORKER_FALLBACK_INTERVAL)

        except KeyboardInterrupt:
            self.running = False
            print("\n[Worker] Shutting down...")
        finally:
            await self._wait_for_in_flight_jobs()
            await self._await_watch_task()

    async def listen_loop(self) -> None:
        """
        Main LISTEN/NOTIFY loop for push-based job processing.
        """
        print("[Worker] Starting LISTEN loop...", flush=True)

        self.pg_conn = self.connect_postgres()
        self.pg_claim_conn = self.connect_postgres()

        # If connection failed, fall back to polling
        if not self.pg_conn:
            print("[Worker] PostgreSQL connection unavailable, using polling mode", flush=True)
            await self.polling_loop()
            return

        try:
            # Subscribe to job queue events
            with self.pg_conn.cursor() as cursor:
                cursor.execute("LISTEN job_queue_events")
            self.pg_conn.commit()
            print("[Worker] Subscribed to job_queue_events channel", flush=True)

            self.running = True
            last_fallback_poll = time.time()

            # Set connection to non-blocking mode for notifications
            self.pg_conn.autocommit = True

            while self.running:
                try:
                    await self._reap_finished_jobs(wait_for_one=False)

                    # Monitoring runs alongside jobs rather than competing for
                    # a slot: a sweep is nearly all waiting on the host rate
                    # limiter, so it does not consume the concurrency that
                    # paid migration work needs.
                    await self._watch_tick()

                    # Opportunistically fill all available capacity.
                    claimed = await self._dispatch_until_capacity("LISTEN dispatch")

                    # Fallback polling (in case we missed notifications or timeout occurred)
                    now = time.time()
                    if now - last_fallback_poll >= WORKER_FALLBACK_INTERVAL:
                        print("[Worker] Fallback poll check...", flush=True)
                        last_fallback_poll = now

                        # Reclaim expired leases
                        await self.reclaim_expired_leases()

                        # Try to claim jobs (in case notifications were missed).
                        claimed += await self._dispatch_until_capacity("fallback poll")

                    if claimed > 0:
                        continue

                    if self.in_flight_tasks:
                        # Keep the dispatch loop responsive while work is in progress.
                        await self._reap_finished_jobs(wait_for_one=True, timeout=1.0)
                        continue

                    # No in-flight work and nothing was claimable; block on
                    # LISTEN in a thread so the event loop stays free.
                    channel = await asyncio.to_thread(
                        self._wait_for_notification, self.pg_conn, WORKER_FALLBACK_INTERVAL
                    )
                    if channel:
                        print(f"[Worker] Received LISTEN notification: {channel}", flush=True)
                    else:
                        print("[Worker] No pending jobs", end='\r', flush=True)

                except TRANSIENT_DB_ERRORS as e:
                    # The connection went away. Rebuild it and carry on; the
                    # in-flight jobs this worker already owns are unaffected,
                    # and their leases keep being extended throughout.
                    print(f"[Worker] PostgreSQL connection lost: {e}", flush=True)
                    if not await self._recover_connections():
                        print(
                            "[Worker] Degrading to polling mode until the "
                            "database is reachable again",
                            flush=True,
                        )
                        await self.polling_loop()
                        return
                    last_fallback_poll = time.time()

        except KeyboardInterrupt:
            self.running = False
            print("\n[Worker] Shutting down...", flush=True)
        finally:
            await self._wait_for_in_flight_jobs()
            await self._await_watch_task()
            if self.pg_conn:
                self.pg_conn.close()
                print("[Worker] PostgreSQL connection closed", flush=True)
            if self.pg_claim_conn:
                self.pg_claim_conn.close()
                print("[Worker] PostgreSQL claim connection closed", flush=True)

    async def run(self) -> None:
        """
        Main entry point for the worker.
        """
        # Check if DATABASE_URL is available
        database_url = os.getenv('DATABASE_URL')
        mode = "Push-Based (LISTEN/NOTIFY)" if database_url else "Polling"

        print("=" * 60, flush=True)
        print(f"Redirx Background Worker ({mode})", flush=True)
        print("=" * 60, flush=True)
        print(f"Worker ID: {self.worker_id}", flush=True)
        print(f"Lease duration: {WORKER_LEASE_DURATION}s", flush=True)
        print(f"Max concurrent: {self.max_concurrent}", flush=True)
        print(f"Max attempts: {WORKER_MAX_ATTEMPTS}", flush=True)
        print(f"Poll interval: {WORKER_FALLBACK_INTERVAL}s", flush=True)
        if not database_url:
            print("⚠️  DATABASE_URL not set - using polling mode", flush=True)
            print("   Set DATABASE_URL for push-based notifications", flush=True)
        print("Press Ctrl+C to stop", flush=True)
        print("=" * 60, flush=True)

        pivot_runner = PivotBackgroundRunner()
        try:
            # Validate configuration
            Config.validate()
            # Note: OpenAI key is validated per-job in process_job for content pipelines only
            pivot_runner.start()

            # Start LISTEN loop (will fallback to polling if needed)
            await self.listen_loop()

        except TRANSIENT_DB_ERRORS as e:
            # Reaching here means recovery inside the loop was exhausted. Say
            # what it actually was rather than calling a dropped socket fatal.
            print(f"[Worker] Database unreachable, exiting for restart: {e}", flush=True)
            traceback.print_exc()
        except Exception as e:
            print(f"[Worker] Fatal error: {e}")
            traceback.print_exc()
        finally:
            await pivot_runner.stop()
            # This loop owns its own limiter pool, distinct from the one the
            # pivot runner's thread owns; each closes the one it opened.
            await close_limiter_pools()
            print(f"[Worker] Total jobs processed: {self.jobs_processed}")


def main():
    """Main entry point."""
    # Force unbuffered output for real-time logging visibility
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    worker = RedirxWorker()
    asyncio.run(worker.run())


if __name__ == '__main__':
    main()
