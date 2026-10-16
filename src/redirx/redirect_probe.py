"""
Ask a live site what it actually does with an old URL.

Export time is a prediction; this is the measurement. Redirects are followed
one hop at a time rather than by aiohttp's `allow_redirects`, because the
things that go wrong post-cutover are visible only in the shape of the chain:

  - how many hops (extra redirects add latency and complicate crawling)
  - whether they were permanent (301/308) or temporary (302/307)
  - where it actually landed, versus where the approved mapping said

Every hop is re-validated against the SSRF rules. A redirect is attacker-
controlled input by definition: the whole point of following one is that some
other server chose the next URL, and `http://old-site/x` is allowed to answer
`Location: http://169.254.169.254/latest/meta-data/`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from yarl import URL

import logging

from .rate_limit import CircuitOpen, get_limiter, parse_retry_after
from .safe_fetch import SSRFBlockedError, validate_public_url

logger = logging.getLogger(__name__)

# A legitimate redirect chain is one hop, occasionally two (http->https->page).
# Ten is far past "misconfigured" and into "loop the client hasn't noticed yet".
MAX_HOPS = 10
HOP_TIMEOUT = 15

# Sent so site owners can identify and allowlist the monitor in their logs.
USER_AGENT = "redirxbot/1.0 (+https://redirx.dev/bot)"

PERMANENT_STATUSES = frozenset({301, 308})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Statuses that mean "this server dislikes HEAD", not "this URL is broken".
# Retrying them with GET avoids reporting a site-wide outage that isn't real.
HEAD_UNSUPPORTED = frozenset({400, 403, 405, 406, 501})

REQUEST_URL_REWRITTEN = "request_url_rewritten"
REQUEST_URL_REWRITTEN_DETAIL = (
    "Not verified: the HTTP client changes this URL's path or query spelling. "
    "Check the exact URL with a tool that preserves the request target."
)


def request_target_preserved(url: str) -> bool:
    """Whether the installed HTTP client's path/query matches the raw input.

    This is not an SSRF check. Fragments are intentionally not sent over HTTP.
    Do not claim exact observations when yarl normalizes escapes, dot segments,
    Unicode or an explicitly empty query delimiter before sending the request.
    """
    try:
        parsed = urlsplit(url)
        raw = parsed.path or "/"
        if "?" in url.split("#", 1)[0]:
            raw += "?" + parsed.query
        return URL(url).raw_path_qs == raw
    except (ValueError, TypeError, UnicodeError):
        return False


# --- issue taxonomy --------------------------------------------------------
# Ordered worst-first within a severity. These strings are persisted in
# watch_issues.issue_type and rendered in alert emails, so they are API.

NO_REDIRECT = "no_redirect"
NOT_FOUND = "not_found"
SERVER_ERROR = "server_error"
WRONG_TARGET = "wrong_target"
REDIRECT_LOOP = "redirect_loop"
REDIRECT_CHAIN = "redirect_chain"
TEMPORARY_REDIRECT = "temporary_redirect"
UNREACHABLE = "unreachable"
BLOCKED = "blocked"

CRITICAL = "critical"
WARNING = "warning"

SEVERITY = {
    NO_REDIRECT: CRITICAL,
    NOT_FOUND: CRITICAL,
    SERVER_ERROR: CRITICAL,
    WRONG_TARGET: CRITICAL,
    REDIRECT_LOOP: CRITICAL,
    REDIRECT_CHAIN: WARNING,
    TEMPORARY_REDIRECT: WARNING,
    UNREACHABLE: WARNING,
    BLOCKED: WARNING,
}

# Human wording for the alert email and the UI. Kept next to the taxonomy so a
# new issue type cannot ship without a sentence explaining it to a customer.
DESCRIPTIONS = {
    NO_REDIRECT: "Still returns a page — the redirect was never deployed",
    NOT_FOUND: "Returns 404 — visitors and crawlers hit a dead end",
    SERVER_ERROR: "Returns a server error",
    WRONG_TARGET: "Redirects somewhere other than the approved target",
    REDIRECT_LOOP: "Redirect loop — the browser gives up",
    REDIRECT_CHAIN: "Reaches the right page, but through extra hops",
    TEMPORARY_REDIRECT: "Uses a temporary redirect — use 301 or 308 for a permanent move",
    UNREACHABLE: "Could not be reached",
    BLOCKED: "Refused the request",
}


@dataclass
class Hop:
    url: str
    status: int
    location: Optional[str] = None


@dataclass
class ProbeResult:
    """What the live site did with one old URL."""

    url: str
    hops: list[Hop] = field(default_factory=list)
    final_url: str = ""
    final_status: Optional[int] = None
    # Set when the chain could not be completed: timeout, dns, connection,
    # blocked, loop, too_many_hops, circuit_open.
    error: Optional[str] = None

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    @property
    def all_permanent(self) -> bool:
        """True when every redirect has a permanent-move HTTP status."""
        return all(h.status in PERMANENT_STATUSES for h in self.hops)


def normalize_for_compare(url: str) -> str:
    """
    Reduce a URL to what actually identifies the destination page.

    Scheme is dropped: an http->https upgrade is a canonical-host hop, already
    counted and reported as chain length, and calling it a wrong target would
    bury the real breakage under noise on every HSTS site. A leading `www.` is
    dropped for the same reason. Case in the host is meaningless; case in the
    path is not, and is preserved.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = f"[{host}]"
    # Scheme upgrades remain equivalent, including explicit standard ports.
    # A distinct nondefault service port still identifies a different target.
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host += f":{port}"

    path = parsed.path or "/"
    # /page and /page/ are the same page to every CMS in the ladder, and
    # treating them as different would flag a trailing-slash normaliser as a
    # wrong target on every URL of the site.
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    return _with_empty_query(url, urlunsplit(("", host, path, parsed.query, "")))


def _with_empty_query(original: str, normalized: str) -> str:
    """Keep an explicit query delimiter distinct from an absent query."""
    before_fragment = original.split("#", 1)[0]
    if "?" in before_fragment and before_fragment.split("?", 1)[1] == "":
        return normalized + "?"
    return normalized


def same_target(a: str, b: str) -> bool:
    if not a or not b:
        return False
    normalized = normalize_for_compare(a)
    return bool(normalized) and normalized == normalize_for_compare(b)


def visit_identity(url: str) -> str:
    """
    Identity for loop detection: has this exact URL already been requested?

    Deliberately much stricter than normalize_for_compare. That function throws
    away scheme and `www.` so an HSTS hop is not misreported as a wrong target
    — but "same page" and "same request" are different questions, and reusing
    it here called every `http://x` -> `https://x` site a redirect loop, which
    is most of the web.

    Only genuinely meaningless variation is normalised: host case and the
    default port. A `/a` -> `/a/` -> `/a` oscillation is a real loop and is
    still caught, on the hop that actually repeats a request.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return url.strip()

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        port = None
    netloc = f"{host}:{port}" if port is not None else host

    return _with_empty_query(url, urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, "")))


async def _request(
    session: aiohttp.ClientSession, method: str, url: str
) -> aiohttp.ClientResponse:
    return await session.request(
        method,
        url,
        allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=HOP_TIMEOUT),
        headers={"User-Agent": USER_AGENT},
    )


async def probe(session: aiohttp.ClientSession, url: str) -> ProbeResult:
    """
    Follow `url`'s redirect chain by hand, recording every hop.

    Never raises: a probe failure is a finding about the site, not an error in
    the sweep, and one unreachable URL must not abort the other 400.
    """
    result = ProbeResult(url=url)
    limiter = get_limiter()
    current = url
    # Requests already issued, by exact identity. See visit_identity: an
    # http->https or bare->www hop is a normal redirect, not a loop.
    seen: set[str] = {visit_identity(url)}

    for _ in range(MAX_HOPS + 1):
        try:
            # urllib validates some authority errors lazily on .port access.
            # Keep those failures inside the probe's bounded-result contract.
            _ = urlsplit(current).port
            validate_public_url(current)
        except (ValueError, TypeError):
            result.error = "invalid_url"
            result.final_url = current
            return result
        except SSRFBlockedError:
            result.error = "blocked"
            result.final_url = current
            return result

        if not request_target_preserved(current):
            result.error = REQUEST_URL_REWRITTEN
            result.final_url = current
            return result

        try:
            if limiter is not None:
                # Same global per-host budget the crawler uses. A monitor that
                # hammers a customer's origin every few hours is a monitor the
                # customer's WAF eventually bans.
                await limiter.acquire(current)

            response = await _request(session, "HEAD", current)
            try:
                status = response.status
                if status in HEAD_UNSUPPORTED:
                    # Not evidence about the URL — evidence about HEAD support.
                    response.release()
                    if limiter is not None:
                        await limiter.acquire(current)
                    response = await _request(session, "GET", current)
                    status = response.status

                location = response.headers.get("Location")
                if limiter is not None:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    if limiter.note_response(status, retry_after):
                        await limiter.record_failure(current, retry_after)
                    elif status < 400:
                        await limiter.record_success(current)
            finally:
                # Status and headers are all we need; never pull down a body.
                response.release()

        except CircuitOpen:
            result.error = "circuit_open"
            result.final_url = current
            return result
        except asyncio.TimeoutError:
            result.error = "timeout"
            result.final_url = current
            return result
        except SSRFBlockedError:
            # Raised at connect time by SafeResolver when a hostname resolves
            # into a denied range — the DNS-rebinding path.
            result.error = "blocked"
            result.final_url = current
            return result
        except aiohttp.ClientError:
            result.error = "connection"
            result.final_url = current
            return result
        except Exception:
            # Reported as unreachable so one bad URL cannot kill a sweep, but
            # logged loudly: anything landing here that is not a network
            # failure is a bug in this module being misfiled as a site problem.
            logger.exception("Unexpected error probing %s", current)
            result.error = "connection"
            result.final_url = current
            return result

        if status not in REDIRECT_STATUSES or not location:
            result.final_status = status
            result.final_url = current
            return result

        result.hops.append(Hop(url=current, status=status, location=location))
        # Location is allowed to be relative, and often is.
        try:
            nxt = urljoin(current, location)
        except (ValueError, TypeError):
            result.error = "invalid_url"
            result.final_url = current
            return result
        # Older Python urljoin versions discard an explicitly empty query.
        # Retain it so the fidelity guard can refuse the unrepresentable hop.
        before_fragment = location.split("#", 1)[0]
        if "?" in before_fragment and before_fragment.split("?", 1)[1] == "":
            parsed_next = urlsplit(nxt)
            nxt = urlunsplit((parsed_next.scheme, parsed_next.netloc,
                             parsed_next.path, "", "")) + "?"
            if parsed_next.fragment:
                nxt += "#" + parsed_next.fragment

        key = visit_identity(nxt)
        if key in seen:
            result.error = "loop"
            result.final_url = nxt
            return result
        seen.add(key)
        current = nxt

    result.error = "too_many_hops"
    result.final_url = current
    return result


def classify(result: ProbeResult, expected_url: Optional[str]) -> Optional[tuple[str, str]]:
    """
    Turn a probe into an issue type, or None when the redirect is correct.

    `expected_url` is the approved mapping's target. Without one — an old URL
    the migration never mapped — only self-evident breakage can be judged, so
    the destination checks are skipped rather than guessed at.
    """
    if result.error == "loop":
        return REDIRECT_LOOP, "Redirect loop detected"
    if result.error == "too_many_hops":
        return REDIRECT_LOOP, f"More than {MAX_HOPS} redirects without resolving"
    if result.error == "blocked":
        return BLOCKED, "Refused: the address resolves to a non-public network"
    if result.error == "circuit_open":
        return UNREACHABLE, "Host is rate-limiting or refusing automated requests"
    if result.error == "timeout":
        return UNREACHABLE, f"No response within {HOP_TIMEOUT}s"
    if result.error == REQUEST_URL_REWRITTEN:
        return UNREACHABLE, REQUEST_URL_REWRITTEN_DETAIL
    if result.error == "invalid_url":
        return UNREACHABLE, "Not verified: the URL or redirect Location has an invalid authority or port. Correct the URL and retry."
    if result.error:
        return UNREACHABLE, "Connection failed"
    if expected_url and not request_target_preserved(expected_url):
        return UNREACHABLE, REQUEST_URL_REWRITTEN_DETAIL

    status = result.final_status or 0

    if status == 404 or status == 410:
        return NOT_FOUND, f"Returns {status}"
    if status >= 500:
        return SERVER_ERROR, f"Returns {status}"

    if not result.hops:
        # Reached without a single redirect. If it still serves a page, the
        # rule is simply not live; anything else is the site's own error.
        if 200 <= status < 300:
            return NO_REDIRECT, f"Returns {status} instead of redirecting"
        if status >= 400:
            return SERVER_ERROR, f"Returns {status}"
        return None

    if expected_url and not same_target(result.final_url, expected_url):
        return WRONG_TARGET, f"Lands on {result.final_url}"

    if status >= 400:
        # Redirected, but into a page that does not exist.
        return NOT_FOUND, f"Redirect target returns {status}"

    if not result.all_permanent:
        temporary = next(
            (h.status for h in result.hops if h.status not in PERMANENT_STATUSES), 302
        )
        return TEMPORARY_REDIRECT, f"Uses {temporary}; use 301 or 308 for a permanent move"

    if len(result.hops) > 1:
        return REDIRECT_CHAIN, f"{len(result.hops)} hops before landing"

    return None
