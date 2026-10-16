"""Polling-only workers must recover abandoned leases without a LISTEN socket."""
import unittest
from unittest.mock import AsyncMock, patch

from backend.worker import RedirxWorker


class WorkerPollingReclaim(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        worker = RedirxWorker.__new__(RedirxWorker)
        worker.running = False
        worker.max_concurrent = 1
        worker.in_flight_tasks = set()
        worker._watch_tick = AsyncMock()
        worker._await_watch_task = AsyncMock()
        return worker

    async def test_polling_recovers_abandoned_processing_job_before_claim(self):
        worker = self.worker()
        state = {'status': 'processing'}
        events = []
        async def reclaim():
            events.append('reclaim')
            state['status'] = 'pending'
            return 1
        async def claim():
            events.append('claim')
            if state['status'] == 'pending':
                state['status'] = 'processing'
                return {'id': 'recovered-job'}
            return None
        async def process(job):
            events.append('process')
            state['status'] = 'completed'
            worker.running = False
            return True
        sleeps = 0
        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                worker.running = False  # Bounds the before-fix reproduction.
        worker.claim_job = claim
        worker.process_job = AsyncMock(side_effect=process)
        worker.reclaim_expired_leases = AsyncMock(side_effect=reclaim)
        with patch('backend.worker.asyncio.sleep', side_effect=sleep):
            await worker.polling_loop()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(events[:2], ['reclaim', 'claim'])
        worker.process_job.assert_awaited_once_with({'id': 'recovered-job'})
        worker.reclaim_expired_leases.assert_awaited_once()

    async def test_busy_or_unconfirmed_reclaim_waits_a_full_interval(self):
        # Exercise the busy-queue path which continues without an idle sleep.
        worker = self.worker()
        clock = [0.0]
        calls = []
        worker._reap_finished_jobs = AsyncMock()
        worker._wait_for_in_flight_jobs = AsyncMock()
        async def reclaim():
            calls.append(clock[0])
            return 0  # Existing RPC boundary also returns zero for unknown responses.
        async def dispatch(_):
            clock[0] += 1
            if clock[0] == 26:
                worker.running = False
            return 1
        worker.reclaim_expired_leases = AsyncMock(side_effect=reclaim)
        worker._dispatch_until_capacity = AsyncMock(side_effect=dispatch)
        with patch('backend.worker.time.monotonic', side_effect=lambda: clock[0]), \
                patch('backend.worker.WORKER_FALLBACK_INTERVAL', 10):
            await worker.polling_loop()
        self.assertEqual(calls, [0.0, 10.0, 20.0])
        worker._wait_for_in_flight_jobs.assert_awaited_once()
        worker._await_watch_task.assert_awaited_once()

    async def test_shutdown_during_claim_does_not_trigger_another_reclaim(self):
        worker = self.worker()
        worker.reclaim_expired_leases = AsyncMock(return_value=0)
        async def claim():
            worker.running = False
            return None
        worker.claim_job = claim
        with patch('backend.worker.asyncio.sleep', new_callable=AsyncMock):
            await worker.polling_loop()
        worker.reclaim_expired_leases.assert_awaited_once()
        self.assertEqual(worker.in_flight_tasks, set())
        worker._await_watch_task.assert_awaited_once()
