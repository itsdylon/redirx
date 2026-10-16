"""
Email service using Resend for transactional and drip emails.

All sends are fire-and-forget: errors are caught and logged, never raised.
This ensures email failures never block critical paths (login, job processing, etc.).
"""

import logging
import os
from urllib.parse import quote, urlencode
from typing import Optional, Dict, Any

import resend
from jinja2 import Environment, FileSystemLoader

from redirx.config import Config
from redirx.database import SupabaseClient
from .email_preferences import unsubscribe_token, UnsubscribeUnavailable

logger = logging.getLogger(__name__)

# Template directory relative to this file
_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "email")


class EmailPreferenceUnavailable(RuntimeError):
    """An optional send must wait until preference authority can be read."""


def accepted_message_id(result: Any) -> Optional[str]:
    """Provider acceptance requires a real, bounded ID, not a truthy response."""
    value = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
    return value if isinstance(value, str) and value.strip() == value and 0 < len(value) <= 256 else None


class EmailType:
    """Constants for email types (used in email_preferences and email_log)."""
    WELCOME = "welcome"
    MAPPING_COMPLETE = "mapping_complete"
    MAPPING_FAILED = "mapping_failed"
    ONBOARD_NUDGE = "onboard_nudge"
    WATCH_ALERT = "watch_alert"

    # Types that ignore preference opt-outs (always send)
    MANDATORY_TYPES = {WELCOME}


class EmailService:
    """Core email service backed by Resend."""

    def __init__(self):
        api_key = Config.RESEND_API_KEY
        if api_key:
            resend.api_key = api_key
        else:
            logger.warning("RESEND_API_KEY not set — emails will not be sent")

        self._jinja = Environment(
            loader=FileSystemLoader(_TEMPLATE_DIR),
            autoescape=True,
        )
        self._from_address = Config.EMAIL_FROM_ADDRESS
        self._app_url = Config.APP_BASE_URL.rstrip("/")

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send_email(
        self,
        user_id: Optional[str],
        to_email: str,
        email_type: str,
        subject: str,
        template_name: str,
        context: Optional[Dict[str, Any]] = None,
        skip_preference_check: bool = False,
    ) -> Optional[str]:
        """
        Render and send an email. Returns an accepted Resend message ID, or None. Acceptance is not delivery.

        Never raises — all errors are caught and logged.
        """
        try:
            if not Config.RESEND_API_KEY:
                logger.info(f"Email skipped (no API key): {email_type} -> {to_email}")
                return None

            # Check opt-out preference (unless mandatory or explicitly skipped)
            if (
                not skip_preference_check
                and email_type not in EmailType.MANDATORY_TYPES
                and user_id
                and self._is_opted_out(user_id, email_type)
            ):
                self._log_send(user_id, to_email, email_type, subject, status="skipped")
                logger.info(f"Email skipped (opted out): {email_type} -> {to_email}")
                return None

            # Build context
            ctx = {
                "app_url": self._app_url,
                "unsubscribe_url": self._unsubscribe_url(user_id, email_type) if user_id and email_type not in EmailType.MANDATORY_TYPES else None,
                **(context or {}),
            }

            html = self._jinja.get_template(template_name).render(ctx)

            # Build headers
            headers: Dict[str, str] = {}
            if ctx.get("unsubscribe_url"):
                headers["List-Unsubscribe"] = f"<{ctx['unsubscribe_url']}>"
                if ctx["unsubscribe_url"].startswith("https://") and "/api/email/unsubscribe?token=" in ctx["unsubscribe_url"]:
                    headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

            params: Dict[str, Any] = {
                "from": self._from_address,
                "to": [to_email],
                "subject": subject,
                "html": html,
            }
            if headers:
                params["headers"] = headers

            result = resend.Emails.send(params)
            message_id = accepted_message_id(result)
            if message_id is None:
                self._log_send(user_id, to_email, email_type, subject,
                               status="failed", error="provider_acknowledgment_missing")
                logger.warning("Email provider did not acknowledge acceptance: %s", email_type)
                return None

            self._log_send(user_id, to_email, email_type, subject, resend_message_id=message_id)
            logger.info(f"Email accepted: {email_type} -> {to_email} (id={message_id})")
            return message_id

        except Exception as error:
            # Do not persist provider payloads, addresses or secrets from an
            # exception. The caller receives None and retains its retry guard.
            code = "preference_lookup_unavailable" if isinstance(error, EmailPreferenceUnavailable) else "email_send_failed"
            logger.warning("Email attempt not accepted: %s (%s)", email_type, code)
            self._log_send(user_id, to_email, email_type, subject, status="failed", error=code)
            return None

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def send_welcome(self, user_id: str, to_email: str, user_name: str = "") -> Optional[str]:
        return self.send_email(
            user_id=user_id,
            to_email=to_email,
            email_type=EmailType.WELCOME,
            subject="Welcome to Redirx",
            template_name="pivot_welcome.html" if os.getenv('MCP_PIVOT_ENABLED', 'false').lower() == 'true' else "welcome.html",
            context={"user_name": user_name},
        )

    def send_mapping_complete(
        self,
        user_id: str,
        to_email: str,
        project_name: str,
        total_mappings: int,
        session_id: str,
        old_site_domain: str = "",
        new_site_domain: str = "",
        migration_id: Optional[str] = None,
    ) -> Optional[str]:
        pivot = os.getenv('MCP_PIVOT_ENABLED', 'false').lower() == 'true'
        return self.send_email(
            user_id=user_id,
            to_email=to_email,
            email_type=EmailType.MAPPING_COMPLETE,
            subject=f"{'Matching complete' if pivot else 'Your redirects are ready'} — {project_name}",
            template_name="pivot_mapping_update.html" if pivot else "mapping_complete.html",
            context={
                "completed": True,
                "result_path": '/migrations/' + quote(str(migration_id), safe='') if migration_id else '/review/' + quote(str(session_id), safe=''),
                "project_name": project_name,
                "total_mappings": total_mappings,
                "session_id": session_id,
                "old_site_domain": old_site_domain,
                "new_site_domain": new_site_domain,
            },
        )

    def send_watch_alert(
        self,
        user_id: str,
        to_email: str,
        project_name: str,
        session_id: str,
        watch_id: str,
        domain: str,
        issues: list,
        total_issues: int,
        critical_count: int,
        clicks_at_risk: int,
    ) -> Optional[str]:
        """
        Report newly-detected redirect problems on a live site.

        Baseline clicks prioritize findings; they do not measure current lost
        traffic or prove that a warning is a broken redirect.
        """
        if clicks_at_risk:
            subject = (
                f"{total_issues} redirect finding{'' if total_issues == 1 else 's'} "
                f"on {domain} — {clicks_at_risk:,} recorded clicks on affected pages"
            )
        else:
            subject = (
                f"{total_issues} redirect finding{'' if total_issues == 1 else 's'} on {domain}"
            )

        return self.send_email(
            user_id=user_id,
            to_email=to_email,
            email_type=EmailType.WATCH_ALERT,
            subject=subject,
            template_name="watch_alert.html",
            context={
                "project_name": project_name,
                "session_id": session_id,
                "watch_id": watch_id,
                "domain": domain,
                "issues": issues,
                "total_issues": total_issues,
                "critical_count": critical_count,
                "clicks_at_risk": clicks_at_risk,
            },
        )

    def send_mapping_failed(
        self,
        user_id: str,
        to_email: str,
        project_name: str,
        error_summary: str = "",
        session_id: str = "",
        migration_id: Optional[str] = None,
    ) -> Optional[str]:
        return self.send_email(
            user_id=user_id,
            to_email=to_email,
            email_type=EmailType.MAPPING_FAILED,
            subject=f"Mapping job failed — {project_name}",
            template_name="pivot_mapping_update.html" if os.getenv('MCP_PIVOT_ENABLED', 'false').lower() == 'true' else "mapping_failed.html",
            context={
                "completed": False,
                "result_path": '/migrations/' + quote(str(migration_id), safe='') if migration_id else '/review/' + quote(str(session_id), safe=''),
                "project_name": project_name,
                "error_summary": error_summary[:500] if error_summary else "",
                "session_id": session_id,
            },
        )

    def send_nudge(self, user_id: str, to_email: str, day: int) -> Optional[str]:
        # These messages teach the retired upload funnel. Preserve delivery
        # history and legacy behavior without sending them in pivot mode.
        if os.getenv('MCP_PIVOT_ENABLED', 'false').lower() == 'true':
            return None
        template_map = {1: "nudge_day1.html", 3: "nudge_day3.html", 7: "nudge_day7.html"}
        subject_map = {
            1: "Quick tip: upload your first CSV",
            3: "Don't miss out on automated redirects",
            7: "Your Redirx account is waiting",
        }
        template = template_map.get(day)
        subject = subject_map.get(day)
        if not template or not subject:
            logger.warning(f"Unknown nudge day: {day}")
            return None

        return self.send_email(
            user_id=user_id,
            to_email=to_email,
            email_type=EmailType.ONBOARD_NUDGE,
            subject=subject,
            template_name=template,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _unsubscribe_url(self, user_id: str, email_type: str) -> str:
        try:
            token = unsubscribe_token(user_id, email_type)
        except UnsubscribeUnavailable:
            # Existing authenticated settings remain usable during missing
            # signing configuration; never mint a predictable public token.
            return f"{self._app_url}/settings?tab=notifications"
        return f"{self._app_url}/api/email/unsubscribe?{urlencode({'token': token})}"

    def _is_opted_out(self, user_id: str, email_type: str) -> bool:
        try:
            client = SupabaseClient.get_client()
            result = (
                client.table("email_preferences")
                .select("opted_out")
                .eq("user_id", user_id)
                .eq("email_type", email_type)
                .maybe_single()
                .execute()
            )
            if result is None or getattr(result, "error", None) is not None or not hasattr(result, "data"):
                raise EmailPreferenceUnavailable("Preference lookup returned no response")
            if result.data is None:
                return False  # Successful lookup, no saved opt-out.
            if not isinstance(result.data, dict) or type(result.data.get("opted_out")) is not bool:
                raise EmailPreferenceUnavailable("Preference lookup returned invalid data")
            return result.data["opted_out"]
        except Exception as error:
            raise EmailPreferenceUnavailable("Could not verify email preferences") from error

    def _log_send(
        self,
        user_id: Optional[str],
        to_email: str,
        email_type: str,
        subject: str,
        resend_message_id: Optional[str] = None,
        status: str = "sent",
        error: Optional[str] = None,
    ) -> None:
        try:
            client = SupabaseClient.get_client()
            row: Dict[str, Any] = {
                "to_email": to_email,
                "email_type": email_type,
                "subject": subject,
                "status": status,
            }
            if user_id:
                row["user_id"] = user_id
            if resend_message_id:
                row["resend_message_id"] = resend_message_id
            if error:
                row["error"] = error[:5000]
            client.table("email_log").insert(row).execute()
        except Exception:
            logger.exception("Failed to log email send")
