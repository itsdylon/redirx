"""Actual loopback parser failures and request identity, without provider calls.

The library patch is covered by upstream parser tests. These cases exercise our
public callers against bounded malformed responses and ensure failures cannot be
reported as successful discovery or redirect verification.
"""
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

import aiohttp
from src.redirx import redirect_probe as probe
from backend.services.migration_discovery_service import SafeDiscoveryFetcher, FetchFailure


class NoLimit:
    async def acquire(self, _url): pass
    def note_response(self, _status, _retry): return False
    async def record_success(self, _url): pass
    async def record_failure(self, _url, _retry): pass


class HTTPClientSecurityTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def origin(self, response):
        requests = []
        async def handle(reader, writer):
            try:
                request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
                requests.append(request.split(b'\r\n', 1)[0])
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', requests
        finally:
            server.close()
            await server.wait_closed()

    async def test_malformed_chunked_discovery_fails_closed(self):
        # Invalid chunk size ends exactly at the offending byte. Keep the payload
        # tiny; no memory-exhaustion or remote exploit payload is sent.
        wire = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0_'
        async with self.origin(wire) as (root, requests):
            fetcher = SafeDiscoveryFetcher(connector_factory=aiohttp.TCPConnector,
                validator=lambda _: None, limiter=NoLimit(), pace_seconds=0)
            with self.assertRaises(FetchFailure) as caught:
                await fetcher.fetch(root + '/sitemap.xml', {root})
            self.assertEqual(caught.exception.code, 'origin_unavailable')
            self.assertEqual(requests, [b'GET /sitemap.xml HTTP/1.1'])

    async def test_excessive_response_headers_are_not_a_successful_probe(self):
        wire = b'HTTP/1.1 200 OK\r\n' + b'X-Fixture: x\r\n' * 130 + b'Content-Length: 0\r\n\r\n'
        async with self.origin(wire) as (root, _), aiohttp.ClientSession() as session:
            with patch.object(probe, 'validate_public_url', lambda _: None), patch.object(probe, 'get_limiter', return_value=None):
                result = await probe.probe(session, root + '/many-headers')
            self.assertEqual(result.error, 'connection')
            self.assertIsNone(result.final_status)
            self.assertEqual(probe.classify(result, root + '/target')[0], probe.UNREACHABLE)

    async def test_malformed_status_does_not_poison_the_next_probe(self):
        async with self.origin(b'HTTP/1.1 X\r\n\r\n') as (bad, _), self.origin(
                b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n') as (good, _), aiohttp.ClientSession() as session:
            with patch.object(probe, 'validate_public_url', lambda _: None), patch.object(probe, 'get_limiter', return_value=None):
                first = await probe.probe(session, bad + '/bad')
                second = await probe.probe(session, good + '/good')
            self.assertEqual(first.error, 'connection')
            self.assertIsNone(second.error)
            self.assertEqual(second.final_status, 200)

    async def test_request_identity_matches_wire_or_is_explicitly_unverified(self):
        async with self.origin(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n') as (root, requests), aiohttp.ClientSession() as session:
            with patch.object(probe, 'validate_public_url', lambda _: None), patch.object(probe, 'get_limiter', return_value=None):
                for target in ['/Case%2FPath?x=2&x=1', '/params;v1?A=1&A=2', '/plain?x=?']:
                    with self.subTest(target=target):
                        result = await probe.probe(session, root + target)
                        self.assertIsNone(result.error)
                        self.assertEqual(requests[-1], f'HEAD {target} HTTP/1.1'.encode())
                before = len(requests)
                for target in ['/Case%2fPath', '/%41', '/plain?', '/a/../b']:
                    with self.subTest(target=target):
                        result = await probe.probe(session, root + target)
                        self.assertEqual(result.error, probe.REQUEST_URL_REWRITTEN)
                self.assertEqual(len(requests), before)
