"""
Pricing and billing routes for RedirX pricing v2.
"""
from __future__ import annotations

import logging
from uuid import UUID

from flask import Blueprint, jsonify, request

from backend.routes.error_utils import error_response
from backend.services.auth_service import require_auth
from backend.services.pricing_service import (
    CONTACT_REQUIRED_PAGE_THRESHOLD,
    PricingService,
)
from backend.services.stripe_service import StripeService

logger = logging.getLogger(__name__)

pricing_blueprint = Blueprint("pricing", __name__)
billing_blueprint = Blueprint("billing", __name__)


def _get_pricing_service() -> PricingService:
    return PricingService()


def _get_stripe_service() -> StripeService:
    return StripeService()


def _default_origin() -> str:
    return request.headers.get("Origin", "http://localhost:3000")


def _map_value_error(exc: Exception):
    message = str(exc).lower()

    if "no stripe customer" in message:
        return (
            "billing_no_customer",
            "No billing account was found for this user.",
            404,
            False,
            "contact_support",
        )

    if "contact required" in message:
        return (
            "pricing_contact_required",
            f"Projects over {CONTACT_REQUIRED_PAGE_THRESHOLD:,} pages require sales assistance.",
            400,
            False,
            "contact_sales",
        )

    if "does not belong" in message:
        return (
            "pricing_quote_forbidden",
            "That project quote does not belong to this account.",
            403,
            False,
            "refresh",
        )

    if "quote" in message and "not found" in message:
        return (
            "pricing_quote_not_found",
            "Project quote not found. Generate a fresh quote and try again.",
            404,
            False,
            "requote",
        )

    if "source session must be completed" in message:
        return (
            "pricing_source_not_ready",
            "Wait for the migration to finish running, then request a quote.",
            400,
            False,
            "wait_for_completion",
        )

    if "billing cycle" in message:
        return (
            "billing_invalid_cycle",
            "Choose a valid Agency billing cycle.",
            400,
            False,
            "select_plan",
        )

    if "price" in message:
        return (
            "billing_price_unavailable",
            "That billing option is currently unavailable.",
            400,
            False,
            "retry",
        )

    return (
        "billing_invalid_request",
        "Your billing request could not be processed.",
        400,
        False,
        "retry",
    )


# ---------------------------------------------------------------------------
# Pricing endpoints (/api/pricing/*)
# ---------------------------------------------------------------------------


@pricing_blueprint.route("/estimate", methods=["GET"])
def estimate_price():
    page_count_raw = request.args.get("page_count", "")

    try:
        page_count = int(page_count_raw)
    except ValueError:
        return error_response(
            code="pricing_invalid_page_count",
            user_message="Provide page_count as a positive integer.",
            status=400,
            retryable=False,
            next_action="fix_input",
        )

    if page_count <= 0:
        return error_response(
            code="pricing_invalid_page_count",
            user_message="Provide page_count as a positive integer.",
            status=400,
            retryable=False,
            next_action="fix_input",
        )

    try:
        estimate = _get_pricing_service().estimate(page_count)
        return jsonify({"success": True, **estimate})
    except Exception as e:
        logger.error("Pricing estimate failed: %s", e)
        return error_response(
            code="pricing_estimate_failed",
            user_message="Unable to generate pricing estimate right now.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@pricing_blueprint.route("/quote", methods=["POST"])
@require_auth
def create_or_refresh_quote():
    data = request.get_json(silent=True) or {}
    source_session_id = data.get("source_session_id")
    if not source_session_id:
        return error_response(
            code="pricing_source_session_required",
            user_message="source_session_id is required to generate a quote.",
            status=400,
            retryable=False,
            next_action="fix_input",
        )

    try:
        source_uuid = UUID(str(source_session_id))
    except ValueError:
        return error_response(
            code="pricing_source_session_invalid",
            user_message="source_session_id must be a valid UUID.",
            status=400,
            retryable=False,
            next_action="fix_input",
        )

    try:
        quote = _get_pricing_service().create_or_refresh_quote(
            source_session_id=source_uuid,
            user_id=str(request.user.id),
        )
        return jsonify({"success": True, "quote": quote})
    except ValueError as e:
        code, user_message, status, retryable, next_action = _map_value_error(e)
        return error_response(
            code=code,
            user_message=user_message,
            status=status,
            retryable=retryable,
            next_action=next_action,
        )
    except Exception as e:
        logger.error("Pricing quote generation failed: %s", e, exc_info=True)
        return error_response(
            code="pricing_quote_failed",
            user_message="Unable to create your project quote right now.",
            status=500,
            retryable=True,
            next_action="retry",
        )


# ---------------------------------------------------------------------------
# Billing endpoints (/api/billing/*)
# ---------------------------------------------------------------------------


@billing_blueprint.route("/project/checkout", methods=["POST"])
@require_auth
def create_project_checkout():
    from backend.services.current_billing_service import enabled
    if enabled():
        return error_response(code='billing_catalog_changed', user_message='Open current billing to purchase a migration.',
                              status=409, retryable=False, next_action='refresh')
    data = request.get_json(silent=True) or {}
    source_session_id = data.get("source_session_id")
    quote_id = data.get("quote_id")

    if not source_session_id and not quote_id:
        return error_response(
            code="pricing_source_session_required",
            user_message="Provide source_session_id or quote_id.",
            status=400,
            retryable=False,
            next_action="fix_input",
        )

    pricing_service = _get_pricing_service()

    try:
        if quote_id:
            quote = pricing_service.get_quote_by_id(str(quote_id))
            if not quote:
                raise ValueError("Quote not found")
            if quote.get("user_id") != str(request.user.id):
                raise ValueError("Quote does not belong to user")
        else:
            quote = pricing_service.create_or_refresh_quote(
                source_session_id=UUID(str(source_session_id)),
                user_id=str(request.user.id),
            )

        if (quote.get("status") or "").lower() == "paid":
            return jsonify(
                {
                    "success": True,
                    "already_paid": True,
                    "quote_id": quote.get("id"),
                    "deep_session_id": quote.get("deep_session_id"),
                }
            )

        subtotal_cents = quote.get("subtotal_cents") or 0
        if 0 < subtotal_cents < 50:
            return error_response(
                code="billing_below_stripe_minimum",
                user_message=(
                    f"Project total is ${subtotal_cents / 100:.2f}, "
                    "which is below Stripe's $0.50 minimum charge. "
                    "Add more pages or contact us for small-project options."
                ),
                status=422,
                retryable=False,
                next_action="add_pages",
            )

        origin = _default_origin()
        success_url = data.get(
            "success_url",
            f"{origin}/review/{quote.get('source_session_id')}?unlock=success",
        )
        cancel_url = data.get(
            "cancel_url",
            f"{origin}/pricing?source_session_id={quote.get('source_session_id')}&status=cancelled",
        )

        stripe_service = _get_stripe_service()
        url, checkout_session_id = stripe_service.create_project_checkout_session(
            user_id=str(request.user.id),
            email=str(request.user.email),
            quote=quote,
            success_url=success_url,
            cancel_url=cancel_url,
        )

        pricing_service.mark_checkout_created(
            quote_id=str(quote["id"]),
            stripe_checkout_session_id=checkout_session_id,
        )

        return jsonify(
            {
                "success": True,
                "url": url,
                "quote_id": quote.get("id"),
                "checkout_session_id": checkout_session_id,
            }
        )
    except ValueError as e:
        code, user_message, status, retryable, next_action = _map_value_error(e)
        return error_response(
            code=code,
            user_message=user_message,
            status=status,
            retryable=retryable,
            next_action=next_action,
        )
    except Exception as e:
        logger.error("Project checkout session creation failed: %s", e)
        return error_response(
            code="billing_checkout_failed",
            user_message="Unable to start checkout right now. Please try again.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@billing_blueprint.route("/agency/checkout", methods=["POST"])
@require_auth
def create_agency_checkout():
    from backend.services.current_billing_service import enabled
    if enabled():
        return error_response(code='billing_catalog_changed', user_message='Open current billing to choose a monthly plan.',
                              status=409, retryable=False, next_action='refresh')
    data = request.get_json(silent=True) or {}
    billing_cycle = str(data.get("billing_cycle") or "monthly").lower()

    try:
        origin = _default_origin()
        success_url = data.get("success_url", f"{origin}/settings?tab=subscription&status=success")
        cancel_url = data.get("cancel_url", f"{origin}/settings?tab=subscription&status=cancelled")

        url, checkout_session_id = _get_stripe_service().create_agency_checkout_session(
            user_id=str(request.user.id),
            email=str(request.user.email),
            billing_cycle=billing_cycle,
            success_url=success_url,
            cancel_url=cancel_url,
        )

        return jsonify(
            {
                "success": True,
                "url": url,
                "checkout_session_id": checkout_session_id,
            }
        )
    except ValueError as e:
        code, user_message, status, retryable, next_action = _map_value_error(e)
        return error_response(
            code=code,
            user_message=user_message,
            status=status,
            retryable=retryable,
            next_action=next_action,
        )
    except Exception as e:
        logger.error("Agency checkout session creation failed: %s", e)
        return error_response(
            code="billing_checkout_failed",
            user_message="Unable to start checkout right now. Please try again.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@billing_blueprint.route("/status", methods=["GET"])
@require_auth
def get_billing_status():
    try:
        status = _get_stripe_service().get_billing_status(str(request.user.id))
        return jsonify({"success": True, **status})
    except ValueError as e:
        code, user_message, http_status, retryable, next_action = _map_value_error(e)
        return error_response(
            code=code,
            user_message=user_message,
            status=http_status,
            retryable=retryable,
            next_action=next_action,
        )
    except Exception as e:
        logger.error("Failed to get billing status: %s", e)
        return error_response(
            code="billing_status_failed",
            user_message="Unable to load billing details right now.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@billing_blueprint.route("/create-portal-session", methods=["POST"])
@require_auth
def create_portal_session():
    data = request.get_json(silent=True) or {}
    origin = _default_origin()
    return_url = data.get("return_url", f"{origin}/settings?tab=subscription")

    try:
        url = _get_stripe_service().create_portal_session(
            user_id=str(request.user.id),
            return_url=return_url,
        )
        return jsonify({"success": True, "url": url})
    except ValueError as e:
        code, user_message, status, retryable, next_action = _map_value_error(e)
        return error_response(
            code=code,
            user_message=user_message,
            status=status,
            retryable=retryable,
            next_action=next_action,
        )
    except Exception as e:
        logger.error("Portal session creation failed: %s", e)
        return error_response(
            code="billing_portal_failed",
            user_message="Unable to open billing portal right now.",
            status=500,
            retryable=True,
            next_action="retry",
        )


@billing_blueprint.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    if not sig_header:
        return error_response(
            code="billing_webhook_signature_missing",
            user_message="Missing Stripe signature header.",
            status=400,
            retryable=False,
            next_action="contact_support",
        )

    try:
        result = _get_stripe_service().handle_webhook_event(payload, sig_header)
        return jsonify(result)
    except ValueError as e:
        logger.warning("Webhook signature verification failed: %s", e)
        return error_response(
            code="billing_webhook_signature_invalid",
            user_message="Invalid webhook signature.",
            status=400,
            retryable=False,
            next_action="contact_support",
        )
    except Exception as e:
        logger.error("Webhook processing failed: %s", e)
        return error_response(
            code="billing_webhook_failed",
            user_message="Webhook processing failed.",
            status=500,
            retryable=True,
            next_action="retry",
        )


# ---------------------------------------------------------------------------
# Legacy endpoint deprecations
# ---------------------------------------------------------------------------


def _legacy_endpoint_deprecated():
    return error_response(
        code="billing_endpoint_deprecated",
        user_message="This billing endpoint has been removed. Use pricing v2 endpoints.",
        status=410,
        retryable=False,
        next_action="upgrade_client",
    )


@billing_blueprint.route("/create-checkout-session", methods=["POST"])
def deprecated_create_checkout_session():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/update-subscription", methods=["POST"])
def deprecated_update_subscription():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/cancel-subscription", methods=["POST"])
def deprecated_cancel_subscription():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/reactivate-subscription", methods=["POST"])
def deprecated_reactivate_subscription():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/plans", methods=["GET"])
def deprecated_plans():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/subscription", methods=["GET"])
def deprecated_subscription():
    return _legacy_endpoint_deprecated()


@billing_blueprint.route("/admin/reconcile", methods=["POST"])
def deprecated_reconcile():
    return _legacy_endpoint_deprecated()
