"""
Crawl-limiter pool ownership across event loops, against real Postgres.

backend/services/pivot_background.py runs its stages on a dedicated thread with
its own event loop, alongside the worker's matching loop, and opens a fresh loop
per asyncio.run() cycle in run_once() — the same shape as the legacy Flask async
path. The limiter's psycopg pool is loop-bound, so those are the conditions the
module-global pool and the module-global asyncio.Lock were never written for.

What must hold no matter how the loops are arranged:

  * the token total for a host is the bucket's, not each loop's — authority is
    the single host_buckets row from migration 025;
  * a loop that loses the race to open the pool still gets an answer, whether
    that is a token or the documented fail-open;
  * a loop that ends gives its connections back.

Buckets here are frozen (refill_rate ~= 0) so a host's *lifetime* token total is
exactly its capacity, which makes "did more tokens come out than the bucket ever
held" a direct assertion rather than a timing estimate.

Requires PREFLIGHT_TEST_DATABASE_URL pointing at a disposable loopback cluster.
Like the other native fixtures this creates its own database on that cluster and
drops it afterwards, so the supplied catalog is never written to; only migration
025 is applied.
"""
import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from redirx import rate_limit
from redirx.rate_limit import HostRateLimiter

CAPACITY = 5
# Small enough that nothing refills inside a test, non-zero so the SQL's
# "time to earn one token" arithmetic still divides by something real.
FROZEN_RATE = 1e-7
# Stands in for a cold TCP connect and auth against the Supabase pooler. The
# race this reproduces needs one loop to still be inside the open when the
# second arrives, which on a loopback socket it never is.
COLD_OPEN_SECONDS = 0.4


@unittest.skipUnless(os.getenv("PREFLIGHT_TEST_DATABASE_URL"), "requires disposable loopback PostgreSQL")
class LimiterPoolOwnership(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_dsn = os.environ["PREFLIGHT_TEST_DATABASE_URL"]
        validate_disposable_postgres_dsn(cls.admin_dsn)
        parsed = urlsplit(cls.admin_dsn)
        cls.database = "redirx_preflight_test_" + uuid4().hex
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.dsn = urlunsplit(parsed._replace(path="/" + cls.database))
        cls.addClassCleanup(cls.cleanup_db)
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute((ROOT / "database/migrations/025_add_host_rate_limiting.sql").read_text())

    @classmethod
    def cleanup_db(cls):
        with psycopg.connect(cls.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    def setUp(self):
        self.addCleanup(os.environ.pop, "CRAWL_LIMITER_DATABASE_URL", None)
        os.environ["CRAWL_LIMITER_DATABASE_URL"] = self.dsn
        self.reset_module_state()
        self.addCleanup(self.reset_module_state)

    # ---- fixture plumbing ----------------------------------------------

    def reset_module_state(self):
        """Drop pools without awaiting: their loops are gone by the time we get here."""
        for pool in list(rate_limit._pools.values()):
            rate_limit._discard_stranded(pool)
        rate_limit._pools.clear()
        rate_limit._open_locks.clear()
        rate_limit._pool_failed_at = None

    def frozen_host(self, label):
        """A host whose bucket starts full and never refills."""
        host = f"{label}-{uuid4().hex[:8]}.example.com"
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO host_buckets (host, tokens, refill_rate, updated_at) "
                "VALUES (%s, %s, %s, NOW())",
                (host, float(CAPACITY), FROZEN_RATE),
            )
        return host

    def tokens_left(self, host):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return conn.execute("SELECT tokens FROM host_buckets WHERE host = %s", (host,)).fetchone()[0]

    def limiter_connections(self):
        """Backends on the limiter database other than this bookkeeping one."""
        with psycopg.connect(self.dsn, autocommit=True, application_name="limiter-loop-test") as conn:
            return conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND application_name <> 'limiter-loop-test'"
            ).fetchone()[0]

    def settled_connections(self, expected, timeout=5.0):
        """Poll until the backend count reaches `expected`; the server closes asynchronously."""
        deadline = time.monotonic() + timeout
        count = self.limiter_connections()
        while count != expected and time.monotonic() < deadline:
            time.sleep(0.05)
            count = self.limiter_connections()
        return count

    @staticmethod
    def limiter():
        return HostRateLimiter(enabled=True, capacity=float(CAPACITY), rate=1.0)

    def slow_the_first_open(self):
        """
        Make opening a pool take as long as it does against the real pooler.

        The race only exists while one loop is still inside the open, and on a
        loopback socket that is barely a millisecond. The delay goes in open()
        rather than anywhere earlier because open() is what runs while the
        open-once lock is held, so this widens the window that actually matters.
        """
        import psycopg_pool

        real_cls = psycopg_pool.AsyncConnectionPool

        class SlowToOpen(real_cls):
            async def open(self, *args, **kwargs):
                await asyncio.sleep(COLD_OPEN_SECONDS)
                return await super().open(*args, **kwargs)

        psycopg_pool.AsyncConnectionPool = SlowToOpen
        self.addCleanup(setattr, psycopg_pool, "AsyncConnectionPool", real_cls)

    @staticmethod
    def on_own_loop(body, close=True):
        """Run `body` on a fresh loop, closing the pools that loop opened."""
        async def main():
            try:
                return await body()
            finally:
                if close:
                    await rate_limit.close_pool()
        return asyncio.run(main())

    def in_threads(self, target, tags=("worker", "pivot-background")):
        threads = [threading.Thread(target=target, args=(tag,), name=tag) for tag in tags]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse([t.name for t in threads if t.is_alive()], "a loop never finished")

    # ---- the token total is the bucket's, not each loop's ----------------

    def test_same_host_token_total_is_shared_across_concurrent_loops(self):
        """Two loops at once draw from one bucket, not one bucket each."""
        host = self.frozen_host("concurrent")
        start = threading.Barrier(2, timeout=30)
        granted, failures = [], []

        def loop(tag):
            async def body():
                limiter = self.limiter()
                start.wait()
                mine = 0
                for _ in range(CAPACITY):
                    if (await limiter.try_acquire(host)).allowed:
                        mine += 1
                    await asyncio.sleep(0.01)
                return mine
            try:
                granted.append(self.on_own_loop(body))
            except BaseException as exc:                      # noqa: BLE001 - reported, not swallowed
                failures.append((tag, repr(exc)))

        self.in_threads(loop)
        self.assertEqual(failures, [], "a loop could not reach the limiter at all")
        # Ten attempts against a bucket that has ever held five tokens.
        self.assertEqual(sum(granted), CAPACITY)
        self.assertLess(self.tokens_left(host), 1.0)

    def test_same_host_token_total_survives_fresh_loop_restarts(self):
        """Sequential asyncio.run() cycles keep drawing from the same bucket."""
        host = self.frozen_host("sequential")
        granted = 0

        for _ in range(3):
            async def body():
                limiter = self.limiter()
                mine = 0
                for _ in range(3):
                    if (await limiter.try_acquire(host)).allowed:
                        mine += 1
                return mine
            granted += self.on_own_loop(body)

        # Nine attempts over three loops; the bucket still only ever held five.
        self.assertEqual(granted, CAPACITY)
        self.assertLess(self.tokens_left(host), 1.0)

    # ---- losing the open race must not wedge a loop ----------------------

    def test_concurrent_cold_start_leaves_neither_loop_waiting(self):
        """
        Both loops reach the database on a cold start.

        The module-global asyncio.Lock this replaced was not a cross-thread
        mutex: the second loop bound it to itself, waited on a future there, and
        the first loop's release() resolved that future from the wrong thread
        without waking the second loop's selector. acquire() then never returned
        and never raised, so it never reached the fail-open path either.
        """
        host = self.frozen_host("coldstart")
        self.slow_the_first_open()
        start = threading.Barrier(2, timeout=30)
        outcomes = {}

        def loop(tag):
            async def body():
                limiter = self.limiter()
                start.wait()
                await limiter.acquire(f"https://{host}/page")
                return "acquired"
            async def bounded():
                return await asyncio.wait_for(body(), timeout=COLD_OPEN_SECONDS + 10)
            try:
                outcomes[tag] = self.on_own_loop(bounded)
            except asyncio.TimeoutError:
                outcomes[tag] = "wedged"
            except BaseException as exc:                      # noqa: BLE001 - reported, not swallowed
                outcomes[tag] = repr(exc)

        with self.assertNoLogs(rate_limit.logger, "WARNING"):
            self.in_threads(loop)

        self.assertEqual(outcomes, {"worker": "acquired", "pivot-background": "acquired"})
        # Both acquisitions were real tokens out of the shared bucket, not two
        # loops failing open past a limiter they could not reach.
        self.assertAlmostEqual(self.tokens_left(host), CAPACITY - 2, places=3)

    # ---- loops give their connections back --------------------------------

    def test_background_runner_closes_real_pools_after_cycle_and_cancellation(self):
        """Exercise the actual lifecycle hooks, without closing pools from the test."""
        from backend.services.pivot_background import PivotBackgroundRunner
        baseline = self.limiter_connections()
        flags = {'MCP_PIVOT_ENABLED': 'true', 'MCP_PIVOT_DISCOVERY_ENABLED': 'true',
                 'MCP_PIVOT_VERIFICATION_ENABLED': 'false', 'MCP_PIVOT_MONITORING_ENABLED': 'false'}
        host = self.frozen_host('runner')
        entered = threading.Event()

        class Discovery:
            hold = False

            async def run_once(inner, **kwargs):
                result = await self.limiter().try_acquire(host)
                entered.set()
                if inner.hold:
                    await asyncio.Event().wait()
                return {'allowed': result.allowed}

        async def lifecycle():
            discovery = Discovery()
            runner = PivotBackgroundRunner(discovery_factory=lambda: discovery)
            result = await runner.run_once()
            self.assertTrue(result['discovery']['allowed'])
            self.assertEqual(rate_limit._pools, {}, 'standalone cycle retained its pool')
            entered.clear()
            discovery.hold = True
            self.assertTrue(runner.start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5), 'runner never obtained its token')
                self.assertEqual(len(rate_limit._pools), 1, 'persistent runner never opened a pool')
            finally:
                await runner.stop()
            self.assertEqual(rate_limit._pools, {}, 'cancelled runner retained its pool')

        with patch.dict(os.environ, flags):
            asyncio.run(lifecycle())
        self.assertAlmostEqual(self.tokens_left(host), CAPACITY - 2, places=3)
        self.assertEqual(self.settled_connections(baseline), baseline)

    def test_each_loop_returns_its_connections_when_it_ends(self):
        host = self.frozen_host("closing")
        baseline = self.limiter_connections()

        for _ in range(3):
            async def body():
                return await self.limiter().try_acquire(host)
            self.on_own_loop(body)

        self.assertEqual(self.settled_connections(baseline), baseline)
        self.assertEqual(rate_limit._pools, {})

    def test_one_loop_ending_cannot_clear_another_loop_s_outage_cooldown(self):
        """
        Teardown is not evidence the database came back.

        The cooldown is process-wide on purpose: every loop resolves the same
        DSN, so one loop's discovery that the limiter database is unreachable
        has to hold for all of them. close_pool() now runs far more often than
        it used to — the background runner closes after every standalone cycle
        — so a teardown that reset it would hand the next cycle the full open
        timeout again, which is the stall POOL_RETRY_COOLDOWN exists to stop.
        """
        host = self.frozen_host("cooldown")

        async def touch():
            return await self.limiter().try_acquire(host)

        # One loop has a pool open; then the database goes away.
        self.on_own_loop(touch, close=False)
        self.assertEqual(len(rate_limit._pools), 1)
        rate_limit._pool_failed_at = time.monotonic()
        self.assertTrue(rate_limit._in_failure_cooldown())

        # A sibling loop ends, tearing down its own pools and reaping that one.
        async def idle():
            return None
        self.on_own_loop(idle)
        self.assertTrue(rate_limit._in_failure_cooldown(), "a sibling teardown cleared the cooldown")

        # The sweep across both module copies must not clear it either.
        async def sweep():
            await rate_limit.close_limiter_pools()
        asyncio.run(sweep())
        self.assertTrue(rate_limit._in_failure_cooldown(), "close_limiter_pools cleared the cooldown")

        # And the limiter is still failing open fast instead of dialling out.
        async def still_short_circuited():
            with self.assertRaises(RuntimeError):
                await self.limiter().try_acquire(host)
        asyncio.run(still_short_circuited())

    def test_a_loop_that_ends_without_closing_is_reaped_by_the_next_one(self):
        """The backstop for a cycle that dies before its teardown runs."""
        host = self.frozen_host("stranded")
        baseline = self.limiter_connections()

        async def body():
            return await self.limiter().try_acquire(host)

        self.on_own_loop(body, close=False)
        self.assertEqual(len(rate_limit._pools), 1, "the stranded pool should still be registered")
        self.assertGreater(self.limiter_connections(), baseline)

        # The next loop reaps it on its way to opening its own.
        self.on_own_loop(body)
        self.assertEqual(self.settled_connections(baseline), baseline)
        self.assertEqual(rate_limit._pools, {})


if __name__ == "__main__":
    unittest.main()
