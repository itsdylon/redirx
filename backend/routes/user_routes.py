"""
User profile, onboarding, and session management endpoints.
"""
from datetime import datetime, timezone
from typing import Any, Dict
from flask import Blueprint, request, jsonify, current_app
import sys
import os

# Add parent directories to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
SRC_DIR = os.path.join(BASE_DIR, "src")
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, SRC_DIR)

from services.auth_service import AuthService, require_auth
from services.onboarding_samples import SAMPLE_TUTORIAL_MAPPINGS
from redirx.database import MigrationSessionDB, SupabaseClient, URLMappingDB
from backend.extensions import limiter

user_blueprint = Blueprint("user", __name__)

ONBOARDING_VERSION = "tutorial_v1"
ONBOARDING_STEPS = (
    "choose_path",
    "generate_mappings",
    "open_review",
    "export_redirects",
)
ONBOARDING_STATUSES = ("not_started", "in_progress", "completed", "dismissed")
ONBOARDING_ACTIONS = ("start", "select_path", "complete_step", "dismiss", "complete", "reset")
STEP_EVENT_KEYS = {
    "choose_path": "path_selected_at",
    "generate_mappings": "mapping_generated_at",
    "open_review": "review_opened_at",
    "export_redirects": "export_downloaded_at",
}


@user_blueprint.record_once
def _init_limiter(state):
    if "limiter" not in state.app.extensions:
        limiter.init_app(state.app)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_onboarding_state() -> Dict[str, Any]:
    return {
        "path": None,
        "steps": {
            step: {
                "completed": False,
                "completed_at": None,
            } for step in ONBOARDING_STEPS
        },
        "path_selected_at": None,
        "mapping_generated_at": None,
        "review_opened_at": None,
        "export_downloaded_at": None,
    }


def _normalize_onboarding_state(raw_state: Any) -> Dict[str, Any]:
    state = _default_onboarding_state()

    if not isinstance(raw_state, dict):
        return state

    path = raw_state.get("path")
    if path in ("sample", "real"):
        state["path"] = path

    for event_key in STEP_EVENT_KEYS.values():
        event_value = raw_state.get(event_key)
        if isinstance(event_value, str):
            state[event_key] = event_value

    raw_steps = raw_state.get("steps", {})
    if isinstance(raw_steps, dict):
        for step in ONBOARDING_STEPS:
            raw_step = raw_steps.get(step)
            if not isinstance(raw_step, dict):
                continue
            state["steps"][step]["completed"] = bool(raw_step.get("completed", False))
            completed_at = raw_step.get("completed_at")
            if isinstance(completed_at, str):
                state["steps"][step]["completed_at"] = completed_at

    return state


def _mark_step_complete(state: Dict[str, Any], step: str, now_iso: str) -> Dict[str, Any]:
    state["steps"][step]["completed"] = True
    state["steps"][step]["completed_at"] = now_iso
    event_key = STEP_EVENT_KEYS.get(step)
    if event_key:
        state[event_key] = now_iso
    return state


def _sanitize_onboarding_status(raw_status: Any) -> str:
    if isinstance(raw_status, str) and raw_status in ONBOARDING_STATUSES:
        return raw_status
    return "not_started"


def _build_onboarding_response(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "onboarding_version": profile.get("onboarding_version") or ONBOARDING_VERSION,
        "onboarding_status": _sanitize_onboarding_status(profile.get("onboarding_status")),
        "onboarding_state": _normalize_onboarding_state(profile.get("onboarding_state")),
        "onboarding_started_at": profile.get("onboarding_started_at"),
        "onboarding_completed_at": profile.get("onboarding_completed_at"),
        "onboarding_last_seen_at": profile.get("onboarding_last_seen_at"),
    }


@user_blueprint.route("/dashboard", methods=["GET"])
@limiter.limit("120 per minute")
@require_auth
def get_dashboard_stats():
    """
    Get dashboard overview with aggregate stats for authenticated user.
    Tutorial sessions are excluded from totals and recent session lists.
    """
    try:
        from backend.services.legacy_dashboard_service import read_dashboard
        return jsonify(read_dashboard(SupabaseClient.get_admin_client(), str(request.user.id))), 200
    except Exception:
        # Missing migration, unavailable transport and malformed acknowledgments
        # must not become a successful dashboard containing fabricated zeros.
        current_app.logger.warning("Historical dashboard snapshot unavailable")
        return jsonify({"success": False, "error": "Dashboard data is unavailable. Try loading it again."}), 503


@user_blueprint.route("/sessions", methods=["GET"])
@limiter.limit("120 per minute")
@require_auth
def get_user_sessions():
    """
    Read owned historical sessions: legacy full list or explicit cursor/search mode.
    """
    from backend.services.legacy_session_history import read_all_sessions, read_session_page, InvalidHistoryPage
    try:
        mode=request.args.get('pagination')
        if mode is None:
            if any(key in request.args for key in ('cursor','limit','q')):
                raise InvalidHistoryPage('Use pagination=cursor for history search and pages.')
            return jsonify(read_all_sessions(SupabaseClient.get_admin_client(),str(request.user.id))),200
        if mode!='cursor':
            raise InvalidHistoryPage('Unknown history pagination mode.')
        try:
            limit=int(request.args.get('limit','50'))
        except ValueError:
            raise InvalidHistoryPage('History page size must be from 1 to 100.') from None
        return jsonify(read_session_page(SupabaseClient.get_admin_client(),str(request.user.id),
            search=request.args.get('q',''),cursor=request.args.get('cursor'),limit=limit)),200
    except InvalidHistoryPage as exc:
        return jsonify({'success':False,'error':str(exc)}),400
    except Exception:
        current_app.logger.warning('Historical session list unavailable')
        return jsonify({'success':False,'error':'Project history is unavailable. Reload to try again.'}),503


@user_blueprint.route("/sessions/<session_id>", methods=["PUT"])
@limiter.limit("60 per minute")
@require_auth
def update_session(session_id):
    """
    Update a migration session (currently supports project_name updates).
    """
    session_db = MigrationSessionDB()

    try:
        existing = session_db.client.table("migration_sessions").select("*").eq(
            "id", session_id
        ).execute()

        if not existing.data:
            return jsonify({
                "success": False,
                "error": "Session not found",
            }), 404

        if existing.data[0]["user_id"] != str(request.user.id):
            return jsonify({
                "success": False,
                "error": "Unauthorized: Session belongs to another user",
            }), 403

        data = request.get_json(silent=True) or {}
        if "project_name" not in data:
            return jsonify({
                "success": False,
                "error": "Missing project_name in request body",
            }), 400

        result = session_db.client.table("migration_sessions").update({
            "project_name": data["project_name"],
        }).eq(
            "id", session_id
        ).execute()

        return jsonify({
            "success": True,
            "session": result.data[0],
        }), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/sessions/<session_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
@require_auth
def delete_session(session_id):
    """Atomically delete an owned historical session and its preview descendants."""
    from uuid import UUID
    try:
        session_id = str(UUID(session_id))
        owner = str(UUID(str(request.user.id)))
    except (ValueError, TypeError, AttributeError):
        return jsonify({"success": False, "error": "Invalid session identifier."}), 400
    try:
        # One database transaction. Never fall back to independent DELETE calls:
        # an unavailable RPC must not partially erase a project's evidence.
        result = SupabaseClient.get_admin_client().rpc('delete_owned_legacy_session', {
            'p_user_id': owner, 'p_session_id': session_id,
        }).execute()
        if getattr(result, 'error', None):
            raise RuntimeError('Session deletion storage unavailable')
        data = getattr(result, 'data', None)
        if (not isinstance(data, dict) or data.get('session_id') != session_id
                or type(data.get('deleted_count')) is not int or data['deleted_count'] < 1):
            raise RuntimeError('Invalid session deletion result')
        return jsonify({"success": True, "message": "Session deleted successfully"}), 200
    except Exception as exc:
        code = getattr(exc, 'message', '')
        if getattr(exc, 'code', '') == 'P0001' and code == 'not_found':
            return jsonify({"success": False, "error": "Session not found"}), 404
        if getattr(exc, 'code', '') == 'P0001' and code == 'operation_conflict':
            return jsonify({"success": False, "error": "This project cannot be deleted from historical session controls."}), 409
        current_app.logger.exception('Atomic session deletion failed for session=%s', session_id)
        return jsonify({"success": False, "error": "Deletion could not be confirmed. Refresh project history before retrying."}), 503


@user_blueprint.route("/sessions/<session_id>/status", methods=["GET"])
@limiter.limit("240 per minute")
@require_auth
def get_session_status(session_id):
    """
    Get the current status of a specific session.
    Used for polling during background job processing.
    """
    session_db = MigrationSessionDB()

    try:
        result = session_db.client.table("migration_sessions").select(
            "id, status, project_name, total_mappings, user_id, current_stage, stage_name, total_stages"
        ).eq("id", session_id).execute()

        if not result.data:
            return jsonify({
                "success": False,
                "error": "Session not found",
            }), 404

        session = result.data[0]
        if session["user_id"] != str(request.user.id):
            return jsonify({
                "success": False,
                "error": "Unauthorized",
            }), 403

        return jsonify({
            "success": True,
            "session_id": session["id"],
            "status": session["status"],
            "project_name": session.get("project_name", "Untitled"),
            "total_mappings": session.get("total_mappings", 0),
            "current_stage": session.get("current_stage"),
            "stage_name": session.get("stage_name"),
            "total_stages": session.get("total_stages"),
        }), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/onboarding", methods=["GET"])
@require_auth
def get_onboarding():
    """
    Get onboarding state for the authenticated user.
    """
    auth_service = AuthService()

    try:
        profile = auth_service.get_user_profile(request.user.id)
        return jsonify(_build_onboarding_response(profile)), 200
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/onboarding", methods=["PATCH"])
@require_auth
def update_onboarding():
    """
    Update onboarding state using an action model.

    Supported actions:
      - start
      - select_path (path=sample|real)
      - complete_step (step in ONBOARDING_STEPS)
      - dismiss
      - complete
      - reset
    """
    auth_service = AuthService()
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if action not in ONBOARDING_ACTIONS:
        return jsonify({
            "success": False,
            "error": f"Invalid action. Expected one of: {', '.join(ONBOARDING_ACTIONS)}",
        }), 400

    if action == "select_path" and data.get("path") not in ("sample", "real"):
        return jsonify({
            "success": False,
            "error": "Invalid path. Expected 'sample' or 'real'.",
        }), 400

    if action == "complete_step" and data.get("step") not in ONBOARDING_STEPS:
        return jsonify({
            "success": False,
            "error": f"Invalid step. Expected one of: {', '.join(ONBOARDING_STEPS)}",
        }), 400

    try:
        profile = auth_service.get_user_profile(request.user.id)

        now_iso = _now_iso()
        state = _normalize_onboarding_state(profile.get("onboarding_state"))
        status = _sanitize_onboarding_status(profile.get("onboarding_status"))
        started_at = profile.get("onboarding_started_at")
        completed_at = profile.get("onboarding_completed_at")

        updates: Dict[str, Any] = {
            "onboarding_version": ONBOARDING_VERSION,
            "onboarding_last_seen_at": now_iso,
        }

        if action == "reset":
            status = "not_started"
            state = _default_onboarding_state()
            started_at = None
            completed_at = None
        elif action == "dismiss":
            status = "dismissed"
            if not started_at:
                started_at = now_iso
        elif action == "start":
            if status in ("not_started", "dismissed"):
                status = "in_progress"
            if not started_at:
                started_at = now_iso
        elif action == "select_path":
            if status != "completed":
                status = "in_progress"
            if not started_at:
                started_at = now_iso
            state["path"] = data.get("path")
            state = _mark_step_complete(state, "choose_path", now_iso)
        elif action == "complete_step":
            if status == "not_started":
                status = "in_progress"
                started_at = started_at or now_iso
            step = data.get("step")
            state = _mark_step_complete(state, step, now_iso)
            if step == "export_redirects":
                status = "completed"
                completed_at = now_iso
        elif action == "complete":
            if not started_at:
                started_at = now_iso
            status = "completed"
            if not state["steps"]["export_redirects"]["completed"]:
                state = _mark_step_complete(state, "export_redirects", now_iso)
            completed_at = now_iso

        updates["onboarding_state"] = state
        updates["onboarding_status"] = status
        updates["onboarding_started_at"] = started_at
        updates["onboarding_completed_at"] = completed_at

        auth_service.client.table("user_profiles").update(updates).eq(
            "id", str(request.user.id)
        ).execute()

        refreshed = auth_service.get_user_profile(request.user.id)
        return jsonify(_build_onboarding_response(refreshed)), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/onboarding/sample-session", methods=["POST"])
@require_auth
def create_onboarding_sample_session():
    """
    Create a completed tutorial session seeded with sample mappings.
    """
    session_db = MigrationSessionDB()
    mapping_db = URLMappingDB()
    user_id = str(request.user.id)

    try:
        session_id = session_db.create_session(
            user_id=user_id,
            project_name="Tutorial Sample Project",
            pipeline_type="url_only",
            is_tutorial=True,
        )

        approved_mappings = 0
        for mapping in SAMPLE_TUTORIAL_MAPPINGS:
            mapping_db.insert_mapping(
                session_id=session_id,
                old_url=mapping["old_url"],
                new_url=mapping["new_url"],
                confidence_score=mapping["confidence_score"],
                match_type=mapping["match_type"],
                needs_review=mapping["needs_review"],
            )
            if not mapping["needs_review"]:
                approved_mappings += 1

        total_mappings = len(SAMPLE_TUTORIAL_MAPPINGS)
        session_db.client.table("migration_sessions").update({
            "status": "completed",
            "total_mappings": total_mappings,
            "approved_mappings": approved_mappings,
            "current_stage": 4,
            "stage_name": "Completed",
            "total_stages": 4,
            "is_tutorial": True,
        }).eq(
            "id", str(session_id)
        ).eq(
            "user_id", user_id
        ).execute()

        return jsonify({
            "success": True,
            "session_id": str(session_id),
            "total_mappings": total_mappings,
            "approved_mappings": approved_mappings,
        }), 200

    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/profile", methods=["GET"])
@require_auth
def get_profile():
    """
    Get user profile.
    """
    auth_service = AuthService()

    try:
        profile = auth_service.get_user_profile(request.user.id)
        return jsonify({
            "success": True,
            "profile": profile,
        }), 200
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500


@user_blueprint.route("/profile", methods=["PUT"])
@require_auth
def update_profile():
    """
    Update user profile.
    """
    data = request.get_json(silent=True) or {}
    auth_service = AuthService()

    updates = {}
    if "full_name" in data:
        updates["full_name"] = data["full_name"]
    if "company" in data:
        updates["company"] = data["company"]

    if not updates:
        return jsonify({
            "success": False,
            "error": "No fields to update",
        }), 400

    try:
        auth_service.client.table("user_profiles").update(updates).eq(
            "id", str(request.user.id)
        ).execute()
        return jsonify({"success": True}), 200
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 500
