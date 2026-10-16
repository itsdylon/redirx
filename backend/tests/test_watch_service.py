"""
Watch reconciliation, alert gating, and fix suggestion.

The sweep itself is I/O; the judgement is here. Two behaviours carry the
product risk: never closing an issue we did not actually re-check, and never
emailing the same standing problem twice. Both are load-bearing for whether a
customer keeps the alerts turned on.
"""
import os
import sys
import unittest

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.services import watch_service as ws
from backend.tests.fake_supabase import FakeSupabase, RecordingEmailService
from redirx import redirect_probe as rp

WATCH_ID = "11111111-1111-1111-1111-111111111111"


def finding(issue_type=rp.NOT_FOUND, clicks=0, expected="https://new.com/b"):
    return {
        "issue_type": issue_type,
        "severity": rp.SEVERITY[issue_type],
        "detail": "detail",
        "http_status": 404,
        "final_url": "https://old.com/a",
        "hops": 0,
        "expected_url": expected,
        "clicks_at_risk": clicks,
        "suggested_target": expected,
        "fix_source": "approved_mapping",
    }


def service(tables=None):
    svc = ws.WatchService(client=FakeSupabase(tables or {}), email_service=RecordingEmailService())
    return svc


class TestBareHost(unittest.TestCase):
    def test_strips_scheme_and_www(self):
        self.assertEqual(ws.bare_host("https://www.Example.com/a"), "example.com")

    def test_accepts_a_bare_domain(self):
        self.assertEqual(ws.bare_host("example.com"), "example.com")

    def test_empty_input_is_empty(self):
        self.assertEqual(ws.bare_host(""), "")


class TestSuggestFix(unittest.TestCase):
    def test_missing_rule_is_fixed_by_the_approved_mapping(self):
        target, source = ws.suggest_fix(
            rp.NO_REDIRECT, "https://new.com/b", rp.ProbeResult(url="https://old.com/a")
        )
        self.assertEqual(target, "https://new.com/b")
        self.assertEqual(source, "approved_mapping")

    def test_wrong_target_is_fixed_by_the_approved_mapping_not_where_it_landed(self):
        result = rp.ProbeResult(url="https://old.com/a", final_url="https://new.com/")
        target, source = ws.suggest_fix(rp.WRONG_TARGET, "https://new.com/b", result)
        # Proposing the observed destination would ratify the bug.
        self.assertEqual(target, "https://new.com/b")
        self.assertEqual(source, "approved_mapping")

    def test_chain_is_collapsed_onto_where_it_already_ends(self):
        result = rp.ProbeResult(url="https://old.com/a", final_url="https://new.com/b")
        target, source = ws.suggest_fix(rp.REDIRECT_CHAIN, "https://new.com/b", result)
        self.assertEqual(target, "https://new.com/b")
        self.assertEqual(source, "collapse_chain")

    def test_temporary_keeps_the_target_and_changes_the_status(self):
        result = rp.ProbeResult(url="https://old.com/a", final_url="https://new.com/b")
        target, source = ws.suggest_fix(rp.TEMPORARY_REDIRECT, "https://new.com/b", result)
        self.assertEqual(target, "https://new.com/b")
        self.assertEqual(source, "force_permanent")

    def test_no_mapping_means_no_suggestion_rather_than_a_guess(self):
        target, source = ws.suggest_fix(
            rp.NOT_FOUND, None, rp.ProbeResult(url="https://old.com/a")
        )
        self.assertIsNone(target)
        self.assertEqual(source, "none")

    def test_outages_have_no_target_fix(self):
        # Choosing a better destination does not fix a loop, an outage, or a
        # 500 — there is nothing to propose, so propose nothing.
        for issue_type in (rp.UNREACHABLE, rp.REDIRECT_LOOP, rp.BLOCKED, rp.SERVER_ERROR):
            target, source = ws.suggest_fix(
                issue_type, "https://new.com/b", rp.ProbeResult(url="https://old.com/a")
            )
            self.assertIsNone(target, issue_type)
            self.assertEqual(source, "none", issue_type)


class TestReconcile(unittest.TestCase):
    def test_a_new_problem_is_inserted_and_counted_as_new(self):
        svc = service({"watch_issues": []})
        counts = svc._reconcile(
            WATCH_ID, {"https://old.com/a": finding()}, {"https://old.com/a"}
        )
        self.assertEqual(counts["new"], 1)
        rows = svc.client.tables["watch_issues"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 1)

    def test_the_same_problem_again_increments_rather_than_duplicating(self):
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/a",
                "issue_type": rp.NOT_FOUND, "occurrences": 1,
                "resolved_at": None, "alerted_at": "yesterday",
            }]
        })
        counts = svc._reconcile(
            WATCH_ID, {"https://old.com/a": finding()}, {"https://old.com/a"}
        )
        self.assertEqual(counts["new"], 0)
        rows = svc.client.tables["watch_issues"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)
        # Still reported: a standing problem must not re-alert every sweep.
        self.assertEqual(rows[0]["alerted_at"], "yesterday")

    def test_a_different_failure_at_the_same_url_re_alerts(self):
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/a",
                "issue_type": rp.NOT_FOUND, "occurrences": 3,
                "resolved_at": None, "alerted_at": "yesterday",
            }]
        })
        counts = svc._reconcile(
            WATCH_ID,
            {"https://old.com/a": finding(issue_type=rp.WRONG_TARGET)},
            {"https://old.com/a"},
        )
        self.assertEqual(counts["new"], 1)
        row = svc.client.tables["watch_issues"][0]
        self.assertEqual(row["issue_type"], rp.WRONG_TARGET)
        self.assertEqual(row["occurrences"], 1)
        self.assertIsNone(row["alerted_at"])

    def test_a_fixed_url_is_resolved(self):
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/a",
                "issue_type": rp.NOT_FOUND, "occurrences": 2,
                "resolved_at": None, "alerted_at": "yesterday",
            }]
        })
        counts = svc._reconcile(WATCH_ID, {}, {"https://old.com/a"})
        self.assertEqual(counts["resolved"], 1)
        self.assertIsNotNone(svc.client.tables["watch_issues"][0]["resolved_at"])

    def test_an_unchecked_url_is_never_resolved(self):
        """
        The bug this guards: MAX_URLS_PER_SWEEP means a large site's tail is not
        probed every round. Treating "absent from findings" as "fixed" would
        silently close real breakage on exactly the sites that need this most.
        """
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/untouched",
                "issue_type": rp.NOT_FOUND, "occurrences": 2,
                "resolved_at": None, "alerted_at": "yesterday",
            }]
        })
        counts = svc._reconcile(WATCH_ID, {}, probed={"https://old.com/other"})
        self.assertEqual(counts["resolved"], 0)
        self.assertIsNone(svc.client.tables["watch_issues"][0]["resolved_at"])

    def test_a_recurrence_reopens_the_same_row(self):
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/a",
                "issue_type": rp.NOT_FOUND, "occurrences": 0,
                "resolved_at": "last week", "alerted_at": "last week",
            }]
        })
        counts = svc._reconcile(
            WATCH_ID, {"https://old.com/a": finding()}, {"https://old.com/a"}
        )
        self.assertEqual(counts["new"], 1)
        rows = svc.client.tables["watch_issues"]
        self.assertEqual(len(rows), 1, "a recurrence must not create a second row")
        self.assertIsNone(rows[0]["resolved_at"])
        self.assertIsNone(rows[0]["alerted_at"])


class TestAlertGating(unittest.TestCase):
    def _svc(self, issues):
        return service({"watch_issues": issues})

    def test_an_unreported_problem_is_pending(self):
        svc = self._svc([{
            "id": "i1", "watch_id": WATCH_ID, "old_url": "u", "issue_type": rp.NOT_FOUND,
            "occurrences": 1, "resolved_at": None, "alerted_at": None, "clicks_at_risk": 5,
        }])
        self.assertEqual(len(svc.pending_alerts(WATCH_ID)), 1)

    def test_an_already_reported_problem_is_not_pending_again(self):
        svc = self._svc([{
            "id": "i1", "watch_id": WATCH_ID, "old_url": "u", "issue_type": rp.NOT_FOUND,
            "occurrences": 4, "resolved_at": None, "alerted_at": "yesterday",
            "clicks_at_risk": 5,
        }])
        self.assertEqual(svc.pending_alerts(WATCH_ID), [])

    def test_a_resolved_problem_is_not_pending(self):
        svc = self._svc([{
            "id": "i1", "watch_id": WATCH_ID, "old_url": "u", "issue_type": rp.NOT_FOUND,
            "occurrences": 0, "resolved_at": "today", "alerted_at": None,
            "clicks_at_risk": 5,
        }])
        self.assertEqual(svc.pending_alerts(WATCH_ID), [])

    def test_a_single_unreachable_probe_does_not_wake_anyone(self):
        # One failed request is a blip, not an outage.
        svc = self._svc([{
            "id": "i1", "watch_id": WATCH_ID, "old_url": "u", "issue_type": rp.UNREACHABLE,
            "occurrences": 1, "resolved_at": None, "alerted_at": None, "clicks_at_risk": 0,
        }])
        self.assertEqual(svc.pending_alerts(WATCH_ID), [])

    def test_two_consecutive_unreachable_probes_do(self):
        svc = self._svc([{
            "id": "i1", "watch_id": WATCH_ID, "old_url": "u", "issue_type": rp.UNREACHABLE,
            "occurrences": 2, "resolved_at": None, "alerted_at": None, "clicks_at_risk": 0,
        }])
        self.assertEqual(len(svc.pending_alerts(WATCH_ID)), 1)


class TestSendAlert(unittest.TestCase):
    def _watch(self):
        return {
            "id": WATCH_ID,
            "user_id": "user-1",
            "session_id": "sess-1",
            "old_domain": "old.com",
            "alert_email": "owner@example.com",
        }

    def test_nothing_pending_sends_nothing(self):
        svc = service({"watch_issues": []})
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 0)
        self.assertEqual(svc.email_service.sent, [])

    def test_sends_once_and_marks_reported(self):
        svc = service({
            "watch_issues": [
                {
                    "id": "i1", "watch_id": WATCH_ID, "old_url": "https://old.com/a",
                    "issue_type": rp.NOT_FOUND, "severity": rp.CRITICAL,
                    "occurrences": 1, "resolved_at": None, "alerted_at": None,
                    "clicks_at_risk": 900, "suggested_target": "https://new.com/b",
                    "detail": "Returns 404",
                },
                {
                    "id": "i2", "watch_id": WATCH_ID, "old_url": "https://old.com/c",
                    "issue_type": rp.TEMPORARY_REDIRECT, "severity": rp.WARNING,
                    "occurrences": 1, "resolved_at": None, "alerted_at": None,
                    "clicks_at_risk": 100, "suggested_target": "https://new.com/d",
                    "detail": "Uses 302, not 301",
                },
            ],
            "migration_sessions": [{"id": "sess-1", "project_name": "Client Relaunch"}],
        })

        self.assertEqual(svc.send_alert_if_needed(self._watch()), 2)

        sent = svc.email_service.sent[0]
        self.assertEqual(sent["to_email"], "owner@example.com")
        self.assertEqual(sent["project_name"], "Client Relaunch")
        self.assertEqual(sent["total_issues"], 2)
        self.assertEqual(sent["critical_count"], 1)
        self.assertEqual(sent["clicks_at_risk"], 1000)

        # A second sweep with no change must stay silent.
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 0)
        self.assertEqual(len(svc.email_service.sent), 1)

    def test_falls_back_to_the_account_email(self):
        watch = self._watch()
        watch["alert_email"] = None
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "u",
                "issue_type": rp.NOT_FOUND, "severity": rp.CRITICAL,
                "occurrences": 1, "resolved_at": None, "alerted_at": None,
                "clicks_at_risk": 0,
            }],
            "user_profiles": [{"id": "user-1", "email": "account@example.com"}],
        })
        svc.send_alert_if_needed(watch)
        self.assertEqual(svc.email_service.sent[0]["to_email"], "account@example.com")

    def test_a_failed_send_leaves_the_issues_unreported(self):
        """
        Regression, caught in production.

        EmailService swallows its own errors and returns None. Stamping
        alerted_at regardless meant a Resend failure burned the alert: the
        next sweep saw the issues as already reported and stayed silent
        forever. Observed live — a send failed and all five issues were marked
        alerted anyway.
        """
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "u",
                "issue_type": rp.NOT_FOUND, "severity": rp.CRITICAL,
                "occurrences": 1, "resolved_at": None, "alerted_at": None,
                "clicks_at_risk": 10,
            }],
            "migration_sessions": [{"id": "sess-1", "project_name": "P"}],
        })
        svc.email_service.message_id = None

        self.assertEqual(svc.send_alert_if_needed(self._watch()), 0)
        self.assertEqual(len(svc.email_service.sent), 1, "the send was attempted")
        self.assertIsNone(svc.client.tables["watch_issues"][0]["alerted_at"])

        # The next sweep must try again.
        svc.email_service.message_id = "msg_ok"
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 1)
        self.assertIsNotNone(svc.client.tables["watch_issues"][0]["alerted_at"])

    def test_no_address_anywhere_does_not_mark_issues_reported(self):
        # Otherwise the alert is lost forever the moment an email lookup fails.
        watch = self._watch()
        watch["alert_email"] = None
        svc = service({
            "watch_issues": [{
                "id": "i1", "watch_id": WATCH_ID, "old_url": "u",
                "issue_type": rp.NOT_FOUND, "severity": rp.CRITICAL,
                "occurrences": 1, "resolved_at": None, "alerted_at": None,
                "clicks_at_risk": 0,
            }],
            "user_profiles": [],
        })
        self.assertEqual(svc.send_alert_if_needed(watch), 0)
        self.assertIsNone(svc.client.tables["watch_issues"][0]["alerted_at"])


class TestFixRows(unittest.TestCase):
    """The corrective patch: only URLs we can actually repair, worst first."""

    def _svc(self, issues):
        return service({"watch_issues": issues})

    @staticmethod
    def _issue(url, target, clicks, issue_type=rp.NOT_FOUND):
        return {
            "id": url, "watch_id": WATCH_ID, "old_url": url,
            "issue_type": issue_type, "severity": rp.SEVERITY[issue_type],
            "resolved_at": None, "alerted_at": None,
            "clicks_at_risk": clicks, "suggested_target": target,
            "fix_source": "approved_mapping",
        }

    def test_issues_without_a_target_are_omitted_not_emitted_blank(self):
        # A blank target would overwrite a working rule with a broken one.
        svc = self._svc([
            self._issue("https://old.com/a", "https://new.com/a", 10),
            self._issue("https://old.com/b", None, 99, issue_type=rp.UNREACHABLE),
        ])
        rows = svc.fix_rows(WATCH_ID)
        self.assertEqual([r["old_url"] for r in rows], ["https://old.com/a"])

    def test_rows_are_ordered_by_traffic(self):
        svc = self._svc([
            self._issue("https://old.com/small", "https://new.com/small", 5),
            self._issue("https://old.com/big", "https://new.com/big", 5000),
        ])
        rows = svc.fix_rows(WATCH_ID)
        self.assertEqual(rows[0]["old_url"], "https://old.com/big")

    def test_resolved_issues_are_not_in_the_patch(self):
        issue = self._issue("https://old.com/a", "https://new.com/a", 10)
        issue["resolved_at"] = "today"
        self.assertEqual(self._svc([issue]).fix_rows(WATCH_ID), [])

    def test_rows_feed_the_exporter_unchanged(self):
        # The whole point of the old_url/new_url shape: the corrective file is
        # produced by the same formatters as the original export.
        from backend.services import redirect_export

        svc = self._svc([self._issue("https://old.com/a", "https://new.com/b", 10)])
        content = redirect_export.build_export(svc.fix_rows(WATCH_ID), "nginx")
        self.assertIn('location = "/a" { return 301 "https://new.com/b"; }', content)
        self.assertTrue(content.strip().endswith("}"))


class TestUrlSelection(unittest.TestCase):
    def test_a_small_site_is_checked_entirely(self):
        svc = service()
        mappings = [{"old_url": f"https://old.com/{i}"} for i in range(10)]
        self.assertEqual(len(svc._select_urls(mappings, {})), 10)

    def test_a_large_site_is_capped_by_traffic_not_by_order(self):
        svc = service()
        mappings = [{"old_url": f"https://old.com/{i}"} for i in range(ws.MAX_URLS_PER_SWEEP + 50)]
        # The last URL is the highest-traffic one; it must survive the cut.
        clicks = {ws._normalize_url_key(mappings[-1]["old_url"]): 10_000}
        selected = svc._select_urls(mappings, clicks)
        self.assertEqual(len(selected), ws.MAX_URLS_PER_SWEEP)
        self.assertIn(mappings[-1], selected)


if __name__ == "__main__":
    unittest.main()

class TestCheckNow(unittest.TestCase):
    def _svc(self, status):
        return service({
            "redirect_watches": [{
                "id": WATCH_ID, "status": status,
                "next_check_at": "2099-01-01T00:00:00+00:00",
            }]
        })

    def test_bumps_the_schedule_of_an_active_watch(self):
        svc = self._svc("active")
        result = svc.queue_check_now(WATCH_ID)
        self.assertIsNotNone(result)
        self.assertNotEqual(
            svc.client.tables["redirect_watches"][0]["next_check_at"],
            "2099-01-01T00:00:00+00:00",
        )

    def test_refuses_to_touch_a_paused_watch(self):
        """
        Regression: check-now used to call set_status('active'), so asking a
        paused watch for one check silently resumed recurring monitoring the
        user had turned off.
        """
        svc = self._svc("paused")
        self.assertIsNone(svc.queue_check_now(WATCH_ID))
        row = svc.client.tables["redirect_watches"][0]
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["next_check_at"], "2099-01-01T00:00:00+00:00")


class TestPreDeployGrace(unittest.TestCase):
    """
    "Start monitoring" is offered on the review page, before anything is
    deployed. The first sweep of an undeployed site finds every URL still
    serving its old page — an email listing the whole migration as broken
    would be the feature's first impression.
    """

    def _watch(self):
        return {
            "id": WATCH_ID, "user_id": "user-1", "session_id": "sess-1",
            "old_domain": "old.com", "alert_email": "owner@example.com",
        }

    def _issue(self, url, issue_type):
        return {
            "id": url, "watch_id": WATCH_ID, "old_url": url,
            "issue_type": issue_type, "severity": rp.SEVERITY[issue_type],
            "occurrences": 1, "resolved_at": None, "alerted_at": None,
            "clicks_at_risk": 0,
        }

    def _check(self, urls_ok):
        return {
            "id": f"c{urls_ok}", "watch_id": WATCH_ID,
            "status": "completed", "urls_ok": urls_ok,
        }

    def test_all_no_redirect_and_no_deploy_ever_seen_holds_the_alert(self):
        svc = service({
            "watch_issues": [self._issue("u1", rp.NO_REDIRECT), self._issue("u2", rp.NO_REDIRECT)],
            "watch_checks": [self._check(urls_ok=0)],
            "migration_sessions": [{"id": "sess-1", "project_name": "P"}],
        })
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 0)
        self.assertEqual(svc.email_service.sent, [])
        # Unreported, so the next sweep re-evaluates rather than forgetting.
        self.assertIsNone(svc.client.tables["watch_issues"][0]["alerted_at"])

    def test_any_working_redirect_in_history_means_alerts_flow(self):
        # A post-deploy config wipe serves 200 everywhere again — but a past
        # sweep saw redirects work, so silence would hide a regression.
        svc = service({
            "watch_issues": [self._issue("u1", rp.NO_REDIRECT)],
            "watch_checks": [self._check(urls_ok=5), self._check(urls_ok=0)],
            "migration_sessions": [{"id": "sess-1", "project_name": "P"}],
        })
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 1)

    def test_a_mixed_failure_set_is_not_held(self):
        # A 404 or wrong target is never "you just have not deployed yet".
        svc = service({
            "watch_issues": [self._issue("u1", rp.NO_REDIRECT), self._issue("u2", rp.NOT_FOUND)],
            "watch_checks": [self._check(urls_ok=0)],
            "migration_sessions": [{"id": "sess-1", "project_name": "P"}],
        })
        self.assertEqual(svc.send_alert_if_needed(self._watch()), 2)


if __name__ == "__main__":
    unittest.main()
