"""
Google Search Console integration routes.

Connect/disconnect the standalone GSC OAuth flow and pull per-URL traffic
metrics into a migration session for traffic-weighted triage.
"""
from urllib.parse import urlencode
from uuid import UUID
import os
import re

from flask import Blueprint, jsonify, redirect, request

from backend.services.auth_service import require_auth
from backend.services.gsc_service import GSCError, GSCService, verify_state_token
from src.redirx.config import Config
from src.redirx.database import MigrationSessionDB

gsc_blueprint = Blueprint("gsc", __name__)


def _gsc_error_response(error: GSCError):
    return jsonify({
        "success": False,
        "error": error.user_message,
        "code": error.code,
        "user_message": error.user_message,
    }), error.status_code


def _safe_return_to(raw: str) -> str:
    """Only allow app-relative return paths to avoid open redirects."""
    if raw and raw.startswith('/') and not raw.startswith('//'):
        return raw
    return '/'


def _frontend_redirect(return_to: str, gsc_result: str):
    # return_to always starts with "/", so a trailing slash on APP_BASE_URL
    # produced "https://host//review/..." — which 404s. Don't depend on the
    # env var being punctuated correctly.
    base = (Config.APP_BASE_URL or '').rstrip('/')
    separator = '&' if '?' in return_to else '?'
    query = urlencode({'gsc': gsc_result})
    return redirect(f"{base}{return_to}{separator}{query}")


@gsc_blueprint.route("/connect", methods=["GET"])
@require_auth
def gsc_connect():
    """Return the Google consent URL for connecting Search Console."""
    return_to = _safe_return_to(request.args.get('return_to', '/'))
    try:
        service = GSCService()
        url = service.build_authorization_url(str(request.user.id), return_to)
        return jsonify({"success": True, "authorization_url": url}), 200
    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "code": "gsc_not_configured",
        }), 503
    except GSCError as e:
        return _gsc_error_response(e)


@gsc_blueprint.route("/callback", methods=["GET"])
def gsc_callback():
    """
    OAuth redirect target for Google. No auth header here — the user identity
    comes from the signed state token minted in /connect.
    """
    state = request.args.get('state', '')
    if os.getenv('MCP_PIVOT_ENABLED', 'false').lower() == 'true' and re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
        # The registered Google callback can serve both protocols. Opaque agent
        # state is resolved only through its durable one-use/PKCE store; never
        # retry a failed agent callback through the legacy JWT-state handler.
        from backend.routes.migration_gsc_routes import agent_callback_response
        return agent_callback_response()
    code = request.args.get('code')
    if not state:
        return _frontend_redirect('/', 'error')

    try:
        service = GSCService()
        payload = service.handle_callback(code, state, request.args.get('error'))
        return_to = _safe_return_to(payload.get('return_to', '/'))
        return _frontend_redirect(return_to, payload['consent_result'])
    except GSCError as exc:
        if exc.code == 'gsc_connection_unconfirmed':
            try:
                return_to = _safe_return_to(verify_state_token(state).get('return_to', '/'))
            except GSCError:
                return_to = '/'
            return _frontend_redirect(return_to, 'unconfirmed')
        return _frontend_redirect('/', 'error')
    except ValueError:
        return _frontend_redirect('/', 'error')


@gsc_blueprint.route("/status", methods=["GET"])
@require_auth
def gsc_status():
    try:
        service = GSCService()
        status = service.get_status(str(request.user.id))
        return jsonify({"success": True, **status}), 200
    except ValueError:
        # GSC OAuth not configured on this deployment.
        return jsonify({"success": True, "connected": False, "configured": False}), 200
    except GSCError as e:
        return _gsc_error_response(e)


@gsc_blueprint.route("/disconnect", methods=["POST"])
@require_auth
def gsc_disconnect():
    try:
        service = GSCService()
        service.disconnect(str(request.user.id))
        return jsonify({"success": True}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 503
    except GSCError as e:
        return _gsc_error_response(e)


@gsc_blueprint.route("/properties", methods=["GET"])
@require_auth
def gsc_properties():
    try:
        service = GSCService()
        properties = service.list_properties(str(request.user.id))
        return jsonify({"success": True, "properties": properties}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 503
    except GSCError as e:
        return _gsc_error_response(e)


@gsc_blueprint.route("/sessions/<session_id>/sync", methods=["POST"])
@require_auth
def gsc_sync_session(session_id: str):
    """Pull Search Analytics metrics for a session's old URLs."""
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return jsonify({
            "success": False,
            "error": f"Invalid session_id format: {session_id}",
        }), 400

    session_db = MigrationSessionDB()
    try:
        session = session_db.get_session(session_uuid)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    if session.get('user_id') != str(request.user.id):
        return jsonify({
            "success": False,
            "error": "Unauthorized: Session belongs to another user",
        }), 403

    body = request.get_json(silent=True) or {}
    site_url = body.get('property') or None

    try:
        service = GSCService()
        result = service.sync_session(str(request.user.id), session, site_url=site_url)
        return jsonify({"success": True, **result}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 503
    except GSCError as e:
        return _gsc_error_response(e)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to sync Search Console data: {str(e)}",
        }), 500
