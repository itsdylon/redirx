"""
Domain URL discovery routes.

Powers the "paste two domains" ingestion path: given a root domain, returns
the site's page URLs via sitemap → CMS API → crawl, capped by plan.
"""
import asyncio
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from backend.extensions import limiter
from backend.services.auth_service import require_auth
from src.redirx.config import Config
from src.redirx.database import UserQuotaDB
from backend.services.ingestion_service import IngestionService
from src.redirx.discovery import DiscoveryError

discovery_blueprint = Blueprint("discovery", __name__)


@discovery_blueprint.record_once
def _init_limiter(state):
    if "limiter" not in state.app.extensions:
        limiter.init_app(state.app)


@discovery_blueprint.route("/discover", methods=["POST"])
@limiter.limit("30 per hour")
@require_auth
def discover():
    """
    Discover a site's page URLs from a root domain.

    Body: {"url": "example.com"}

    Returns the discovered URL list plus how it was found (sitemap,
    wordpress_api, shopify_api, or crawl), the detected platform, and
    whether the plan cap truncated the result.
    """
    body = request.get_json(silent=True) or {}
    raw_url = str(body.get("url") or "").strip()
    if not raw_url:
        return jsonify({
            "success": False,
            "error": "A 'url' field is required.",
            "code": "missing_url",
        }), 400

    # The old side leads with Search Console; the new side's URLs are not
    # indexed yet, so GSC would return nothing there.
    side = "old" if str(body.get("side") or "old").lower() == "old" else "new"

    use_gsc = body.get("use_gsc", True)
    if type(use_gsc) is not bool:
        return jsonify({"success": False, "code": "invalid_use_gsc",
                        "error": "use_gsc must be true or false."}), 400

    user_id = str(request.user.id)
    plan = UserQuotaDB().get_plan(user_id)
    max_urls = (
        Config.DISCOVERY_MAX_URLS_FREE
        if plan == "free"
        else Config.DISCOVERY_MAX_URLS_PAID
    )

    # Chosen in the UI when auto-detection cannot pick between several verified
    # properties. Validated against the user's own list before it is used.
    gsc_property = str(body.get("gsc_property") or "").strip() or None

    try:
        ingestion = asyncio.run(IngestionService().ingest_side(
            user_id=user_id,
            domain=raw_url,
            side=side,
            max_urls=max_urls,
            time_budget=Config.DISCOVERY_TIME_BUDGET_SECONDS,
            gsc_property_override=gsc_property,
            use_gsc=use_gsc,
        ))
    except DiscoveryError as e:
        return jsonify({
            "success": False,
            "error": e.user_message,
            "code": e.code,
            "user_message": e.user_message,
        }), 422
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Discovery failed: {str(e)}",
            "code": "discovery_failed",
        }), 500

    entries = ingestion["entries"]
    summary = ingestion["summary"]
    root_url = ingestion["root_url"]

    if not entries:
        if ingestion.get("robots_blocked"):
            # Not a failure to find pages — a refusal to take them this way.
            # Verifying the domain in Search Console both proves ownership and
            # is the thing that makes the rest of the product work.
            message = (
                f"{urlparse(root_url).hostname} asks automated tools not to read "
                "it, and it publishes no sitemap. Connect Search Console to "
                "confirm you own it, or upload a sitemap or CSV export instead."
            )
            return jsonify({
                "success": False,
                "code": "robots_blocked",
                "error": message,
                "user_message": message,
                "root_url": root_url,
                "generator": ingestion["generator"],
            }), 422

        if ingestion["rate_limited"]:
            minutes = max(1, round(ingestion["retry_after_seconds"] / 60))
            return jsonify({
                "success": False,
                "code": "rate_limited",
                "error": (
                    f"{urlparse(root_url).hostname} is currently rate-limiting "
                    f"automated requests. Try again in about {minutes} "
                    f"minute{'s' if minutes != 1 else ''}, or upload a sitemap or "
                    "CSV export instead."
                ),
                "root_url": root_url,
                "retry_after_seconds": ingestion["retry_after_seconds"],
                "generator": ingestion["generator"],
            }), 429

        return jsonify({
            "success": False,
            "code": "no_urls_found",
            "error": (
                f"Could not find any pages on {root_url}. "
                "The site may block automated requests — you can upload a "
                "sitemap or CSV instead."
            ),
            "root_url": root_url,
            "generator": ingestion["generator"],
        }), 422

    return jsonify({
        "success": True,
        "root_url": root_url,
        # Flat list kept for the existing upload flow, which turns these into
        # a generated URL file.
        "urls": [e["url"] for e in entries],
        # Tagged form: which source found each URL, and its traffic weight.
        "entries": entries,
        "count": len(entries),
        "total_found": summary["total"],
        "truncated": ingestion["truncated"],
        "max_urls": max_urls,
        "method": "gsc" if ingestion["gsc_url_count"] else ingestion["discovery_method"],
        "discovery_method": ingestion["discovery_method"],
        "generator": ingestion["generator"],
        "side": ingestion["side"],
        "summary": summary,
        "gsc_property": ingestion["gsc_property"],
        "gsc_url_count": ingestion["gsc_url_count"],
        "baseline_captured": bool(ingestion["baseline_id"]),
        "plan": plan,
    }), 200
