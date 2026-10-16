"""
Job timing and queue priority.

Deep Match wall-clock is the cost driver that decides the free-tier page cap
(embeddings are ~$0.005 for a 250-page job — noise), but nothing recorded how
long a job took. These cover the two ends of that measurement and the ordering
that keeps free work from starving paid work.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.worker import TERMINAL_JOB_STATUSES, RedirxWorker


def run(coro):
    return asyncio.run(coro)


class _Recorder:
    """Captures the payload passed to .update()."""

    def __init__(self):
        self.payload = None

    def table(self, _name):
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *_a):
        return self

    def gt(self, *_a):
        return self

    def is_(self, *_a):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class TestCompletionStamping(unittest.TestCase):
    def setUp(self):
        self.worker = RedirxWorker.__new__(RedirxWorker)
        self.worker.worker_id = "w1"
        self.session_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def _release(self, status):
        recorder = _Recorder()
        with patch("backend.worker.SupabaseClient") as client_cls:
            client_cls.get_client.return_value = recorder
            run(self.worker.release_lease(self.session_id, status, attempt_count=1))
        return recorder.payload

    def test_completion_is_stamped(self):
        payload = self._release("completed")
        self.assertIn("completed_at", payload)
        self.assertIsNotNone(payload["completed_at"])

    def test_permanent_failure_is_stamped(self):
        # A job that died still consumed worker time, which is what we are
        # measuring — excluding failures would bias the duration data.
        payload = self._release("permanently_failed")
        self.assertIsNotNone(payload["completed_at"])

    def test_retry_is_not_stamped(self):
        # Releasing back to 'pending' is a retry, not an ending. Stamping it
        # would report a duration for a run that never finished.
        payload = self._release("pending")
        self.assertNotIn("completed_at", payload)

    def test_terminal_statuses_exclude_pending(self):
        self.assertNotIn("pending", TERMINAL_JOB_STATUSES)
        self.assertIn("completed", TERMINAL_JOB_STATUSES)
        self.assertIn("permanently_failed", TERMINAL_JOB_STATUSES)


class TestFallbackClaimTiming(unittest.TestCase):
    """
    The RPC stamps started_at itself. The REST fallback bypasses the RPC, so it
    has to do the same or jobs claimed that way would have no duration at all.
    """

    def setUp(self):
        self.worker = RedirxWorker.__new__(RedirxWorker)
        self.worker.worker_id = "w1"
        self.worker.pg_claim_conn = None

    def _claim(self):
        candidate = {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "user_id": "u1",
            "project_name": "Example",
            "old_urls": [],
            "new_urls": [],
            "attempt_count": 0,
            "pipeline_type": "content",
            "is_preview": False,
            "source_session_id": None,
        }

        select_query = Mock()
        select_query.select.return_value = select_query
        select_query.eq.return_value = select_query
        select_query.order.return_value = select_query
        select_query.limit.return_value = select_query
        select_query.execute.return_value = SimpleNamespace(data=[candidate])

        update_query = Mock()
        update_query.update.return_value = update_query
        update_query.eq.return_value = update_query
        update_query.execute.return_value = SimpleNamespace(data=[{"id": candidate["id"]}])

        client = Mock()
        client.table.side_effect = [select_query, update_query]

        self.worker._claim_job_fallback(client, "2026-02-24T12:00:00+00:00")
        return select_query, update_query

    def test_started_at_is_set(self):
        _, update_query = self._claim()
        payload = update_query.update.call_args[0][0]
        self.assertIsNotNone(payload.get("started_at"))

    def test_previous_completion_is_cleared_on_retry(self):
        # Otherwise a retried job keeps the old completed_at and reports a
        # negative duration once started_at moves forward.
        _, update_query = self._claim()
        payload = update_query.update.call_args[0][0]
        self.assertIsNone(payload.get("completed_at"))
        self.assertIn("completed_at", payload)

    def test_priority_is_ordered_before_created_at(self):
        # Mirrors claim_next_job. Without this the fallback path would ignore
        # priority entirely and hand a free job to a worker ahead of a paid one.
        select_query, _ = self._claim()
        ordered = [c.args[0] for c in select_query.order.call_args_list]
        self.assertEqual(ordered[:2], ["priority", "created_at"])


if __name__ == "__main__":
    unittest.main()
