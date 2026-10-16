import os
import sys
import unittest
import asyncio
from unittest.mock import AsyncMock, patch


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.worker import RedirxWorker


class WorkerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = RedirxWorker.__new__(RedirxWorker)
        self.worker.running = True
        self.worker.max_concurrent = 2
        self.worker.in_flight_tasks = set()
        self.worker.reclaim_expired_leases = AsyncMock(return_value=0)
        self.worker._watch_tick = AsyncMock()

    async def test_dispatch_until_capacity_respects_limit(self):
        queued_jobs = [
            {"id": "job-1"},
            {"id": "job-2"},
            {"id": "job-3"},
        ]

        async def claim_job():
            if queued_jobs:
                return queued_jobs.pop(0)
            return None

        active = 0
        max_active = 0
        lock = asyncio.Lock()
        release = asyncio.Event()

        async def process_job(_job):
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await release.wait()
            async with lock:
                active -= 1
            return True

        self.worker.claim_job = claim_job
        self.worker.process_job = process_job

        claimed = await self.worker._dispatch_until_capacity("unit-test")
        await asyncio.sleep(0)  # Let dispatched tasks start

        self.assertEqual(claimed, 2)
        self.assertEqual(len(self.worker.in_flight_tasks), 2)
        self.assertLessEqual(max_active, 2)

        release.set()
        await self.worker._wait_for_in_flight_jobs()
        self.assertEqual(len(self.worker.in_flight_tasks), 0)

    async def test_polling_loop_processes_up_to_limit_in_parallel(self):
        self.worker.max_concurrent = 3
        self.worker.running = False

        queued_jobs = [{"id": f"job-{i}"} for i in range(9)]
        processed = 0
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def claim_job():
            if queued_jobs:
                return queued_jobs.pop(0)
            if not self.worker.in_flight_tasks:
                self.worker.running = False
            return None

        async def process_job(_job):
            nonlocal processed, active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
                processed += 1
            return True

        self.worker.claim_job = claim_job
        self.worker.process_job = process_job

        with patch("backend.worker.WORKER_FALLBACK_INTERVAL", 0):
            await self.worker.polling_loop()

        self.assertEqual(processed, 9)
        self.assertLessEqual(max_active, 3)
        self.assertGreaterEqual(max_active, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
