"""
Registry of all email templates available for admin testing.

Single source of truth: both the list-templates API and test-send API
read from this registry. To add a new testable template, append one dict
to EMAIL_TEMPLATES below.
"""

from typing import Optional

EMAIL_TEMPLATES = [
    # ── Transactional ──────────────────────────────────────────────────
    {
        "id": "welcome",
        "name": "Welcome",
        "description": "Sent on first login",
        "category": "transactional",
        "email_type": "welcome",
        "subject": "[TEST] Welcome to Redirx",
        "template_name": "welcome.html",
        "test_context": {"user_name": "Test User"},
    },
    {
        "id": "mapping_complete",
        "name": "Mapping Complete",
        "description": "Sent when a redirect mapping job finishes successfully",
        "category": "transactional",
        "email_type": "mapping_complete",
        "subject": "[TEST] Your redirects are ready",
        "template_name": "mapping_complete.html",
        "test_context": {
            "project_name": "Example Migration",
            "total_mappings": 142,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "old_site_domain": "old-site.com",
            "new_site_domain": "new-site.com",
        },
    },
    {
        "id": "mapping_failed",
        "name": "Mapping Failed",
        "description": "Sent when a redirect mapping job permanently fails",
        "category": "transactional",
        "email_type": "mapping_failed",
        "subject": "[TEST] Mapping job failed",
        "template_name": "mapping_failed.html",
        "test_context": {
            "project_name": "Example Migration",
            "error_summary": "ConnectionError: Failed to reach https://old-site.com after 3 retries",
            "session_id": "00000000-0000-0000-0000-000000000000",
        },
    },
    {
        "id": "watch_alert",
        "name": "Watch Alert",
        "description": "Sent when post-cutover monitoring finds new redirect problems",
        "category": "transactional",
        "email_type": "watch_alert",
        "subject": "[TEST] Redirect monitoring findings",
        "template_name": "watch_alert.html",
        "test_context": {
            "project_name": "Example Migration",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "watch_id": "00000000-0000-0000-0000-000000000000",
            "domain": "old-site.com",
            "total_issues": 3,
            "critical_count": 2,
            "clicks_at_risk": 4312,
            # One of each severity, and one without traffic data, so the test
            # send exercises every conditional branch in the template.
            "issues": [
                {
                    "old_url": "https://old-site.com/blog/seo-tips-2024",
                    "label": "Returns 404 — visitors and crawlers hit a dead end",
                    "issue_type": "not_found",
                    "severity": "critical",
                    "clicks_at_risk": 3120,
                    "suggested_target": "https://new-site.com/resources/seo-guide",
                    "detail": "Returns 404",
                },
                {
                    "old_url": "https://old-site.com/portfolio/client-work",
                    "label": "Redirects somewhere other than the approved target",
                    "issue_type": "wrong_target",
                    "severity": "critical",
                    "clicks_at_risk": 1192,
                    "suggested_target": "https://new-site.com/case-studies",
                    "detail": "Lands on https://new-site.com/",
                },
                {
                    "old_url": "https://old-site.com/contact-us",
                    "label": "Uses a temporary redirect — confirm a permanent move should use 301 or 308",
                    "issue_type": "temporary_redirect",
                    "severity": "warning",
                    "clicks_at_risk": 0,
                    "suggested_target": "https://new-site.com/company/contact",
                    "detail": "Uses 302, not 301",
                },
            ],
        },
    },
    # ── Onboarding ─────────────────────────────────────────────────────
    {
        "id": "nudge_day1",
        "name": "Nudge Day 1",
        "description": "Onboarding drip email sent 1 day after signup",
        "category": "onboarding",
        "email_type": "onboard_nudge",
        "subject": "[TEST] Quick tip: upload your first CSV",
        "template_name": "nudge_day1.html",
        "test_context": {},
    },
    {
        "id": "nudge_day3",
        "name": "Nudge Day 3",
        "description": "Onboarding drip email sent 3 days after signup",
        "category": "onboarding",
        "email_type": "onboard_nudge",
        "subject": "[TEST] Don't miss out on automated redirects",
        "template_name": "nudge_day3.html",
        "test_context": {},
    },
    {
        "id": "nudge_day7",
        "name": "Nudge Day 7",
        "description": "Onboarding drip email sent 7 days after signup",
        "category": "onboarding",
        "email_type": "onboard_nudge",
        "subject": "[TEST] Your Redirx account is waiting",
        "template_name": "nudge_day7.html",
        "test_context": {},
    },
]


def get_template_by_id(template_id: str) -> Optional[dict]:
    """Look up a template entry by its id."""
    for t in EMAIL_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


def get_templates_for_api() -> list[dict]:
    """Return only the fields the frontend needs (no test_context)."""
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "category": t["category"],
        }
        for t in EMAIL_TEMPLATES
    ]
