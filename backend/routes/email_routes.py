"""
Email routes: unsubscribe, preference management, nudge cron trigger, and admin test sends.
"""

from flask import Blueprint, request, jsonify, render_template
from backend.services.email_preferences import OPTIONAL_EMAIL_TYPES, verify_unsubscribe_token, UnsubscribeUnavailable
import logging

from backend.services.auth_service import require_auth, require_admin

logger = logging.getLogger(__name__)

email_blueprint = Blueprint("email", __name__)


@email_blueprint.after_request
def protect_preference_response(response):
    if request.endpoint == "email.unsubscribe":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _get_email_service():
    """Lazily import and create EmailService (so the module loads even without resend)."""
    from backend.services.email_service import EmailService
    return EmailService()


# ============================================================================
# Public: One-click unsubscribe (linked from email footer)
# ============================================================================

def _save_preference(user_id, email_type, opted_out):
    from redirx.database import SupabaseClient
    result = SupabaseClient.get_client().table("email_preferences").upsert(
        {"user_id": user_id, "email_type": email_type, "opted_out": opted_out},
        on_conflict="user_id,email_type",
    ).execute()
    if getattr(result, "error", None) is not None:
        raise RuntimeError("Email preference write returned an error")
    rows = getattr(result, "data", None)
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and row.get("user_id") == user_id
        and row.get("email_type") == email_type and row.get("opted_out") is opted_out
        for row in rows
    ):
        raise RuntimeError("Email preference write was not acknowledged")


@email_blueprint.route("/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    """GET confirms intent; only a signed, scoped POST changes preferences."""
    token = request.args.get("token", "")
    try:
        user_id, email_type = verify_unsubscribe_token(token)
    except UnsubscribeUnavailable:
        return render_template("email/unsubscribe.html", state="unavailable"), 503
    except ValueError:
        # Previously sent user_id/type links were public mutable identifiers.
        # Keep a useful recovery path without accepting them as authorization.
        return render_template("email/unsubscribe.html", state="invalid"), 400
    if request.method == "GET":
        return render_template("email/unsubscribe.html", state="confirm", email_type=email_type)
    try:
        _save_preference(user_id, email_type, True)
        return render_template("email/unsubscribe.html", state="saved", email_type=email_type)
    except Exception:
        logger.warning("Unsubscribe preference could not be saved")
        return render_template("email/unsubscribe.html", state="failed"), 503


# ============================================================================
# Authenticated: Preference management
# ============================================================================

@email_blueprint.route("/preferences", methods=["GET"])
@require_auth
def get_preferences():
    """Return the authenticated user's email preferences."""
    try:
        from redirx.database import SupabaseClient
        client = SupabaseClient.get_client()

        result = (
            client.table("email_preferences")
            .select("email_type, opted_out, updated_at")
            .eq("user_id", request.user.id)
            .execute()
        )

        if getattr(result, "error", None) is not None:
            raise RuntimeError("Email preference read returned an error")
        return jsonify({"preferences": result.data or []})

    except Exception:
        logger.exception("Failed to fetch email preferences")
        return jsonify({"error": "Failed to fetch preferences"}), 500


@email_blueprint.route("/preferences", methods=["PUT"])
@require_auth
def update_preferences():
    """
    Update email preferences.
    Body: { "email_type": "onboard_nudge", "opted_out": true }
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "email_type" not in data or "opted_out" not in data:
        return jsonify({"error": "email_type and opted_out are required"}), 400

    email_type = data["email_type"]
    opted_out = data["opted_out"]
    if not isinstance(email_type, str) or email_type not in OPTIONAL_EMAIL_TYPES:
        return jsonify({"error": "Choose a supported optional email type."}), 400
    if type(opted_out) is not bool:
        return jsonify({"error": "opted_out must be true or false."}), 400

    try:
        _save_preference(str(request.user.id), email_type, opted_out)

        return jsonify({"success": True, "email_type": email_type, "opted_out": opted_out})

    except Exception:
        logger.exception("Failed to update email preferences")
        return jsonify({"error": "Failed to update preferences"}), 500


# ============================================================================
# Admin: Nudge cron trigger
# ============================================================================

@email_blueprint.route("/admin/nudge-cron", methods=["POST"])
def run_nudge_cron():
    """
    Run the onboarding nudge cron. Protected by X-Cron-Secret header
    (same secret as the trial expiry cron).
    """
    from redirx.config import Config
    cron_secret = Config.CRON_SECRET
    provided = request.headers.get("X-Cron-Secret", "")

    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        from backend.services.email_nudge_cron import run_nudges
        sent = run_nudges()
        return jsonify({"nudges_sent": sent})
    except Exception:
        logger.exception("Nudge cron failed")
        return jsonify({"error": "Failed to run nudge cron"}), 500


# ============================================================================
# Admin: List templates (registry-driven)
# ============================================================================

@email_blueprint.route("/admin/templates", methods=["GET"])
@require_auth
@require_admin
def list_templates():
    """Return all testable email templates from the registry."""
    from backend.services.email_templates_registry import get_templates_for_api
    return jsonify({"templates": get_templates_for_api()})


# ============================================================================
# Admin: Recent email log
# ============================================================================

@email_blueprint.route("/admin/log", methods=["GET"])
@require_auth
@require_admin
def get_email_log():
    """
    Return recent email_log rows.
    Query params: ?limit=10&email_type=welcome
    """
    limit = min(int(request.args.get("limit", 20)), 100)
    email_type = request.args.get("email_type")

    try:
        from redirx.database import SupabaseClient
        client = SupabaseClient.get_client()

        query = (
            client.table("email_log")
            .select("id, user_id, to_email, email_type, subject, status, error, resend_message_id, created_at")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if email_type:
            query = query.eq("email_type", email_type)

        result = query.execute()
        return jsonify({"entries": result.data or []})

    except Exception:
        logger.exception("Failed to fetch email log")
        return jsonify({"error": "Failed to fetch email log"}), 500


# ============================================================================
# Admin: Test email sending (registry-driven)
# ============================================================================

@email_blueprint.route("/admin/test", methods=["POST"])
@require_auth
@require_admin
def send_test_email():
    """
    Send a test email for template verification.

    Body: {
        "template_id": "welcome|mapping_complete|mapping_failed|nudge_day1|nudge_day3|nudge_day7",
        "to_email": "optional-override@example.com"
    }
    """
    data = request.get_json()
    template_id = (data or {}).get("template_id") or (data or {}).get("email_type")
    if not template_id:
        return jsonify({"error": "template_id is required"}), 400

    from backend.services.email_templates_registry import get_template_by_id
    tmpl = get_template_by_id(template_id)
    if not tmpl:
        return jsonify({"error": f"Unknown template: {template_id}"}), 400

    to_email = (data or {}).get("to_email") or request.user.email
    service = _get_email_service()

    message_id = service.send_email(
        user_id=request.user.id,
        to_email=to_email,
        email_type=tmpl["email_type"],
        subject=tmpl["subject"],
        template_name=tmpl["template_name"],
        context=tmpl.get("test_context") or {},
        skip_preference_check=True,
    )

    if message_id:
        return jsonify({"success": True, "message_id": message_id})
    else:
        return jsonify({"success": False, "error": "Email was not sent (check logs)"}), 500
