"""
Authentication endpoints for user registration, login, logout, and token refresh.
"""
from flask import Blueprint, request, jsonify
import logging
import sys
import os

# Add parent directories to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
sys.path.insert(0, BACKEND_DIR)

from services.auth_service import AuthService, AuthServiceError, require_auth
from routes.error_utils import error_response
from backend.extensions import limiter

auth_blueprint = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)


def _looks_like_email(email: str) -> bool:
    return "@" in email and "." in email and " " not in email


def _send_welcome_if_needed(
    user_id: str,
    user_email: str,
    profile: dict | None = None,
) -> None:
    """Best-effort welcome email send, gated by user_profiles.welcome_email_sent."""
    try:
        from redirx.database import SupabaseClient
        client = SupabaseClient.get_client()

        profile_data = profile
        if not profile_data:
            profile_result = client.table('user_profiles').select(
                'welcome_email_sent, full_name'
            ).eq('id', user_id).maybe_single().execute()
            profile_data = profile_result.data if profile_result else None

        if not profile_data or profile_data.get('welcome_email_sent'):
            return

        from backend.services.email_service import EmailService
        email_service = EmailService()
        user_name = profile_data.get('full_name', '')
        message_id = email_service.send_welcome(
            user_id=user_id,
            to_email=user_email,
            user_name=user_name,
        )
        from backend.services.email_service import accepted_message_id
        if accepted_message_id({'id': message_id}) is None:
            return
        client.table('user_profiles').update(
            {'welcome_email_sent': True}
        ).eq('id', user_id).execute()
    except Exception:
        logger.exception("Welcome email failed (non-blocking)")


@auth_blueprint.record_once
def _init_limiter(state):
    if "limiter" not in state.app.extensions:
        limiter.init_app(state.app)


@auth_blueprint.route("/register", methods=["POST"])
@limiter.limit("10 per hour")
def register():
    """
    Register a new user.

    Request Body:
        {
            "email": "user@example.com",
            "password": "securepassword123",
            "full_name": "John Doe"  # optional
        }

    Response:
        201: {
            "success": true,
            "user_id": "uuid",
            "email": "user@example.com",
            "access_token": "jwt...",
            "refresh_token": "jwt..."
        }
        400: {"success": false, "error": "Error message"}
    """
    data = request.get_json(silent=True) or {}

    # Validation
    email = data.get('email')
    password = data.get('password')
    full_name = data.get('full_name', '')

    if not email or not password:
        return error_response(
            code="auth_missing_credentials",
            user_message="Enter both email and password.",
            status=400,
            retryable=False,
            next_action="fill_form",
        )

    if len(password) < 8:
        return error_response(
            code="auth_password_too_short",
            user_message="Password must be at least 8 characters.",
            status=400,
            retryable=False,
            next_action="fill_form",
        )

    try:
        auth_service = AuthService()
        result = auth_service.register(email, password, full_name)

        # Check if email confirmation is required
        if result.get('email_confirmation_required'):
            return jsonify({
                "success": True,
                "email_confirmation_required": True,
                "message": "Please check your email to confirm your account",
                "email": result['user'].email
            }), 200

        return jsonify({
            "success": True,
            "user_id": result['user'].id,
            "email": result['user'].email,
            "access_token": result['access_token'],
            "refresh_token": result['refresh_token'],
            "email_confirmation_required": False
        }), 201

    except Exception as e:
        error_msg = str(e)

        # Handle common Supabase errors
        if "already registered" in error_msg.lower() or "duplicate" in error_msg.lower():
            return error_response(
                code="auth_email_already_registered",
                user_message="This email is already registered. Try logging in instead.",
                status=409,
                retryable=False,
                next_action="login",
            )

        logger.exception("Registration failed")
        return error_response(
            code="auth_registration_failed",
            user_message="Unable to create your account right now. Please try again.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@auth_blueprint.route("/login", methods=["POST"])
@limiter.limit("60 per hour")
def login():
    """
    Login user with email and password.

    Request Body:
        {
            "email": "user@example.com",
            "password": "securepassword123"
        }

    Response:
        200: {
            "success": true,
            "user_id": "uuid",
            "email": "user@example.com",
            "access_token": "jwt...",
            "refresh_token": "jwt..."
        }
        401: {"success": false, "error": "Invalid credentials"}
    """
    data = request.get_json(silent=True) or {}

    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return error_response(
            code="auth_missing_credentials",
            user_message="Enter both email and password.",
            status=400,
            retryable=False,
            next_action="fill_form",
        )

    try:
        auth_service = AuthService()
        result = auth_service.login(email, password)

        # Send welcome email on first login (fire-and-forget)
        _send_welcome_if_needed(
            user_id=result['user'].id,
            user_email=result['user'].email,
        )

        return jsonify({
            "success": True,
            "user_id": result['user'].id,
            "email": result['user'].email,
            "access_token": result['access_token'],
            "refresh_token": result['refresh_token']
        }), 200

    except AuthServiceError as e:
        return error_response(
            code=e.code,
            user_message=e.user_message,
            status=e.status_code,
            retryable=e.retryable,
            next_action=e.next_action,
        )
    except Exception:
        logger.exception("Unexpected login error")
        return error_response(
            code="auth_service_unavailable",
            user_message="Sign-in is temporarily unavailable. Please try again shortly.",
            status=503,
            retryable=True,
            next_action="retry",
        )


@auth_blueprint.route("/resend-confirmation", methods=["POST"])
@limiter.limit("6 per hour")
def resend_confirmation():
    """
    Request a new signup confirmation email.
    Anti-enumeration: always returns a neutral success response for valid inputs.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email or not _looks_like_email(email):
        return error_response(
            code="auth_invalid_email",
            user_message="Enter a valid email address.",
            status=400,
            retryable=False,
            next_action="fill_form",
        )

    origin = request.headers.get("Origin", "http://localhost:3000")
    redirect_to = data.get("redirect_to") or f"{origin}/auth/callback"

    try:
        auth_service = AuthService()
        auth_service.resend_confirmation_email(email, redirect_to)
    except Exception:
        # Keep response neutral for anti-enumeration and account privacy.
        logger.exception("Resend confirmation request failed")

    return jsonify({
        "success": True,
        "message": "If an unconfirmed account exists, a new confirmation email has been sent."
    }), 200


@auth_blueprint.route("/logout", methods=["POST"])
def logout():
    """
    Logout user (invalidate session).

    Headers:
        Authorization: Bearer <access_token>

    Response:
        200: {"success": true}
    """
    auth_header = request.headers.get('Authorization')

    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        auth_service = AuthService()
        auth_service.logout(token)

    return jsonify({"success": True}), 200


@auth_blueprint.route("/refresh", methods=["POST"])
@limiter.limit("240 per hour")
def refresh():
    """
    Refresh access token using refresh token.

    Request Body:
        {
            "refresh_token": "jwt..."
        }

    Response:
        200: {
            "success": true,
            "access_token": "jwt...",
            "refresh_token": "jwt..."
        }
        401: {"success": false, "error": "Invalid refresh token"}
    """
    data = request.get_json(silent=True) or {}
    refresh_token = data.get('refresh_token')

    if not refresh_token:
        return error_response(
            code="auth_refresh_token_required",
            user_message="Refresh token is required.",
            status=400,
            retryable=False,
            next_action="login",
        )

    try:
        auth_service = AuthService()
        result = auth_service.refresh_token(refresh_token)

        return jsonify({
            "success": True,
            "access_token": result['access_token'],
            "refresh_token": result['refresh_token']
        }), 200

    except AuthServiceError as e:
        return error_response(
            code=e.code,
            user_message=e.user_message,
            status=e.status_code,
            retryable=e.retryable,
            next_action=e.next_action,
        )
    except Exception:
        logger.exception("Token refresh failed")
        return error_response(
            code="auth_invalid_refresh_token",
            user_message="Session expired. Please log in again.",
            status=401,
            retryable=False,
            next_action="login",
        )


@auth_blueprint.route("/me", methods=["GET"])
@require_auth
def get_current_user():
    """
    Get current user profile (requires authentication).

    Headers:
        Authorization: Bearer <access_token>

    Response:
        200: {
            "success": true,
            "user": {
                "id": "uuid",
                "email": "user@example.com",
                "full_name": "John Doe",
                "plan": "launch",
                "credits_limit": 10000,
                "credits_used": 0,
                ...
            }
        }
        401: {"success": false, "error": "Unauthorized"}
    """
    auth_service = AuthService()

    try:
        profile = auth_service.get_user_profile(request.user.id)
        _send_welcome_if_needed(
            user_id=request.user.id,
            user_email=request.user.email,
            profile=profile,
        )

        return jsonify({
            "success": True,
            "user": {
                "id": request.user.id,
                "email": request.user.email,
                **profile
            }
        }), 200
    except Exception:
        logger.exception("Failed to load current user profile")
        return error_response(
            code="auth_profile_load_failed",
            user_message="Unable to load your profile right now. Please try again.",
            status=500,
            retryable=True,
            next_action="retry",
        )
