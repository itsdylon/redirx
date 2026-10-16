"""Job wiring and context isolation; no external HTTP or database access."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.services import migration_verification_service as verification
from backend.services.migration_monitoring_service import run_monitoring_batch
from src.redirx import redirect_probe, rate_limit


class VerificationPolitenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_probe_uses_watch_budget_for_each_request_and_restores_context(self):
        for monitoring in (False, True):
            with self.subTest(monitoring=monitoring):
                limiter = SimpleNamespace(acquire=AsyncMock(), record_success=AsyncMock(),
                    note_response=Mock(return_value=False), record_failure=AsyncMock())
                caller = object()
                token = rate_limit.set_limiter(caller)
                recorded = []
                item = {'source_url': 'https://old.example/a', 'expected_url': 'https://new.example/b',
                        'ordinal': 0, 'attempt': 1}
                service = SimpleNamespace(claim=lambda *_: {'verification_id': 'fixture', 'items': [item]},
                    record=lambda *args: recorded.append(args) or True, schedule_due=lambda: 1)

                async def request(_session, method, url):
                    self.assertIs(rate_limit.get_limiter(), limiter)
                    self.assertEqual(method, 'HEAD')
                    return SimpleNamespace(status=301 if 'old.example' in url else 200,
                        headers={'Location': item['expected_url']} if 'old.example' in url else {}, release=lambda: None)

                try:
                    with patch.object(verification, 'HostRateLimiter', return_value=limiter) as factory, \
                         patch.object(redirect_probe, '_request', side_effect=request):
                        run = run_monitoring_batch if monitoring else verification.run_verification_batch
                        result = await run(service, 'worker', 1)
                    factory.assert_called_once_with(namespace=rate_limit.WATCH_NAMESPACE,
                        capacity=rate_limit.WATCH_CAPACITY, rate=rate_limit.WATCH_RATE)
                    self.assertEqual([call.args[0] for call in limiter.acquire.await_args_list],
                        [item['source_url'], item['expected_url']])
                    self.assertEqual(limiter.record_success.await_count, 2)
                    self.assertEqual(result['recorded'], 1)
                    self.assertEqual(recorded[0][3], 'passed')
                    self.assertIs(rate_limit.get_limiter(), caller)
                finally:
                    rate_limit.reset_limiter(token)

    async def test_context_restores_on_claim_failure_and_cancellation(self):
        for error in (RuntimeError('fixture claim failure'), asyncio.CancelledError()):
            caller = object()
            token = rate_limit.set_limiter(caller)
            limiter = object()
            async def fail(*_args):
                self.assertIs(rate_limit.get_limiter(), limiter)
                raise error
            try:
                with patch.object(verification, 'HostRateLimiter', return_value=limiter), \
                     patch.object(verification, '_run_verification_batch', side_effect=fail):
                    with self.assertRaises(type(error)):
                        await verification.run_verification_batch(None, 'worker')
                self.assertIs(rate_limit.get_limiter(), caller)
            finally:
                rate_limit.reset_limiter(token)

    async def test_rate_limited_origin_updates_shared_backoff_and_stays_unchecked(self):
        limiter = SimpleNamespace(acquire=AsyncMock(), record_success=AsyncMock(),
            note_response=Mock(return_value=True), record_failure=AsyncMock())
        item = {'source_url': 'https://old.example/a', 'expected_url': 'https://new.example/b',
                'ordinal': 0, 'attempt': 1}
        recorded = []
        service = SimpleNamespace(claim=lambda *_: {'verification_id': 'fixture', 'items': [item]},
            record=lambda *args: recorded.append(args) or True)
        response = SimpleNamespace(status=429, headers={'Retry-After': '5'}, release=lambda: None)
        with patch.object(verification, 'HostRateLimiter', return_value=limiter), \
             patch.object(redirect_probe, '_request', new=AsyncMock(return_value=response)) as request:
            await verification.run_verification_batch(service, 'worker', 1)
        request.assert_awaited_once()
        limiter.acquire.assert_awaited_once_with(item['source_url'])
        limiter.note_response.assert_called_once_with(429, 5.0)
        limiter.record_failure.assert_awaited_once_with(item['source_url'], 5.0)
        limiter.record_success.assert_not_awaited()
        self.assertEqual(recorded[0][3], 'unchecked')
        self.assertEqual(recorded[0][4]['issue'], 'origin_unavailable')
