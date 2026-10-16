"""
Tests for GSC-first ingestion: source union, tagging, and the old/new
asymmetry. Network and database are stubbed.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import Mock, patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

os.environ.setdefault("GSC_STATE_SECRET", "test-secret")

from backend.services.ingestion_service import (
    SOURCE_CRAWL,
    SOURCE_GSC,
    SOURCE_SITEMAP,
    IngestionService,
    bare_host,
    merge_sources,
    summarize,
)


def run(coro):
    return asyncio.run(coro)


def gsc(url, clicks=0, impressions=0):
    return {"url": url, "clicks": clicks, "impressions": impressions}


class TestBareHost(unittest.TestCase):
    def test_normalizes_www_and_scheme(self):
        for value in ("https://www.example.com/x", "example.com", "http://EXAMPLE.com"):
            self.assertEqual(bare_host(value), "example.com")


class TestMergeSources(unittest.TestCase):
    def test_union_tags_both_sources(self):
        entries = merge_sources(
            [gsc("https://e.com/a", 100, 500)],
            ["https://e.com/a", "https://e.com/b"],
            SOURCE_SITEMAP,
        )
        by_url = {e["url"]: e for e in entries}
        self.assertEqual(by_url["https://e.com/a"]["sources"], [SOURCE_GSC, SOURCE_SITEMAP])
        self.assertEqual(by_url["https://e.com/b"]["sources"], [SOURCE_SITEMAP])

    def test_gsc_only_urls_are_kept(self):
        # Sitemaps omit plenty of URLs that still rank; losing them would
        # defeat the point of GSC-first discovery.
        entries = merge_sources([gsc("https://e.com/orphan", 50, 200)], [], SOURCE_SITEMAP)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sources"], [SOURCE_GSC])

    def test_sitemap_only_urls_have_zero_traffic(self):
        entries = merge_sources([], ["https://e.com/new"], SOURCE_SITEMAP)
        self.assertEqual(entries[0]["clicks"], 0)
        self.assertEqual(entries[0]["sources"], [SOURCE_SITEMAP])

    def test_url_variants_collapse_and_sum_traffic(self):
        entries = merge_sources(
            [gsc("https://e.com/a", 10, 100), gsc("https://e.com/a/", 5, 50)],
            [],
            SOURCE_SITEMAP,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["clicks"], 15)
        self.assertEqual(entries[0]["impressions"], 150)

    def test_matching_is_normalized_across_sources(self):
        # http vs https for the same page must not produce two entries.
        entries = merge_sources(
            [gsc("https://e.com/a", 10)], ["http://e.com/a/"], SOURCE_SITEMAP
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(sorted(entries[0]["sources"]), [SOURCE_GSC, SOURCE_SITEMAP])

    def test_prefers_the_indexed_url_form(self):
        entries = merge_sources([gsc("https://e.com/a", 10)], ["http://e.com/a/"], SOURCE_SITEMAP)
        self.assertEqual(entries[0]["url"], "https://e.com/a")

    def test_sorted_by_traffic_desc(self):
        entries = merge_sources(
            [gsc("https://e.com/low", 1), gsc("https://e.com/high", 900)],
            ["https://e.com/none"],
            SOURCE_SITEMAP,
        )
        self.assertEqual(
            [e["url"] for e in entries],
            ["https://e.com/high", "https://e.com/low", "https://e.com/none"],
        )

    def test_crawl_source_tag_is_used(self):
        entries = merge_sources([], ["https://e.com/a"], SOURCE_CRAWL)
        self.assertEqual(entries[0]["sources"], [SOURCE_CRAWL])

    def test_gsc_image_results_are_not_mapped(self):
        # Search Console reports image URLs because they rank in Google
        # Images. On a real WordPress site six of the top "GSC-only" results
        # were /wp-content/uploads/*.jpeg — not pages, and never redirects.
        entries = merge_sources(
            [
                gsc("https://e.com/wp-content/uploads/2024/04/image-3.png", 0, 26),
                gsc("https://e.com/wp-content/uploads/2023/11/photo.jpeg", 0, 18),
                gsc("https://e.com/real-page", 40, 900),
            ],
            [],
            SOURCE_SITEMAP,
        )
        self.assertEqual([e["url"] for e in entries], ["https://e.com/real-page"])


class TestSummarize(unittest.TestCase):
    def test_counts(self):
        entries = merge_sources(
            [gsc("https://e.com/a", 100, 500), gsc("https://e.com/b", 0, 20)],
            ["https://e.com/a", "https://e.com/c"],
            SOURCE_SITEMAP,
        )
        s = summarize(entries)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["with_traffic"], 2)      # a (clicks), b (impressions)
        self.assertEqual(s["gsc_only"], 1)          # b: in GSC but not the sitemap
        self.assertEqual(s["no_recorded_traffic"], 1)  # c: sitemap only
        self.assertEqual(s["total_clicks"], 100)


class FakeDiscovery:
    def __init__(self, urls, method="sitemap", robots_blocked=False):
        self.urls = urls
        self.method = method
        self.root_url = "https://e.com"
        self.generator = None
        self.rate_limited = False
        self.retry_after_seconds = 0
        self.robots_blocked = robots_blocked


class TestIngestSideAsymmetry(unittest.TestCase):
    def _service(self, gsc_rows):
        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )
        svc.gsc_urls_for_domain = lambda user_id, domain, **kwargs: (
            gsc_rows, "sc-domain:e.com"
        )
        svc.capture_baseline = lambda *a, **k: "baseline-1"
        return svc

    def test_old_side_uses_gsc(self):
        svc = self._service([gsc("https://e.com/ranked", 300, 900)])
        with patch(
            "backend.services.ingestion_service.discover_site",
            side_effect=_async(FakeDiscovery(["https://e.com/other"])),
        ):
            out = run(svc.ingest_side("u1", "e.com", "old", 100, 5))
        self.assertEqual(out["gsc_url_count"], 1)
        self.assertTrue(out["baseline_id"])
        urls = [e["url"] for e in out["entries"]]
        self.assertEqual(urls[0], "https://e.com/ranked")  # traffic leads

    def test_explicit_opt_out_skips_gsc_construction_enrichment_and_baseline(self):
        svc = IngestionService(connection_db=object(), baseline_db=object())
        with patch("backend.services.ingestion_service.GSCService") as constructor, patch.object(
            svc, "gsc_urls_for_domain", wraps=svc.gsc_urls_for_domain
        ) as enrich, patch.object(svc, "capture_baseline", wraps=svc.capture_baseline) as baseline, patch(
            "backend.services.ingestion_service.discover_site",
            side_effect=_async(FakeDiscovery(["https://e.com/Only%2FPage?q=A&b=2"])),
        ) as discovery:
            out = run(svc.ingest_side("u1", "e.com", "old", 100, 5,
                                     gsc_property_override="sc-domain:e.com", use_gsc=False))
        self.assertEqual(constructor.call_count, 0)
        self.assertEqual(enrich.call_count, 0)
        self.assertEqual(baseline.call_count, 0)
        self.assertEqual(discovery.call_count, 1)
        self.assertFalse(discovery.call_args.kwargs["owner_verified"])
        self.assertEqual(out["gsc_url_count"], 0)
        self.assertIsNone(out["baseline_id"])
        self.assertEqual([e["url"] for e in out["entries"]], ["https://e.com/Only%2FPage?q=A&b=2"])

    def test_opt_out_and_default_with_known_traffic(self):
        svc = self._service([gsc("https://e.com/ranked", 300, 900)])
        svc.gsc_urls_for_domain = Mock(wraps=svc.gsc_urls_for_domain)
        svc.capture_baseline = Mock(wraps=svc.capture_baseline)
        with patch("backend.services.ingestion_service.discover_site", side_effect=_async(FakeDiscovery(["https://e.com/page"]))):
            enabled = run(svc.ingest_side("u1", "e.com", "old", 100, 5))
            disabled = run(svc.ingest_side("u1", "e.com", "old", 100, 5, use_gsc=False))
        self.assertEqual(svc.gsc_urls_for_domain.call_count, 1)
        self.assertEqual(svc.capture_baseline.call_count, 1)
        self.assertEqual(enabled["summary"]["total_clicks"], 300)
        self.assertEqual(disabled["summary"]["total_clicks"], 0)
        self.assertEqual(len(enabled["entries"]), 2)
        self.assertEqual(len(disabled["entries"]), 1)

    def test_rejects_non_boolean_gsc_flag_before_discovery(self):
        svc = self._service([])
        with patch("backend.services.ingestion_service.discover_site") as discovery:
            for flag in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(flag=flag), self.assertRaises(ValueError):
                    run(svc.ingest_side("u1", "e.com", "old", 100, 5, use_gsc=flag))
        discovery.assert_not_called()

    def test_new_side_skips_gsc_entirely(self):
        # New URLs aren't indexed, so querying GSC would cost a round trip to
        # learn nothing — and must not capture a baseline for the new domain.
        called = {"gsc": False}

        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )

        def _should_not_run(*a, **k):
            called["gsc"] = True
            return [], None

        svc.gsc_urls_for_domain = _should_not_run
        with patch(
            "backend.services.ingestion_service.discover_site",
            side_effect=_async(FakeDiscovery(["https://e.com/new-a"])),
        ):
            out = run(svc.ingest_side("u1", "e.com", "new", 100, 5))
        self.assertFalse(called["gsc"])
        self.assertEqual(out["gsc_url_count"], 0)
        self.assertIsNone(out["baseline_id"])

    def test_respects_max_urls_and_reports_truncation(self):
        svc = self._service([gsc(f"https://e.com/p{i}", 100 - i) for i in range(10)])
        with patch(
            "backend.services.ingestion_service.discover_site",
            side_effect=_async(FakeDiscovery([])),
        ):
            out = run(svc.ingest_side("u1", "e.com", "old", 4, 5))
        self.assertEqual(len(out["entries"]), 4)
        self.assertTrue(out["truncated"])


class FakeGscService:
    def __init__(self, properties, fail=False):
        self.properties = properties
        self.fail = fail

    def list_properties(self, user_id):
        if self.fail:
            raise RuntimeError("GSC unreachable")
        return [{"site_url": p, "permission_level": "siteOwner"} for p in self.properties]

    def query_search_analytics(self, user_id, site_url, lookback_days=None):
        return []


class FakeConnectionDB:
    def __init__(self, connected=True):
        self.connected = connected

    def get_connection(self, user_id):
        return {"id": "conn-1"} if self.connected else None


class TestGscPropertyOverride(unittest.TestCase):
    """
    The property can be chosen in the UI when auto-detection cannot decide —
    an agency with several verified properties, or a host covered by both a
    domain and a URL-prefix property. It arrives from the client, so it is
    honoured only when the user actually has it verified.
    """

    def _service(self, properties, connected=True, fail=False):
        return IngestionService(
            gsc_service=FakeGscService(properties, fail=fail),
            connection_db=FakeConnectionDB(connected),
            baseline_db=object(),
        )

    def test_verified_property_is_honoured(self):
        svc = self._service(["sc-domain:mine.com", "https://other.com/"])
        self.assertEqual(
            svc.verified_property("u1", "sc-domain:mine.com"), "sc-domain:mine.com"
        )

    def test_unverified_property_is_refused(self):
        # Never query a property this user has no permission on.
        svc = self._service(["sc-domain:mine.com"])
        self.assertIsNone(svc.verified_property("u1", "sc-domain:someone-elses.com"))

    def test_no_property_requested(self):
        svc = self._service(["sc-domain:mine.com"])
        self.assertIsNone(svc.verified_property("u1", None))
        self.assertIsNone(svc.verified_property("u1", ""))

    def test_disconnected_user_has_no_verified_properties(self):
        svc = self._service(["sc-domain:mine.com"], connected=False)
        self.assertIsNone(svc.verified_property("u1", "sc-domain:mine.com"))

    def test_lookup_failure_degrades_to_none(self):
        # GSC is an enhancement; a lookup failure must not raise into ingestion.
        svc = self._service(["sc-domain:mine.com"], fail=True)
        self.assertIsNone(svc.verified_property("u1", "sc-domain:mine.com"))

    def test_override_beats_auto_detection(self):
        svc = self._service(["sc-domain:chosen.com"])
        svc.find_property = lambda user_id, domain: "sc-domain:auto.com"
        rows, used = svc.gsc_urls_for_domain(
            "u1", "chosen.com", gsc_property="sc-domain:chosen.com"
        )
        self.assertEqual(used, "sc-domain:chosen.com")

    def test_unverified_override_falls_back_to_auto_detection(self):
        svc = self._service(["sc-domain:mine.com"])
        svc.find_property = lambda user_id, domain: "sc-domain:auto.com"
        _, used = svc.gsc_urls_for_domain(
            "u1", "e.com", gsc_property="sc-domain:someone-elses.com"
        )
        self.assertEqual(used, "sc-domain:auto.com")

    def test_override_reaches_the_gsc_lookup(self):
        seen = {}
        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )

        def _capture(user_id, domain, **kwargs):
            seen["gsc_property"] = kwargs.get("gsc_property")
            return [], None

        svc.gsc_urls_for_domain = _capture
        with patch(
            "backend.services.ingestion_service.discover_site",
            side_effect=_async(FakeDiscovery(["https://e.com/a"])),
        ):
            run(svc.ingest_side(
                "u1", "e.com", "old", 100, 5,
                gsc_property_override="sc-domain:picked.com",
            ))
        self.assertEqual(seen["gsc_property"], "sc-domain:picked.com")


class TestOwnershipLiftsRobots(unittest.TestCase):
    """
    A verified Search Console property proves the user owns the domain, which
    lifts robots.txt Disallow on the link-following crawl. robots.txt asks
    crawlers to stay out; an owner reading their own site to plan its
    migration is not the visitor it is addressing.
    """

    def _run(self, gsc_property):
        seen = {}

        async def _capture(domain, **kwargs):
            seen["owner_verified"] = kwargs.get("owner_verified")
            return FakeDiscovery(["https://e.com/a"])

        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )
        svc.gsc_urls_for_domain = lambda user_id, domain, **kw: (
            ([gsc("https://e.com/x", 5)], gsc_property) if gsc_property else ([], None)
        )
        svc.capture_baseline = lambda *a, **k: "baseline-1"
        with patch("backend.services.ingestion_service.discover_site", _capture):
            run(svc.ingest_side("u1", "e.com", "old", 100, 5))
        return seen["owner_verified"]

    def test_verified_property_marks_the_user_as_owner(self):
        self.assertTrue(self._run("sc-domain:e.com"))

    def test_no_property_means_robots_still_applies(self):
        self.assertFalse(self._run(None))

    def test_new_side_is_never_treated_as_owner(self):
        # The new side skips GSC entirely, so there is no ownership proof.
        seen = {}

        async def _capture(domain, **kwargs):
            seen["owner_verified"] = kwargs.get("owner_verified")
            return FakeDiscovery(["https://e.com/a"])

        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )
        with patch("backend.services.ingestion_service.discover_site", _capture):
            run(svc.ingest_side("u1", "e.com", "new", 100, 5))
        self.assertFalse(seen["owner_verified"])

    def test_robots_block_is_reported_upward(self):
        async def _blocked(domain, **kwargs):
            return FakeDiscovery([], method="none", robots_blocked=True)

        svc = IngestionService(
            gsc_service=object(), connection_db=object(), baseline_db=object()
        )
        svc.gsc_urls_for_domain = lambda user_id, domain, **kw: ([], None)
        with patch("backend.services.ingestion_service.discover_site", _blocked):
            out = run(svc.ingest_side("u1", "e.com", "old", 100, 5))
        self.assertTrue(out["robots_blocked"])
        self.assertEqual(out["entries"], [])



def _async(value):
    """Coroutine factory for patching an async function."""
    async def _coro(*args, **kwargs):
        return value
    return _coro


if __name__ == "__main__":
    unittest.main()
