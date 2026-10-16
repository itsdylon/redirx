"""
The prober against a real HTTP server.

Hop-following is the part unit tests cannot vouch for: relative Location
headers, servers that refuse HEAD, and loops are all things aiohttp and the
socket decide, not our classifier. This spins up a local aiohttp server and
probes it for real.

The SSRF guard blocks loopback by design, so it is patched out for the
hop-shape tests and asserted separately — the point being that the guard is
what makes a local server unreachable, which is exactly the behaviour we want
in production.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import aiohttp
from aiohttp import web

from redirx import redirect_probe as rp
from redirx.safe_fetch import SSRFBlockedError


class ProbeAgainstRealServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_route("*", "/ok", self._ok)
        app.router.add_route("*", "/gone", self._gone)
        app.router.add_route("*", "/boom", self._boom)
        app.router.add_route("*", "/one-hop", self._one_hop)
        app.router.add_route("*", "/relative", self._relative)
        app.router.add_route("*", "/chain", self._chain)
        app.router.add_route("*", "/mid", self._mid)
        app.router.add_route("*", "/temp", self._temp)
        app.router.add_route("*", "/loop-a", self._loop_a)
        app.router.add_route("*", "/loop-b", self._loop_b)
        app.router.add_route("*", "/head-hostile", self._head_hostile)
        app.router.add_route("*", "/dead-target", self._dead_target)
        app.router.add_route("*", "/canonical-hop", self._canonical_hop)
        app.router.add_route("*", "/canonical-hop/", self._ok)
        app.router.add_route("*", "/slash-loop", self._slash_loop_bare)
        app.router.add_route("*", "/slash-loop/", self._slash_loop_slashed)
        app.router.add_route("*", "/params;v1", self._parameter_hop)
        app.router.add_route("*", "/params;v2", self._parameter_target)
        self.raw_requests = []

        self.head_hostile_methods = []

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{port}"

        self.session = aiohttp.ClientSession()
        # Loopback is denied by the SSRF rules; that is asserted separately.
        self._patch = mock.patch.object(rp, "validate_public_url", lambda url: None)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        await self.session.close()
        await self.runner.cleanup()

    # -- handlers ----------------------------------------------------------

    async def _ok(self, request):
        return web.Response(text="fine")

    async def _gone(self, request):
        return web.Response(status=404, text="nope")

    async def _boom(self, request):
        return web.Response(status=503, text="down")

    async def _one_hop(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/ok"})

    async def _relative(self, request):
        # Location headers are allowed to be relative and frequently are.
        return web.Response(status=301, headers={"Location": "/ok"})

    async def _chain(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/mid"})

    async def _mid(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/ok"})

    async def _temp(self, request):
        return web.Response(status=302, headers={"Location": f"{self.base}/ok"})

    async def _loop_a(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/loop-b"})

    async def _loop_b(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/loop-a"})

    async def _head_hostile(self, request):
        self.head_hostile_methods.append(request.method)
        if request.method == "HEAD":
            return web.Response(status=405)
        return web.Response(status=301, headers={"Location": f"{self.base}/ok"})

    async def _dead_target(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/gone"})

    async def _canonical_hop(self, request):
        # Stands in for the http->https / bare->www hop: the same page by our
        # target comparison, but a genuinely different request.
        return web.Response(
            status=301, headers={"Location": f"{self.base}/canonical-hop/"}
        )

    async def _slash_loop_bare(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/slash-loop/"})

    async def _slash_loop_slashed(self, request):
        return web.Response(status=301, headers={"Location": f"{self.base}/slash-loop"})

    async def _parameter_hop(self, request):
        self.raw_requests.append(request.raw_path)
        return web.Response(status=301, headers={"Location": "/params;v2?b=2&a=1"})

    async def _parameter_target(self, request):
        self.raw_requests.append(request.raw_path)
        return web.Response(status=200)

    # -- tests -------------------------------------------------------------

    async def test_plain_200_records_no_hops(self):
        result = await rp.probe(self.session, f"{self.base}/ok")
        self.assertIsNone(result.error)
        self.assertEqual(result.final_status, 200)
        self.assertEqual(result.hop_count, 0)
        self.assertEqual(
            rp.classify(result, f"{self.base}/ok")[0], rp.NO_REDIRECT
        )

    async def test_semicolon_path_and_query_order_arrive_on_the_wire(self):
        result = await rp.probe(self.session, f"{self.base}/params;v1")
        self.assertEqual(result.final_status, 200)
        self.assertIsNone(result.error)
        self.assertEqual(self.raw_requests, ['/params;v1', '/params;v2?b=2&a=1'])
        self.assertIsNone(rp.classify(result, f"{self.base}/params;v2?b=2&a=1"))

    async def test_single_hop_is_followed_to_the_destination(self):
        result = await rp.probe(self.session, f"{self.base}/one-hop")
        self.assertIsNone(result.error)
        self.assertEqual(result.hop_count, 1)
        self.assertEqual(result.final_status, 200)
        self.assertTrue(result.final_url.endswith("/ok"))
        self.assertIsNone(rp.classify(result, f"{self.base}/ok"))

    async def test_relative_location_is_resolved_against_the_current_url(self):
        result = await rp.probe(self.session, f"{self.base}/relative")
        self.assertEqual(result.final_status, 200)
        self.assertEqual(result.final_url, f"{self.base}/ok")

    async def test_a_two_hop_chain_is_reported_as_a_chain(self):
        result = await rp.probe(self.session, f"{self.base}/chain")
        self.assertEqual(result.hop_count, 2)
        self.assertEqual(rp.classify(result, f"{self.base}/ok")[0], rp.REDIRECT_CHAIN)

    async def test_302_is_reported_as_temporary(self):
        result = await rp.probe(self.session, f"{self.base}/temp")
        self.assertFalse(result.all_permanent)
        self.assertEqual(
            rp.classify(result, f"{self.base}/ok")[0], rp.TEMPORARY_REDIRECT
        )

    async def test_a_loop_terminates_instead_of_running_to_max_hops(self):
        result = await rp.probe(self.session, f"{self.base}/loop-a")
        self.assertEqual(result.error, "loop")
        self.assertLess(result.hop_count, rp.MAX_HOPS)
        self.assertEqual(rp.classify(result, None)[0], rp.REDIRECT_LOOP)

    async def test_405_on_head_retries_with_get_rather_than_reporting_breakage(self):
        result = await rp.probe(self.session, f"{self.base}/head-hostile")
        self.assertEqual(self.head_hostile_methods[:2], ["HEAD", "GET"])
        self.assertEqual(result.hop_count, 1)
        self.assertIsNone(rp.classify(result, f"{self.base}/ok"))

    async def test_404_is_reported(self):
        result = await rp.probe(self.session, f"{self.base}/gone")
        self.assertEqual(result.final_status, 404)
        self.assertEqual(rp.classify(result, f"{self.base}/ok")[0], rp.NOT_FOUND)

    async def test_5xx_is_a_server_error(self):
        result = await rp.probe(self.session, f"{self.base}/boom")
        self.assertEqual(rp.classify(result, f"{self.base}/ok")[0], rp.SERVER_ERROR)

    async def test_a_canonical_hop_is_not_a_loop(self):
        """
        Regression, measured against github.com and google.com.

        Loop detection used the target-comparison normaliser, which ignores
        scheme and `www.` by design. Every site doing http->https or
        bare->www — most of the web — came back as a redirect loop.
        """
        result = await rp.probe(self.session, f"{self.base}/canonical-hop")
        self.assertIsNone(result.error)
        self.assertEqual(result.final_status, 200)
        self.assertEqual(result.hop_count, 1)
        self.assertIsNone(rp.classify(result, f"{self.base}/canonical-hop"))

    async def test_a_genuine_slash_oscillation_is_still_a_loop(self):
        # The other half of the fix: stricter identity must not go so far that
        # a real loop stops being detected.
        result = await rp.probe(self.session, f"{self.base}/slash-loop")
        self.assertEqual(result.error, "loop")
        self.assertEqual(rp.classify(result, None)[0], rp.REDIRECT_LOOP)

    async def test_a_redirect_into_a_404_reports_the_dead_target(self):
        result = await rp.probe(self.session, f"{self.base}/dead-target")
        self.assertEqual(result.hop_count, 1)
        self.assertEqual(result.final_status, 404)
        self.assertEqual(rp.classify(result, f"{self.base}/gone")[0], rp.NOT_FOUND)


class SsrfGuardStillApplies(unittest.IsolatedAsyncioTestCase):
    """Without the patch above, the same local address must be refused."""

    async def test_loopback_is_blocked(self):
        async with aiohttp.ClientSession() as session:
            result = await rp.probe(session, "http://127.0.0.1:1/anything")
        self.assertEqual(result.error, "blocked")
        self.assertEqual(rp.classify(result, None)[0], rp.BLOCKED)

    async def test_link_local_metadata_address_is_blocked(self):
        async with aiohttp.ClientSession() as session:
            result = await rp.probe(
                session, "http://169.254.169.254/latest/meta-data/"
            )
        self.assertEqual(result.error, "blocked")

    async def test_non_http_scheme_is_blocked(self):
        async with aiohttp.ClientSession() as session:
            result = await rp.probe(session, "file:///etc/passwd")
        self.assertEqual(result.error, "blocked")


if __name__ == "__main__":
    unittest.main()
