"""
SSRF hardening tests — the P0 acceptance criteria plus unit coverage.

Acceptance (from the phase-2 spec): cloud metadata, localhost services, and
a public domain redirecting to an internal address must all fail closed with
a generic error.
"""
import asyncio
import os
import socket
import sys
import unittest
from unittest.mock import patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from redirx.safe_fetch import (
    SafeResolver,
    SSRFBlockedError,
    is_forbidden_ip,
    resolve_and_validate_host,
    validate_public_url,
)
from redirx.discovery import (
    DiscoveryError,
    _fetch,
    discover_site,
    discover_via_crawl,
    normalize_root,
)

from backend.tests.test_discovery import FakeResponse, FakeSession


def run(coro):
    return asyncio.run(coro)


class TestIsForbiddenIp(unittest.TestCase):
    def test_denied_ranges(self):
        for ip in (
            "169.254.169.254",   # cloud metadata
            "10.0.0.1",
            "172.16.5.5",
            "192.168.1.1",
            "127.0.0.1",
            "0.0.0.0",
            "100.64.0.1",        # CGNAT
            "198.18.0.1",        # benchmarking
            "::1",
            "fc00::1",           # unique-local v6
            "fe80::1",           # link-local v6
            "::ffff:10.0.0.1",   # IPv4-mapped private
        ):
            self.assertTrue(is_forbidden_ip(ip), ip)

    def test_public_ips_allowed(self):
        for ip in ("8.8.8.8", "93.184.216.34", "2607:f8b0::1"):
            self.assertFalse(is_forbidden_ip(ip), ip)

    def test_garbage_refused(self):
        self.assertTrue(is_forbidden_ip("not-an-ip"))


class TestValidatePublicUrl(unittest.TestCase):
    def test_metadata_and_localhost_blocked(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:6379",
            "http://localhost/",
            "http://foo.localhost/",
            "http://db.internal/",
            "http://127.0.0.1:80/",
        ):
            with self.assertRaises(SSRFBlockedError, msg=url):
                validate_public_url(url)

    def test_scheme_and_port_restrictions(self):
        for url in (
            "ftp://example.com/",
            "file:///etc/passwd",
            "gopher://example.com/",
            "http://example.com:6379/",
            "https://example.com:8443/",
            "http://example.com:0/",
            "https://example.com:0/",
            "https://example.com:not-a-port/",
            "https://example.com:65536/",
            "https://example.com:-1/",
            "https://[invalid/",
        ):
            with self.assertRaises(SSRFBlockedError, msg=url):
                validate_public_url(url)

    def test_normal_urls_pass(self):
        validate_public_url("https://example.com/page")
        validate_public_url("http://example.com:80/")
        validate_public_url("https://example.com:443/x")


class TestSafeResolver(unittest.TestCase):
    def _resolve_with(self, fake_infos):
        async def fake_resolve(self, host, port=0, family=socket.AF_INET):
            return fake_infos

        # SafeResolver must be constructed inside a running loop (aiohttp's
        # DefaultResolver captures it in __init__).
        async def go():
            resolver = SafeResolver()
            with patch("redirx.safe_fetch.DefaultResolver.resolve", fake_resolve):
                return await resolver.resolve("example.com", 80)

        return run(go())

    def test_private_resolution_blocked(self):
        # DNS rebinding: public hostname resolving to an internal address.
        with self.assertRaises(SSRFBlockedError):
            self._resolve_with([{"host": "10.0.0.1", "port": 80}])

    def test_mixed_answer_returns_only_public(self):
        infos = self._resolve_with([
            {"host": "10.0.0.1", "port": 80},
            {"host": "93.184.216.34", "port": 80},
        ])
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["host"], "93.184.216.34")

    def test_public_resolution_passes(self):
        infos = self._resolve_with([{"host": "8.8.8.8", "port": 80}])
        self.assertEqual(len(infos), 1)


class RedirectingFakeSession(FakeSession):
    """FakeSession where an entry may specify a redirect Location."""

    def get(self, url, **kwargs):
        self.requested.append(url)
        entry = self.routes.get(url)
        if entry and entry[0] in (301, 302, 303, 307, 308):
            return FakeResponse(url, entry[0], b"", {"Location": entry[1]})
        return super().get(url, **kwargs)


class TestRedirectHopValidation(unittest.TestCase):
    def test_redirect_to_private_ip_fails_closed(self):
        session = RedirectingFakeSession({
            "https://example.com/sitemap.xml": (302, "http://10.0.0.1/"),
        })
        errors: list[str] = []
        status, text, _, _ = run(_fetch(session, "https://example.com/sitemap.xml", errors))
        self.assertEqual(status, 0)
        self.assertEqual(text, "")
        # The internal hop must never be requested.
        self.assertNotIn("http://10.0.0.1/", session.requested)
        self.assertTrue(any("blocked" in e for e in errors))

    def test_redirect_to_nonstandard_port_fails_closed(self):
        session = RedirectingFakeSession({
            "https://example.com/a": (302, "https://example.com:8443/b"),
        })
        errors: list[str] = []
        status, _, _, _ = run(_fetch(session, "https://example.com/a", errors))
        self.assertEqual(status, 0)

    def test_redirect_loop_capped(self):
        session = RedirectingFakeSession({
            "https://example.com/a": (302, "https://example.com/a"),
        })
        errors: list[str] = []
        status, _, _, _ = run(_fetch(session, "https://example.com/a", errors))
        self.assertEqual(status, 0)
        self.assertTrue(any("too many redirects" in e for e in errors))

    def test_invalid_authority_is_blocked_before_any_hop_request(self):
        for target in ("https://example.com:0/b", "https://example.com:bad/b",
                       "https://example.com:65536/b"):
            for redirect in (False, True):
                with self.subTest(target=target, redirect=redirect):
                    source = "https://example.com/a" if redirect else target
                    session = RedirectingFakeSession({source: (302, target)} if redirect else {})
                    errors = []
                    status, text, _, _ = run(_fetch(session, source, errors))
                    self.assertEqual((status, text), (0, ""))
                    self.assertNotIn(target, session.requested)
                    self.assertTrue(any("blocked target" in error for error in errors))

    def test_normal_redirect_followed(self):
        session = RedirectingFakeSession({
            "https://example.com/a": (301, "https://example.com/b"),
            "https://example.com/b": (200, "<html>ok</html>"),
        })
        errors: list[str] = []
        status, text, _, _ = run(_fetch(session, "https://example.com/a", errors))
        self.assertEqual(status, 200)
        self.assertIn("ok", text)


class TestResolveAndValidateHost(unittest.TestCase):
    """Up-front resolve-then-validate, before any connection is opened."""

    def _with_addrinfo(self, ips):
        async def fake_getaddrinfo(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

        async def go():
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", fake_getaddrinfo):
                return await resolve_and_validate_host("example.com", 443)

        return run(go())

    def test_public_hostname_resolving_to_private_ip_blocked(self):
        # The nip.io-style attack: legitimate public name, internal answer.
        with self.assertRaises(SSRFBlockedError):
            self._with_addrinfo(["10.0.0.1"])

    def test_metadata_answer_blocked(self):
        with self.assertRaises(SSRFBlockedError):
            self._with_addrinfo(["169.254.169.254"])

    def test_mixed_answer_refused_outright(self):
        # Stricter than the connector: any private answer refuses the host.
        with self.assertRaises(SSRFBlockedError):
            self._with_addrinfo(["93.184.216.34", "10.0.0.1"])

    def test_public_answer_allowed(self):
        self.assertEqual(self._with_addrinfo(["93.184.216.34"]), ["93.184.216.34"])

    def test_nxdomain_blocked(self):
        async def boom(host, port, **kwargs):
            raise socket.gaierror("nxdomain")

        async def go():
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", boom):
                return await resolve_and_validate_host("nope.invalid", 443)

        with self.assertRaises(SSRFBlockedError):
            run(go())


class TestCrawlReachability(unittest.TestCase):
    def test_unreachable_host_reports_nothing(self):
        # Regression: URLs are queued into `found` before fetching, so a host
        # we never reached used to report its own homepage as "1 page found".
        session = FakeSession({})  # every request 404s
        errors: list[str] = []
        urls = run(discover_via_crawl(
            session, "https://unreachable.example", 50, __import__("time").monotonic() + 30, errors
        ))
        self.assertEqual(urls, [])

    def test_reachable_host_still_returns_pages(self):
        session = FakeSession({
            "https://example.com/": (200, '<a href="/about">A</a>'),
            "https://example.com/about": (200, "<p>leaf</p>"),
        })
        errors: list[str] = []
        urls = run(discover_via_crawl(
            session, "https://example.com", 50, __import__("time").monotonic() + 30, errors
        ))
        self.assertGreaterEqual(len(urls), 2)


class TestDiscoveryEntryPoints(unittest.TestCase):
    def test_metadata_ip_rejected_generically(self):
        with self.assertRaises(DiscoveryError) as ctx:
            run(discover_site("169.254.169.254", max_urls=10, time_budget=1))
        self.assertEqual(ctx.exception.user_message, "Cannot reach that host.")

    def test_localhost_with_port_rejected(self):
        with self.assertRaises(DiscoveryError):
            run(discover_site("localhost:6379", max_urls=10, time_budget=1))

    def test_nonstandard_port_rejected(self):
        with self.assertRaises(DiscoveryError):
            normalize_root("https://example.com:6379")

    def test_invalid_authorities_have_bounded_discovery_errors(self):
        for url in ("https://example.com:0", "https://example.com:bad",
                    "https://example.com:65536", "https://[invalid"):
            with self.subTest(url=url):
                with self.assertRaises(DiscoveryError) as ctx:
                    normalize_root(url)
                self.assertEqual(ctx.exception.user_message, "Cannot reach that host.")


if __name__ == "__main__":
    unittest.main()
