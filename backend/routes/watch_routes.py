"""
Post-cutover monitoring, for humans.

These are browser endpoints behind session auth. They start and stop a watch
and read what it found; they never run a sweep. Probing a few thousand URLs
takes tens of minutes and the API is a single sync gunicorn worker — doing it
here would block every other request in the process. Sweeps belong to the
worker, and these endpoints only move rows it will act on.
"""
from flask import Blueprint, Response, jsonify, request

from backend.extensions import limiter
from backend.services import redirect_export
from backend.services.auth_service import require_auth
from backend.services.watch_service import WatchService, plan_allows_watch
from src.redirx.database import UserQuotaDB

watch_blueprint = Blueprint("watches", __name__)


@watch_blueprint.record_once
def _init_limiter(state):
    if "limiter" not in state.app.extensions:
        limiter.init_app(state.app)


def _owned_watch(watch_id: str):
    """
    Fetch a watch, or None when it is missing *or* someone else's.

    Both cases return the same 404 on purpose: distinguishing them would let a
    logged-in user enumerate which migration IDs exist.
    """
    watch = WatchService().get_watch(watch_id)
    if not watch or str(watch.get("user_id")) != str(request.user.id):
        return None
    return watch


@watch_blueprint.route("", methods=["GET"])
@require_auth
def list_watches():
    return jsonify({
        "success": True,
        "watches": WatchService().list_watches(str(request.user.id)),
    }), 200


@watch_blueprint.route("", methods=["POST"])
@limiter.limit("30 per hour")
@require_auth
def create_watch():
    """
    Start watching a migration's redirects on the live site.

    Idempotent per migration: clicking twice returns the existing watch rather
    than doubling the probe traffic aimed at the customer's origin.
    """
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"success": False, "error": "session_id is required."}), 400

    # Monitoring recurs forever against the customer's origin, so it is a
    # paid-plan feature. Creation is the only gated verb: an existing watch
    # can still be paused, resumed or read if a plan ever lapses.
    user_id = str(request.user.id)
    if not plan_allows_watch(UserQuotaDB().get_plan(user_id), user_id):
        return jsonify({
            "success": False,
            "error": "Monitoring is part of the paid plans.",
            "code": "watch_requires_paid_plan",
            "next_action": "pricing_checkout",
        }), 403

    try:
        watch = WatchService().create_watch(
            user_id=str(request.user.id),
            session_id=str(session_id),
            alert_email=body.get("alert_email"),
            check_interval_minutes=int(body.get("check_interval_minutes") or 1440),
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "session_not_found":
            return jsonify({"success": False, "error": "Migration not found."}), 404
        if reason == "no_old_domain":
            return jsonify({
                "success": False,
                "error": "This migration has no old-site domain to monitor.",
            }), 400
        raise

    return jsonify({"success": True, "watch": watch}), 201


@watch_blueprint.route("/<watch_id>", methods=["GET"])
@require_auth
def get_watch(watch_id: str):
    watch = _owned_watch(watch_id)
    if not watch:
        return jsonify({"success": False, "error": "Watch not found."}), 404

    service = WatchService()
    issues = service.list_issues(watch_id)
    return jsonify({
        "success": True,
        "watch": watch,
        "issues": issues,
        "checks": service.list_checks(watch_id),
        "summary": {
            "open_issues": len(issues),
            "critical": sum(1 for i in issues if i.get("severity") == "critical"),
            "clicks_at_risk": sum(int(i.get("clicks_at_risk") or 0) for i in issues),
        },
    }), 200


@watch_blueprint.route("/<watch_id>", methods=["PATCH"])
@require_auth
def update_watch(watch_id: str):
    if not _owned_watch(watch_id):
        return jsonify({"success": False, "error": "Watch not found."}), 404

    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("active", "paused", "ended"):
        return jsonify({
            "success": False,
            "error": "status must be active, paused, or ended.",
        }), 400

    return jsonify({
        "success": True,
        "watch": WatchService().set_status(watch_id, status),
    }), 200


@watch_blueprint.route("/<watch_id>/check-now", methods=["POST"])
@limiter.limit("6 per hour")
@require_auth
def check_now(watch_id: str):
    """
    Move the next sweep to now.

    This queues rather than runs: the response says when the worker will pick
    it up, not what it found. Rate-limited because "check now" spammed in a
    browser tab would turn into sustained load on someone's production site.
    """
    if not _owned_watch(watch_id):
        return jsonify({"success": False, "error": "Watch not found."}), 404

    watch = WatchService().queue_check_now(watch_id)
    if watch is None:
        # Paused or ended. Resuming is a deliberate act, not a side effect of
        # asking for a check.
        return jsonify({
            "success": False,
            "error": "This watch is paused. Resume it first, then check.",
        }), 409

    return jsonify({
        "success": True,
        "watch": watch,
        "queued": True,
        "notice": "Queued. Results usually appear within a few minutes.",
    }), 202


@watch_blueprint.route("/<watch_id>/issues", methods=["GET"])
@require_auth
def list_issues(watch_id: str):
    if not _owned_watch(watch_id):
        return jsonify({"success": False, "error": "Watch not found."}), 404

    include_resolved = str(request.args.get("include_resolved", "")).lower() in (
        "1", "true", "yes",
    )
    return jsonify({
        "success": True,
        "issues": WatchService().list_issues(watch_id, include_resolved=include_resolved),
    }), 200


@watch_blueprint.route("/<watch_id>/fixes", methods=["GET"])
@require_auth
def list_fixes(watch_id: str):
    """The corrections we would deploy, for review before downloading them."""
    if not _owned_watch(watch_id):
        return jsonify({"success": False, "error": "Watch not found."}), 404

    rows = WatchService().fix_rows(watch_id)
    return jsonify({
        "success": True,
        "fixes": rows,
        "count": len(rows),
        "clicks_recovered": sum(r["clicks_at_risk"] for r in rows),
    }), 200


@watch_blueprint.route("/<watch_id>/fixes/export", methods=["GET"])
@require_auth
def export_fixes(watch_id: str):
    """
    A corrective redirect file covering only the URLs currently failing.

    Built by the same formatters as the original export, so it drops into the
    customer's existing deploy process. Defaults to `paths` for the same
    reason the main export does: most targets match on the request path, and
    an absolute source loads fine while silently redirecting nothing.
    """
    if not _owned_watch(watch_id):
        return jsonify({"success": False, "error": "Watch not found."}), 404

    fmt = (request.args.get("format") or "csv").lower()
    if fmt not in redirect_export.FORMATS:
        return jsonify({
            "success": False,
            "error": f"'format' must be one of: {', '.join(redirect_export.FORMATS)}.",
        }), 400

    url_format = (request.args.get("url_format") or "paths").lower()
    if url_format not in ("paths", "full"):
        return jsonify({
            "success": False,
            "error": "'url_format' must be 'paths' or 'full'.",
        }), 400

    rows = WatchService().fix_rows(watch_id)
    try:
        content = redirect_export.build_export(rows, fmt, url_format=url_format)
    except redirect_export.UnsupportedExportInput as exc:
        return jsonify({"success": False, "code": "unsupported_export_input", "error": str(exc)}), 422
    selection = redirect_export.select_export_mappings(rows, url_format=url_format)

    response = Response(content, mimetype=redirect_export.content_type_for(fmt))
    response.headers["Content-Disposition"] = (
        f'attachment; filename="fix-{redirect_export.filename_for(fmt)}"'
    )
    response.headers["X-Redirect-Count"] = str(selection["included_count"])
    response.headers["X-Redirect-Excluded-Count"] = str(selection["excluded_count"])
    warning = redirect_export.warning_for(fmt, url_format)
    if warning:
        response.headers["X-Redirx-Warning"] = warning
    return response
