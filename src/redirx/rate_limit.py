"""
Global per-host crawl politeness.

Concurrency limits were per-worker asyncio semaphores, so N workers each doing
a "polite" rate produced N times that rate at the origin — which is why real
sites already 429 us. This limiter is shared state in Postgres, so the rate is
global across every worker and the API.

Postgres rather than Redis deliberately: the limiter caps at ~1 req/s per host,
so sub-millisecond op latency buys nothing, and a second stateful service would
be a second source of truth to monitor. Revisit only above a few hundred
limiter ops/sec.

State lives in host_buckets and is manipulated only through the functions in
migration 025, each of which is a single atomic statement (lazy refill from
elapsed wall-clock time, so there is no background job and no drift).

Transport is direct async Postgres rather than PostgREST: lower latency, and
it keeps the limiter functions off the public REST surface entirely.

Connection note: point CRAWL_LIMITER_DATABASE_URL at the Supabase transaction
pooler (port 6543) so scaling workers cannot exhaust connections. Transaction
mode is safe here because these calls rely only on ON CONFLICT row locks,
which are transaction-scoped — the limiter never takes a session-scoped
advisory lock (which silently does nothing in transaction mode).

The psycopg pool is owned by the event loop that opened it, not by the process
— see the ownership note above _pools. Tokens are not: they are the host's row
in Postgres, shared by every loop, thread and process alike.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

# Imported for its side effect of loading .env, so the DSN and tunables below
# resolve the same way whether we run under Flask, the worker, or a script.
from .config import Config  # noqa: F401

logger = logging.getLogger(__name__)

# Bucket depth. Small burst allowance so a handful of sitemap fetches go out
# back-to-back without the origin seeing sustained parallelism.
DEFAULT_CAPACITY = float(os.getenv("CRAWL_BURST_CAPACITY", "3"))
# Starting steady-state rate, per host. AIMD adapts from here.
DEFAULT_RATE = float(os.getenv("CRAWL_DEFAULT_RATE", "1"))
MAX_RATE = float(os.getenv("CRAWL_MAX_RATE", "4"))
RATE_INCREMENT = float(os.getenv("CRAWL_RATE_INCREMENT", "0.1"))
BREAKER_THRESHOLD = int(os.getenv("CRAWL_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN = float(os.getenv("CRAWL_BREAKER_COOLDOWN", "900"))
MIN_RATE = float(os.getenv("CRAWL_MIN_RATE", "0.1"))
# Ceiling on how long a single acquire will wait before giving up on a host.
MAX_ACQUIRE_WAIT = float(os.getenv("CRAWL_MAX_ACQUIRE_WAIT", "30"))

# Discovery reads robots.txt and a handful of sitemap documents — static,
# usually CDN-cached, and not something WAFs meaningfully rate-limit. Pacing
# those like sustained page scraping just burns the request's time budget, so
# they get their own bucket with a looser rate.
# Measured: a 10-deep burst at 8/s drew a 429 from allbirds.com's WAF even on
# sitemap paths, so this is tuned down from the theoretical "sitemaps are free".
DISCOVERY_CAPACITY = float(os.getenv("DISCOVERY_BURST_CAPACITY", "5"))
DISCOVERY_RATE = float(os.getenv("DISCOVERY_DEFAULT_RATE", "4"))
DISCOVERY_NAMESPACE = "discovery"

# Post-cutover monitoring sends HEADs that a server answers from its redirect
# table without touching the application — cheaper than discovery's sitemap
# reads. It is paced more conservatively anyway, because unlike a crawl it
# recurs forever: a rate a site tolerates once is a rate it will be asked to
# tolerate every few hours for months, and a monitor that gets a customer's
# own site to ban us has failed at the only thing it does.
WATCH_CAPACITY = float(os.getenv("WATCH_BURST_CAPACITY", "4"))
WATCH_RATE = float(os.getenv("WATCH_DEFAULT_RATE", "2"))
WATCH_NAMESPACE = "watch"

# Statuses that mean "you are going too fast" or "you are not welcome".
BACKOFF_STATUSES = (403, 429, 503)


class CircuitOpen(Exception):
    """Host is in breaker cooldown; stop fetching it for this job."""

    def __init__(self, host: str, retry_after: float):
        super().__init__(f"circuit open for {host} ({retry_after:.0f}s remaining)")
        self.host = host
        self.retry_after = retry_after


@dataclass
class AcquireResult:
    allowed: bool
    retry_after: float
    reason: str


def host_of(url: str) -> str:
    """Bucket key: registrable-ish host, www-normalized so www and bare share a bucket."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date. Honor both."""
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def parse_crawl_delay(robots_txt: str, user_agent_token: str = "redirxbot") -> float | None:
    """
    Crawl-delay for our UA, falling back to the wildcard group. Returns seconds.
    """
    ua_delay: float | None = None
    wildcard_delay: float | None = None
    current: str | None = None

    for line in (robots_txt or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            current = value.lower()
        elif field == "crawl-delay" and current is not None:
            try:
                delay = float(value)
            except ValueError:
                continue
            if current == "*":
                wildcard_delay = delay
            elif user_agent_token in current:
                ua_delay = delay

    return ua_delay if ua_delay is not None else wildcard_delay


def limiter_dsn() -> Optional[str]:
    """
    Prefer a pooler URL dedicated to the limiter; fall back to DATABASE_URL.
    Returns None when no database is configured (limiter then no-ops).
    """
    return os.getenv("CRAWL_LIMITER_DATABASE_URL") or os.getenv("DATABASE_URL") or None


# How long to wait for the pool to open, and how long to stop trying after it
# fails. Without the cooldown a failed open is retried on every single limiter
# call, so each fetch pays the full timeout again: measured in production, an
# unreachable DSN turned one discovery request into repeated 10s stalls and
# pushed it past gunicorn's request timeout, which killed the worker and
# returned a bodiless 500. A limiter outage must degrade to a fast no-op.
POOL_OPEN_TIMEOUT = float(os.getenv("CRAWL_LIMITER_OPEN_TIMEOUT", "5"))
POOL_RETRY_COOLDOWN = float(os.getenv("CRAWL_LIMITER_RETRY_COOLDOWN", "60"))
POOL_CLOSE_TIMEOUT = float(os.getenv("CRAWL_LIMITER_CLOSE_TIMEOUT", "2"))

# Pool ownership is per event loop, not per process.
#
# psycopg_pool runs an AsyncConnectionPool's workers and scheduler as tasks on
# the loop that opened it and guards it with asyncio primitives bound to that
# loop. This process has more than one loop: the worker's matching loop, the
# dedicated thread the pivot background runner starts alongside it
# (backend/services/pivot_background.py), and a fresh loop per asyncio.run()
# cycle on the legacy Flask async path. One module-global pool was therefore
# shared by loops that cannot safely share it.
#
# The single module-global asyncio.Lock was the sharper edge: it is not a
# cross-thread mutex. Loop A takes it uncontended, which binds nothing; loop B
# then contends, binds the lock to *its* loop and waits on a future created
# there; loop A's release() resolves that future from the wrong thread without
# waking loop B's selector, and loop B waits forever. Measured 8/8 against real
# Postgres: acquire() never returned, never raised, and so never reached the
# fail-open path that exists precisely to stop a limiter fault wedging a crawl.
#
# So: one pool per (loop, dsn); a plain threading.Lock around the registry,
# held only across dict access and never across an await; and an open-once
# asyncio.Lock per loop, so contention is only ever between tasks that already
# share a loop.
#
# None of this touches where the tokens live. Authority is still the single
# host_buckets row in Postgres keyed by host, so same-host token totals stay
# atomic across loops, threads and processes exactly as before.
_pools: dict[tuple[object, str], object] = {}
_open_locks: dict[object, asyncio.Lock] = {}
_registry_guard = threading.Lock()
# The fail-open cooldown stays process-wide. Every loop resolves the same DSN,
# so an outage is an outage for all of them, and a per-loop cooldown would let
# each new loop pay the open timeout the cooldown exists to avoid.
_pool_failed_at: float | None = None


def _is_connectivity_error(exc: BaseException) -> bool:
    """
    Whether a failure means the database is unreachable, as opposed to a query
    being wrong. Only the former should disable the limiter: a bad statement
    fails in milliseconds and must not switch politeness off for every host.
    """
    try:
        from psycopg_pool import PoolTimeout

        if isinstance(exc, PoolTimeout):
            return True
    except Exception:
        pass
    try:
        import psycopg

        if isinstance(exc, psycopg.OperationalError):
            return True
    except Exception:
        pass
    return False


def _in_failure_cooldown() -> bool:
    return (
        _pool_failed_at is not None
        and (time.monotonic() - _pool_failed_at) < POOL_RETRY_COOLDOWN
    )


def _discard_stranded(pool) -> None:
    """
    Reclaim a pool whose loop died without closing it.

    close() is a coroutine that takes the pool's own asyncio.Lock, so only the
    owning loop can run it; once that loop is gone, the sockets can only be
    released through libpq directly. Best effort, and a backstop only — the
    loops we own close their own pools on the way out.
    """
    try:
        idle = list(pool._pool)
        pool._pool.clear()
    except Exception:
        return
    for conn in idle:
        try:
            conn.pgconn.finish()
        except Exception:
            pass


def _reap_closed_loops() -> None:
    """Drop registry entries for loops that have shut down. Caller holds the guard."""
    for key in [k for k in _pools if k[0].is_closed()]:
        _discard_stranded(_pools.pop(key))
        _open_locks.pop(key[0], None)
    for loop in [dead for dead in _open_locks if dead.is_closed()]:
        _open_locks.pop(loop, None)


async def _get_pool(dsn: Optional[str] = None):
    """
    Lazily open one async pool per (running event loop, dsn).

    Returns None when no database is configured or a recent open failed, so
    callers fail open immediately instead of blocking on a dead host.
    """
    global _pool_failed_at
    loop = asyncio.get_running_loop()
    dsn = dsn or limiter_dsn()
    if not dsn:
        return None
    key = (loop, dsn)

    with _registry_guard:
        _reap_closed_loops()
        pool = _pools.get(key)
        if pool is not None:
            return pool
        if _in_failure_cooldown():
            return None
        lock = _open_locks.get(loop)
        if lock is None:
            # Created while this loop is running, and only ever awaited from
            # it, so the loop it binds to on first contention is its own.
            lock = _open_locks[loop] = asyncio.Lock()

    async with lock:
        with _registry_guard:
            pool = _pools.get(key)
            if pool is not None:
                return pool
            if _in_failure_cooldown():
                return None
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=int(os.getenv("CRAWL_LIMITER_POOL_SIZE", "4")),
            open=False,
            timeout=POOL_OPEN_TIMEOUT,
            # Transaction-pooler friendly: no server-side prepared statements.
            kwargs={"prepare_threshold": None},
        )
        try:
            await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT)
        except Exception:
            _pool_failed_at = time.monotonic()
            # The pool spawns reconnect workers even when open() times out, so
            # it has to be closed or they spin forever. close() itself blocks
            # until an in-flight connect finishes, which against a blackholed
            # host is indefinitely — bound it rather than trade one hang for
            # another. The cooldown caps this to once per window.
            try:
                await asyncio.wait_for(pool.close(), timeout=POOL_CLOSE_TIMEOUT)
            except Exception:
                pass
            raise
        with _registry_guard:
            _pools[key] = pool
            _pool_failed_at = None
    return pool


async def close_pool() -> None:
    """
    Close the pools this event loop owns (tests, worker and background-runner
    shutdown).

    Only the owning loop can close a pool, so every loop that used the limiter
    calls this before it goes away; a loop that dies without doing so is reaped
    on the next _get_pool(). Bounded for the same reason the failed-open path
    is: shutdown is exactly where waiting forever on a dead host costs most.

    The outage cooldown is deliberately left alone. Closing a pool says nothing
    about whether the database came back, and run_once() closes after every
    standalone cycle — clearing it there would make each cycle pay the open
    timeout again, which is the stall POOL_RETRY_COOLDOWN exists to prevent.
    """
    loop = asyncio.get_running_loop()
    with _registry_guard:
        _reap_closed_loops()
        mine = [_pools.pop(key) for key in [k for k in _pools if k[0] is loop]]
        _open_locks.pop(loop, None)
    for pool in mine:
        try:
            await asyncio.wait_for(pool.close(), timeout=POOL_CLOSE_TIMEOUT)
        except Exception as exc:
            logger.debug("limiter pool close failed: %s", exc)


async def close_limiter_pools() -> None:
    """
    Close this loop's limiter pools in every loaded copy of this module.

    `redirx.rate_limit` and `src.redirx.rate_limit` are distinct module objects
    in the worker and the API — both spellings are deliberately importable (see
    the sys.path note in backend/worker.py) — so each copy keeps its own
    registry. A loop shutting down has to close whichever copies it opened
    pools in, and it does not know which those were.
    """
    for name in ("redirx.rate_limit", "src.redirx.rate_limit"):
        module = sys.modules.get(name)
        if module is not None:
            await module.close_pool()


class HostRateLimiter:
    """Async facade over the Postgres token-bucket functions."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        enabled: bool | None = None,
        namespace: str = "",
        capacity: float | None = None,
        rate: float | None = None,
    ):
        """
        Args:
            dsn: Limiter database override. Defaults to limiter_dsn(), which is
                what every caller in the tree uses; a pool is opened per DSN.
            namespace: Bucket key prefix. Discovery (a handful of static,
                usually CDN-cached sitemap/robots requests) and content
                scraping (sustained page fetches) impose very different loads
                on an origin, so they get separate buckets rather than
                fighting over one adaptive rate for the same host.
            capacity: Bucket depth. Defaults to CRAWL_BURST_CAPACITY.
            rate: Starting refill rate. Only applied when the bucket row is
                first created; after that the stored adaptive rate governs.
        """
        self._dsn = dsn
        if enabled is None:
            enabled = os.getenv("CRAWL_POLITENESS_ENABLED", "true").strip().lower() not in (
                "0", "false", "no", "off",
            )
        self.enabled = enabled
        self.namespace = namespace
        self.capacity = DEFAULT_CAPACITY if capacity is None else capacity
        self.rate = DEFAULT_RATE if rate is None else rate
        # Paused hosts -> seconds remaining, so a paused host reports its real
        # wait rather than the generic breaker cooldown.
        self._tripped: dict[str, float] = {}
        # Hosts we've already warned about, so a limiter outage logs once per
        # host instead of once per fetch.
        self._warned: set[str] = set()

    async def _call(self, sql: str, params: tuple):
        global _pool_failed_at
        if _in_failure_cooldown():
            raise RuntimeError("limiter database unavailable (cooldown)")
        # Passing the instance DSN through is what makes an explicit
        # HostRateLimiter(dsn=...) mean anything: pools are keyed by it, so an
        # override gets its own pool instead of silently reusing the ambient one.
        pool = await _get_pool(self._dsn)
        if pool is None:
            raise RuntimeError("no limiter database configured")
        # A pool with min_size=0 opens instantly, so an unreachable database
        # surfaces here — when checking out a connection — not at open(). This
        # is the path that actually stalled in production, so it is the one
        # that has to arm the cooldown.
        try:
            return await self._execute(pool, sql, params)
        except Exception as exc:
            if _is_connectivity_error(exc):
                _pool_failed_at = time.monotonic()
            raise

    async def _execute(self, pool, sql: str, params: tuple):
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                if cur.description is None:
                    return []
                columns = [d.name for d in cur.description]
                return [dict(zip(columns, row)) for row in await cur.fetchall()]

    def _key(self, host: str) -> str:
        return f"{self.namespace}:{host}" if self.namespace else host

    async def try_acquire(self, host: str) -> AcquireResult:
        rows = await self._call(
            "SELECT allowed, retry_after, reason FROM try_consume_host_token("
            "%s::text, %s::double precision, %s::double precision)",
            (self._key(host), self.capacity, self.rate),
        )
        row = rows[0] if rows else {}
        return AcquireResult(
            allowed=bool(row.get("allowed")),
            retry_after=float(row.get("retry_after") or 0.0),
            reason=str(row.get("reason") or "unknown"),
        )

    async def acquire(self, url: str) -> None:
        """
        Wait until a token is available for this URL's host.

        Raises:
            CircuitOpen: breaker is open for the host, or we waited too long.
        """
        if not self.enabled:
            return
        host = host_of(url)
        if not host:
            return
        if host in self._tripped:
            raise CircuitOpen(host, self._tripped[host])

        waited = 0.0
        while True:
            try:
                result = await self.try_acquire(host)
            except Exception as exc:
                # Never let limiter infrastructure failure stop a crawl; a brief
                # unthrottled window is preferable to a dead pipeline.
                if host not in self._warned:
                    self._warned.add(host)
                    logger.warning(
                        "rate limiter unavailable for %s (failing open): %s",
                        host,
                        str(exc).splitlines()[0],
                    )
                return

            if result.allowed:
                return
            if result.reason == "circuit_open":
                # A single 429 carrying a short Retry-After sets blocked_until
                # without tripping the breaker. Waiting that out is the polite
                # and correct response; aborting would turn one transient
                # throttle into "0 pages found" for a site that works fine.
                remaining = MAX_ACQUIRE_WAIT - waited
                if result.retry_after <= remaining:
                    await asyncio.sleep(max(result.retry_after, 0.05))
                    waited += result.retry_after
                    continue
                self._tripped[host] = result.retry_after
                raise CircuitOpen(host, result.retry_after)

            delay = min(max(result.retry_after, 0.05), 5.0)
            if waited + delay > MAX_ACQUIRE_WAIT:
                self._tripped[host] = result.retry_after
                raise CircuitOpen(host, result.retry_after)
            await asyncio.sleep(delay)
            waited += delay

    async def record_success(self, url: str) -> None:
        if not self.enabled:
            return
        host = host_of(url)
        if not host:
            return
        try:
            await self._call(
                "SELECT record_host_success(%s::text, %s::double precision, %s::double precision)",
                (self._key(host), RATE_INCREMENT, MAX_RATE),
            )
        except Exception as exc:
            logger.debug("record_host_success failed for %s: %s", host, exc)

    async def record_failure(self, url: str, retry_after: float | None = None) -> bool:
        """
        Halve the host's rate and count toward the breaker.

        Returns True if the breaker tripped.
        """
        if not self.enabled:
            return False
        host = host_of(url)
        if not host:
            return False
        try:
            rows = await self._call(
                "SELECT circuit_open, new_rate FROM record_host_failure("
                "%s::text, %s::double precision, %s::integer, "
                "%s::double precision, %s::double precision)",
                (self._key(host), retry_after, BREAKER_THRESHOLD, BREAKER_COOLDOWN, MIN_RATE),
            )
        except Exception as exc:
            logger.debug("record_host_failure failed for %s: %s", host, exc)
            return False

        row = rows[0] if rows else {}
        tripped = bool(row.get("circuit_open"))
        if tripped:
            self._tripped[host] = BREAKER_COOLDOWN
            logger.warning("circuit breaker tripped for %s", host)
        return tripped

    async def apply_crawl_delay(self, url: str, robots_txt: str) -> float | None:
        """Pin the host's rate from robots.txt Crawl-delay, if present."""
        if not self.enabled:
            return None
        delay = parse_crawl_delay(robots_txt)
        if not delay or delay <= 0:
            return None
        host = host_of(url)
        if not host:
            return None
        try:
            await self._call("SELECT set_host_crawl_delay(%s::text, %s::double precision)", (self._key(host), delay))
        except Exception as exc:
            logger.debug("set_host_crawl_delay failed for %s: %s", host, exc)
        return delay

    def note_response(self, status: int, retry_after: float | None = None) -> bool:
        """
        Whether a response should count as a politeness failure.

        429 and 503 unambiguously mean "slow down". 403 does not: discovery
        speculatively probes several candidate sitemap paths, and a WAF
        answering 403 for one that doesn't exist is saying "not here", not
        "back off". Treating that as throttling let a single probe block an
        entire working domain. A 403 only counts when it carries Retry-After,
        which is an explicit throttle signal.
        """
        if status in (429, 503):
            return True
        if status == 403:
            return retry_after is not None
        return False


# The limiter is a job-scoped dependency threaded through deep call stacks
# (discovery strategies, scraper fan-out). A ContextVar propagates correctly
# across asyncio tasks without churning every intermediate signature, and
# defaults to None so existing call paths and tests are unaffected.
import contextvars  # noqa: E402

_current_limiter: contextvars.ContextVar[Optional["HostRateLimiter"]] = contextvars.ContextVar(
    "redirx_host_limiter", default=None
)


def get_limiter() -> Optional["HostRateLimiter"]:
    return _current_limiter.get()


def set_limiter(limiter: Optional["HostRateLimiter"]):
    """Install the limiter for the current async context. Returns the token."""
    return _current_limiter.set(limiter)


def reset_limiter(token):
    """Restore the caller's async context after a bounded job finishes."""
    _current_limiter.reset(token)
