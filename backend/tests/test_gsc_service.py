"""
Unit tests for the Google Search Console integration helpers.

Covers the pure logic only (URL normalization, property matching, risk
summary, OAuth state tokens) — no network or database access.
"""
import sys
import os
import unittest
from unittest.mock import patch

# Add parent directory to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.services.gsc_service import (
    GSCError,
    create_state_token,
    normalize_url,
    pick_property_for_urls,
    property_matches_host,
    verify_state_token,
)
from backend.services.results_formatter import (
    compute_risk_summary,
    format_results_response,
)


class TestNormalizeUrl(unittest.TestCase):
    """
    normalize_url is an identity key for "same page", not a display form:
    scheme and a leading www. are excluded so a sitemap listing http:// or the
    bare host matches what Search Console reports.
    """

    def test_strips_trailing_slash_and_lowercases_host(self):
        # Path case is preserved — paths are case-sensitive on many servers.
        self.assertEqual(normalize_url("HTTPS://Example.com/Path/"), "example.com/Path")

    def test_drops_query_and_fragment(self):
        self.assertEqual(normalize_url("https://example.com/page?utm=1#top"), "example.com/page")

    def test_scheme_and_www_insensitive(self):
        # Regression: keying on these split one page's traffic across two
        # entries when unioning GSC with sitemap results.
        variants = [
            "https://example.com/about",
            "http://example.com/about",
            "https://www.example.com/about/",
            "http://WWW.Example.com/about",
        ]
        self.assertEqual(len({normalize_url(v) for v in variants}), 1)

    def test_distinct_pages_stay_distinct(self):
        self.assertNotEqual(normalize_url("https://e.com/a"), normalize_url("https://e.com/b"))
        self.assertNotEqual(
            normalize_url("https://e.com/a"), normalize_url("https://blog.e.com/a")
        )

    def test_variants_collapse_to_same_key(self):
        variants = [
            "https://example.com/about",
            "https://example.com/about/",
            "https://EXAMPLE.com/about",
        ]
        self.assertEqual(len({normalize_url(v) for v in variants}), 1)


class TestPropertyMatching(unittest.TestCase):
    def test_domain_property_covers_subdomains(self):
        self.assertTrue(property_matches_host("sc-domain:example.com", "example.com"))
        self.assertTrue(property_matches_host("sc-domain:example.com", "www.example.com"))
        self.assertFalse(property_matches_host("sc-domain:example.com", "notexample.com"))

    def test_url_prefix_property_is_www_insensitive(self):
        # Verified against a real property: dreamhomestudio.com's only GSC
        # property is the www URL-prefix form. Requiring an exact host match
        # meant find_property returned None and GSC discovery silently never
        # activated — the feature looked fine while always falling back to
        # sitemap-only.
        for host in ("www.example.com", "example.com"):
            self.assertTrue(property_matches_host("https://www.example.com/", host), host)
        for host in ("www.example.com", "example.com"):
            self.assertTrue(property_matches_host("https://example.com/", host), host)

    def test_url_prefix_property_rejects_other_hosts(self):
        self.assertFalse(property_matches_host("https://www.example.com/", "other.com"))
        # A subdomain is a genuinely different property, not a www variant.
        self.assertFalse(property_matches_host("https://www.example.com/", "blog.example.com"))

    def test_picks_property_covering_most_urls(self):
        urls = [
            "https://old.example.com/a",
            "https://old.example.com/b",
            "https://other.io/c",
        ]
        picked = pick_property_for_urls(
            ["https://other.io/", "sc-domain:example.com"], urls
        )
        self.assertEqual(picked, "sc-domain:example.com")

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(
            pick_property_for_urls(["sc-domain:unrelated.com"], ["https://example.com/a"])
        )


class TestStateTokens(unittest.TestCase):
    def setUp(self):
        from src.redirx.config import Config
        signing = patch.object(Config, 'GSC_STATE_SECRET', 'fixture-only-state-signing-key-0000000000')
        signing.start()
        self.addCleanup(signing.stop)

    def test_round_trip(self):
        token = create_state_token("user-123", "/review/abc")
        payload = verify_state_token(token)
        self.assertEqual(payload["sub"], "user-123")
        self.assertEqual(payload["return_to"], "/review/abc")

    def test_garbage_state_rejected(self):
        with self.assertRaises(GSCError):
            verify_state_token("not-a-jwt")


def _mapping(clicks, impressions, band="high", warnings=None):
    return {
        "gscClicks": clicks,
        "gscImpressions": impressions,
        "confidenceBand": band,
        "warnings": warnings or [],
    }


class TestComputeRiskSummary(unittest.TestCase):
    def test_finds_top_urls_carrying_traffic_share(self):
        # One URL carries 90% of clicks; it alone crosses the 80% target.
        mappings = [
            _mapping(900, 1000, band="low"),
            _mapping(50, 100),
            _mapping(50, 100),
            _mapping(0, 0),
        ]
        summary = compute_risk_summary(mappings)
        self.assertEqual(summary["topUrlCount"], 1)
        self.assertEqual(summary["topUrlsAtRisk"], 1)
        self.assertEqual(summary["totalClicks"], 1000)
        self.assertEqual(summary["urlsWithTraffic"], 3)
        self.assertEqual(summary["weightMetric"], "clicks")

    def test_needs_review_counts_as_at_risk(self):
        mappings = [
            _mapping(500, 600, band="high", warnings=["needs-review"]),
            _mapping(100, 200),
        ]
        summary = compute_risk_summary(mappings)
        self.assertEqual(summary["topUrlsAtRisk"], 1)

    def test_falls_back_to_impressions_when_no_clicks(self):
        mappings = [_mapping(0, 400, band="low"), _mapping(0, 50)]
        summary = compute_risk_summary(mappings)
        self.assertEqual(summary["weightMetric"], "impressions")
        self.assertEqual(summary["topUrlCount"], 1)
        self.assertEqual(summary["topUrlsAtRisk"], 1)

    def test_empty_traffic_returns_zeroes(self):
        summary = compute_risk_summary([_mapping(0, 0), _mapping(0, 0)])
        self.assertEqual(summary["topUrlCount"], 0)
        self.assertEqual(summary["topUrlsAtRisk"], 0)
        self.assertEqual(summary["urlsWithTraffic"], 0)


class TestFormatResultsWithGsc(unittest.TestCase):
    def _db_mapping(self, old_url, confidence=0.9):
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "old_url": old_url,
            "new_url": "https://new.example.com/x",
            "confidence_score": confidence,
            "match_type": "semantic",
            "needs_review": False,
        }

    def test_metrics_attached_when_synced(self):
        session = {
            "id": "00000000-0000-0000-0000-000000000002",
            "status": "completed",
            "pipeline_type": "url_only",
            "gsc_property": "sc-domain:example.com",
            "gsc_synced_at": "2026-08-01T00:00:00+00:00",
        }
        metrics = [{"url": "https://old.example.com/a", "clicks": 42, "impressions": 100}]
        response = format_results_response(
            [self._db_mapping("https://old.example.com/a")], session, metrics
        )
        self.assertTrue(response["gsc"]["synced"])
        self.assertEqual(response["mappings"][0]["gscClicks"], 42)
        self.assertEqual(response["mappings"][0]["gscImpressions"], 100)
        self.assertIn("riskSummary", response["gsc"])

    def test_no_gsc_fields_when_not_synced(self):
        session = {
            "id": "00000000-0000-0000-0000-000000000002",
            "status": "completed",
            "pipeline_type": "url_only",
        }
        response = format_results_response(
            [self._db_mapping("https://old.example.com/a")], session
        )
        self.assertFalse(response["gsc"]["synced"])
        self.assertNotIn("gscClicks", response["mappings"][0])


if __name__ == "__main__":
    unittest.main()
