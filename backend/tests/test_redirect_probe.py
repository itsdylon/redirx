"""
Redirect probing and classification.

The classifier decides what a customer gets emailed about, so the failure that
matters most is not a missed problem but a false one: a monitor that reports
working redirects as broken gets muted, and then it reports nothing. Most of
these tests pin down cases that *must not* be flagged.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from redirx import redirect_probe as rp


def result(url="https://old.com/a", hops=(), status=200, final=None, error=None):
    """Build a ProbeResult from (status, location) pairs."""
    r = rp.ProbeResult(url=url)
    current = url
    for hop_status, location in hops:
        r.hops.append(rp.Hop(url=current, status=hop_status, location=location))
        current = location
    r.final_status = status
    r.final_url = final if final is not None else current
    r.error = error
    return r


class TestNormalization(unittest.TestCase):
    def test_trailing_slash_is_not_a_different_page(self):
        self.assertTrue(rp.same_target("https://n.com/a", "https://n.com/a/"))

    def test_scheme_upgrade_is_not_a_different_page(self):
        # Otherwise every HSTS site reports 100% wrong targets.
        self.assertTrue(rp.same_target("http://n.com/a", "https://n.com/a"))

    def test_www_prefix_is_not_a_different_page(self):
        self.assertTrue(rp.same_target("https://www.n.com/a", "https://n.com/a"))

    def test_host_case_is_ignored_but_path_case_is_not(self):
        self.assertTrue(rp.same_target("https://N.com/a", "https://n.com/a"))
        self.assertFalse(rp.same_target("https://n.com/A", "https://n.com/a"))

    def test_query_string_distinguishes_pages(self):
        self.assertFalse(rp.same_target("https://n.com/a?p=1", "https://n.com/a?p=2"))

    def test_raw_params_empty_query_and_encoding_distinguish_targets(self):
        for first, second in [("/a;v1", "/a;v2"), ("/a;", "/a"),
                              ("/a?", "/a"), ("/a%2Fb", "/a/b"),
                              ("/a?q=%41", "/a?q=A")]:
            with self.subTest(first=first, second=second):
                self.assertFalse(rp.same_target('https://n.com'+first, 'https://n.com'+second))

    def test_root_path_keeps_its_slash(self):
        self.assertTrue(rp.same_target("https://n.com", "https://n.com/"))

    def test_different_paths_are_different(self):
        self.assertFalse(rp.same_target("https://n.com/a", "https://n.com/b"))

    def test_empty_never_matches(self):
        self.assertFalse(rp.same_target("", "https://n.com/a"))
        self.assertFalse(rp.same_target("https://n.com/a", ""))

    def test_port_identity_preserves_standard_upgrades_and_distinct_services(self):
        for first, second in [('http://www.n.com:80/a/', 'https://n.com:443/a'),
                              ('http://n.com:80/a', 'http://n.com/a'),
                              ('https://n.com:443/a', 'https://n.com/a'),
                              ('https://[2001:4860:4860::8888]:443/a', 'https://[2001:4860:4860::8888]/a')]:
            with self.subTest(first=first, second=second):
                self.assertTrue(rp.same_target(first, second))
        for first, second in [('https://n.com:8443/a', 'https://n.com:9443/a'),
                              ('https://n.com:8443/a', 'https://n.com/a'),
                              ('https://n.com:80/a', 'https://n.com:443/a'),
                              ('http://n.com:443/a', 'https://n.com:443/a')]:
            with self.subTest(first=first, second=second):
                self.assertFalse(rp.same_target(first, second))
        for malformed in ['https://n.com:bad/a', 'https://n.com:65536/a', 'https://[broken/a']:
            self.assertFalse(rp.same_target(malformed, malformed))


class TestVisitIdentity(unittest.TestCase):
    """
    Loop detection identity, kept strictly separate from target comparison.

    Regression: loop detection originally reused normalize_for_compare, which
    drops scheme and `www.` on purpose. That made every `http://x` ->
    `https://x` and every bare -> www redirect look like a loop — measured
    against github.com and google.com, both were reported as broken loops.
    """

    def test_scheme_upgrade_is_two_distinct_requests(self):
        self.assertNotEqual(
            rp.visit_identity("http://x.com/"), rp.visit_identity("https://x.com/")
        )

    def test_www_hop_is_two_distinct_requests(self):
        self.assertNotEqual(
            rp.visit_identity("http://x.com/"), rp.visit_identity("http://www.x.com/")
        )

    def test_trailing_slash_is_a_distinct_request(self):
        # /a -> /a/ is a real hop, and /a/ -> /a would be a real loop.
        self.assertNotEqual(
            rp.visit_identity("https://x.com/a"), rp.visit_identity("https://x.com/a/")
        )

    def test_host_case_is_the_same_request(self):
        self.assertEqual(
            rp.visit_identity("https://X.com/a"), rp.visit_identity("https://x.com/a")
        )

    def test_default_port_is_the_same_request(self):
        self.assertEqual(
            rp.visit_identity("https://x.com:443/a"), rp.visit_identity("https://x.com/a")
        )
        self.assertEqual(
            rp.visit_identity("http://x.com:80/a"), rp.visit_identity("http://x.com/a")
        )

    def test_non_default_port_is_a_distinct_request(self):
        self.assertNotEqual(
            rp.visit_identity("https://x.com:8443/a"), rp.visit_identity("https://x.com/a")
        )

    def test_fragment_is_not_part_of_a_request(self):
        self.assertEqual(
            rp.visit_identity("https://x.com/a#top"), rp.visit_identity("https://x.com/a")
        )

    def test_query_distinguishes_requests(self):
        self.assertNotEqual(
            rp.visit_identity("https://x.com/a?p=1"), rp.visit_identity("https://x.com/a?p=2")
        )

    def test_identity_is_stricter_than_target_comparison(self):
        # The invariant behind the bug: same page, different request.
        a, b = "http://x.com/page", "https://www.x.com/page/"
        self.assertTrue(rp.same_target(a, b))
        self.assertNotEqual(rp.visit_identity(a), rp.visit_identity(b))

    def test_raw_identity_preserves_semicolons_queries_and_ipv6_authority(self):
        for url in ['https://x.com/a;v1', 'https://x.com/a;', 'https://x.com/a?',
                    'https://x.com/a?q=?', 'https://x.com/Case%2FPath?q=%41&b=2',
                    'https://[2001:4860:4860::8888]/a',
                    'https://[2001:4860:4860::8888]:8443/a']:
            with self.subTest(url=url):
                self.assertEqual(rp.visit_identity(url), url)
        self.assertEqual(rp.visit_identity('https://[2001:4860:4860::8888]:443/a'),
                         'https://[2001:4860:4860::8888]/a')
        self.assertEqual(rp.visit_identity('https://x.com/a#fragment?'), 'https://x.com/a')


class TestCorrectRedirectsAreNotFlagged(unittest.TestCase):
    """The most damaging bug in a monitor is the false positive."""

    def test_single_permanent_hop_to_the_approved_target_is_clean(self):
        r = result(hops=[(301, "https://new.com/b")], status=200)
        self.assertIsNone(rp.classify(r, "https://new.com/b"))

    def test_308_counts_as_permanent(self):
        r = result(hops=[(308, "https://new.com/b")], status=200)
        self.assertIsNone(rp.classify(r, "https://new.com/b"))

    def test_target_differing_only_by_trailing_slash_is_clean(self):
        r = result(hops=[(301, "https://new.com/b/")], status=200)
        self.assertIsNone(rp.classify(r, "https://new.com/b"))

    def test_no_expected_url_still_passes_a_working_redirect(self):
        # An old URL the migration never mapped: we can see it redirects
        # somewhere and returns 200, and we have no basis to call that wrong.
        r = result(hops=[(301, "https://new.com/somewhere")], status=200)
        self.assertIsNone(rp.classify(r, None))


class TestBrokenRedirects(unittest.TestCase):
    def test_200_with_no_redirect_means_the_rule_never_shipped(self):
        r = result(status=200)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.NO_REDIRECT)

    def test_404_is_reported(self):
        r = result(status=404)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.NOT_FOUND)

    def test_410_is_reported_as_not_found(self):
        r = result(status=410)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.NOT_FOUND)

    def test_500_is_a_server_error(self):
        r = result(status=500)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.SERVER_ERROR)

    def test_redirect_landing_on_404_is_not_found_not_wrong_target(self):
        # Right destination, dead page. Reporting "wrong target" would send the
        # user hunting through their redirect rules for a bug that isn't there.
        r = result(hops=[(301, "https://new.com/b")], status=404)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.NOT_FOUND)

    def test_landing_elsewhere_is_a_wrong_target(self):
        r = result(hops=[(301, "https://new.com/elsewhere")], status=200)
        issue, detail = rp.classify(r, "https://new.com/b")
        self.assertEqual(issue, rp.WRONG_TARGET)
        self.assertIn("elsewhere", detail)

    def test_catch_all_to_homepage_is_a_wrong_target(self):
        # The single most common real failure: a blanket rule to "/" that looks
        # healthy in every uptime checker because it returns 200.
        r = result(hops=[(301, "https://new.com/")], status=200)
        self.assertEqual(rp.classify(r, "https://new.com/deep/page")[0], rp.WRONG_TARGET)

    def test_302_to_the_right_place_is_unsuitable_for_a_permanent_move(self):
        r = result(hops=[(302, "https://new.com/b")], status=200)
        issue, detail = rp.classify(r, "https://new.com/b")
        self.assertEqual(issue, rp.TEMPORARY_REDIRECT)
        self.assertIn("302", detail)

    def test_307_is_temporary_too(self):
        r = result(hops=[(307, "https://new.com/b")], status=200)
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.TEMPORARY_REDIRECT)

    def test_multiple_permanent_hops_are_a_chain(self):
        r = result(
            hops=[(301, "https://new.com/mid"), (301, "https://new.com/b")], status=200
        )
        issue, detail = rp.classify(r, "https://new.com/b")
        self.assertEqual(issue, rp.REDIRECT_CHAIN)
        self.assertIn("2 hops", detail)

    def test_a_temporary_hop_anywhere_in_a_chain_outranks_the_chain_itself(self):
        # Permanent-move policy prioritizes the temporary status over extra hops.
        r = result(
            hops=[(301, "https://new.com/mid"), (302, "https://new.com/b")], status=200
        )
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.TEMPORARY_REDIRECT)

    def test_wrong_target_outranks_chain_length(self):
        r = result(
            hops=[(301, "https://new.com/mid"), (301, "https://new.com/nope")], status=200
        )
        self.assertEqual(rp.classify(r, "https://new.com/b")[0], rp.WRONG_TARGET)


class TestProbeErrors(unittest.TestCase):
    def test_loop(self):
        self.assertEqual(
            rp.classify(result(error="loop"), "https://new.com/b")[0], rp.REDIRECT_LOOP
        )

    def test_too_many_hops_reads_as_a_loop(self):
        self.assertEqual(
            rp.classify(result(error="too_many_hops"), None)[0], rp.REDIRECT_LOOP
        )

    def test_ssrf_block_is_its_own_category(self):
        # Not "unreachable": the site is up, it pointed us at a private range.
        self.assertEqual(rp.classify(result(error="blocked"), None)[0], rp.BLOCKED)

    def test_timeout_and_connection_are_unreachable(self):
        self.assertEqual(rp.classify(result(error="timeout"), None)[0], rp.UNREACHABLE)
        self.assertEqual(
            rp.classify(result(error="connection"), None)[0], rp.UNREACHABLE
        )

    def test_circuit_open_is_unreachable_not_a_site_fault(self):
        self.assertEqual(
            rp.classify(result(error="circuit_open"), None)[0], rp.UNREACHABLE
        )


class TestTaxonomyCompleteness(unittest.TestCase):
    """A new issue type must not reach a customer without wording or a severity."""

    def test_every_issue_type_has_a_severity_and_a_description(self):
        types = {
            rp.NO_REDIRECT, rp.NOT_FOUND, rp.SERVER_ERROR, rp.WRONG_TARGET,
            rp.REDIRECT_LOOP, rp.REDIRECT_CHAIN, rp.TEMPORARY_REDIRECT,
            rp.UNREACHABLE, rp.BLOCKED,
        }
        self.assertEqual(set(rp.SEVERITY), types)
        self.assertEqual(set(rp.DESCRIPTIONS), types)

    def test_severities_are_known_values(self):
        self.assertTrue(set(rp.SEVERITY.values()) <= {rp.CRITICAL, rp.WARNING})


class TestProbeResultShape(unittest.TestCase):
    def test_all_permanent_is_true_for_an_empty_chain(self):
        # No redirects means nothing impermanent happened.
        self.assertTrue(result().all_permanent)

    def test_hop_count_tracks_the_chain(self):
        r = result(hops=[(301, "https://n.com/x"), (301, "https://n.com/y")])
        self.assertEqual(r.hop_count, 2)


class TestOfflineRequestIdentity(unittest.IsolatedAsyncioTestCase):
    """Real probe orchestration and SSRF validation; requests are local stubs."""

    async def run_chain(self, start, responses):
        request = AsyncMock(side_effect=[SimpleNamespace(status=status,
            headers={'Location': location} if location else {}, release=Mock())
            for status, location in responses])
        with patch.object(rp, '_request', request), patch.object(rp, 'get_limiter', return_value=None):
            observed = await rp.probe(object(), start)
        return observed, request

    async def test_parameter_change_reaches_second_request_and_records_exact_target(self):
        observed, request = await self.run_chain('https://x.com/a;v1', [(301, '/a;v2'), (200, None)])
        self.assertIsNone(observed.error)
        self.assertEqual(observed.final_url, 'https://x.com/a;v2')
        self.assertEqual([c.args[2] for c in request.await_args_list], ['https://x.com/a;v1', 'https://x.com/a;v2'])
        self.assertIsNone(rp.classify(observed, 'https://x.com/a;v2'))
        self.assertEqual(rp.classify(observed, 'https://x.com/a;v1')[0], rp.WRONG_TARGET)

    async def test_real_parameter_oscillation_still_detects_loop(self):
        observed, request = await self.run_chain('https://x.com/a;v1', [(301, '/a;v2'), (301, '/a;v1')])
        self.assertEqual(observed.error, 'loop')
        self.assertEqual(request.await_count, 2)

    async def test_client_rewrites_are_unverified_without_request_or_status(self):
        for suffix in ['/a?', '/%41', '/a/../b', '/a%2fb', '/a?q=%41', '/café']:
            with self.subTest(suffix=suffix):
                observed, request = await self.run_chain('https://x.com'+suffix, [])
                self.assertEqual(observed.error, rp.REQUEST_URL_REWRITTEN)
                self.assertIsNone(observed.final_status)
                self.assertEqual(request.await_count, 0)
                self.assertEqual(rp.classify(observed, None), (rp.UNREACHABLE, rp.REQUEST_URL_REWRITTEN_DETAIL))

    async def test_rewritten_redirect_target_records_only_first_observation(self):
        observed, request = await self.run_chain('https://x.com/a', [(301, '/b?')])
        self.assertEqual(observed.error, rp.REQUEST_URL_REWRITTEN)
        self.assertEqual(observed.final_url, 'https://x.com/b?')
        self.assertIsNone(observed.final_status)
        self.assertEqual(observed.hop_count, 1)
        self.assertEqual(request.await_count, 1)

    async def test_exact_reserved_encoding_queries_and_fragments_still_request(self):
        raw = 'https://x.com/Case%2FPath;q=1?b=2&a=1#fragment?'
        observed, request = await self.run_chain(raw, [(200, None)])
        self.assertIsNone(observed.error)
        self.assertEqual(observed.final_status, 200)
        self.assertEqual(request.await_count, 1)
        self.assertEqual(request.await_args.args[2], raw)

    async def test_ssrf_revalidation_precedes_fidelity_and_stops_redirect(self):
        for target in ['http://169.254.169.254/a?', 'http://[::1]/a;', 'https://x.com:8443/a']:
            with self.subTest(target=target):
                observed, request = await self.run_chain('https://x.com/a;v1', [(301, target)])
                self.assertEqual(observed.error, 'blocked')
                self.assertEqual(request.await_count, 1)

    async def test_invalid_authorities_and_ports_are_bounded_without_requests(self):
        for target in ['https://x.com:bad/a', 'https://x.com:65536/a', 'https://x.com:-1/a', 'https://[broken/a']:
            with self.subTest(target=target):
                observed, request = await self.run_chain(target, [])
                self.assertEqual(observed.error, 'invalid_url')
                self.assertIsNone(observed.final_status)
                self.assertEqual(request.await_count, 0)
                self.assertEqual(rp.classify(observed, None)[0], rp.UNREACHABLE)
                self.assertIn('Correct the URL and retry', rp.classify(observed, None)[1])

    async def test_invalid_redirect_authorities_are_bounded_after_one_observation(self):
        for target in ['https://x.com:bad/a', 'https://[broken/a']:
            with self.subTest(target=target):
                observed, request = await self.run_chain('https://x.com/start', [(301, target)])
                self.assertEqual(observed.error, 'invalid_url')
                self.assertIsNone(observed.final_status)
                self.assertEqual(observed.hop_count, 1)
                self.assertEqual(request.await_count, 1)

    async def test_empty_query_locations_do_not_inherit_previous_query(self):
        for location in ['?', '?#fragment', '/next?', 'https://x.com/next?']:
            with self.subTest(location=location):
                observed, request = await self.run_chain('https://x.com/a?old=1', [(301, location)])
                self.assertEqual(observed.error, rp.REQUEST_URL_REWRITTEN)
                self.assertNotIn('old=1', observed.final_url)
                self.assertTrue(observed.final_url.split('#', 1)[0].endswith('?'))
                self.assertEqual(request.await_count, 1)

    async def test_relative_location_evidence_is_the_resolved_request_target(self):
        observed, request = await self.run_chain('https://x.com/dir/a?old=1',
                                                [(301, '../next?q=2'), (200, None)])
        self.assertIsNone(observed.error)
        self.assertEqual(observed.hops[0].location, '../next?q=2')
        self.assertEqual(observed.final_url, 'https://x.com/next?q=2')
        self.assertEqual(request.await_args.args[2], 'https://x.com/next?q=2')

    def test_unrepresentable_expected_target_is_not_wrong_target_or_pass(self):
        observed = result(hops=[(301, 'https://x.com/a')])
        self.assertEqual(rp.classify(observed, 'https://x.com/a?'),
                         (rp.UNREACHABLE, rp.REQUEST_URL_REWRITTEN_DETAIL))

    def test_artifact_verification_marks_rewritten_source_and_expected_target_unchecked(self):
        from backend.services.migration_verification_service import assess_probe
        cases = [
            (result(final='https://x.com/a?', status=None, error=rp.REQUEST_URL_REWRITTEN), 'https://x.com/b'),
            (result(hops=[(301, 'https://x.com/a')]), 'https://x.com/a?'),
            (result(hops=[(301, 'https://x.com/A')]), 'https://x.com/%41'),
        ]
        for observed, expected in cases:
            with self.subTest(expected=expected):
                state, finding = assess_probe(observed, expected)
                self.assertEqual(state, 'unchecked')
                self.assertEqual(finding['measurement'], 'unavailable')
                self.assertEqual(finding['issue'], rp.REQUEST_URL_REWRITTEN)
                self.assertEqual(finding['detail'], rp.REQUEST_URL_REWRITTEN_DETAIL)
        state, finding = assess_probe(result(hops=[(301, 'https://x.com/a;v2')]), 'https://x.com/a;v2')
        self.assertEqual(state, 'passed')
        self.assertEqual(finding['measurement'], 'observed')
        self.assertEqual(assess_probe(result(hops=[(301, 'https://x.com/a;v2')]), 'https://x.com/a;v1')[0], 'failed')


if __name__ == "__main__":
    unittest.main()
