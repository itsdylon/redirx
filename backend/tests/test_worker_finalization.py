"""Offline stale/deleted legacy jobs cannot publish unconfirmed completion."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
from backend.worker import RedirxWorker, WORKER_MAX_ATTEMPTS

SESSION = '11111111-1111-4111-8111-111111111111'


class Client:
    def __init__(self, session, response=None):
        self.session, self.response, self.calls = session, response, []
    def table(self, table): return Query(self, table)


class Query:
    def __init__(self, client, table):
        self.client, self.table, self.filters, self.values = client, table, [], None
    def select(self, *_): return self
    def maybe_single(self): return self
    def update(self, values): self.values = values; return self
    def eq(self, key, value): self.filters.append(('eq', key, value)); return self
    def gt(self, key, value): self.filters.append(('gt', key, value)); return self
    def is_(self, key, value): self.filters.append(('is', key, value)); return self
    def execute(self):
        self.client.calls.append((self.table, self.filters, self.values))
        if self.table == 'user_profiles': return SimpleNamespace(data={'email': 'fixture@example.invalid'})
        row = self.client.session
        def matches(op, key, value):
            if op == 'is': return row.get(key) is None and value == 'null'
            if op == 'eq': return row.get(key) == value
            return row.get(key) is not None and row[key] > value
        if row is None or not all(matches(*f) for f in self.filters):
            return SimpleNamespace(data=[], error=None)
        if self.values: row.update(self.values)
        if self.client.response: return self.client.response(dict(row))
        return SimpleNamespace(data=[dict(row)], error=None)


class WorkerFinalizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = RedirxWorker.__new__(RedirxWorker)
        self.worker.worker_id = 'worker-a'
        self.worker.jobs_processed = 0
        self.worker.session_db = Mock()
        self.worker._lease_extension_loop = AsyncMock()
        self.worker._apply_usage_accounting = Mock()

    def session(self, **values):
        return {'id': SESSION, 'user_id': 'fixture-owner', 'status': 'processing',
                'locked_by': 'worker-a', 'attempt_count': 1, 'mcp_run_id': None,
                'lease_expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), **values}

    async def test_noncurrent_or_pivot_attempts_cannot_be_finalized(self):
        for change in ({'locked_by': 'worker-b'}, {'attempt_count': 2}, {'status': 'completed'},
                       {'mcp_run_id': 'pivot-run'}, {'lease_expires_at': '2000-01-01T00:00:00+00:00'}):
            with self.subTest(change=change):
                client = Client(self.session(**change))
                original = dict(client.session)
                with patch('backend.worker.SupabaseClient') as factory:
                    factory.get_client.return_value = client
                    self.assertFalse(await self.worker.release_lease(UUID(SESSION), 'completed', attempt_count=1))
                self.assertEqual(client.session, original)

    async def test_lease_extension_requires_live_matching_attempt_and_receipt(self):
        for row in (None, self.session(attempt_count=2), self.session(locked_by='worker-b'),
                    self.session(status='pending'), self.session(lease_expires_at='2000-01-01T00:00:00+00:00')):
            with self.subTest(row=row), patch('backend.worker.SupabaseClient') as factory:
                factory.get_client.return_value = Client(row)
                self.assertFalse(await self.worker.extend_lease(UUID(SESSION), attempt_count=1))
        client = Client(self.session())
        with patch('backend.worker.SupabaseClient') as factory:
            factory.get_client.return_value = client
            self.assertTrue(await self.worker.extend_lease(UUID(SESSION), attempt_count=1))
        self.assertGreater(datetime.fromisoformat(client.session['lease_expires_at']), datetime.now(timezone.utc))

    async def run_job(self, client, *, fail=False, deleted_after_commit=False, is_preview=False,
                      preview_error=False, missing_urls=False, attempt_count=None):
        class Pipeline:
            total_stages = 1
            current_stage_index = 1
            stage_names = ['Fixture']
            async def iterate(self):
                if fail: raise RuntimeError('fixture pipeline failure')
                yield {'done': True}
        job = {'id': SESSION, 'user_id': 'fixture-owner', 'old_urls': ['https://old.example/a'],
               'new_urls': ['https://new.example/a'], 'attempt_count': WORKER_MAX_ATTEMPTS if fail else 1,
               'pipeline_type': 'content' if is_preview else 'url_only', 'is_preview': is_preview}
        if attempt_count is not None: job['attempt_count'] = attempt_count
        if missing_urls: job['old_urls'] = []
        self.worker.session_db.get_session.side_effect = lambda _: None if deleted_after_commit else client.session
        with ExitStack() as stack:
            stack.enter_context(patch('backend.worker.SupabaseClient')).get_client.return_value = client
            stack.enter_context(patch('backend.worker.Pipeline', return_value=Pipeline()))
            stack.enter_context(patch('backend.worker.Config.validate_embeddings'))
            stack.enter_context(patch('backend.worker.URLMappingDB')).return_value.get_mappings_by_session.return_value = []
            stack.enter_context(patch('backend.worker.MatchRepairService')).return_value.repair_session.return_value = SimpleNamespace(as_dict=lambda: {})
            preview = stack.enter_context(patch('backend.worker.DeepPreviewService')).return_value
            def finalize_preview(*_):
                self.assertEqual(client.session['status'], 'completed')
                if preview_error: raise RuntimeError('fixture preview failure')
                return {'status': 'completed'}
            preview.finalize_preview.side_effect = finalize_preview
            preview.mark_failed.side_effect = lambda *_: self.assertNotEqual(client.session['status'], 'processing')
            email = stack.enter_context(patch('backend.services.email_service.EmailService'))
            completed = await self.worker.process_job(job)
        return completed, email, preview

    async def test_unconfirmed_completion_has_no_accounting_metrics_preview_or_email(self):
        def lost_response(row): raise TimeoutError('commit may have succeeded')
        cases = [Client(None), Client(self.session(attempt_count=2)),
                 Client(self.session(), lambda row: SimpleNamespace(data=[], error=None)),
                 Client(self.session(), lambda row: SimpleNamespace(data=[{**row, 'id': 'wrong'}], error=None)),
                 Client(self.session(), lambda row: SimpleNamespace(data=[{**row, 'status': 'processing'}], error=None)),
                 Client(self.session(), lambda row: SimpleNamespace(data=[row], error='unknown')),
                 Client(self.session(), lost_response)]
        for client in cases:
            with self.subTest(response=client.response):
                completed, email, preview = await self.run_job(client)
                self.assertFalse(completed)
                self.assertEqual(self.worker.jobs_processed, 0)
                email.assert_not_called()
                preview.maybe_queue_preview.assert_not_called()
                self.worker._apply_usage_accounting.assert_not_called()
                self.assertEqual(sum(values is not None for _, _, values in client.calls), 1)
        # The failed response is not evidence of rollback: this fixture committed.
        self.assertEqual(cases[-1].session['status'], 'completed')

    async def test_confirmed_completion_accounts_then_notifies_once(self):
        client = Client(self.session())
        self.worker._apply_usage_accounting.side_effect = lambda **_: self.assertEqual(client.session['status'], 'completed')
        completed, email, preview = await self.run_job(client)
        self.assertTrue(completed)
        self.assertEqual(self.worker.jobs_processed, 1)
        self.worker._apply_usage_accounting.assert_called_once()
        preview.maybe_queue_preview.assert_called_once()
        email.return_value.send_mapping_complete.assert_called_once()
        self.assertIsNone(client.session['locked_by'])

    async def test_parent_removed_after_completion_suppresses_notification(self):
        completed, email, _ = await self.run_job(Client(self.session()), deleted_after_commit=True)
        self.assertTrue(completed)
        email.assert_not_called()

    async def test_accounting_error_does_not_requeue_confirmed_completion(self):
        client = Client(self.session())
        self.worker._apply_usage_accounting.side_effect = RuntimeError('fixture unavailable')
        completed, email, _ = await self.run_job(client)
        self.assertTrue(completed)
        self.assertEqual(client.session['status'], 'completed')
        self.assertEqual(sum(values is not None for _, _, values in client.calls), 1)
        email.return_value.send_mapping_complete.assert_called_once()

    async def test_unconfirmed_failure_does_not_send_failure_email(self):
        client = Client(self.session(attempt_count=WORKER_MAX_ATTEMPTS), lambda row: SimpleNamespace(data=[], error=None))
        completed, email, _ = await self.run_job(client, fail=True)
        self.assertFalse(completed)
        email.assert_not_called()
        self.worker._apply_usage_accounting.assert_not_called()
        self.assertEqual(self.worker.jobs_processed, 0)

    async def test_confirmed_failure_sends_one_failure_email(self):
        completed, email, _ = await self.run_job(Client(self.session(attempt_count=WORKER_MAX_ATTEMPTS)), fail=True)
        self.assertFalse(completed)
        email.return_value.send_mapping_failed.assert_called_once()
        self.worker._apply_usage_accounting.assert_not_called()

    def test_receipt_requires_exact_attempt_and_all_fields_but_accepts_timestamp_rendering(self):
        expected = {'status': 'completed', 'completed_at': '2026-10-16T12:00:00+00:00'}
        row = {'id': SESSION, 'attempt_count': 1, 'status': 'completed', 'completed_at': '2026-10-16T12:00:00Z'}
        ack = lambda data: self.worker._session_update_acknowledged(SimpleNamespace(data=data, error=None), SESSION, 1, expected)
        self.assertTrue(ack([row]))
        for data in ([], [row, row], [{**row, 'attempt_count': True}], [{**row, 'completed_at': None}], [{k: v for k, v in row.items() if k != 'status'}]):
            self.assertFalse(ack(data))

    async def test_historical_preview_finishes_before_snapshot_and_stays_nonbillable(self):
        for preview_error in (False, True):
            with self.subTest(preview_error=preview_error):
                self.worker.jobs_processed = 0
                client = Client(self.session())
                completed, email, preview = await self.run_job(client, is_preview=True, preview_error=preview_error)
                self.assertTrue(completed)
                self.assertEqual(client.session['status'], 'completed')
                self.assertEqual(self.worker.jobs_processed, 1)
                preview.finalize_preview.assert_called_once()
                self.assertEqual(preview.mark_failed.call_count, int(preview_error))
                preview.maybe_queue_preview.assert_not_called()
                email.assert_not_called()
                self.assertTrue(self.worker._apply_usage_accounting.call_args.kwargs['is_preview'])

    async def test_unconfirmed_preview_never_publishes_snapshot_or_failure_status(self):
        for fail, missing, attempt in ((False, False, 1), (True, False, 1),
                                      (True, False, WORKER_MAX_ATTEMPTS), (False, True, 1)):
            with self.subTest(fail=fail, missing=missing, attempt=attempt):
                client = Client(self.session(attempt_count=attempt), lambda row: SimpleNamespace(data=[], error=None))
                completed, email, preview = await self.run_job(client, is_preview=True, fail=fail,
                    missing_urls=missing, attempt_count=attempt)
                self.assertFalse(completed)
                preview.finalize_preview.assert_not_called()
                preview.mark_failed.assert_not_called()
                email.assert_not_called()
                self.worker._apply_usage_accounting.assert_not_called()

    async def test_confirmed_preview_failure_marks_snapshot_after_job_transition(self):
        for attempt, expected in ((1, 'pending'), (WORKER_MAX_ATTEMPTS, 'permanently_failed')):
            with self.subTest(attempt=attempt):
                client = Client(self.session(attempt_count=attempt))
                completed, email, preview = await self.run_job(client, is_preview=True, fail=True, attempt_count=attempt)
                self.assertFalse(completed)
                self.assertEqual(client.session['status'], expected)
                preview.mark_failed.assert_called_once()
                email.assert_not_called()

    async def test_heartbeat_keeps_exact_attempt_live_across_multiple_lease_durations(self):
        # Advance a virtual clock; no real waiting or database/provider calls.
        for duration in (30, 600):
            with self.subTest(duration=duration):
                now = datetime.now(timezone.utc)
                class Clock:
                    @staticmethod
                    def now(_): return now
                    fromisoformat = datetime.fromisoformat
                client = Client(self.session(lease_expires_at=(now + timedelta(seconds=duration)).isoformat()))
                intervals = []
                async def sleep(seconds):
                    nonlocal now
                    intervals.append(seconds)
                    if len(intervals) == 8: raise asyncio.CancelledError()
                    now += timedelta(seconds=seconds)
                    self.assertGreater(datetime.fromisoformat(client.session['lease_expires_at']), now)
                with patch('backend.worker.WORKER_LEASE_DURATION', duration), \
                        patch('backend.worker.datetime', Clock), patch('backend.worker.asyncio.sleep', side_effect=sleep), \
                        patch('backend.worker.SupabaseClient.get_client', return_value=client):
                    await RedirxWorker._lease_extension_loop(self.worker, UUID(SESSION), attempt_count=1)
                self.assertEqual(intervals, [duration / 3] * 8)
                self.assertEqual(len(client.calls), 7)
                self.assertGreater(datetime.fromisoformat(client.session['lease_expires_at']), now)

    async def test_heartbeat_stops_after_unconfirmed_renewal_without_retry(self):
        self.worker.extend_lease = AsyncMock(return_value=False)
        with patch('backend.worker.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await RedirxWorker._lease_extension_loop(self.worker, UUID(SESSION), attempt_count=1)
        sleep.assert_awaited_once()
        self.worker.extend_lease.assert_awaited_once_with(UUID(SESSION), attempt_count=1)
