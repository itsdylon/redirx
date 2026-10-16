"""Scoped unsubscribe authorization; never derive a capability from public values."""
import base64
import hashlib
import hmac
import json
import time

from redirx.config import Config

OPTIONAL_EMAIL_TYPES = frozenset({"mapping_complete", "mapping_failed", "onboard_nudge", "watch_alert"})
_PURPOSE = "redirx/email-unsubscribe/v1"
_MAX_AGE_SECONDS = 365 * 24 * 60 * 60


class UnsubscribeUnavailable(ValueError):
    pass


def _key() -> bytes:
    # .env.example defines SUPABASE_KEY as the existing backend service-role
    # secret. A purpose-separated derived key cannot authorize another action.
    secret = Config.SUPABASE_KEY
    if not isinstance(secret, str) or not secret.strip():
        raise UnsubscribeUnavailable("Email preference links are unavailable; use signed-in settings.")
    return hmac.new(secret.encode(), _PURPOSE.encode(), hashlib.sha256).digest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unsubscribe_token(user_id: str, email_type: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128 or email_type not in OPTIONAL_EMAIL_TYPES:
        raise ValueError("Invalid email preference scope")
    payload = _encode(json.dumps({"v": 1, "purpose": _PURPOSE, "user_id": user_id,
                                 "email_type": email_type, "issued_at": int(time.time())},
                                sort_keys=True, separators=(",", ":")).encode())
    return payload + "." + _encode(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def verify_unsubscribe_token(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or len(token) > 2048:
        raise ValueError("Invalid unsubscribe link")
    try:
        payload, signature = token.split(".")
        expected = _encode(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid unsubscribe link")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        issued = data.get("issued_at")
        if (type(data.get("v")) is not int or data["v"] != 1 or data.get("purpose") != _PURPOSE or type(issued) is not int
                or not -300 <= time.time() - issued <= _MAX_AGE_SECONDS
                or not isinstance(data.get("user_id"), str) or not 0 < len(data["user_id"]) <= 128
                or data.get("email_type") not in OPTIONAL_EMAIL_TYPES):
            raise ValueError("Invalid unsubscribe link")
        return data["user_id"], data["email_type"]
    except UnsubscribeUnavailable:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise ValueError("Invalid or expired unsubscribe link") from None
