"""
Onboarding nudge email cron job.

Sends drip emails to users who signed up 1, 3, or 7 days ago
and haven't run a migration yet.

Run via Render Cron Job:
  Schedule: 0 14 * * * (2 PM UTC daily — morning in US)
  Command:  python -m backend.services.email_nudge_cron

Can also be triggered via POST /api/email/admin/nudge-cron with X-Cron-Secret.
"""

import sys
import os
import logging
from datetime import datetime, timezone, timedelta

# Setup path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
SRC_DIR = os.path.join(BASE_DIR, "src")
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, SRC_DIR)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def run_nudges() -> int:
    """
    Find eligible users and send nudge emails.

    Returns the total number of nudges sent.
    """
    # Retired upload-funnel nudges stay disabled before any database work.
    if os.getenv("MCP_PIVOT_ENABLED", "false").lower() == "true":
        return 0

    from redirx.database import SupabaseClient
    from backend.services.email_service import EmailService, EmailType

    client = SupabaseClient.get_client()
    email_service = EmailService()
    now = datetime.now(timezone.utc)
    total_sent = 0

    # For each nudge day, find users created exactly N days ago (within a 24h window)
    for day in (1, 3, 7):
        window_start = (now - timedelta(days=day, hours=12)).isoformat()
        window_end = (now - timedelta(days=day - 1, hours=12)).isoformat()

        logger.info(f"Checking day-{day} nudge: window {window_start} to {window_end}")

        # Get users created in this window who have received the welcome email
        result = client.table("user_profiles").select(
            "id, email, full_name, welcome_email_sent"
        ).gte("created_at", window_start).lt("created_at", window_end).eq(
            "welcome_email_sent", True
        ).execute()

        if not result.data:
            logger.info(f"Day-{day}: no eligible users")
            continue

        for user in result.data:
            user_id = user["id"]
            user_email = user.get("email")
            if not user_email:
                continue

            # Day 3 and 7: skip users who have already run a migration
            if day in (3, 7):
                sessions = client.table("migration_sessions").select(
                    "id", count="exact"
                ).eq("user_id", user_id).limit(1).execute()
                if sessions.count and sessions.count > 0:
                    logger.info(f"Day-{day}: skipping {user_email} (has sessions)")
                    continue

            # Check if this specific nudge was already sent
            existing = client.table("email_log").select("id").eq(
                "user_id", user_id
            ).eq("email_type", EmailType.ONBOARD_NUDGE).eq("status", "sent").like(
                "subject", f"%day {day}%"
            ).limit(1).execute()

            # Also check by template matching in a more reliable way
            existing2 = client.table("email_log").select("id").eq(
                "user_id", user_id
            ).eq("email_type", EmailType.ONBOARD_NUDGE).eq("status", "sent").like(
                "subject", f"%{_nudge_subjects[day]}%"
            ).limit(1).execute()

            if (existing.data and len(existing.data) > 0) or (existing2.data and len(existing2.data) > 0):
                logger.info(f"Day-{day}: skipping {user_email} (already sent)")
                continue

            # Check opt-out
            pref = client.table("email_preferences").select("opted_out").eq(
                "user_id", user_id
            ).eq("email_type", EmailType.ONBOARD_NUDGE).maybe_single().execute()

            if pref.data and pref.data.get("opted_out"):
                logger.info(f"Day-{day}: skipping {user_email} (opted out)")
                continue

            # Send nudge
            msg_id = email_service.send_nudge(user_id=user_id, to_email=user_email, day=day)
            if msg_id:
                total_sent += 1
                logger.info(f"Day-{day}: sent to {user_email}")
            else:
                logger.warning(f"Day-{day}: failed to send to {user_email}")

    return total_sent


# Subject snippets for dedup checking
_nudge_subjects = {
    1: "Quick tip",
    3: "Don't miss out",
    7: "account is waiting",
}


def main():
    logger.info("Starting onboarding nudge cron job")
    count = run_nudges()
    logger.info(f"Nudge cron complete: {count} email(s) sent")


if __name__ == '__main__':
    main()
