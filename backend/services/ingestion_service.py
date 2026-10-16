"""
Old-site and new-site URL ingestion.

The asymmetry between the two sides is the point:

                  | Old site                      | New site
    --------------|-------------------------------|------------------
    Primary       | GSC Search Analytics          | sitemap.xml
    Fallback      | sitemap, then crawl           | crawl
    GSC useful?   | yes                           | no — not indexed yet

Search Analytics returns exactly the URLs carrying rankings or traffic worth
preserving, already traffic-weighted, in seconds, with no rate limiting, no
WAF exposure, and no dependency on the old site still being reachable.
Discovery and prioritization collapse into one operation.

GSC alone is not sufficient, though: Google applies privacy thresholding and
drops very-low-impression URLs, and history caps at 16 months. So the
discovery set is GSC ∪ sitemap ∪ (optional crawl), with every URL tagged by
the source(s) that found it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backend.services.gsc_service import (
    GSC_DATA_LAG_DAYS,
    GSCError,
    GSCService,
    normalize_url,
    pick_property_for_urls,
    property_matches_host,
)
from src.redirx.config import Config
from src.redirx.database import GSCConnectionDB, TrafficBaselineDB
from src.redirx.discovery import (
    DiscoveryError,
    clean_page_url,
    discover_site,
    normalize_root,
)
from src.redirx.url_kind import classify_url_kind, is_low_priority

logger = logging.getLogger(__name__)

SOURCE_GSC = "gsc"
SOURCE_SITEMAP = "sitemap"
SOURCE_CRAWL = "crawl"
SOURCE_CSV = "csv"

# Discovery methods map onto source tags; sitemap/CMS-API results are all
# "the site told us this URL exists", crawl is "we inferred it".
_METHOD_TO_SOURCE = {
    "sitemap": SOURCE_SITEMAP,
    "wordpress_api": SOURCE_SITEMAP,
    "shopify_api": SOURCE_SITEMAP,
    "crawl": SOURCE_CRAWL,
}


def bare_host(url_or_domain: str) -> str:
    """Normalized host for baseline lookup and property matching."""
    candidate = url_or_domain if "://" in url_or_domain else f"https://{url_or_domain}"
    host = (urlparse(candidate).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def merge_sources(
    gsc_rows: List[Dict[str, Any]],
    crawled_urls: List[str],
    crawl_source: str,
) -> List[Dict[str, Any]]:
    """
    Union GSC results with crawled/sitemap URLs, preserving per-URL provenance
    and traffic weight.

    Matching is on normalized URLs so http/https and trailing-slash variants
    collapse, but the returned URL prefers the GSC form — that is the one
    Google actually indexed, and therefore the one that will receive traffic.

    Returns entries sorted by clicks desc, so the riskiest URLs lead.
    """
    by_key: Dict[str, Dict[str, Any]] = {}

    for row in gsc_rows:
        # Search Console reports image and file URLs too, because they rank in
        # Google Images. Measured on a real WordPress site, six of the top
        # "GSC-only" results were /wp-content/uploads/*.jpeg. They are not
        # pages and must never become redirect rules, so GSC rows get the same
        # asset filtering the crawled URLs already had.
        if clean_page_url(row["url"]) is None:
            continue
        key = normalize_url(row["url"])
        existing = by_key.get(key)
        if existing:
            # http/https or slash variants of one page: sum their traffic.
            existing["clicks"] += int(row.get("clicks", 0))
            existing["impressions"] += int(row.get("impressions", 0))
            continue
        by_key[key] = {
            "url": row["url"],
            "sources": [SOURCE_GSC],
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
        }

    for url in crawled_urls:
        key = normalize_url(url)
        existing = by_key.get(key)
        if existing:
            if crawl_source not in existing["sources"]:
                existing["sources"].append(crawl_source)
            continue
        by_key[key] = {
            "url": url,
            "sources": [crawl_source],
            "clicks": 0,
            "impressions": 0,
        }

    for entry in by_key.values():
        entry["kind"] = classify_url_kind(entry["url"])
        # Advisory only: recorded traffic always outranks path shape.
        entry["low_priority"] = is_low_priority(
            entry["url"], entry["clicks"], entry["impressions"]
        )

    return sorted(
        by_key.values(),
        key=lambda e: (e["clicks"], e["impressions"]),
        reverse=True,
    )


def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts the review UI needs to explain where the URL set came from."""
    with_traffic = [e for e in entries if e["clicks"] > 0 or e["impressions"] > 0]
    gsc_only = [e for e in entries if e["sources"] == [SOURCE_GSC]]
    no_traffic = [e for e in entries if SOURCE_GSC not in e["sources"]]
    kinds: Dict[str, int] = {}
    for e in entries:
        k = e.get("kind") or "page"
        kinds[k] = kinds.get(k, 0) + 1

    return {
        "total": len(entries),
        "with_traffic": len(with_traffic),
        "gsc_only": len(gsc_only),
        "no_recorded_traffic": len(no_traffic),
        "low_priority": len([e for e in entries if e.get("low_priority")]),
        "kinds": kinds,
        "total_clicks": sum(e["clicks"] for e in entries),
        "total_impressions": sum(e["impressions"] for e in entries),
    }


class IngestionService:
    """Builds a tagged, traffic-weighted URL set for one side of a migration."""

    def __init__(
        self,
        gsc_service: Optional[GSCService] = None,
        connection_db: Optional[GSCConnectionDB] = None,
        baseline_db: Optional[TrafficBaselineDB] = None,
    ):
        self._gsc_service = gsc_service
        self.connection_db = connection_db or GSCConnectionDB()
        self.baseline_db = baseline_db or TrafficBaselineDB()

    @property
    def gsc(self) -> Optional[GSCService]:
        """None when GSC OAuth is not configured on this deployment."""
        if self._gsc_service is None:
            try:
                self._gsc_service = GSCService()
            except ValueError:
                return None
        return self._gsc_service

    def find_property(self, user_id: str, domain: str) -> Optional[str]:
        """
        The verified GSC property covering this domain, if any.

        Degrades to None on any failure rather than raising: GSC is an
        enhancement over sitemap/crawl discovery, so a connection lookup
        failing must not take the whole ingestion down with it.
        """
        service = self.gsc
        if service is None:
            return None
        try:
            if not self.connection_db.get_connection(user_id):
                return None
            properties = [p["site_url"] for p in service.list_properties(user_id)]
        except GSCError:
            return None
        except Exception:
            logger.warning("GSC property lookup unavailable for %s", domain, exc_info=True)
            return None
        host = bare_host(domain)
        direct = [p for p in properties if property_matches_host(p, host)]
        if direct:
            # Prefer a domain property: it covers www and bare alike.
            direct.sort(key=lambda p: 0 if p.startswith("sc-domain:") else 1)
            return direct[0]
        return pick_property_for_urls(properties, [f"https://{host}/"])

    def same_property(self, user_id: str, old_domain: str, new_domain: str) -> Optional[str]:
        """
        The single property covering both domains, if there is one.

        A redesign keeps one property and needs date ranges to separate before
        from after; a domain migration has two. Detect it rather than making
        the user think about it.
        """
        old_property = self.find_property(user_id, old_domain)
        if not old_property:
            return None
        return old_property if property_matches_host(old_property, bare_host(new_domain)) else None

    def verified_property(self, user_id: str, site_url: str) -> Optional[str]:
        """
        `site_url` if the user actually has it verified, else None.

        The property can be chosen in the UI, so it arrives from the client and
        cannot be trusted. Google would reject a property this user has no
        permission on anyway, but checking here keeps an unauthorized value from
        reaching the API call at all — and lets us fall back to auto-detection
        instead of surfacing a Google error.
        """
        service = self.gsc
        if service is None or not site_url:
            return None
        try:
            if not self.connection_db.get_connection(user_id):
                return None
            properties = {p["site_url"] for p in service.list_properties(user_id)}
        except GSCError:
            return None
        except Exception:
            logger.warning("GSC property check unavailable", exc_info=True)
            return None
        return site_url if site_url in properties else None

    def gsc_urls_for_domain(
        self,
        user_id: str,
        domain: str,
        lookback_days: Optional[int] = None,
        gsc_property: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Search Analytics rows for a domain, restricted to that host.

        A domain property covers subdomains too, so results are filtered to the
        host actually being migrated.

        `gsc_property` overrides auto-detection for the case auto-detection
        cannot resolve on its own: an agency with several verified properties,
        or a host covered by both a domain and a URL-prefix property.
        """
        site_url = (
            self.verified_property(user_id, gsc_property)
            or self.find_property(user_id, domain)
        )
        if not site_url:
            return [], None

        service = self.gsc
        try:
            rows = service.query_search_analytics(user_id, site_url, lookback_days)
        except GSCError as exc:
            logger.info("GSC discovery unavailable for %s: %s", domain, exc.code)
            return [], site_url

        host = bare_host(domain)
        return [r for r in rows if bare_host(r["url"]) == host], site_url

    def capture_baseline(
        self,
        user_id: str,
        domain: str,
        rows: List[Dict[str, Any]],
        gsc_property: str,
        source_session_id=None,
        lookback_days: Optional[int] = None,
    ) -> Optional[str]:
        """
        Persist the pre-migration traffic distribution.

        Called on every GSC-backed ingestion regardless of tier. Failure here
        must never break ingestion, but it is logged loudly: this snapshot
        cannot be recreated later.
        """
        if not rows:
            return None
        days = lookback_days or Config.GSC_LOOKBACK_DAYS
        end = date.today() - timedelta(days=GSC_DATA_LAG_DAYS)
        start = end - timedelta(days=days)
        try:
            baseline_id = self.baseline_db.create_baseline(
                user_id=user_id,
                gsc_property=gsc_property,
                domain=bare_host(domain),
                range_start=start.isoformat(),
                range_end=end.isoformat(),
                rows=rows,
                source_session_id=source_session_id,
            )
            return str(baseline_id)
        except Exception:
            logger.exception("failed to capture traffic baseline for %s", domain)
            return None

    async def ingest_side(
        self,
        user_id: str,
        domain: str,
        side: str,
        max_urls: int,
        time_budget: float,
        capture_baseline: bool = True,
        source_session_id=None,
        gsc_property_override: Optional[str] = None,
        use_gsc: bool = True,
    ) -> Dict[str, Any]:
        """
        Build the tagged URL set for one side of a migration.

        The old side leads with Search Analytics and unions in sitemap/crawl
        results to recover the tail Google withholds. The new side skips GSC
        entirely — its URLs are not indexed yet, so GSC would return nothing
        and cost a round trip to learn it.
        """
        if type(use_gsc) is not bool:
            raise ValueError("use_gsc must be true or false")
        normalize_root(domain)  # raises DiscoveryError on bad input
        gsc_rows: List[Dict[str, Any]] = []
        gsc_property: Optional[str] = None
        baseline_id: Optional[str] = None

        if side == "old" and use_gsc:
            gsc_rows, gsc_property = await asyncio.to_thread(
                self.gsc_urls_for_domain,
                user_id,
                domain,
                gsc_property=gsc_property_override,
            )
            if gsc_rows and capture_baseline and gsc_property:
                baseline_id = await asyncio.to_thread(
                    self.capture_baseline,
                    user_id, domain, gsc_rows, gsc_property, source_session_id,
                )

        # Sitemap/crawl still runs: GSC misses privacy-thresholded and
        # never-ranked URLs, and the new side has no GSC data at all.
        #
        # A verified Search Console property is proof this user owns the
        # domain, which lifts robots.txt Disallow on the link-following crawl.
        # robots.txt asks crawlers to stay out; an owner reading their own site
        # to plan its migration is not the visitor it is addressing.
        crawl_result = await discover_site(
            domain,
            max_urls=max_urls,
            time_budget=time_budget,
            owner_verified=bool(gsc_property),
        )
        crawl_source = _METHOD_TO_SOURCE.get(crawl_result.method, SOURCE_SITEMAP)

        entries = merge_sources(gsc_rows, crawl_result.urls, crawl_source)
        truncated = len(entries) > max_urls
        entries = entries[:max_urls]

        return {
            "domain": domain,
            "root_url": crawl_result.root_url,
            "side": side,
            "entries": entries,
            "summary": summarize(entries),
            "truncated": truncated,
            "discovery_method": crawl_result.method,
            "generator": crawl_result.generator,
            "gsc_property": gsc_property,
            "gsc_url_count": len(gsc_rows),
            "baseline_id": baseline_id,
            "rate_limited": crawl_result.rate_limited,
            "retry_after_seconds": crawl_result.retry_after_seconds,
            "robots_blocked": crawl_result.robots_blocked,
        }
